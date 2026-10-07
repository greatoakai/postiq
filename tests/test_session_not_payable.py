"""A payment for a session that's in progress must post later, on its own.

On 10/06/2026 three early-morning payments (Tyler Dunning, Shianne Peters, Al Ford)
failed: each session was in progress, the appointment still showed "Start Billing"
with its action panel spinning, Accept Payment opened no form, and the bot crashed
on the missing amount field ('NoneType' object has no attribute 'click'). V1 then
failed before reaching its own form, and nothing about it was retryable, so all
three went to staff. (Jacob Legrand hit the same page that morning and V1 posted
him, so V1 stays.) Now each step says what happened, and when nothing got as far
as a form the poller retries hourly that day, by when the session is over.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from test_retry_failed_payments import _LedgerCase, _ago, bot, ps

TODAY = datetime.now(timezone.utc).astimezone().strftime("%m/%d/%Y")

UNBILLED = (f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}; "
            f"V2-retry: {bot.SESSION_NOT_PAYABLE_REASON}; V1: {bot.V1_BEFORE_FORM_REASON}")


class SessionWaitRetryGateTest(_LedgerCase):
    def _today(self, **overrides):
        """An unbilled row paid today (the only day it's retried), in today's ledger file."""
        row = {"id": "sq_1", "name": "Pat Example", "date": TODAY, "amount": "30.00",
               "status": "FAILED", "account": "C000000001", "reason": UNBILLED,
               "method": "", "note": "", "failed_at": _ago(10), "retries": 0}
        row.update(overrides)
        (ps.LEDGER_DIR / f"{TODAY.replace('/', '')}.json").write_text(json.dumps([row]))
        return row

    def test_new_reasons_are_retryable_and_safe_wording(self):
        self.assertTrue(ps._reason_is_only_retryable(UNBILLED))
        for r in (bot.SESSION_NOT_PAYABLE_REASON, bot.PAYMENT_FORM_NOT_READY_REASON):
            self.assertNotIn("FLAG", r)
            self.assertNotIn("AT_PAYMENT_FORM", r)
            self.assertFalse(bot._is_no_appointment_error(r),
                             "must never reroute the money to the open balance")

    def test_first_retry_waits_an_hour_after_the_failure(self):
        self._today(failed_at=_ago(30))
        self.assertEqual(self._ids(), [])

    def test_session_wait_is_retried_past_the_usual_window_and_the_report(self):
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(minutes=5)
        self._today(failed_at=_ago(ps.RETRY_WINDOW_MIN + 120))
        self.assertEqual(self._ids(), ["sq_1"], "today's payments are on no report yet")

    def test_session_wait_stops_once_its_day_is_over(self):
        self._row(reason=UNBILLED)              # dated 09/16/2026
        self.assertEqual(self._ids(), [])

    def test_form_not_ready_alone_keeps_the_usual_window(self):
        self._today(reason=f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}",
                    failed_at=_ago(ps.RETRY_WINDOW_MIN + 1))
        self.assertEqual(self._ids(), [])

    def test_awaiting_session_is_sticky(self):
        self._today(reason=f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}", awaiting_session=True,
                    retries=ps.RETRY_MAX_ATTEMPTS, failed_at=_ago(ps.RETRY_WINDOW_MIN + 60))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_session_wait_waits_an_hour_between_attempts(self):
        self._today(retries=1, last_retry_at=_ago(20))
        self.assertEqual(self._ids(), [])
        self._today(retries=1, last_retry_at=_ago(56))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_session_wait_gives_up_after_its_own_limit(self):
        self._today(retries=ps.RETRY_MAX_ATTEMPTS, failed_at=_ago(120))
        self.assertEqual(self._ids(), ["sq_1"], "more attempts than an ordinary failure")
        self._today(retries=ps.RETRY_MAX_ATTEMPTS_SESSION, failed_at=_ago(120))
        self.assertEqual(self._ids(), [])

    def test_ordinary_failures_come_before_billing_waits(self):
        rows = [{"id": f"wait_{i}", "name": "Pat", "date": TODAY, "amount": "1.00",
                 "status": "FAILED", "reason": UNBILLED, "failed_at": _ago(300 - i),
                 "retries": 0} for i in range(ps.RETRY_MAX_PER_RUN)]
        rows.append({"id": "fresh", "name": "Sam", "date": TODAY, "amount": "1.00",
                     "status": "FAILED", "reason": f"V2: {bot.APP_NOT_RENDERING_REASON}",
                     "failed_at": _ago(5), "retries": 0})
        (ps.LEDGER_DIR / f"{TODAY.replace('/', '')}.json").write_text(json.dumps(rows))
        self.assertEqual(self._ids()[0], "fresh")

    def test_retry_bookkeeping_survives_the_failed_retry_rewriting_the_row(self):
        row = self._today()
        self.assertTrue(ps.mark_retry_attempt(row))
        ps.record_ledger(row["id"], row["name"], row["date"], row["amount"], "FAILED",
                         row["account"], f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}")
        saved = json.loads((ps.LEDGER_DIR / f"{TODAY.replace('/', '')}.json").read_text())[0]
        self.assertEqual(saved["retries"], 1)
        self.assertTrue(saved.get("last_retry_at"))
        self.assertTrue(saved.get("awaiting_session"), "a half-drawn retry keeps it waiting")


class _Loc:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    def count(self):
        return 1 if self.visible else 0

    def wait_for(self, state=None, timeout=None):
        if not self.visible:
            raise TimeoutError("not visible")


class _Page:
    """Answers `text="..." >> visible=true` locators from a {text: visible} map."""
    def __init__(self, **visible):
        self.visible = visible

    def locator(self, selector):
        text = selector.split('"')[1]
        return _Loc(self.visible.get(text, False))

    def wait_for_timeout(self, ms):
        pass


class RequirePaymentFormTest(unittest.TestCase):
    def test_form_on_screen_passes(self):
        bot._require_payment_form(
            _Page(**{"External Credit Card": True, "Payment Amount": True}), timeout_ms=1)

    def test_payment_history_row_alone_is_not_a_form(self):
        with self.assertRaisesRegex(Exception, bot.PAYMENT_FORM_NOT_READY_REASON):
            bot._require_payment_form(_Page(**{"External Credit Card": True}), timeout_ms=1)

    def test_start_billing_on_the_appointment_means_not_billed_yet(self):
        with self.assertRaisesRegex(Exception, "no payment form yet"):
            bot._require_payment_form(_Page(**{"Start Billing": True}), timeout_ms=1,
                                      appointment=True)

    def test_start_billing_elsewhere_is_not_this_session(self):
        with self.assertRaisesRegex(Exception, bot.PAYMENT_FORM_NOT_READY_REASON):
            bot._require_payment_form(_Page(**{"Start Billing": True}), timeout_ms=1)

    def test_anything_else_is_form_not_ready(self):
        with self.assertRaisesRegex(Exception, bot.PAYMENT_FORM_NOT_READY_REASON):
            bot._require_payment_form(_Page(), timeout_ms=1)


class FallbackTest(unittest.TestCase):
    """The real post_payment, with V2 and V1's steps faked."""

    def setUp(self):
        self._saved = (bot.post_payment_v2, bot.post_payment_v1, bot.reset_app_render_state)
        bot.reset_app_render_state = lambda: None
        bot.post_payment_v2 = self._v2
        self.v2_errors = [bot.SESSION_NOT_PAYABLE_REASON, bot.SESSION_NOT_PAYABLE_REASON]

    def tearDown(self):
        bot.post_payment_v2, bot.post_payment_v1, bot.reset_app_render_state = self._saved

    def _v2(self, *a, **k):
        raise Exception(self.v2_errors.pop(0))

    def _post(self):
        return bot.post_payment(None, "Pat Example", "10/06/2026", "30.00")

    def test_v1_still_runs_and_can_post(self):
        # Jacob Legrand, 10/06: the appointment page offered no form, V1 posted.
        bot.post_payment_v1 = lambda *a, **k: (True, "Posted ✓")
        ok, status, *_ = self._post()
        self.assertEqual((ok, status), (True, "V1"))

    def test_v1_stopping_before_its_form_makes_the_chain_retryable(self):
        def v1(*a, **k):
            raise Exception(f"{bot.V1_BEFORE_FORM_TAG} Client 'Pat Example' not found in "
                            f"autocomplete (7 results)")
        bot.post_payment_v1 = v1
        ok, status, error, *_ = self._post()
        self.assertEqual((ok, status), (False, "FAILED"))
        self.assertTrue(ps._reason_is_only_retryable(error), error)

    def test_v1_failing_after_its_form_opened_is_not_retryable(self):
        def v1(*a, **k):
            raise Exception("Locator.click: Timeout 30000ms exceeded")
        bot.post_payment_v1 = v1
        ok, status, error, *_ = self._post()
        self.assertFalse(ps._reason_is_only_retryable(error))

    def test_v1_flag_before_its_form_still_flags(self):
        def v1(*a, **k):
            raise Exception(f"{bot.V1_BEFORE_FORM_TAG} FLAG: multiple matches for Pat Example")
        bot.post_payment_v1 = v1
        ok, status, *_ = self._post()
        self.assertEqual(status, "FLAGGED")

    def test_generic_v2_failure_keeps_the_chain_unretryable(self):
        self.v2_errors = ["Locator.click: Timeout 30000ms exceeded", bot.SESSION_NOT_PAYABLE_REASON]
        def v1(*a, **k):
            raise Exception(f"{bot.V1_BEFORE_FORM_TAG} Page.click: Timeout")
        bot.post_payment_v1 = v1
        _ok, _status, error, *_ = self._post()
        self.assertFalse(ps._reason_is_only_retryable(error))


class V1TagTest(unittest.TestCase):
    def test_steps_before_the_form_are_tagged(self):
        saved = bot.navigate_to_billing
        def boom(page):
            raise RuntimeError("Page.click: Timeout 30000ms exceeded waiting for text=Billing")
        bot.navigate_to_billing = boom
        try:
            with self.assertRaisesRegex(Exception, "^" + bot.V1_BEFORE_FORM_TAG):
                bot.post_payment_v1(None, "Pat Example", "30.00")
        finally:
            bot.navigate_to_billing = saved


class ReportWordingTest(unittest.TestCase):
    def test_reconcile_and_classifier_name_the_real_cause(self):
        import reconcile as rec
        why, todo = rec.explain("FAILED", UNBILLED, "Pat Example")
        self.assertIn("in progress", why)
        self.assertNotIn("popup", why.lower())
        self.assertEqual(bot._classify_issue(UNBILLED)[0], "Appointment not payable yet")


if __name__ == "__main__":
    unittest.main()
