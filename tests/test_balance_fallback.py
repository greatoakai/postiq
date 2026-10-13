"""Payments the bot used to give up on now go to the client's earlier balances.

10/09/2026, all three posted by staff by hand:
  Sam Clark $35          — 10/09 appointment rescheduled; staff put it on his 09/26 session.
  Aunicka Tumbleson $20  — 10/09 late cancel, fee billed after the bot's last try at 9 PM.
  Brittni Baker $5       — 10/09 late cancel with a $0 fee: no charge that day at all.
"""
import unittest
from datetime import datetime, timezone

from test_retry_failed_payments import bot, ps


class _Row:
    def __init__(self, status):
        self.status = status

    def text_content(self):
        return f"10/09/2026 10:30 AM {self.status}"


class _Link:
    def __init__(self, status):
        self.row = _Row(status)
        self.clicked = False

    def locator(self, sel):
        return self.row

    def text_content(self):
        return "10/09/2026"

    def click(self):
        self.clicked = True


class _Page:
    def __init__(self, links):
        self.links = links

    def locator(self, sel):
        page = self

        class _L:
            def all(self_inner):
                return page.links
        return _L()


class RescheduledDayTest(unittest.TestCase):
    def test_all_rescheduled_goes_to_balance_not_a_flag(self):
        page = _Page([_Link("Rescheduled to 10/16/2026")])
        with self.assertRaises(Exception) as cm:
            bot.click_appointment_by_date(page, "10/09/2026", "Sam Clark")
        msg = str(cm.exception)
        self.assertNotIn("FLAG", msg)
        self.assertTrue(bot._is_no_appointment_error(msg), "V2 retry must take the balance path")

    def test_plain_cancel_too(self):
        page = _Page([_Link("Cancelled"), _Link("Rescheduled to 10/16/2026")])
        with self.assertRaises(Exception) as cm:
            bot.click_appointment_by_date(page, "10/09/2026", "Sam Clark")
        self.assertTrue(bot._is_no_appointment_error(str(cm.exception)))

    def test_late_cancel_is_still_a_target(self):
        link = _Link("Late Cancel")
        saved = bot.settle
        bot.settle = lambda page: None
        page = _Page([link])
        page.wait_for_timeout = lambda ms: None
        try:
            bot.click_appointment_by_date(page, "10/09/2026", "Aunicka Tumbleson")
        finally:
            bot.settle = saved
        self.assertTrue(link.clicked)


class SessionFallbackTest(unittest.TestCase):
    """The real post_payment_v2 with TA's pages faked."""
    NAMES = ("search_client", "navigate_to_appointments", "ensure_date_filters",
             "click_appointment_by_date", "click_accept_payment", "fill_payment_form",
             "post_payment_to_balance", "screenshot", "submit_payment")

    def setUp(self):
        self._saved = {n: getattr(bot, n) for n in self.NAMES}
        bot.search_client = lambda page, name, account=None: (True, None)
        bot.navigate_to_appointments = lambda page: None
        bot.ensure_date_filters = lambda page: None
        bot.click_appointment_by_date = lambda page, date, name: None
        bot.screenshot = lambda page, name: None
        self.balance = []
        bot.post_payment_to_balance = lambda page, name, amount, dry_run=False, profile_url=None, date=None: (
            self.balance.append(amount) or (True, "posted to open balance", "0.00"))

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(bot, n, v)

    class _P:
        url = "https://portal/client/1"

    def _unpayable(self, *a, **k):
        raise Exception(bot.SESSION_NOT_PAYABLE_REASON)

    def test_last_try_posts_to_the_open_balance(self):
        bot.click_accept_payment = self._unpayable
        ok, note, due, method = bot.post_payment_v2(self._P(), "Aunicka Tumbleson", "10/09/2026",
                                                    "20.00", session_fallback=True)
        self.assertEqual((ok, method, self.balance), (True, "V2-balance", ["20.00"]))

    def test_also_when_the_form_check_finds_it(self):
        bot.click_accept_payment = lambda page, name, same_day=False: (None, None)
        bot.fill_payment_form = lambda page, amount, appointment=False: self._unpayable()
        ok, note, due, method = bot.post_payment_v2(self._P(), "Brittni Baker", "10/09/2026",
                                                    "5.00", session_fallback=True)
        self.assertEqual((ok, method, self.balance), (True, "V2-balance", ["5.00"]))

    def test_earlier_tries_still_wait(self):
        bot.click_accept_payment = self._unpayable
        with self.assertRaisesRegex(Exception, "no payment form yet"):
            bot.post_payment_v2(self._P(), "Aunicka Tumbleson", "10/09/2026", "20.00")
        self.assertEqual(self.balance, [])


class PostPaymentPassThroughTest(unittest.TestCase):
    def setUp(self):
        self._saved = (bot.post_payment_v2, bot.reset_app_render_state)
        bot.reset_app_render_state = lambda: None

    def tearDown(self):
        bot.post_payment_v2, bot.reset_app_render_state = self._saved

    def test_session_fallback_reaches_both_v2_legs(self):
        seen = []
        def v2(*a, **k):
            seen.append(k.get("session_fallback"))
            raise Exception(bot.PAYMENT_FORM_NOT_READY_REASON)
        bot.post_payment_v2 = v2
        bot.post_payment(None, "Pat", "10/09/2026", "5.00", hold_for_session=True,
                         session_fallback=True)
        self.assertEqual(seen, [True, True])


class LastSessionTryTest(unittest.TestCase):
    def _at(self, hour):
        local = datetime.now().astimezone().replace(hour=hour, minute=0)
        return local.astimezone(timezone.utc)

    def test_final_attempt_or_late_evening(self):
        self.assertTrue(ps._last_session_try(ps.RETRY_MAX_ATTEMPTS_SESSION, self._at(14)))
        self.assertTrue(ps._last_session_try(3, self._at(ps.SESSION_FALLBACK_HOUR)))
        self.assertFalse(ps._last_session_try(3, self._at(14)))


if __name__ == "__main__":
    unittest.main()
