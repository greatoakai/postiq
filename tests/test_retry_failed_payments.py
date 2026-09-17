"""Which failures the poller may re-attempt on its own — and which it must never touch.

Re-posting money that is already in TA is the one mistake this system cannot
detect afterwards, so the gate is an ALLOWLIST: only reasons that cannot have
reached TA's payment form. "status == FAILED" is not enough on its own — V1 calls
submit_payment without V2's AT_PAYMENT_FORM wrapper, so a raise after TA had
already saved comes back as a plain FAILED carrying no may-have-posted marker.
"""
import json
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone


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
import bot_v2 as bot          # noqa: E402
import poll_square as ps      # noqa: E402

SAFE_REASON = f"V2: {bot.APP_NOT_RENDERING_REASON}; V1: {bot.APP_NOT_RENDERING_REASON}"


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._ledger, self._cleared = ps.LEDGER_DIR, ps.CLEARED_PATH
        self._report = ps.last_report_at
        ps.LEDGER_DIR = pathlib.Path(self._dir.name)
        ps.CLEARED_PATH = pathlib.Path(self._dir.name) / "manual_cleared.json"
        # Pin the report boundary well back, so only the test under examination moves it.
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(days=1)

    def tearDown(self):
        ps.LEDGER_DIR, ps.CLEARED_PATH = self._ledger, self._cleared
        ps.last_report_at = self._report
        self._dir.cleanup()

    def _write(self, rows):
        (ps.LEDGER_DIR / "09162026.json").write_text(json.dumps(rows))

    def _row(self, **overrides):
        row = {"id": "sq_1", "name": "Pat Example", "date": "09/16/2026", "amount": "110.30",
               "status": "FAILED", "account": "C000000001", "reason": SAFE_REASON,
               "method": "", "note": "", "failed_at": _ago(10), "retries": 0}
        row.update(overrides)
        self._write([row])
        return row

    def _ids(self, posted=()):
        return [r["id"] for r in ps.retry_candidates(set(posted))]


class RetryGateTest(_LedgerCase):
    """Each test changes exactly one thing about an otherwise-retryable row."""

    def test_blank_app_failure_is_retried(self):
        self._row()
        self.assertEqual(self._ids(), ["sq_1"])

    def test_any_other_failure_reason_is_never_retried(self):
        # The V1 double-charge hole: a plain FAILED that may have submitted.
        self._row(reason='V2: Page.click: Timeout 30000ms exceeded waiting for locator("text=Clients")')
        self.assertEqual(self._ids(), [], "only allowlisted reasons may be retried")

    def test_empty_reason_is_never_retried(self):
        self._row(reason="")
        self.assertEqual(self._ids(), [])

    def test_flagged_is_never_retried(self):
        self._row(status="FLAGGED")
        self.assertEqual(self._ids(), [])

    def test_may_have_posted_is_never_retried(self):
        self._row(reason=bot._may_have_posted_flag("Pat Example", SAFE_REASON))
        self.assertEqual(self._ids(), [])

    def test_already_posted_is_never_retried(self):
        self._row()
        self.assertEqual(self._ids(posted=["sq_1"]), [])

    def test_staff_cleared_is_never_retried(self):
        self._row()
        ps.CLEARED_PATH.write_text(json.dumps({"keys": ["sq:sq_1"]}))
        self.assertEqual(self._ids(), [])

    def test_skipped_no_name_is_never_retried(self):
        self._row(status="SKIPPED_NO_NAME")
        self.assertEqual(self._ids(), [])

    def test_stale_failure_is_left_for_the_report(self):
        self._row(failed_at=_ago(ps.RETRY_WINDOW_MIN + 1))
        self.assertEqual(self._ids(), [])

    def test_failure_already_on_the_morning_report_is_left_alone(self):
        # Recent enough for the window, but staff have already seen it.
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(minutes=5)
        self._row(failed_at=_ago(10))
        self.assertEqual(self._ids(), [], "never race staff working the report by hand")

    def test_legacy_row_without_a_timestamp_is_never_retried(self):
        row = self._row()
        row.pop("failed_at")
        self._write([row])
        self.assertEqual(self._ids(), [])

    def test_gives_up_after_max_attempts(self):
        self._row(retries=ps.RETRY_MAX_ATTEMPTS)
        self.assertEqual(self._ids(), [])

    def test_capped_per_run_and_oldest_first(self):
        self._write([{"id": f"sq_{i}", "name": "Pat Example", "date": "09/16/2026",
                      "amount": "10.00", "status": "FAILED", "account": "",
                      "reason": SAFE_REASON, "method": "", "note": "",
                      "failed_at": _ago(i + 1), "retries": 0}
                     for i in range(ps.RETRY_MAX_PER_RUN + 3)])
        got = self._ids()
        self.assertEqual(len(got), ps.RETRY_MAX_PER_RUN)
        expected = [f"sq_{i}" for i in range(ps.RETRY_MAX_PER_RUN + 3)][::-1][:ps.RETRY_MAX_PER_RUN]
        self.assertEqual(got, expected, "oldest failure first")


class MalformedLedgerTest(_LedgerCase):
    """One bad row must never abort a run and strand that run's new payments."""

    def test_rows_missing_an_id_are_skipped(self):
        bad = self._row()
        bad.pop("id")
        self._write([bad])
        self.assertEqual(self._ids(), [])

    def test_non_numeric_retries_is_skipped_not_raised(self):
        self._row(retries="lots")
        self.assertEqual(self._ids(), [])

    def test_unparseable_timestamp_is_skipped(self):
        self._row(failed_at="yesterday-ish")
        self.assertEqual(self._ids(), [])

    def test_junk_entries_do_not_hide_a_good_one(self):
        good = {"id": "sq_good", "name": "Pat", "date": "09/16/2026", "amount": "10.00",
                "status": "FAILED", "account": "", "reason": SAFE_REASON, "method": "",
                "note": "", "failed_at": _ago(5), "retries": 0}
        self._write(["not a dict", None, {"status": "FAILED"}, good])
        self.assertEqual(self._ids(), ["sq_good"])

    def test_a_ledger_file_that_isnt_a_list_is_skipped(self):
        (ps.LEDGER_DIR / "09162026.json").write_text(json.dumps({"oops": True}))
        self.assertEqual(ps.retry_candidates(set()), [])


class AttemptAccountingTest(_LedgerCase):
    """An attempt is spent when it is queued, so a crash mid-attempt still costs one."""

    def test_marking_an_attempt_persists_immediately(self):
        row = self._row()
        ps.mark_retry_attempt(row)
        self.assertEqual(json.loads((ps.LEDGER_DIR / "09162026.json").read_text())[0]["retries"], 1)
        self.assertEqual(self._ids(), ["sq_1"], "one attempt spent, one left")
        ps.mark_retry_attempt(row)
        self.assertEqual(self._ids(), [], "budget exhausted")

    def test_record_ledger_preserves_the_count_rather_than_incrementing(self):
        self._row(retries=1)
        ps.record_ledger("sq_1", "Pat Example", "09/16/2026", "110.30", "FAILED",
                         reason=SAFE_REASON)
        row = json.loads((ps.LEDGER_DIR / "09162026.json").read_text())[0]
        self.assertEqual(row["retries"], 1)
        self.assertIn("failed_at", row)

    def test_a_successful_post_clears_the_retry_bookkeeping(self):
        self._row(retries=1)
        ps.record_ledger("sq_1", "Pat Example", "09/16/2026", "110.30", "OK", method="V2")
        row = json.loads((ps.LEDGER_DIR / "09162026.json").read_text())[0]
        self.assertEqual(row["status"], "OK")
        self.assertNotIn("failed_at", row)
        self.assertNotIn("retries", row)


class SquareRecheckTest(unittest.TestCase):
    """A retry re-reads Square, so nothing stale or refunded is re-posted."""

    def setUp(self):
        self._get = ps.square_get

    def tearDown(self):
        ps.square_get = self._get

    def _payment(self, **fields):
        base = {"id": "sq_1", "status": "COMPLETED"}
        base.update(fields)
        ps.square_get = lambda path, params=None: {"payment": base}

    def test_completed_and_unrefunded_is_returned(self):
        self._payment()
        self.assertIsNotNone(ps.payment_still_completed("sq_1"))

    def test_non_completed_is_skipped(self):
        self._payment(status="FAILED")
        self.assertIsNone(ps.payment_still_completed("sq_1"))

    def test_refunded_payment_is_skipped_despite_being_completed(self):
        self._payment(refunded_money={"amount": 11030, "currency": "USD"})
        self.assertIsNone(ps.payment_still_completed("sq_1"))

    def test_partially_refunded_payment_is_skipped(self):
        self._payment(refund_ids=["rf_1"])
        self.assertIsNone(ps.payment_still_completed("sq_1"))

    def test_zero_refund_field_does_not_block(self):
        self._payment(refunded_money={"amount": 0})
        self.assertIsNotNone(ps.payment_still_completed("sq_1"))

    def test_square_error_is_skipped_not_raised(self):
        def boom(path, params=None):
            raise RuntimeError("square down")
        ps.square_get = boom
        self.assertIsNone(ps.payment_still_completed("sq_1"))


class ChainedReasonTest(_LedgerCase):
    """Every leg must be allowlisted. One unexplained leg means it may have posted."""

    def test_all_legs_blank_app_is_retried(self):
        self._row(reason=f"V2: {bot.APP_NOT_RENDERING_REASON}; "
                         f"V2-retry: {bot.APP_NOT_RENDERING_REASON}; "
                         f"V1: {bot.APP_NOT_RENDERING_REASON}")
        self.assertEqual(self._ids(), ["sq_1"])

    def test_v1_leg_that_could_have_submitted_blocks_the_retry(self):
        # TA recovered, V1 ran all the way to Save, then raised on the success
        # path -- plain FAILED, but the money may be in TA.
        self._row(reason=f"V2: {bot.APP_NOT_RENDERING_REASON}; "
                         f"V2-retry: {bot.APP_NOT_RENDERING_REASON}; "
                         f"V1: Page.click: Timeout 30000ms exceeded")
        self.assertEqual(self._ids(), [], "an unexplained leg must block the retry")

    def test_single_unlabelled_allowlisted_reason_is_retried(self):
        self._row(reason=bot.APP_NOT_RENDERING_REASON)
        self.assertEqual(self._ids(), ["sq_1"])


class FailedAtIsStableTest(_LedgerCase):
    """Restamping the failure time would walk a row past the report guard."""

    def test_original_timestamp_survives_a_repeat_failure(self):
        original = _ago(90)
        self._row(failed_at=original)
        ps.record_ledger("sq_1", "Pat Example", "09/16/2026", "110.30", "FAILED",
                         reason=SAFE_REASON)
        row = json.loads((ps.LEDGER_DIR / "09162026.json").read_text())[0]
        self.assertEqual(row["failed_at"], original)

    def test_junk_rows_do_not_break_recording(self):
        self._write(["not a dict", None, {"id": "sq_1", "status": "FAILED"}])
        ps.record_ledger("sq_1", "Pat Example", "09/16/2026", "110.30", "OK", method="V2")
        rows = json.loads((ps.LEDGER_DIR / "09162026.json").read_text())
        self.assertEqual([r for r in rows if isinstance(r, dict) and r.get("id") == "sq_1"][0]["status"], "OK")


class MarkAttemptReportsFailureTest(_LedgerCase):
    """An attempt that could not be written down must not be made."""

    def test_returns_true_when_it_increments(self):
        row = self._row()
        self.assertTrue(ps.mark_retry_attempt(row))

    def test_returns_false_when_no_row_matches(self):
        self._row()
        self.assertFalse(ps.mark_retry_attempt({"id": "sq_missing", "date": "09/16/2026"}))

    def test_returns_false_when_the_file_is_missing(self):
        self.assertFalse(ps.mark_retry_attempt({"id": "sq_1", "date": "01/01/2020"}))

    def test_returns_false_when_the_file_is_not_a_list(self):
        (ps.LEDGER_DIR / "09162026.json").write_text(json.dumps({"oops": True}))
        self.assertFalse(ps.mark_retry_attempt({"id": "sq_1", "date": "09/16/2026"}))


class RunLockTest(unittest.TestCase):
    """Two pollers at once would post the same payments twice."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path = ps.LOCK_PATH
        ps.LOCK_PATH = pathlib.Path(self._dir.name) / "poll_square.lock"

    def tearDown(self):
        ps.LOCK_PATH = self._path
        self._dir.cleanup()

    def test_second_run_is_refused_while_the_first_holds_it(self):
        first, may_run = ps.acquire_run_lock()
        self.assertTrue(may_run)
        _second, may_run_again = ps.acquire_run_lock()
        self.assertFalse(may_run_again, "a concurrent run must be refused")
        first.close()

    def test_lock_is_available_again_once_released(self):
        first, _ = ps.acquire_run_lock()
        first.close()
        handle, may_run = ps.acquire_run_lock()
        self.assertTrue(may_run)
        handle.close()


class DryRunPersistsNothingTest(unittest.TestCase):
    """--dry-run is run for diagnosis, sometimes while a real run is going.

    On 2026-09-16 the "nothing new to post" early return saved state before the
    dry-run branch, and a diagnostic dry-run advanced the live cursor by 75
    minutes and rewound last_action_at, making two scheduled runs skip.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._saved = []
        self._orig = {n: getattr(ps, n) for n in
                      ("save_state", "load_state", "fetch_completed_payments", "log",
                       "LOCK_PATH", "SQUARE_ACCESS_TOKEN")}
        ps.save_state = lambda state: self._saved.append(dict(state))
        ps.load_state = lambda: {"last_polled_at": "2026-09-16T00:00:00Z",
                                 "last_action_at": "2026-09-16T00:00:00",
                                 "posted_payment_ids": []}
        ps.fetch_completed_payments = lambda since: []
        ps.log = lambda msg: None
        ps.LOCK_PATH = pathlib.Path(self._dir.name) / "poll_square.lock"
        ps.SQUARE_ACCESS_TOKEN = "test-token"
        self._argv = sys.argv

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(ps, name, value)
        sys.argv = self._argv
        self._dir.cleanup()

    def test_dry_run_writes_no_state(self):
        sys.argv = ["poll_square.py", "--once", "--dry-run"]
        ps.main()
        self.assertEqual(self._saved, [], "--dry-run must not persist state")

    def test_a_real_run_does_write_state(self):
        sys.argv = ["poll_square.py", "--once"]
        ps.main()
        self.assertEqual(len(self._saved), 1, "a live run still advances the cursor")
        self.assertNotEqual(self._saved[0]["last_polled_at"], "2026-09-16T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
