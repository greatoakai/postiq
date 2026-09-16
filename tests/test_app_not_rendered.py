"""The blank-TA path: reload, then fail fast — and never tell staff a payment
that reached TA's form is safe to re-post.

Covers the 2026-09-15/16 incidents: TA intermittently serves a blank app shell
(a 502 on one of its JS chunks), which cost 17 payments before the reload existed
and one more (Rebecca Alvarado, $110.30) when a single reload missed by seconds.
"""
import pathlib
import sys
import types
import unittest


def _stub_imports():
    """Same trick as test_submit_payment_guard: no browser, no packages needed."""
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
import reconcile as rec       # noqa: E402


class FakePage:
    """Renders only once `renders_after` reloads have happened (None = never)."""

    def __init__(self, renders_after):
        self.renders_after = renders_after
        self.gotos = 0
        self.waits = 0

    def wait_for_selector(self, selector, timeout=None):
        if self.renders_after is None or self.gotos < self.renders_after:
            raise TimeoutError("no sidebar")

    def goto(self, url, **kwargs):
        self.gotos += 1

    def wait_for_timeout(self, ms):
        self.waits += 1


class EnsureSidebarTest(unittest.TestCase):
    def setUp(self):
        self._settle, self._shot = bot.settle, bot.screenshot
        bot.settle = lambda page: None
        self.shots = []
        bot.screenshot = lambda page, tag: self.shots.append(tag)
        bot.reset_app_render_state()

    def tearDown(self):
        bot.settle, bot.screenshot = self._settle, self._shot
        bot.reset_app_render_state()

    def test_healthy_page_costs_nothing(self):
        page = FakePage(0)
        bot._ensure_sidebar(page)
        self.assertEqual(page.gotos, 0)

    def test_recovers_on_reload(self):
        page = FakePage(1)
        bot._ensure_sidebar(page)          # must not raise
        self.assertEqual(page.gotos, 1)
        self.assertFalse(bot._app_unrendered)

    def test_uses_its_whole_reload_budget(self):
        page = FakePage(bot.SIDEBAR_RELOAD_ATTEMPTS)
        bot._ensure_sidebar(page)          # the case that failed on 2026-09-15
        self.assertEqual(page.gotos, bot.SIDEBAR_RELOAD_ATTEMPTS)

    def test_raises_once_the_budget_is_spent(self):
        page = FakePage(None)
        with self.assertRaises(bot.AppNotRenderedError):
            bot._ensure_sidebar(page)
        self.assertEqual(page.gotos, bot.SIDEBAR_RELOAD_ATTEMPTS)
        self.assertTrue(bot._app_unrendered)
        self.assertIn("sidebar_missing_app_not_rendered", self.shots)

    def test_later_clicks_in_the_same_payment_fail_fast(self):
        page = FakePage(None)
        with self.assertRaises(bot.AppNotRenderedError):
            bot._ensure_sidebar(page)
        spent = page.gotos
        with self.assertRaises(bot.AppNotRenderedError):
            bot._ensure_sidebar(page)
        self.assertEqual(page.gotos, spent, "should not reload again after giving up")

    def test_next_payment_gets_a_fresh_budget(self):
        with self.assertRaises(bot.AppNotRenderedError):
            bot._ensure_sidebar(FakePage(None))
        bot.reset_app_render_state()       # what post_payment() does per payment
        page = FakePage(1)
        bot._ensure_sidebar(page)
        self.assertEqual(page.gotos, 1)


class ReasonStringSafetyTest(unittest.TestCase):
    """The reason must not collide with the markers that reroute a payment."""

    def test_carries_no_routing_markers(self):
        reason = bot.APP_NOT_RENDERING_REASON
        self.assertNotIn("FLAG", reason)
        self.assertNotIn("AT_PAYMENT_FORM", reason)
        self.assertFalse(bot._is_no_appointment_error(Exception(reason)))


class StaffReportWordingTest(unittest.TestCase):
    """reconcile.explain() is the only safeguard against a double charge:
    poll_square retires ids carrying MAY_HAVE_POSTED_MARKER, so they are never
    retried. A payment that reached TA's form must never read as safe to post."""

    def _why(self, status, reason):
        return rec.explain(status, reason, "Test Client")[0].lower()

    def test_may_have_posted_outranks_a_no_appointment_leg(self):
        reason = bot._may_have_posted_flag(
            "Test Client",
            "V2: No appointment found on 09/16/2026 for Test Client; "
            "V2-retry: AT_PAYMENT_FORM: page went dark")
        self.assertIn("may or may not have saved", self._why("FLAGGED", reason))

    def test_may_have_posted_outranks_a_name_miss(self):
        reason = bot._may_have_posted_flag(
            "Test Client",
            "V2: Client 'X' not found in search results; V1: AT_PAYMENT_FORM: dark")
        self.assertIn("may or may not have saved", self._why("FLAGGED", reason))

    def test_blank_app_is_named_and_makes_no_promise(self):
        reason = f"V2: {bot.APP_NOT_RENDERING_REASON}"
        why, todo = rec.explain("FAILED", reason, "Test Client")
        self.assertIn("blank page", why.lower())
        self.assertIn("check the ledger", todo.lower())

    def test_untouched_branches_still_route(self):
        self.assertIn("had no appointment",
                      self._why("FAILED", "V2: No appointment found on 09/16/2026 for X"))
        self.assertIn("no therapyappointment client matched",
                      self._why("FAILED", "V2: Client 'X' not found in search results"))
        self.assertIn("more than one appointment",
                      self._why("FLAGGED", "FLAG: Multiple appointments on 09/16/2026 for X"))


class PropagationTest(unittest.TestCase):
    """A blank shell must escape the retry handlers instead of being retried."""

    def setUp(self):
        self._nav = bot.navigate_to_clients
        self._by_acct = bot.search_client_by_account
        self._enabled = bot.ACCOUNT_MATCH_ENABLED
        bot.reset_app_render_state()

    def tearDown(self):
        bot.navigate_to_clients = self._nav
        bot.search_client_by_account = self._by_acct
        bot.ACCOUNT_MATCH_ENABLED = self._enabled

    def test_account_search_reraises_without_retrying(self):
        def blank(page):
            raise bot.AppNotRenderedError(bot.APP_NOT_RENDERING_REASON)
        bot.navigate_to_clients = blank
        page = FakePage(None)
        with self.assertRaises(bot.AppNotRenderedError):
            bot.search_client_by_account(page, "C007710917")
        self.assertEqual(page.waits, 0, "should not sleep between retries")

    def test_search_client_does_not_fall_back_to_a_name_search(self):
        def blank(page, account):
            raise bot.AppNotRenderedError(bot.APP_NOT_RENDERING_REASON)
        bot.search_client_by_account = blank
        bot.ACCOUNT_MATCH_ENABLED = True
        with self.assertRaises(bot.AppNotRenderedError):
            bot.search_client(FakePage(None), "Test Client", account="C007710917")


if __name__ == "__main__":
    unittest.main()
