"""The morning report lists clients who still owe after a payment posted.

The poller records TA's "Due From Client Now" (due_before) when the payment form
showed other open charges; reconcile turns due_before - amount into a "Still owe
after paying" list so staff can charge the card on file.
"""
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
import poll_square as ps  # noqa: E402
import reconcile as rec   # noqa: E402


def _row(name, amount, due_before=None, account="", date="10/05/2026", status="OK"):
    e = {"id": f"sq_{name}_{amount}", "name": name, "date": date, "amount": amount,
         "status": status, "account": account}
    if due_before is not None:
        e["due_before"] = due_before
    return e


def owing(rows):
    return rec.still_owing(rec.latest_balances(rows))


class StillOwingTest(unittest.TestCase):
    def test_remaining_balance_is_listed(self):
        out = owing([_row("Kate Harding", "320.00", "480.00", "C007852421")])
        self.assertEqual([(o["name"], o["remaining"]) for o in out], [("Kate Harding", 160.0)])

    def test_paid_in_full_or_no_figure_is_not_listed(self):
        self.assertEqual(owing([_row("A B", "60.00", "60.00"),
                                          _row("C D", "10.00"),
                                          _row("E F", "50.00", "20.00")]), [])

    def test_later_payment_same_day_supersedes_earlier(self):
        rows = [_row("Kate Harding", "100.00", "480.00", "C007852421"),
                _row("Kate Harding", "380.00", "380.00", "C007852421")]
        self.assertEqual(owing(rows), [], "second payment cleared the balance")

    def _combined(self, *days):
        rs = [{"balances": rec.latest_balances(d)} for d in days]
        bal = rec._latest_per_client(x for r in rs for x in r["balances"])
        return rec.still_owing(bal)

    def test_combine_keeps_latest_day_per_client(self):
        out = self._combined(
            [_row("Kate Harding", "100.00", "480.00", "C007852421", date="10/02/2026")],
            [_row("Kate Harding", "320.00", "380.00", "C007852421", date="10/05/2026")])
        self.assertEqual([(o["date"], o["remaining"]) for o in out], [("10/05/2026", 60.0)])

    def test_later_day_clearing_payment_removes_earlier_balance(self):
        out = self._combined(
            [_row("Kate Harding", "100.00", "480.00", "C007852421", date="10/02/2026")],
            [_row("Kate Harding", "380.00", "380.00", "C007852421", date="10/04/2026")])
        self.assertEqual(out, [])

    def test_retry_that_posted_last_wins_over_a_later_square_date(self):
        fri = _row("Kate Harding", "380.00", "380.00", "C007852421", date="10/02/2026")
        fri["posted_at"] = "2026-10-04T15:00:00Z"   # auto-retried Sunday
        sat = _row("Kate Harding", "100.00", "480.00", "C007852421", date="10/03/2026")
        sat["posted_at"] = "2026-10-03T15:00:00Z"
        self.assertEqual(self._combined([fri], [sat]), [])

    def test_later_payment_with_no_figure_supersedes_earlier(self):
        rows = [_row("Kate Harding", "100.00", "480.00", "C007852421"),
                _row("Kate Harding", "380.00", None, "C007852421")]
        self.assertEqual(owing(rows), [])
        out = self._combined(rows[:1], [{**rows[1], "date": "10/06/2026"}])
        self.assertEqual(out, [])

    def test_report_has_section_and_subject_bit(self):
        r = {"txn_date": "10/05/2026", "csv": "x.csv", "csv_count": 1, "ledger_count": 1,
             "matched": [{}], "gaps": [], "errors": [], "discrepancies": [], "extras": [],
             "missing_account": [], "credits": [],
             "owing": owing([_row("Kate Harding", "320.00", "480.00", "C007852421")])}
        subject, html, clean = rec.build_report_html(r)
        self.assertIn("1 still owe", subject)
        self.assertIn("Still owe after paying", html)
        self.assertIn("$160.00", html)
        self.assertIn("C007852421", html)
        self.assertTrue(clean, "a balance owed is not a reconciliation exception")


class LedgerTest(unittest.TestCase):
    def test_due_before_is_written_only_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            saved = ps.LEDGER_DIR
            ps.LEDGER_DIR = pathlib.Path(d)
            try:
                ps._write_result_row({**_row("Kate Harding", "320.00"), "due_before": "480.00"})
                ps._write_result_row(_row("Joe Smith", "10.00"))
                rows = {e["name"]: e for e in ps._read_ledger_file(pathlib.Path(d) / "10052026.json")}
            finally:
                ps.LEDGER_DIR = saved
        self.assertEqual(rows["Kate Harding"]["due_before"], "480.00")
        self.assertNotIn("due_before", rows["Joe Smith"])
        self.assertIn("posted_at", rows["Kate Harding"])


if __name__ == "__main__":
    unittest.main()
