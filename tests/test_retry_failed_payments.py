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
        self._report, self._log = ps.last_report_at, ps.log
        # ps.log appends to the real daily poll log; on 2026-09-16 these tests wrote
        # five warnings into production's. Silence it for every ledger test.
        ps.log = lambda msg: None
        ps.LEDGER_DIR = pathlib.Path(self._dir.name)
        ps.CLEARED_PATH = pathlib.Path(self._dir.name) / "manual_cleared.json"
        # Pin the report boundary well back, so only the test under examination moves it.
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(days=1)

    def tearDown(self):
        ps.LEDGER_DIR, ps.CLEARED_PATH = self._ledger, self._cleared
        ps.last_report_at, ps.log = self._report, self._log
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
        self._get, self._log = ps.square_get, ps.log
        ps.log = lambda msg: None

    def tearDown(self):
        ps.square_get, ps.log = self._get, self._log

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
        warnings.simplefilter("ignore", ResourceWarning)  # main() leaves the lock to process exit
        self._dir = tempfile.TemporaryDirectory()
        self._saved = []
        self._orig = {n: getattr(ps, n) for n in
                      ("save_state", "load_state", "fetch_completed_payments", "log",
                       "LOCK_PATH", "SQUARE_ACCESS_TOKEN", "LEDGER_DIR", "CLEARED_PATH",
                       "alert_admin", "INFLIGHT_DIR")}
        ps.alert_admin = lambda kind, detail: None  # never send real email from a test
        ps.INFLIGHT_DIR = pathlib.Path(self._dir.name) / "inflight"
        # main() scans for retry candidates, which reads the ledger: never the real one.
        ps.LEDGER_DIR = pathlib.Path(self._dir.name)
        ps.CLEARED_PATH = pathlib.Path(self._dir.name) / "manual_cleared.json"
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
        # the live-run test calls main(), which installs a SIGTERM handler; leaving it
        # installed means `kill` can no longer stop the test runner
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        ps._termination_requested = False
        self._dir.cleanup()

    def test_dry_run_writes_no_state(self):
        sys.argv = ["poll_square.py", "--once", "--dry-run"]
        ps.main()
        self.assertEqual(self._saved, [], "--dry-run must not persist state")

    def test_a_real_run_does_write_state(self):
        sys.argv = ["poll_square.py", "--once"]
        ps.main()
        # a live run now proves state is writable before anything else, then advances the cursor
        self.assertTrue(self._saved, "a live run still writes state")
        self.assertNotEqual(self._saved[-1]["last_polled_at"], "2026-09-16T00:00:00Z")



# ============================================================================
# Rework (after five review rounds): the fixes below each close a real double-post
# or lost-payment path found in review. Every behavioural test here was checked to
# FAIL on the pre-fix code.
# ============================================================================

import os          # noqa: E402
import signal      # noqa: E402
import warnings    # noqa: E402


class FailClosedReasonTest(unittest.TestCase):
    """Anything that isn't an allowlisted reason, a leg label or a separator refuses."""

    R = bot.APP_NOT_RENDERING_REASON

    def test_pure_allowlisted_chain_is_accepted(self):
        self.assertTrue(ps._reason_is_only_retryable(f"V2: {self.R}; V2-retry: {self.R}; V1: {self.R}"))

    def test_odd_spacing_and_punctuation_still_accepted(self):
        self.assertTrue(ps._reason_is_only_retryable(f"V2:{self.R};V1:  {self.R} ;"))

    def test_an_unrecognised_leg_label_refuses(self):
        # If bot_v2 ever adds a route, an unknown label must not slip through.
        self.assertFalse(ps._reason_is_only_retryable(f"V2: {self.R}; V3: {self.R}"))

    def test_changed_separator_with_real_failure_text_refuses(self):
        # A splitter keyed on "; V1:" would miss this and let V2 vouch for V1.
        self.assertFalse(ps._reason_is_only_retryable(f"V2: {self.R} | V1: Page.click timeout"))

    def test_stray_text_refuses(self):
        self.assertFalse(ps._reason_is_only_retryable(f"{self.R} (then Save was clicked)"))

    def test_no_allowlisted_reason_refuses(self):
        self.assertFalse(ps._reason_is_only_retryable("V2: ; V1: ;"))


class AtomicStateTest(unittest.TestCase):
    """A kill mid-write must never leave state that loses posted ids."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path, self._log = ps.STATE_PATH, ps.log
        ps.STATE_PATH = pathlib.Path(self._dir.name) / "poll_state.json"
        ps.log = lambda msg: None

    def tearDown(self):
        ps.STATE_PATH, ps.log = self._path, self._log
        self._dir.cleanup()

    def test_save_leaves_no_temp_file_and_round_trips(self):
        ps.save_state({"posted_payment_ids": ["a", "b"], "last_polled_at": "x"})
        self.assertEqual(ps.load_state()["posted_payment_ids"], ["a", "b"])
        self.assertEqual([p.name for p in pathlib.Path(self._dir.name).iterdir()], ["poll_state.json"])

    def test_a_failed_rename_leaves_the_old_state_intact(self):
        ps.save_state({"posted_payment_ids": ["kept"]})
        real_replace = os.replace
        def dies(src, dst):
            raise OSError("killed mid-save")
        ps.os.replace = dies
        try:
            with self.assertRaises(OSError):
                ps.save_state({"posted_payment_ids": ["half-written"]})
        finally:
            ps.os.replace = real_replace
        self.assertEqual(ps.load_state()["posted_payment_ids"], ["kept"])

    def test_missing_file_is_a_first_run(self):
        self.assertEqual(ps.load_state()["posted_payment_ids"], [])

    def test_corrupt_file_refuses_rather_than_starting_fresh(self):
        ps.STATE_PATH.write_text('{"posted_payment_ids": ["a", "b"')  # truncated
        with self.assertRaises(ps.StateUnreadableError):
            ps.load_state()


class PersisterTest(unittest.TestCase):
    """posted_payment_ids first, and never lost quietly."""

    def setUp(self):
        self.calls = []
        self._save, self._ledger, self._log = ps.save_state, ps.record_ledger, ps.log
        ps.save_state = lambda state: self.calls.append(("state", sorted(state["posted_payment_ids"])))
        ps.record_ledger = lambda *a, **k: self.calls.append(("ledger", a[0]))
        ps.log = lambda msg: None

    def tearDown(self):
        ps.save_state, ps.record_ledger, ps.log = self._save, self._ledger, self._log

    def _r(self):
        return {"id": "sq_1", "name": "Pat", "date": "09/16/2026", "amount": "10.00", "status": "OK"}

    def test_state_is_saved_before_the_ledger(self):
        ps.make_persister({}, {"sq_1"})(self._r())
        self.assertEqual([c[0] for c in self.calls], ["state", "ledger"])

    def test_a_ledger_failure_does_not_skip_the_state_save(self):
        def ledger_fails(*a, **k):
            raise OSError("disk full")
        ps.record_ledger = ledger_fails
        persist = ps.make_persister({}, {"sq_1"})
        ps._flush(persist, self._r())  # logged, not raised
        self.assertEqual(self.calls, [("state", ["sq_1"])])

    def test_a_state_save_failure_stops_the_batch(self):
        def state_fails(state):
            raise OSError("disk full")
        ps.save_state = state_fails
        with self.assertRaises(ps.StatePersistError):
            ps._flush(ps.make_persister({}, {"sq_1"}), self._r())

    def test_persist_does_not_touch_last_action_at(self):
        state = {"last_action_at": "run-start"}
        ps.make_persister(state, {"sq_1"})(self._r())
        self.assertEqual(state["last_action_at"], "run-start", "cadence must behave as before")


class _FakePage:
    def set_default_timeout(self, ms):
        pass


class _FakeBrowser:
    def new_page(self):
        return _FakePage()

    def close(self):
        pass


class _FakeSyncPlaywright:
    class _PW:
        class chromium:
            @staticmethod
            def launch(headless=True):
                return _FakeBrowser()

    def __enter__(self):
        return self._PW()

    def __exit__(self, *a):
        return False


class PostLoopTest(unittest.TestCase):
    """The real post_new_payments, with the browser and TA faked."""

    NAMES = ("sync_playwright", "log", "_self_heal_account", "INFLIGHT_DIR")
    BOT = ("block_beacon", "login", "post_payment", "recover_to_dashboard")

    def setUp(self):
        self._ps = {n: getattr(ps, n) for n in self.NAMES}
        self._bot = {n: getattr(bot, n) for n in self.BOT}
        self._inflight_tmp = tempfile.TemporaryDirectory()
        ps.INFLIGHT_DIR = pathlib.Path(self._inflight_tmp.name)
        ps.sync_playwright = lambda: _FakeSyncPlaywright()
        ps.log = lambda msg: None
        ps._self_heal_account = lambda *a, **k: None
        bot.block_beacon = lambda page: None
        bot.login = lambda page: None
        bot.recover_to_dashboard = lambda page: None
        ps._termination_requested = False
        self.attempted = []

    def tearDown(self):
        for n, v in self._ps.items():
            setattr(ps, n, v)
        for n, v in self._bot.items():
            setattr(bot, n, v)
        ps._termination_requested = False
        self._inflight_tmp.cleanup()

    def _items(self, n):
        return [{"id": f"sq_{i}", "name": f"Pat {i}", "date": "09/16/2026", "amount": "10.00",
                 "account": "", "customer_id": ""} for i in range(n)]

    def _script(self, *outcomes):
        seq = list(outcomes)
        def post(page, name, date, amount, account=None):
            self.attempted.append(name)
            return seq.pop(0)
        bot.post_payment = post

    def test_may_have_posted_id_is_retired_before_it_is_persisted(self):
        flag = bot._may_have_posted_flag("Pat 0", "V2: went dark")
        self._script((False, "FLAGGED", flag, None))
        posted, seen = set(), []
        ps.post_new_payments(self._items(1), posted,
                             on_result=lambda r: seen.append("sq_0" in posted))
        self.assertEqual(seen, [True], "the persisted state must already include the retired id")

    def test_recovery_failure_does_not_rewrite_the_recorded_status(self):
        self._script((False, "FAILED", "V2: something", None))
        def recover_fails(page):
            raise RuntimeError("wedged")
        bot.recover_to_dashboard = recover_fails
        recorded = []
        results = ps.post_new_payments(self._items(1), set(), on_result=recorded.append)
        self.assertEqual([r["status"] for r in results], ["FAILED"])
        self.assertEqual(len(recorded), 1, "exactly one outcome per payment")

    def test_self_heal_failure_cannot_add_a_second_result_to_a_posted_payment(self):
        self._script((True, "V2", None, None))
        def heal_fails(*a, **k):
            raise RuntimeError("square down")
        ps._self_heal_account = heal_fails
        recorded = []
        results = ps.post_new_payments(self._items(1), set(), on_result=recorded.append)
        self.assertEqual([r["status"] for r in results], ["OK"])
        self.assertEqual(len(recorded), 1)

    def test_sigterm_stops_before_the_next_payment(self):
        self._script((True, "V2", None, None), (True, "V2", None, None))
        def persist(r):
            ps._termination_requested = True   # SIGTERM arrives during payment 1
        ps.post_new_payments(self._items(2), set(), on_result=persist)
        self.assertEqual(self.attempted, ["Pat 0"], "payment 2 must not be started")

    def test_state_persist_failure_stops_the_batch(self):
        self._script((True, "V2", None, None), (True, "V2", None, None))
        def persist(r):
            raise ps.StatePersistError("disk full")
        with self.assertRaises(ps.StatePersistError):
            ps.post_new_payments(self._items(2), set(), on_result=persist)
        self.assertEqual(self.attempted, ["Pat 0"])


class SigtermKeepsCursorTest(unittest.TestCase):
    """Stopping early must not move the cursor past payments never attempted."""

    NAMES = ("save_state", "load_state", "fetch_completed_payments", "log", "LOCK_PATH",
             "SQUARE_ACCESS_TOKEN", "extract_payment_fields", "retry_candidates",
             "post_new_payments", "LEDGER_DIR", "CLEARED_PATH", "alert_admin", "INFLIGHT_DIR")

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # main() leaves the lock to process exit
        self._dir = tempfile.TemporaryDirectory()
        self._orig = {n: getattr(ps, n) for n in self.NAMES}
        self.saved = []
        ps.save_state = lambda state: self.saved.append(dict(state))
        ps.load_state = lambda: {"last_polled_at": "2026-09-16T00:00:00Z",
                                 "last_action_at": "2026-09-16T00:00:00", "posted_payment_ids": []}
        ps.fetch_completed_payments = lambda since: [{"id": "sq_a"}, {"id": "sq_b"}]
        ps.extract_payment_fields = lambda p: (f"Pat {p['id']}", "09/16/2026", "10.00", "")
        ps.retry_candidates = lambda posted: []
        ps.LEDGER_DIR = pathlib.Path(self._dir.name)
        ps.CLEARED_PATH = pathlib.Path(self._dir.name) / "manual_cleared.json"
        ps.alert_admin = lambda kind, detail: None
        ps.INFLIGHT_DIR = pathlib.Path(self._dir.name) / "inflight"
        ps.log = lambda msg: None
        ps.LOCK_PATH = pathlib.Path(self._dir.name) / "poll_square.lock"
        ps.SQUARE_ACCESS_TOKEN = "test-token"
        ps._termination_requested = False
        self._argv = sys.argv

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(ps, n, v)
        ps._termination_requested = False
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        sys.argv = self._argv
        self._dir.cleanup()

    def test_early_stop_leaves_the_cursor_where_it_was(self):
        def partial(to_post, posted_ids, on_result=None):
            posted_ids.add("sq_a")
            ps._termination_requested = True   # stopped after the first
            return [{**to_post[0], "status": "OK"}]
        ps.post_new_payments = partial
        sys.argv = ["poll_square.py", "--once"]
        ps.main()
        self.assertTrue(self.saved, "posted ids must still be saved")
        self.assertEqual(self.saved[-1]["last_polled_at"], "2026-09-16T00:00:00Z",
                         "cursor must not skip the unattempted payment")
        self.assertIn("sq_a", self.saved[-1]["posted_payment_ids"])

    def test_a_normal_run_still_advances_the_cursor(self):
        ps.post_new_payments = lambda to_post, posted_ids, on_result=None: [
            {**t, "status": "OK"} for t in to_post]
        sys.argv = ["poll_square.py", "--once"]
        ps.main()
        self.assertNotEqual(self.saved[-1]["last_polled_at"], "2026-09-16T00:00:00Z")


class FlagOrderingTest(unittest.TestCase):
    """An AT_PAYMENT_FORM error whose message happens to contain "FLAG" still gets the marker."""

    def setUp(self):
        self._v2 = bot.post_payment_v2

    def tearDown(self):
        bot.post_payment_v2 = self._v2

    def test_tagged_error_containing_flag_is_still_marked_may_have_posted(self):
        def v2(*a, **k):
            raise Exception("AT_PAYMENT_FORM: TA said FLAGGED_FOR_REVIEW then the page closed")
        bot.post_payment_v2 = v2
        success, method, error, *_ = bot.post_payment(None, "Pat Example", "09/16/2026", "75.00")
        self.assertEqual(method, "FLAGGED")
        self.assertIn(bot.MAY_HAVE_POSTED_MARKER, error,
                      "without the marker the poller would not retire it")



class FailClosedThroughTheGateTest(_LedgerCase):
    """Through the real retry gate: an unfamiliar separator must not let V2 vouch for V1."""

    def test_changed_separator_is_not_retried(self):
        R = bot.APP_NOT_RENDERING_REASON
        self._row(reason=f"V2: {R} | V1: Page.click: Timeout 30000ms exceeded")
        self.assertEqual(self._ids(), [], "V1 may have submitted; must not be retried")


class CorruptStateNeverForgetsTest(unittest.TestCase):
    """A corrupt state file must never quietly come back as 'nothing posted yet'."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path, self._log = ps.STATE_PATH, ps.log
        ps.STATE_PATH = pathlib.Path(self._dir.name) / "poll_state.json"
        ps.log = lambda msg: None

    def tearDown(self):
        ps.STATE_PATH, ps.log = self._path, self._log
        self._dir.cleanup()

    def test_corrupt_file_is_not_silently_replaced_with_empty_state(self):
        ps.STATE_PATH.write_text('{"posted_payment_ids": ["a", "b"')  # truncated mid-write
        try:
            state = ps.load_state()
        except Exception:
            return  # refusing to run is the correct outcome
        self.fail(f"load_state silently returned {state!r}; the run would re-post today's payments")



# ============================================================================
# Durability round: a state or ledger write that fails, is killed mid-write, or is
# undone by a power loss must never re-post a payment or hide one from staff.
# ============================================================================


class _MainHarness(unittest.TestCase):
    """Drives the real main() with Square, TA and the filesystem faked.

    Uses getattr(..., None) so the harness also runs against code that predates a
    symbol — which is what lets a pre-fix run show a behavioural failure rather than
    just a missing name.
    """

    NAMES = ("save_state", "load_state", "fetch_completed_payments", "log", "LOCK_PATH",
             "SQUARE_ACCESS_TOKEN", "extract_payment_fields", "retry_candidates",
             "post_new_payments", "LEDGER_DIR", "CLEARED_PATH", "alert_admin", "record_ledger",
             "INFLIGHT_DIR", "payment_still_completed")

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)
        self._dir = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._dir.name)
        self._orig = {n: getattr(ps, n, None) for n in self.NAMES}
        self.saved, self.alerts, self.posted_to = [], [], []
        ps.save_state = lambda state: self.saved.append({k: (list(v) if isinstance(v, (list, set)) else v)
                                                        for k, v in state.items()})
        ps.load_state = lambda: {"last_polled_at": "2026-09-16T00:00:00Z",
                                 "last_action_at": "2026-09-16T00:00:00", "posted_payment_ids": []}
        ps.fetch_completed_payments = lambda since: [{"id": "sq_a"}]
        ps.extract_payment_fields = lambda p: ("Pat Example", "09/16/2026", "10.00", "")
        ps.retry_candidates = lambda posted: []
        ps.log = lambda msg: None
        ps.LOCK_PATH = self.dir / "poll_square.lock"
        ps.SQUARE_ACCESS_TOKEN = "test-token"
        ps.LEDGER_DIR = self.dir
        ps.INFLIGHT_DIR = self.dir / "inflight"
        ps.CLEARED_PATH = self.dir / "manual_cleared.json"
        ps.alert_admin = lambda kind, detail: self.alerts.append(kind)

        def post(to_post, posted_ids, on_result=None):
            self.posted_to.extend(t["id"] for t in to_post)
            return []
        ps.post_new_payments = post
        ps._termination_requested = False
        self._argv = sys.argv
        sys.argv = ["poll_square.py", "--once"]

    def tearDown(self):
        for n, v in self._orig.items():
            if v is None:
                if hasattr(ps, n):
                    delattr(ps, n)
            else:
                setattr(ps, n, v)
        ps._termination_requested = False
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        sys.argv = self._argv
        self._dir.cleanup()

    def run_main(self):
        try:
            ps.main()
        except BaseException:
            pass

    def ledger(self, rows):
        (self.dir / "09162026.json").write_text(json.dumps(rows))


class LedgerIsTheSecondDedupeTest(_MainHarness):
    """If state ever loses a posted id, the ledger row must still stop a re-post."""

    def test_ok_ledger_row_is_not_posted_again(self):
        self.ledger([{"id": "sq_a", "status": "OK", "date": "09/16/2026"}])
        self.run_main()
        self.assertEqual(self.posted_to, [], "already OK in the ledger; posting again double-charges")

    def test_may_have_posted_ledger_row_is_not_posted_again(self):
        self.ledger([{"id": "sq_a", "status": "FLAGGED", "date": "09/16/2026",
                      "reason": bot._may_have_posted_flag("Pat Example", "V2: went dark")}])
        self.run_main()
        self.assertEqual(self.posted_to, [])

    def test_healed_id_is_written_back_to_state(self):
        self.ledger([{"id": "sq_a", "status": "OK", "date": "09/16/2026"}])
        self.run_main()
        self.assertTrue(self.saved and "sq_a" in self.saved[-1].get("posted_payment_ids", []))

    def test_a_failed_payment_is_not_reposted_as_new_when_the_cursor_rereads_it(self):
        # The overlap window, or a run that stopped early and kept its cursor, brings it back
        # among "new" payments. Posting it from there skips the reason allowlist, the report
        # cutoff and the attempt limit — and "V2: x" may well have submitted.
        self.ledger([{"id": "sq_a", "status": "FAILED", "date": "09/16/2026", "reason": "V2: x"}])
        self.run_main()
        self.assertEqual(self.posted_to, [], "only the retry gate may re-attempt a failure")

    def test_a_shadow_would_post_row_is_still_posted(self):
        self.ledger([{"id": "sq_a", "status": "WOULD_POST", "date": "09/16/2026"}])
        self.run_main()
        self.assertEqual(self.posted_to, ["sq_a"])

    def test_a_retryable_failure_is_still_retried_exactly_once(self):
        row = {"id": "sq_a", "name": "Pat Example", "status": "FAILED", "date": "09/16/2026",
               "amount": "10.00", "reason": SAFE_REASON, "failed_at": _ago(5), "retries": 0}
        self.ledger([row])
        ps.retry_candidates = lambda posted: [dict(row)]
        ps.payment_still_completed = lambda pid: {"id": pid}
        self.run_main()
        self.assertEqual(self.posted_to, ["sq_a"])


class PreflightTest(_MainHarness):
    """An unwritable state file must stop the run BEFORE any payment is attempted."""

    def test_unwritable_state_refuses_before_touching_ta(self):
        def unwritable(state):
            raise OSError("disk full")
        ps.save_state = unwritable
        self.run_main()
        self.assertEqual(self.posted_to, [], "no payment may be attempted if it can't be recorded")


class UnreadableStateAlertsTest(_MainHarness):
    def test_refusing_to_run_is_not_silent(self):
        def corrupt():
            raise ps.StateUnreadableError("poll_state.json is truncated")
        ps.load_state = corrupt
        self.run_main()
        self.assertIn("state-unreadable", self.alerts, "a stalled poller must tell someone")


class LedgerSecondChanceTest(_MainHarness):
    """A ledger row that can't be written must not quietly leave the report wrong."""

    def _post_one_ok(self):
        def post(to_post, posted_ids, on_result=None):
            r = {**to_post[0], "status": "OK", "method": "V2"}
            posted_ids.add(r["id"])
            ps._flush(on_result, r)
            return [r]
        ps.post_new_payments = post

    def test_ledger_that_still_fails_raises_an_alert(self):
        self._post_one_ok()
        def always_fails(*a, **k):
            raise OSError("disk full")
        ps.record_ledger = always_fails
        self.run_main()
        self.assertIn("ledger-write-failed", self.alerts)

    def test_ledger_that_succeeds_on_second_try_is_quiet(self):
        self._post_one_ok()
        attempts = []
        def fails_once(*a, **k):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("transient")
        ps.record_ledger = fails_once
        self.run_main()
        self.assertNotIn("ledger-write-failed", self.alerts)


class LedgerNeverOverwrittenWhenUnreadableTest(_LedgerCase):
    def test_corrupt_day_file_is_left_exactly_as_it_was(self):
        path = ps.LEDGER_DIR / "09162026.json"
        path.write_text('[{"id": "sq_x", "status": "OK", "amount": "95.00"')  # truncated by a kill
        before = path.read_bytes()
        try:
            ps.record_ledger("sq_new", "Pat", "09/16/2026", "10.00", "OK")
        except Exception:
            pass
        self.assertEqual(path.read_bytes(), before,
                         "overwriting it erases the day's recorded payments")

    def test_retry_attempt_never_writes_over_a_corrupt_file(self):
        path = ps.LEDGER_DIR / "09162026.json"
        path.write_text('[{"id": "sq_1"')
        before = path.read_bytes()
        self.assertFalse(ps.mark_retry_attempt({"id": "sq_1", "date": "09/16/2026"}))
        self.assertEqual(path.read_bytes(), before)


class LedgerWriteIsAtomicTest(_LedgerCase):
    def test_a_failed_rename_leaves_the_day_file_intact(self):
        ps.record_ledger("sq_1", "Pat", "09/16/2026", "10.00", "OK")
        path = ps.LEDGER_DIR / "09162026.json"
        before = path.read_bytes()
        real = os.replace
        def dies(src, dst):
            raise OSError("killed mid-write")
        ps.os.replace = dies
        try:
            with self.assertRaises(OSError):
                ps.record_ledger("sq_2", "Pat", "09/16/2026", "20.00", "OK")
        finally:
            ps.os.replace = real
        self.assertEqual(path.read_bytes(), before)


class PersisterWritesLedgerDespiteStateFailureTest(PersisterTest):
    def test_ledger_row_is_still_written_when_state_save_fails(self):
        def state_fails(state):
            raise OSError("disk full")
        ps.save_state = state_fails
        with self.assertRaises(ps.StatePersistError):
            ps._flush(ps.make_persister({}, {"sq_1"}), self._r())
        self.assertIn(("ledger", "sq_1"), self.calls,
                      "the ledger row is how the next run recognises it")

    def test_ledger_failure_is_kept_for_a_second_attempt(self):
        def ledger_fails(*a, **k):
            raise OSError("disk full")
        ps.record_ledger = ledger_fails
        persist = ps.make_persister({}, {"sq_1"})
        ps._flush(persist, self._r())
        self.assertEqual([r["id"] for r in persist.ledger_failures], ["sq_1"])
        self.assertEqual(len(ps.retry_failed_ledger_writes(persist)), 1)


class FullFsyncTest(unittest.TestCase):
    """os.fsync on macOS doesn't flush the drive's cache; a power loss could undo the save."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path = ps.STATE_PATH
        ps.STATE_PATH = pathlib.Path(self._dir.name) / "poll_state.json"
        self._fcntl = ps.fcntl.fcntl

    def tearDown(self):
        ps.fcntl.fcntl = self._fcntl
        ps.STATE_PATH = self._path
        self._dir.cleanup()

    @unittest.skipUnless(hasattr(ps.fcntl, "F_FULLFSYNC"), "F_FULLFSYNC is macOS-only")
    def test_state_save_asks_the_drive_to_flush(self):
        calls, real = [], self._fcntl
        def record(fd, cmd, *a):
            calls.append(cmd)
            return real(fd, cmd, *a)
        ps.fcntl.fcntl = record
        ps.save_state({"posted_payment_ids": ["a"]})
        self.assertIn(ps.fcntl.F_FULLFSYNC, calls)


class AlertRateLimitTest(unittest.TestCase):
    """The poller fires every 30 minutes; a stuck condition must not send 40 emails."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._data, self._send, self._log = bot.DATA_DIR, bot.send_email, ps.log
        bot.DATA_DIR = pathlib.Path(self._dir.name)
        ps.log = lambda msg: None
        self.sent = []
        bot.send_email = lambda *a, **k: self.sent.append(a[2]) or True

    def tearDown(self):
        bot.DATA_DIR, bot.send_email, ps.log = self._data, self._send, self._log
        self._dir.cleanup()

    def test_same_kind_same_day_sends_once(self):
        ps.alert_admin("state-save-failed", "disk full")
        ps.alert_admin("state-save-failed", "disk full")
        self.assertEqual(len(self.sent), 1)

    def test_a_different_kind_still_sends(self):
        ps.alert_admin("state-save-failed", "x")
        ps.alert_admin("ledger-write-failed", "y")
        self.assertEqual(len(self.sent), 2)



# ============================================================================
# Round 8: write-ahead intent. Money moves only after a durable record that it is about
# to; a run that dies (or whose writes both fail) leaves that record behind, and the next
# run treats the payment as may-have-posted instead of posting it again.
# ============================================================================


class InterruptedPostTest(_MainHarness):
    """The reviewer's double failure: posted in TA, then state AND ledger writes both failed."""

    def _mark_started(self, pid):
        d = self.dir / "inflight"
        d.mkdir(exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps({"id": pid}))

    def test_started_but_unrecorded_payment_is_not_posted_again(self):
        self._mark_started("sq_a")  # state lost it, and the ledger has no row
        self.run_main()
        self.assertEqual(self.posted_to, [], "it may already be in TA")

    def test_it_is_flagged_for_staff_and_the_admin_is_told(self):
        self._mark_started("sq_a")
        self.run_main()
        rows = json.loads((self.dir / "09162026.json").read_text())
        self.assertTrue(any(r.get("id") == "sq_a" and bot.MAY_HAVE_POSTED_MARKER in (r.get("reason") or "")
                            for r in rows), "the morning report must say check TA first")
        self.assertIn("interrupted-post", self.alerts)

    def test_corrupt_ledger_plus_a_started_marker_is_not_posted(self):
        self._mark_started("sq_a")
        (self.dir / "09162026.json").write_text('[{"id": "sq_a"')  # unreadable
        self.run_main()
        self.assertEqual(self.posted_to, [])

    def test_stale_marker_for_a_recorded_ok_payment_is_just_cleared(self):
        self._mark_started("sq_a")
        self.ledger([{"id": "sq_a", "status": "OK", "date": "09/16/2026"}])
        self.run_main()
        self.assertEqual(self.posted_to, [])
        self.assertNotIn("interrupted-post", self.alerts, "a recorded OK is not an interruption")
        self.assertFalse((self.dir / "inflight" / "sq_a.json").exists())

    def test_interrupted_retry_is_not_retried(self):
        R = bot.APP_NOT_RENDERING_REASON
        row = {"id": "sq_r", "name": "Pat Retry", "date": "09/16/2026", "amount": "10.00",
               "status": "FAILED", "account": "", "reason": f"V2: {R}",
               "failed_at": "2026-09-16T12:00:00Z", "retries": 0}
        self.ledger([row])
        self._mark_started("sq_r")  # its FAILED row predates the attempt that was cut off
        ps.fetch_completed_payments = lambda since: []
        ps.retry_candidates = lambda posted: [] if "sq_r" in posted else [row]
        ps.payment_still_completed = lambda pid: {"id": pid, "customer_id": ""}
        self.run_main()
        self.assertEqual(self.posted_to, [], "the interrupted attempt may have posted it")


class PreflightBeforeRetrySpendTest(_MainHarness):
    def test_a_run_that_cannot_post_does_not_use_up_a_retry(self):
        R = bot.APP_NOT_RENDERING_REASON
        row = {"id": "sq_r", "name": "Pat Retry", "date": "09/16/2026", "amount": "10.00",
               "status": "FAILED", "account": "", "reason": f"V2: {R}",
               "failed_at": "2026-09-16T12:00:00Z", "retries": 0}
        self.ledger([row])
        ps.fetch_completed_payments = lambda since: []
        ps.retry_candidates = lambda posted: [row]
        ps.payment_still_completed = lambda pid: {"id": pid, "customer_id": ""}
        def unwritable(state):
            raise OSError("disk full")
        ps.save_state = unwritable
        self.run_main()
        rows = json.loads((self.dir / "09162026.json").read_text())
        self.assertEqual(rows[0]["retries"], 0, "two such runs would exhaust the budget for nothing")


class AlertDoesNotHideADifferentProblemTest(unittest.TestCase):
    setUp = AlertRateLimitTest.setUp
    tearDown = AlertRateLimitTest.tearDown

    def test_same_kind_but_a_different_problem_still_alerts(self):
        ps.alert_admin("ledger-write-failed", "an unattributed $10.00 payment could not be recorded")
        ps.alert_admin("ledger-write-failed", "Pat B $95.00 posted in TA but has no ledger row")
        self.assertEqual(len(self.sent), 2, "the later, serious one must not be swallowed")

    def test_identical_repeats_are_still_rate_limited(self):
        for _ in range(3):
            ps.alert_admin("state-save-failed", "disk full")
        self.assertEqual(len(self.sent), 1)


class WriteAheadIntentTest(unittest.TestCase):
    """Contract for the intent record itself, driven through the real post_new_payments."""

    NAMES = PostLoopTest.NAMES
    BOT = PostLoopTest.BOT
    setUp = PostLoopTest.setUp
    tearDown = PostLoopTest.tearDown
    _items = PostLoopTest._items
    _script = PostLoopTest._script

    def _markers(self):
        d = ps.INFLIGHT_DIR
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def test_intent_is_on_disk_before_the_post(self):
        seen = []
        def post(page, name, date, amount, account=None):
            seen.append(self._markers())
            return (True, "V2", None, None)
        bot.post_payment = post
        ps.post_new_payments(self._items(1), set(), on_result=lambda r: None)
        self.assertEqual(seen, [["sq_0"]])

    def test_intent_is_cleared_once_the_outcome_is_saved(self):
        self._script((True, "V2", None, None))
        ps.post_new_payments(self._items(1), set(), on_result=lambda r: None)
        self.assertEqual(self._markers(), [])

    def test_intent_is_kept_when_the_outcome_could_not_be_saved(self):
        self._script((True, "V2", None, None))
        def persist(r):
            raise ps.StatePersistError("disk full")
        with self.assertRaises(ps.StatePersistError):
            ps.post_new_payments(self._items(1), set(), on_result=persist)
        self.assertEqual(self._markers(), ["sq_0"], "the next run must see it was started")

    def test_nothing_is_posted_if_the_intent_cannot_be_recorded(self):
        self._script((True, "V2", None, None))
        real = ps.mark_inflight
        def unwritable(item):
            raise OSError("disk full")
        ps.mark_inflight = unwritable
        try:
            with self.assertRaises(ps.StatePersistError):
                ps.post_new_payments(self._items(1), set(), on_result=lambda r: None)
        finally:
            ps.mark_inflight = real
        self.assertEqual(self.attempted, [], "no money moves without a durable intent record")



# ============================================================================
# Round 9: an intent marker is removed only once a durable record makes it redundant.
# Otherwise a payment could end up retired with no record — shown as simply unposted on
# the morning report, posted by staff, and possibly already in TA.
# ============================================================================


class InterruptedPostRecordTest(_MainHarness):
    def _mark_started(self, pid, date="09/16/2026"):
        d = self.dir / "inflight"
        d.mkdir(exist_ok=True)
        (d / f"{pid}.json").write_text(json.dumps(
            {"id": pid, "name": "Pat Example", "date": date, "amount": "10.00"}))

    def _marker_exists(self, pid):
        return (self.dir / "inflight" / f"{pid}.json").exists()

    def _state_with(self, *ids):
        ps.load_state = lambda: {"last_polled_at": "2026-09-16T00:00:00Z",
                                 "last_action_at": "2026-09-16T00:00:00",
                                 "posted_payment_ids": list(ids)}

    def test_marker_is_kept_until_the_flag_is_recorded(self):
        self._mark_started("sq_a")
        (self.dir / "09162026.json").write_text('[{"id": "sq_a"')  # the flag can't be written
        self.run_main()
        self.assertTrue(self._marker_exists("sq_a"), "no durable record yet, so keep the intent")

    def test_retired_id_with_no_record_is_flagged_not_silently_cleared(self):
        self._mark_started("sq_a")
        self._state_with("sq_a")
        ps.fetch_completed_payments = lambda since: []
        self.run_main()
        self.assertIn("interrupted-post", self.alerts,
                      "retired with no ledger record would read as simply unposted")

    def test_retired_id_with_an_ok_ledger_row_is_cleared_quietly(self):
        self._mark_started("sq_a")
        self.ledger([{"id": "sq_a", "status": "OK", "date": "09/16/2026"}])
        self._state_with("sq_a")
        ps.fetch_completed_payments = lambda since: []
        self.run_main()
        self.assertFalse(self._marker_exists("sq_a"))
        self.assertNotIn("interrupted-post", self.alerts)

    def test_orphan_marker_not_seen_this_run_is_still_handled(self):
        self._mark_started("sq_z")
        ps.fetch_completed_payments = lambda since: []
        self.run_main()
        self.assertIn("interrupted-post", self.alerts, "a marker must never be ignored forever")


class LedgerFailureKeepsIntentTest(unittest.TestCase):
    NAMES = PostLoopTest.NAMES
    BOT = PostLoopTest.BOT
    setUp = PostLoopTest.setUp
    tearDown = PostLoopTest.tearDown
    _items = PostLoopTest._items
    _script = PostLoopTest._script

    def test_intent_marker_stays_when_the_ledger_row_did_not_land(self):
        self._script((True, "V2", None, None))
        real_save, real_ledger = ps.save_state, ps.record_ledger
        ps.save_state = lambda state: None
        def ledger_fails(*a, **k):
            raise OSError("disk full")
        ps.record_ledger = ledger_fails
        try:
            posted = set()
            ps.post_new_payments(self._items(1), posted, on_result=ps.make_persister({}, posted))
        finally:
            ps.save_state, ps.record_ledger = real_save, real_ledger
        self.assertEqual(sorted(p.stem for p in ps.INFLIGHT_DIR.glob("*.json")), ["sq_0"],
                         "posted but unrecorded in the ledger: keep the intent")



# ============================================================================
# Round 10 (poller side): the run lock fails closed; a ledger row that lands on its second
# chance clears its intent marker; refusal alerts name payments left mid-post.
# ============================================================================


class RunLockFailsClosedTest(unittest.TestCase):
    """Two unlocked runs would post the same payments twice."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path, self._alert, self._log = ps.LOCK_PATH, getattr(ps, "alert_admin", None), ps.log
        self.alerts = []
        ps.alert_admin = lambda kind, detail: self.alerts.append(kind)
        ps.log = lambda msg: None
        ps.LOCK_PATH = pathlib.Path(self._dir.name)  # a directory: open(..., "w") must fail

    def tearDown(self):
        ps.LOCK_PATH, ps.log = self._path, self._log
        if self._alert is not None:
            ps.alert_admin = self._alert
        self._dir.cleanup()

    def test_a_lock_that_cannot_be_opened_refuses_the_run(self):
        _handle, may_run = ps.acquire_run_lock()
        self.assertFalse(may_run, "running unlocked risks two runs posting the same payments")

    def test_and_tells_the_admin(self):
        ps.acquire_run_lock()
        self.assertIn("run-lock-unavailable", self.alerts)


class SecondChanceClearsMarkerTest(unittest.TestCase):
    """A row that lands late must not leave a marker that later reads as an interruption."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._dir.name)
        self._orig = {n: getattr(ps, n) for n in ("save_state", "record_ledger", "log", "INFLIGHT_DIR")}
        ps.save_state = lambda state: None
        ps.log = lambda msg: None
        ps.INFLIGHT_DIR = self.dir

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(ps, n, v)
        self._dir.cleanup()

    def test_marker_is_cleared_when_the_second_attempt_lands(self):
        (self.dir / "sq_1.json").write_text(json.dumps({"id": "sq_1"}))
        attempts = []
        def fails_once(*a, **k):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("transient")
        ps.record_ledger = fails_once
        persist = ps.make_persister({}, {"sq_1"})
        ps._flush(persist, {"id": "sq_1", "name": "Pat", "date": "09/16/2026",
                            "amount": "10.00", "status": "FAILED"})
        self.assertEqual(ps.retry_failed_ledger_writes(persist), [])
        self.assertFalse((self.dir / "sq_1.json").exists(),
                         "a stale marker would overwrite the accurate row with a generic flag")


class RefusalAlertsListPendingMarkersTest(_MainHarness):
    """While the poller refuses to run, nothing else says which payments were left mid-post."""

    def setUp(self):
        _MainHarness.setUp(self)
        self.details = []
        ps.alert_admin = lambda kind, detail: (self.alerts.append(kind), self.details.append(detail))
        d = self.dir / "inflight"
        d.mkdir(exist_ok=True)
        (d / "sq_left.json").write_text(json.dumps(
            {"id": "sq_left", "name": "Pat Leftover", "date": "09/16/2026", "amount": "95.00"}))

    def test_preflight_refusal_names_the_pending_payment(self):
        def unwritable(state):
            raise OSError("disk full")
        ps.save_state = unwritable
        self.run_main()
        self.assertTrue(any("sq_left" in d for d in self.details),
                        "staff must know this may already be in TA")

    def test_unreadable_state_refusal_names_the_pending_payment(self):
        def corrupt():
            raise ps.StateUnreadableError("truncated")
        ps.load_state = corrupt
        self.run_main()
        self.assertTrue(any("sq_left" in d for d in self.details))



class RunLockNonContentionAlertsTest(unittest.TestCase):
    """Only real contention may refuse quietly; a filesystem that can't lock must alert."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._path, self._log = ps.LOCK_PATH, ps.log
        self._alert = getattr(ps, "alert_admin", None)
        self._flock = ps.fcntl.flock
        self.alerts = []
        ps.alert_admin = lambda kind, detail: self.alerts.append(kind)
        ps.log = lambda msg: None
        ps.LOCK_PATH = pathlib.Path(self._dir.name) / "poll_square.lock"

    def tearDown(self):
        ps.fcntl.flock = self._flock
        ps.LOCK_PATH, ps.log = self._path, self._log
        if self._alert is not None:
            ps.alert_admin = self._alert
        self._dir.cleanup()

    def test_a_filesystem_that_cannot_lock_alerts(self):
        import errno
        def cannot_lock(handle, op):
            raise OSError(errno.ENOLCK, "No locks available")
        ps.fcntl.flock = cannot_lock
        _h, may_run = ps.acquire_run_lock()
        self.assertFalse(may_run)
        self.assertIn("run-lock-unavailable", self.alerts,
                      "otherwise posting stops for good and nobody is told")

    def test_ordinary_contention_stays_quiet(self):
        def held(handle, op):
            raise BlockingIOError("held by another run")
        ps.fcntl.flock = held
        _h, may_run = ps.acquire_run_lock()
        self.assertFalse(may_run)
        self.assertEqual(self.alerts, [], "another run holding the lock is normal")



class ReportTimeFromStampTest(_LedgerCase):
    """The morning report reads the ledger whenever it actually runs — 08:20 if the Mac woke
    late. A failure it may have shown staff must never be auto-retried, so the cutoff comes
    from the report's own stamp, not an assumed 08:00."""

    def setUp(self):
        super().setUp()
        ps.last_report_at = self._report          # the real one, under test
        self._stamp = ps.REPORT_STAMP_PATH
        ps.REPORT_STAMP_PATH = pathlib.Path(self._dir.name) / "report_stamp.json"

    def tearDown(self):
        ps.REPORT_STAMP_PATH = self._stamp
        super().tearDown()

    @staticmethod
    def _scheduled():
        local_now = datetime.now().astimezone()
        b = local_now.replace(hour=ps.REPORT_HOUR, minute=0, second=0, microsecond=0)
        if b > local_now:
            b -= timedelta(days=1)
        return b.astimezone(timezone.utc)

    def _stamp_file(self, **fields):
        ps.REPORT_STAMP_PATH.write_text(json.dumps(fields))

    @staticmethod
    def _local(day, hour):
        """A fixed local wall-clock time in September 2026, as UTC (17th = Thursday)."""
        return datetime(2026, 9, day, hour, 0).astimezone().astimezone(timezone.utc)

    @staticmethod
    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_no_stamp_on_a_weekend_falls_back_to_the_scheduled_time(self):
        self.assertEqual(ps.last_report_at(self._local(19, 10)), self._local(19, 8))

    def test_no_stamp_before_the_report_is_due_falls_back_to_yesterdays(self):
        self.assertEqual(ps.last_report_at(self._local(17, 7)), self._local(16, 8))

    def test_a_weekday_report_that_left_no_stamp_holds_every_retry(self):
        # It hasn't run yet (the Mac slept through 08:00), or it ran and couldn't write the
        # stamp; either way staff may have a list the poller can't see.
        now = self._local(17, 10)
        self.assertEqual(ps.last_report_at(now), now)

    def test_a_stamp_from_yesterday_does_not_count_for_today(self):
        self._stamp_file(started_at=self._iso(self._local(16, 8)), sent_at=self._iso(self._local(16, 8)))
        now = self._local(17, 10)
        self.assertEqual(ps.last_report_at(now), now)

    def test_todays_sent_report_releases_retries_after_it(self):
        self._stamp_file(started_at=self._iso(self._local(17, 9)), sent_at=self._iso(self._local(17, 9)))
        self.assertEqual(ps.last_report_at(self._local(17, 10)), self._local(17, 9) + timedelta(seconds=1))

    def test_a_late_report_moves_the_cutoff_past_what_it_read(self):
        sent = datetime.now(timezone.utc) - timedelta(seconds=30)
        if sent <= self._scheduled() + timedelta(seconds=5):
            self.skipTest("too close to the scheduled report time to tell the two apart")
        self._stamp_file(started_at=_ago(1), sent_at=sent.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertGreater(ps.last_report_at(), sent)

    def test_a_failure_the_late_report_may_have_listed_is_not_retried(self):
        self._stamp_file(started_at=_ago(2), sent_at=_ago(1))
        self._row(failed_at=_ago(3))
        if datetime.now(timezone.utc) - timedelta(minutes=3) <= self._scheduled():
            self.skipTest("too close to the scheduled report time to tell the two apart")
        self.assertEqual(self._ids(), [], "staff may be posting it by hand from the email")

    def test_a_failure_after_the_report_went_out_is_still_retried(self):
        self._stamp_file(started_at=_ago(3), sent_at=_ago(2))
        self._row(failed_at=_ago(1))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_nothing_is_retried_while_a_report_is_being_built(self):
        self._stamp_file(started_at=_ago(1), sent_at=_ago(60 * 24))
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertGreaterEqual(ps.last_report_at(), before)

    def test_a_report_that_died_stops_holding_retries_after_the_grace(self):
        started = self._local(17, 9)
        self._stamp_file(started_at=self._iso(started))
        self.assertEqual(ps.last_report_at(self._local(17, 12)), started + ps.REPORT_RUN_GRACE)

    def test_an_unreadable_stamp_holds_all_retries(self):
        ps.REPORT_STAMP_PATH.write_text("{not json")
        self._row(failed_at=_ago(1))
        self.assertEqual(self._ids(), [])



class RetryReportRaceTest(PostLoopTest):
    """A run can be minutes into posting before it reaches a queued retry, and the morning
    report may start in between. The cutoff is checked again, per retry, just before posting."""

    def setUp(self):
        super().setUp()
        self._report = ps.last_report_at

    def tearDown(self):
        ps.last_report_at = self._report
        super().tearDown()

    def _retry(self, failed_minutes_ago=5):
        item = self._items(1)[0]
        item["retry_failed_at"] = _ago(failed_minutes_ago)
        return item

    def test_a_retry_the_report_has_since_read_is_not_posted(self):
        self._script((True, "V2", None, None))
        ps.last_report_at = lambda: datetime.now(timezone.utc)
        results = ps.post_new_payments([self._retry()], set(), on_result=lambda r: None)
        self.assertEqual(self.attempted, [], "staff may be posting it by hand from the email")
        self.assertEqual(results, [])
        self.assertEqual(list(ps.INFLIGHT_DIR.glob("*.json")), [], "nothing was attempted")

    def test_the_cutoff_is_read_after_the_intent_marker_is_written(self):
        self._script((True, "V2", None, None))
        seen = []
        def spy():
            seen.append(bool(list(ps.INFLIGHT_DIR.glob("*.json"))))
            return datetime.now(timezone.utc) - timedelta(days=1)
        ps.last_report_at = spy
        ps.post_new_payments([self._retry()], set(), on_result=lambda r: None)
        self.assertTrue(seen, "the cutoff must be re-checked per retry")
        self.assertTrue(all(seen), "checked before the marker, a report could slip in between")

    def test_a_retry_with_no_report_since_is_posted(self):
        self._script((True, "V2", None, None))
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(days=1)
        ps.post_new_payments([self._retry()], set(), on_result=lambda r: None)
        self.assertEqual(self.attempted, ["Pat 0"])

    def test_a_new_payment_is_not_held_by_the_report_cutoff(self):
        self._script((True, "V2", None, None))
        ps.last_report_at = lambda: datetime.now(timezone.utc)
        ps.post_new_payments(self._items(1), set(), on_result=lambda r: None)
        self.assertEqual(self.attempted, ["Pat 0"])


if __name__ == "__main__":
    unittest.main()
