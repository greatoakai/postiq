"""A client who pays before their session is billed must post later, on its own.

On 10/06/2026 three early-morning payments (Tyler Dunning, Shianne Peters, Al Ford)
failed because the 8:00/8:30 sessions still showed "Start Billing" in TA — no charge,
so Accept Payment opened no form — and the bot crashed on a missing amount field
('NoneType' object has no attribute 'click'). None of it was retryable, so all three
went to staff. Now the bot says what happened, skips V1 (which would put the money
on an older session), and retries hourly until the session has been billed.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from test_retry_failed_payments import _LedgerCase, _ago, bot, ps

UNBILLED = (f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}; "
            f"V2-retry: {bot.NOT_BILLED_YET_REASON}")


class UnbilledRetryGateTest(_LedgerCase):
    def test_new_reasons_are_retryable_and_safe_wording(self):
        self.assertTrue(ps._reason_is_only_retryable(UNBILLED))
        for r in (bot.NOT_BILLED_YET_REASON, bot.PAYMENT_FORM_NOT_READY_REASON):
            self.assertNotIn("FLAG", r)
            self.assertNotIn("AT_PAYMENT_FORM", r)
            self.assertFalse(bot._is_no_appointment_error(r),
                             "must never reroute the money to the open balance")

    def test_unbilled_is_retried_past_the_usual_window(self):
        self._row(reason=UNBILLED, failed_at=_ago(ps.RETRY_WINDOW_MIN + 120))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_form_not_ready_alone_keeps_the_usual_window(self):
        self._row(reason=f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}",
                  failed_at=_ago(ps.RETRY_WINDOW_MIN + 1))
        self.assertEqual(self._ids(), [])

    def test_unbilled_waits_an_hour_between_attempts(self):
        self._row(reason=UNBILLED, retries=1, last_retry_at=_ago(20))
        self.assertEqual(self._ids(), [])
        self._row(reason=UNBILLED, retries=1, last_retry_at=_ago(61))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_unbilled_gives_up_after_its_own_limit(self):
        self._row(reason=UNBILLED, retries=ps.RETRY_MAX_ATTEMPTS)
        self.assertEqual(self._ids(), ["sq_1"], "more attempts than an ordinary failure")
        self._row(reason=UNBILLED, retries=ps.RETRY_MAX_ATTEMPTS_UNBILLED)
        self.assertEqual(self._ids(), [])

    def test_unbilled_still_stops_at_the_morning_report(self):
        ps.last_report_at = lambda: datetime.now(timezone.utc) - timedelta(minutes=5)
        self._row(reason=UNBILLED, failed_at=_ago(60))
        self.assertEqual(self._ids(), [], "staff may be posting it by hand now")

    def test_attempt_time_survives_the_failed_retry_rewriting_the_row(self):
        row = self._row(reason=UNBILLED)
        self.assertTrue(ps.mark_retry_attempt(row))
        ps.record_ledger(row["id"], row["name"], row["date"], row["amount"], "FAILED",
                         row["account"], UNBILLED)
        saved = json.loads((ps.LEDGER_DIR / "09162026.json").read_text())[0]
        self.assertEqual(saved["retries"], 1)
        self.assertTrue(saved.get("last_retry_at"))


class _Loc:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    def wait_for(self, state=None, timeout=None):
        if not self.visible:
            raise TimeoutError("not visible")

    def is_visible(self):
        return self.visible


class _Page:
    def __init__(self, **visible):
        self.visible = visible

    def get_by_text(self, text, exact=False):
        return _Loc(self.visible.get(text, False))


class RequirePaymentFormTest(unittest.TestCase):
    def test_form_on_screen_passes(self):
        bot._require_payment_form(_Page(**{"External Credit Card": True}), timeout_ms=1)

    def test_start_billing_means_not_billed_yet(self):
        with self.assertRaisesRegex(Exception, "not billed in TA yet"):
            bot._require_payment_form(_Page(**{"Start Billing": True}), timeout_ms=1)

    def test_anything_else_is_form_not_ready(self):
        with self.assertRaisesRegex(Exception, bot.PAYMENT_FORM_NOT_READY_REASON):
            bot._require_payment_form(_Page(), timeout_ms=1)


class SkipV1WhenUnbilledTest(unittest.TestCase):
    def setUp(self):
        self._saved = (bot.post_payment_v2, bot.post_payment_v1, bot.reset_app_render_state)
        bot.reset_app_render_state = lambda: None
        self.v1_calls = []
        bot.post_payment_v1 = lambda *a, **k: self.v1_calls.append(a) or (True, "10/06/2026")

    def tearDown(self):
        bot.post_payment_v2, bot.post_payment_v1, bot.reset_app_render_state = self._saved

    def test_unbilled_session_is_not_sent_to_v1(self):
        def v2(*a, **k):
            raise Exception(bot.NOT_BILLED_YET_REASON)
        bot.post_payment_v2 = v2
        ok, status, error, *_ = bot.post_payment(None, "Pat Example", "10/06/2026", "30.00")
        self.assertFalse(ok)
        self.assertEqual(status, "FAILED")
        self.assertEqual(self.v1_calls, [])
        self.assertTrue(ps._reason_is_only_retryable(error))

    def test_other_failures_still_fall_back_to_v1(self):
        def v2(*a, **k):
            raise Exception(bot.PAYMENT_FORM_NOT_READY_REASON)
        bot.post_payment_v2 = v2
        ok, status, *_ = bot.post_payment(None, "Pat Example", "10/06/2026", "30.00")
        self.assertTrue(ok)
        self.assertEqual(status, "V1")


class ReportWordingTest(unittest.TestCase):
    def test_reconcile_and_classifier_name_the_real_cause(self):
        import reconcile as rec
        why, todo = rec.explain("FAILED", UNBILLED, "Pat Example")
        self.assertIn("billed", why)
        self.assertNotIn("popup", why.lower())
        self.assertEqual(bot._classify_issue(UNBILLED)[0], "Session not billed yet")


if __name__ == "__main__":
    unittest.main()
