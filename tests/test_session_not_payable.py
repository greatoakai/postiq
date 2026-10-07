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

def _today_str():
    return datetime.now(timezone.utc).astimezone().strftime("%m/%d/%Y")

UNBILLED = (f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}; "
            f"V2-retry: {bot.SESSION_NOT_PAYABLE_REASON}")


class SessionWaitRetryGateTest(_LedgerCase):
    def _today(self, **overrides):
        """An unbilled row paid today (the only day it's retried), in today's ledger file."""
        row = {"id": "sq_1", "name": "Pat Example", "date": _today_str(), "amount": "30.00",
               "status": "FAILED", "account": "C000000001", "reason": UNBILLED,
               "method": "", "note": "", "failed_at": _ago(10), "retries": 0}
        row.update(overrides)
        (ps.LEDGER_DIR / f"{_today_str().replace('/', '')}.json").write_text(json.dumps([row]))
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

    def test_session_wait_is_ordinary_once_its_day_is_over(self):
        self._row(reason=UNBILLED, failed_at=_ago(30))   # dated 09/16/2026: usual window
        self.assertEqual(self._ids(), ["sq_1"])
        self._row(reason=UNBILLED, failed_at=_ago(ps.RETRY_WINDOW_MIN + 1))
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
        self._today(retries=1, last_retry_at=_ago(51))
        self.assertEqual(self._ids(), ["sq_1"])

    def test_session_wait_gives_up_after_its_own_limit(self):
        self._today(retries=ps.RETRY_MAX_ATTEMPTS, failed_at=_ago(120))
        self.assertEqual(self._ids(), ["sq_1"], "more attempts than an ordinary failure")
        self._today(retries=ps.RETRY_MAX_ATTEMPTS_SESSION, failed_at=_ago(120))
        self.assertEqual(self._ids(), [])

    def test_ordinary_failures_come_before_billing_waits(self):
        rows = [{"id": f"wait_{i}", "name": "Pat", "date": _today_str(), "amount": "1.00",
                 "status": "FAILED", "reason": UNBILLED, "failed_at": _ago(300 - i),
                 "retries": 0} for i in range(ps.RETRY_MAX_PER_RUN)]
        rows.append({"id": "fresh", "name": "Sam", "date": _today_str(), "amount": "1.00",
                     "status": "FAILED", "reason": f"V2: {bot.APP_NOT_RENDERING_REASON}",
                     "failed_at": _ago(5), "retries": 0})
        (ps.LEDGER_DIR / f"{_today_str().replace('/', '')}.json").write_text(json.dumps(rows))
        self.assertEqual(self._ids()[0], "fresh")

    def test_retry_bookkeeping_survives_the_failed_retry_rewriting_the_row(self):
        row = self._today()
        self.assertTrue(ps.mark_retry_attempt(row))
        ps.record_ledger(row["id"], row["name"], row["date"], row["amount"], "FAILED",
                         row["account"], f"V2: {bot.PAYMENT_FORM_NOT_READY_REASON}")
        saved = json.loads((ps.LEDGER_DIR / f"{_today_str().replace('/', '')}.json").read_text())[0]
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
        sel = selector.split(" >> ")[0][len("text="):].strip('"')
        return _Loc(any(v and sel.lower() in k.lower() for k, v in self.visible.items()))

    def wait_for_timeout(self, ms):
        pass


class RequirePaymentFormTest(unittest.TestCase):
    def test_form_on_screen_passes(self):
        bot._require_payment_form(
            _Page(**{"External Credit Card": True, "Payment Amount *": True}), timeout_ms=1)

    def test_payment_history_row_alone_is_not_a_form(self):
        with self.assertRaisesRegex(Exception, bot.PAYMENT_FORM_NOT_READY_REASON):
            bot._require_payment_form(_Page(**{"External Credit Card": True}), timeout_ms=1)

    def test_start_billing_still_showing_while_the_form_loads_is_not_a_verdict(self):
        page = _Page(**{"Start Billing": True})
        def form_arrives(ms):
            page.visible = {"Payment Amount": True, "External Credit Card": True}
        page.wait_for_timeout = form_arrives
        bot._require_payment_form(page, timeout_ms=1000, appointment=True)

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

    def test_unpayable_session_waits_for_a_retry_instead_of_v1(self):
        v1 = []
        bot.post_payment_v1 = lambda *a, **k: v1.append(1) or (True, "Posted ✓")
        ok, status, error, *_ = self._post()
        self.assertEqual((ok, status, v1), (False, "FAILED", []))
        self.assertTrue(ps._reason_is_only_retryable(error))

    def test_retry_leg_never_reroutes_to_the_open_balance(self):
        seen = []
        def v2(*a, **k):
            seen.append(k.get("allow_balance"))
            raise Exception(bot.SESSION_NOT_PAYABLE_REASON)
        bot.post_payment_v2 = v2
        bot.post_payment_v1 = lambda *a, **k: (True, "Posted ✓")
        self._post()
        self.assertEqual(seen, [False, False])

    def test_v1_stopping_before_its_form_makes_the_chain_retryable(self):
        self.v2_errors = [bot.PAYMENT_FORM_NOT_READY_REASON, bot.PAYMENT_FORM_NOT_READY_REASON]
        def v1(*a, **k):
            raise Exception(f"{bot.V1_BEFORE_FORM_TAG} Client 'Pat Example' not found in "
                            f"autocomplete (7 results)")
        bot.post_payment_v1 = v1
        ok, status, error, *_ = self._post()
        self.assertEqual((ok, status), (False, "FAILED"))
        self.assertTrue(ps._reason_is_only_retryable(error), error)
        self.assertIn("not found in autocomplete", error, "the detail is kept for people")

    def test_v1_failing_after_its_form_opened_is_not_retryable(self):
        self.v2_errors = [bot.PAYMENT_FORM_NOT_READY_REASON, bot.PAYMENT_FORM_NOT_READY_REASON]
        def v1(*a, **k):
            raise Exception("Locator.click: Timeout 30000ms exceeded")
        bot.post_payment_v1 = v1
        ok, status, error, *_ = self._post()
        self.assertFalse(ps._reason_is_only_retryable(error))

    def test_v1_flag_before_its_form_still_flags_untagged(self):
        saved = bot.navigate_to_billing
        def flag(page):
            raise Exception("FLAG: Multiple matches for Pat Example")
        bot.navigate_to_billing = flag
        try:
            with self.assertRaises(Exception) as cm:
                saved_v1 = self._saved[1]
                saved_v1(None, "Pat Example", "30.00")
        finally:
            bot.navigate_to_billing = saved
        self.assertTrue(str(cm.exception).startswith("FLAG:"))

    def test_one_unpayable_leg_is_enough_to_keep_v1_away(self):
        self.v2_errors = ["No appointment found on 10/06/2026 for Pat Example",
                          bot.SESSION_NOT_PAYABLE_REASON]
        v1 = []
        bot.post_payment_v1 = lambda *a, **k: v1.append(1) or (True, "Posted ✓")
        ok, status, error, *_ = self._post()
        self.assertEqual((ok, status, v1), (False, "FAILED", []))
        self.assertTrue(ps._reason_is_only_retryable(error), "the slow first leg mustn't block it")


    def test_known_session_wait_never_goes_to_v1_or_the_balance(self):
        self.v2_errors = [bot.PAYMENT_FORM_NOT_READY_REASON, bot.PAYMENT_FORM_NOT_READY_REASON]
        seen, v1 = [], []
        def v2(*a, **k):
            seen.append(k.get("allow_balance"))
            raise Exception(self.v2_errors.pop(0))
        bot.post_payment_v2 = v2
        bot.post_payment_v1 = lambda *a, **k: v1.append(1) or (True, "Posted ✓")
        ok, status, error, *_ = bot.post_payment(None, "Pat Example", "10/06/2026", "30.00",
                                                 hold_for_session=True)
        self.assertEqual((ok, status, seen, v1), (False, "FAILED", [False, False], []))
        self.assertTrue(ps._reason_is_only_retryable(error))


class IsTodayTest(unittest.TestCase):
    def test_formats(self):
        today = datetime.now()
        self.assertTrue(bot._is_today(today.strftime("%m/%d/%Y")))
        self.assertTrue(bot._is_today(today.strftime("%Y-%m-%d")))
        self.assertFalse(bot._is_today((today - timedelta(days=3)).strftime("%m/%d/%Y")))
        self.assertFalse(bot._is_today("garbage"))


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
        why2, _ = rec.explain("FAILED", "V2: No appointment found on 10/06/2026 for Pat; "
                              f"V2-retry: {bot.SESSION_NOT_PAYABLE_REASON}", "Pat")
        self.assertIn("in progress", why2)
        self.assertIn("in progress", why)
        self.assertNotIn("popup", why.lower())
        self.assertEqual(bot._classify_issue(UNBILLED)[0], "Appointment not payable yet")


if __name__ == "__main__":
    unittest.main()
