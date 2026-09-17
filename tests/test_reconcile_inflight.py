"""The morning report must never tell staff to post a payment that may already be in TA.

When a poller run is interrupted after TA saved a payment, the only record is the poller's
intent marker — there is no ledger row. Without reading those markers, reconcile shows the
payment as a plain GAP ("post it in TA"), and staff post it a second time. These drive the
real reconcile() with the CSV, the ledger and the marker directory faked.
"""
import json
import pathlib
import sys
import tempfile
import types
import unittest


def _stub_imports():
    pw = types.ModuleType("playwright")
    api = types.ModuleType("playwright.sync_api")

    class _Timeout(Exception):
        pass

    api.sync_playwright = lambda: None
    api.TimeoutError = _Timeout
    pw.sync_api = api
    sys.modules.setdefault("playwright", pw)
    sys.modules.setdefault("playwright.sync_api", api)
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules.setdefault("dotenv", dotenv)


_stub_imports()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import bot_v2 as bot      # noqa: E402
import reconcile as rec   # noqa: E402

DAY = "09/16/2026"
CSV = pathlib.Path("09.16.2026_Daily.Square.Log.csv")


class ReconcileInflightTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._data, self._read, self._ledger = bot.DATA_DIR, bot.read_csv, rec.load_ledger_for_date
        bot.DATA_DIR = pathlib.Path(self._dir.name)
        (bot.DATA_DIR / "poll_inflight").mkdir()
        self.csv_rows, self.ledger = [], []
        bot.read_csv = lambda path: [dict(r) for r in self.csv_rows]
        rec.load_ledger_for_date = lambda date: [dict(e) for e in self.ledger]

    def tearDown(self):
        bot.DATA_DIR, bot.read_csv, rec.load_ledger_for_date = self._data, self._read, self._ledger
        self._dir.cleanup()

    def marker(self, pid, name, amount, date=DAY):
        (bot.DATA_DIR / "poll_inflight" / f"{pid}.json").write_text(
            json.dumps({"id": pid, "name": name, "date": date, "amount": amount}))

    def run_rec(self):
        return rec.reconcile(CSV)

    def _mhp(self, items):
        return [i for i in items if bot.MAY_HAVE_POSTED_MARKER in (i.get("reason") or "")]

    def test_matched_by_name_is_reported_may_have_posted_not_as_a_gap(self):
        self.csv_rows = [{"name": "Pat Example", "date": DAY, "amount": "95.00"}]
        self.marker("sq_a", "Pat Example", "95.00")
        r = self.run_rec()
        self.assertEqual(r["gaps"], [], "a gap tells staff to post it; it may already be in TA")
        [item] = self._mhp(r["errors"])
        self.assertEqual(item["id"], "sq_a")
        why, todo = rec.explain(item["status"], item["reason"], item["name"])
        self.assertIn("may or may not have saved", why)

    def test_different_spelling_but_unique_amount_is_still_caught(self):
        self.csv_rows = [{"name": "Cash Stone IV", "date": DAY, "amount": "40.00"}]
        self.marker("sq_b", "William Stone IV", "40.00")
        r = self.run_rec()
        self.assertEqual(r["gaps"], [])
        self.assertEqual([i["id"] for i in self._mhp(r["errors"])], ["sq_b"])

    def test_ambiguous_same_amount_gaps_are_all_flagged_check_first(self):
        self.csv_rows = [{"name": "Client One", "date": DAY, "amount": "25.00"},
                         {"name": "Client Two", "date": DAY, "amount": "25.00"}]
        self.marker("sq_c", "Someone Else", "25.00")
        r = self.run_rec()
        self.assertEqual(r["gaps"], [], "either could be the interrupted one: never say 'post it'")
        flagged = self._mhp(r["errors"])
        self.assertEqual(len(flagged), 2)
        self.assertTrue(all("id" not in i for i in flagged), "no two items may share a clear-key")

    def test_marker_with_no_csv_row_and_no_ledger_row_is_still_reported(self):
        self.marker("sq_d", "Pat Missing", "60.00")
        r = self.run_rec()
        self.assertEqual([i["id"] for i in self._mhp(r["errors"])], ["sq_d"],
                         "a marker must never be invisible on the report")

    def test_marker_for_a_payment_with_a_ledger_row_is_left_to_the_poller(self):
        self.csv_rows = [{"name": "Pat Example", "date": DAY, "amount": "95.00"}]
        self.ledger = [{"id": "sq_e", "name": "Pat Example", "date": DAY, "amount": "95.00",
                        "status": "OK"}]
        self.marker("sq_e", "Pat Example", "95.00")
        r = self.run_rec()
        self.assertEqual(self._mhp(r["errors"]), [], "an outcome was recorded; don't double-report")

    def test_other_days_markers_do_not_leak_in(self):
        self.csv_rows = [{"name": "Pat Example", "date": DAY, "amount": "95.00"}]
        self.marker("sq_f", "Pat Example", "95.00", date="09/15/2026")
        r = self.run_rec()
        self.assertEqual(len(r["gaps"]), 1)



class InterruptedRetryAndNoCsvTest(unittest.TestCase):
    """An interrupted RETRY already has a ledger row, and a no-CSV day has no gaps to match.
    Neither may leave a payment that could already be in TA reading "post it in TA"."""

    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown
    marker = ReconcileInflightTest.marker
    run_rec = ReconcileInflightTest.run_rec
    _mhp = ReconcileInflightTest._mhp
    R = "V2: TA app not rendering (blank page with no sidebar after repeated reloads)"

    def setUp(self):
        self._base_setUp()
        self._find = rec.find_csv
        rec.find_csv = lambda date_dotted: None

    def tearDown(self):
        rec.find_csv = self._find
        self._base_tearDown()

    def _failed(self, pid, name, amount):
        return {"id": pid, "name": name, "date": DAY, "amount": amount,
                "status": "FAILED", "reason": self.R}

    def test_interrupted_retry_matched_by_name_reads_may_have_posted(self):
        self.csv_rows = [{"name": "Pat Retry", "date": DAY, "amount": "10.00"}]
        self.ledger = [self._failed("sq_r", "Pat Retry", "10.00")]
        self.marker("sq_r", "Pat Retry", "10.00")
        [item] = [i for i in self.run_rec()["errors"] if i.get("id") == "sq_r"]
        self.assertIn(bot.MAY_HAVE_POSTED_MARKER, item["reason"],
                      "the cut-off retry may have saved it; the old FAILED reason says post it")

    def test_interrupted_retry_paired_by_amount_reads_may_have_posted(self):
        self.csv_rows = [{"name": "Cash Retry", "date": DAY, "amount": "10.00"}]
        self.ledger = [self._failed("sq_r", "William Retry", "10.00")]
        self.marker("sq_r", "William Retry", "10.00")
        items = [i for i in self.run_rec()["errors"] if i.get("id") == "sq_r"]
        self.assertTrue(items)
        self.assertTrue(all(bot.MAY_HAVE_POSTED_MARKER in i["reason"] for i in items))

    def test_no_csv_day_reports_an_unrecorded_marker(self):
        self.marker("sq_n", "Pat NoCsv", "60.00")
        r = rec.reconcile_ledger_only(DAY)
        self.assertEqual([i["id"] for i in self._mhp(r["errors"])], ["sq_n"])

    def test_no_csv_day_rewrites_an_interrupted_retry(self):
        self.ledger = [self._failed("sq_r", "Pat Retry", "10.00")]
        self.marker("sq_r", "Pat Retry", "10.00")
        [item] = [i for i in rec.reconcile_ledger_only(DAY)["errors"] if i.get("id") == "sq_r"]
        self.assertIn(bot.MAY_HAVE_POSTED_MARKER, item["reason"])

    def test_backlog_scan_without_a_csv_still_shows_the_marker(self):
        self.marker("sq_h", "Pat Backlog", "45.00")
        items, _review = rec.history_for_day("09.16.2026", {})
        self.assertEqual([i.get("id") for i in self._mhp(items)], ["sq_h"])

    def test_no_csv_day_leaves_a_recorded_ok_payment_alone(self):
        self.ledger = [{"id": "sq_ok", "name": "Pat OK", "date": DAY, "amount": "20.00", "status": "OK"}]
        self.marker("sq_ok", "Pat OK", "20.00")
        self.assertEqual(self._mhp(rec.reconcile_ledger_only(DAY)["errors"]), [])



class InterruptedRetryUnpairedTest(unittest.TestCase):
    """A cut-off retry whose ledger row no CSV row claimed: its CSV row is still a gap."""

    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown
    marker = ReconcileInflightTest.marker
    run_rec = ReconcileInflightTest.run_rec
    _mhp = ReconcileInflightTest._mhp

    def setUp(self):
        self._base_setUp()

    def tearDown(self):
        self._base_tearDown()

    def test_ambiguous_same_amount_gaps_are_all_flagged_check_ta_first(self):
        self.csv_rows = [{"name": "Cash Stone IV", "date": DAY, "amount": "40.00"},
                         {"name": "Other Person", "date": DAY, "amount": "40.00"}]
        self.ledger = [{"id": "sq_r", "name": "William Stone IV", "date": DAY, "amount": "40.00",
                        "status": "FAILED", "reason": "V2: TA app not rendering"}]
        self.marker("sq_r", "William Stone IV", "40.00")
        r = self.run_rec()
        self.assertEqual([g for g in r["gaps"] if g["amount"] == "40.00"], [],
                         "either gap may be the payment the cut-off retry saved")
        self.assertTrue(all(bot.MAY_HAVE_POSTED_MARKER in i["reason"] for i in r["errors"]))
        self.assertEqual(len([i for i in r["errors"] if i.get("id") == "sq_r"]), 1)

    def test_a_claimed_row_does_not_flag_other_clients_gaps(self):
        self.csv_rows = [{"name": "Pat Retry", "date": DAY, "amount": "40.00"},
                         {"name": "Other Person", "date": DAY, "amount": "40.00"}]
        self.ledger = [{"id": "sq_r", "name": "Pat Retry", "date": DAY, "amount": "40.00",
                        "status": "FAILED", "reason": "V2: TA app not rendering"}]
        self.marker("sq_r", "Pat Retry", "40.00")
        r = self.run_rec()
        self.assertEqual([g["name"] for g in r["gaps"]], ["Other Person"])


class ReportStampTest(unittest.TestCase):
    """The emailed report records when it read the ledger, for the poller's retry cutoff."""

    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown

    def setUp(self):
        self._base_setUp()
        self._saved = {k: getattr(rec, k) for k in
                       ("load_unreported_heals", "email_report", "day_extras", "reconcile")}
        self._argv = sys.argv
        self.seen_at_read = "unset"
        real = rec.reconcile

        def spy(path):
            p = bot.DATA_DIR / "report_stamp.json"
            self.seen_at_read = json.loads(p.read_text()) if p.exists() else None
            return real(path)

        rec.reconcile = spy
        rec.load_unreported_heals = lambda: ([], [])
        rec.day_extras = lambda d: []
        self.csv = bot.DATA_DIR / CSV.name
        self.csv.write_text("")
        self.csv_rows = [{"name": "Pat Example", "date": DAY, "amount": "95.00"}]

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(rec, k, v)
        sys.argv = self._argv
        self._base_tearDown()

    def _main(self, sent):
        rec.email_report = lambda *a, **k: sent
        sys.argv = ["reconcile.py", "--email", "--no-backlog", "--csv", str(self.csv)]
        with self.assertRaises(SystemExit):
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                rec.main()
        p = bot.DATA_DIR / "report_stamp.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_started_is_recorded_before_the_ledger_is_read_and_sent_after(self):
        stamp = self._main(sent=True)
        self.assertIsNotNone(self.seen_at_read, "the poller must know a report is reading")
        self.assertIn("started_at", self.seen_at_read)
        self.assertNotIn("sent_at", self.seen_at_read)
        self.assertIn("sent_at", stamp)
        self.assertGreaterEqual(stamp["sent_at"], stamp["started_at"])

    def test_a_failed_send_leaves_no_sent_time(self):
        stamp = self._main(sent=False)
        self.assertIn("started_at", stamp)
        self.assertNotIn("sent_at", stamp)



class MarkersReadBeforeLedgerTest(unittest.TestCase):
    """A retry can finish between the report's two reads: the poller writes the OK row, then
    removes the marker. Reading the ledger first sees FAILED and then no marker."""

    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown
    marker = ReconcileInflightTest.marker

    def setUp(self):
        self._base_setUp()
        self._markers_fn, self._ledger_fn = rec.load_inflight_markers, rec.load_ledger_for_date
        self.done = False
        self.ledger = [{"id": "sq_r", "name": "Pat Retry", "date": DAY, "amount": "10.00",
                        "status": "FAILED", "reason": "V2: TA app not rendering"}]
        self.marker("sq_r", "Pat Retry", "10.00")
        rec.load_inflight_markers = lambda d: self._then_finish(self._markers_fn(d))
        rec.load_ledger_for_date = lambda d: self._then_finish([dict(e) for e in self.ledger])

    def tearDown(self):
        rec.load_inflight_markers, rec.load_ledger_for_date = self._markers_fn, self._ledger_fn
        self._base_tearDown()

    def _then_finish(self, value):
        # The poller's retry completes straight after the report's first read, whichever it is.
        if not self.done:
            self.done = True
            self.ledger = [{**self.ledger[0], "status": "OK", "reason": ""}]
            (bot.DATA_DIR / "poll_inflight" / "sq_r.json").unlink()
        return value

    def _post_it(self, items):
        return [i for i in items if i.get("id") == "sq_r"
                and bot.MAY_HAVE_POSTED_MARKER not in (i.get("reason") or "")]

    def test_reconcile(self):
        self.csv_rows = [{"name": "Pat Retry", "date": DAY, "amount": "10.00"}]
        r = rec.reconcile(CSV)
        self.assertEqual(self._post_it(r["errors"]), [], "it just posted; staff would post it again")

    def test_ledger_only_day(self):
        self.assertEqual(self._post_it(rec.reconcile_ledger_only(DAY)["errors"]), [])

    def test_backlog_day_without_a_csv(self):
        find = rec.find_csv
        rec.find_csv = lambda d: None
        try:
            items, _ = rec.history_for_day("09.16.2026", {})
        finally:
            rec.find_csv = find
        self.assertEqual(self._post_it(items), [])


class ReportStampFailureAlertsTest(unittest.TestCase):
    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown

    def setUp(self):
        self._base_setUp()
        self._send, self.sent = bot.send_email, []
        bot.send_email = lambda **k: self.sent.append(k) or True

    def tearDown(self):
        bot.send_email = self._send
        self._base_tearDown()

    def test_an_unwritable_stamp_tells_the_admin(self):
        (bot.DATA_DIR / "report_stamp.json.tmp").mkdir()   # a write that can never succeed
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rec._write_report_stamp(started_at="2026-09-17T13:00:00Z")
        self.assertEqual([m["to"] for m in self.sent], [rec.ADMIN_TO])



class MarkerMatchingEdgeTest(unittest.TestCase):
    _base_setUp = ReconcileInflightTest.setUp
    _base_tearDown = ReconcileInflightTest.tearDown
    marker = ReconcileInflightTest.marker
    run_rec = ReconcileInflightTest.run_rec

    def setUp(self):
        self._base_setUp()

    def tearDown(self):
        self._base_tearDown()

    @staticmethod
    def _plain(items, name):
        return [i for i in items if i.get("name") == name
                and bot.MAY_HAVE_POSTED_MARKER not in (i.get("reason") or "")]

    def test_a_one_cent_base_amount_difference_is_still_caught(self):
        self.csv_rows = [{"name": "Jane Doe", "date": DAY, "amount": "97.08"}]
        self.marker("sq_j", "Jane Doe", "97.09")
        r = self.run_rec()
        self.assertEqual(r["gaps"], [], "the gap says post it; it may already be in TA")
        self.assertEqual(self._plain(r["errors"], "Jane Doe"), [])

    def test_a_marker_row_is_not_grafted_onto_the_clients_other_failure(self):
        self.csv_rows = [{"name": "Jane Doe", "date": DAY, "amount": "50.00"},
                         {"name": "Jane Doe", "date": DAY, "amount": "100.00"}]
        self.ledger = [{"id": "sq_100", "name": "Jane Doe", "date": DAY, "amount": "100.00",
                        "status": "FAILED", "reason": "V2: TA app not rendering"}]
        self.marker("sq_50", "Jane Doe", "50.00")
        r = self.run_rec()
        fifty = [i for i in r["errors"] + r["gaps"] if _amt_is(i, "50.00")]
        self.assertTrue(fifty)
        self.assertTrue(all(bot.MAY_HAVE_POSTED_MARKER in (i.get("reason") or "") for i in fifty),
                        "the $50 may already be in TA")
        hundred = [i for i in r["errors"] if i.get("id") == "sq_100"]
        self.assertEqual(len(hundred), 1, "the real $100 failure is still listed, once")

    def test_an_unrelated_gap_is_left_alone(self):
        self.csv_rows = [{"name": "Other Person", "date": DAY, "amount": "20.00"},
                         {"name": "Jane Doe", "date": DAY, "amount": "97.09"}]
        self.marker("sq_j", "Jane Doe", "97.09")
        r = self.run_rec()
        self.assertEqual([g["name"] for g in r["gaps"]], ["Other Person"])


def _amt_is(item, amount):
    return rec._amt(item.get("amount")) == amount


if __name__ == "__main__":
    unittest.main()
