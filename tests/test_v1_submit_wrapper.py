"""A plain FAILED must mean nothing was submitted.

The morning report tells staff to post a plain FAILED by hand, so a payment TA had
already saved must never come back that way. Two gaps allowed it:

- submit_payment ran an unguarded pause and screenshot AFTER confirming the save, so
  a page dying at that moment turned a saved payment into a raised exception.
- post_payment_v1 called submit_payment bare, while V2 and the balance post wrap it
  in AT_PAYMENT_FORM. A raise there escaped V1 untagged and became a plain FAILED.
"""
import pathlib
import sys
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
import bot_v2 as bot  # noqa: E402

TA = "https://portal.therapyappointment.com/index.cfm"


class _Locator:
    def __init__(self, n=0):
        self._n = n

    def count(self):
        return self._n

    @property
    def first(self):
        return self

    def click(self, *a, **k):
        pass


class SavedPage:
    """A TA page where the save is confirmed. `die_after_save` makes the page fail
    the moment anything is asked of it after Save Payment was clicked."""

    def __init__(self, die_after_save=False):
        self.url = TA
        self._die = die_after_save
        self._saved = False

    def click(self, sel):
        if "Save Payment" in sel:
            self._saved = True

    def wait_for_timeout(self, ms):
        if self._die and self._saved:
            raise RuntimeError("Target page, context or browser has been closed")

    def wait_for_load_state(self, state, timeout=None):
        pass

    def locator(self, sel):
        return _Locator(0)


class ConfirmedSaveStaysSavedTest(unittest.TestCase):
    def setUp(self):
        self._shot = bot.screenshot
        bot.screenshot = lambda page, tag: None

    def tearDown(self):
        bot.screenshot = self._shot

    def test_a_normal_confirmed_save_returns_true(self):
        self.assertTrue(bot.submit_payment(SavedPage(), "Pat Example"))

    def test_page_dying_after_the_save_still_returns_true(self):
        self.assertTrue(bot.submit_payment(SavedPage(die_after_save=True), "Pat Example"),
                        "a confirmed save must not become a failure")

    def test_screenshot_failing_after_the_save_still_returns_true(self):
        def boom(page, tag):
            if tag.endswith("_04_saved"):
                raise RuntimeError("screenshot failed")
        bot.screenshot = boom
        self.assertTrue(bot.submit_payment(SavedPage(), "Pat Example"))


class V1WrapperTest(unittest.TestCase):
    """Drives the real post_payment_v1 with everything before the save stubbed."""

    NAMES = ("navigate_to_billing", "settle", "select_client_v1", "screenshot",
             "scrape_allocation_date", "fill_payment_form", "submit_payment",
             "scrape_confirmation_date")

    def setUp(self):
        self._orig = {n: getattr(bot, n) for n in self.NAMES}
        bot.navigate_to_billing = lambda page: None
        bot.settle = lambda page: None
        bot.select_client_v1 = lambda page, name: None
        bot.screenshot = lambda page, tag: None
        bot.scrape_allocation_date = lambda page: ("09/16/2026", True)
        bot.fill_payment_form = lambda page, amount: None
        bot.scrape_confirmation_date = lambda page: "09/16/2026"

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(bot, n, v)

    class _Page:
        def locator(self, sel):
            return _Locator(1)

        def wait_for_timeout(self, ms):
            pass

    def test_a_raise_inside_submit_payment_is_tagged(self):
        def raises(page, name, dry_run=False):
            raise RuntimeError("Target page, context or browser has been closed")
        bot.submit_payment = raises
        with self.assertRaises(Exception) as ctx:
            bot.post_payment_v1(self._Page(), "Pat Example", "75.00")
        self.assertIn("AT_PAYMENT_FORM", str(ctx.exception))

    def test_a_returned_failure_is_still_tagged(self):
        bot.submit_payment = lambda page, name, dry_run=False: False
        with self.assertRaises(Exception) as ctx:
            bot.post_payment_v1(self._Page(), "Pat Example", "75.00")
        self.assertIn("AT_PAYMENT_FORM", str(ctx.exception))

    def test_a_successful_save_still_posts(self):
        bot.submit_payment = lambda page, name, dry_run=False: True
        ok, status = bot.post_payment_v1(self._Page(), "Pat Example", "75.00")
        self.assertTrue(ok)

    def test_a_failure_before_the_form_is_not_tagged(self):
        # Nothing was submitted, so this must stay an ordinary failure.
        def no_client(page, name):
            raise RuntimeError("Client 'Pat Example' not found in autocomplete")
        bot.select_client_v1 = no_client
        bot.submit_payment = lambda page, name, dry_run=False: True
        with self.assertRaises(Exception) as ctx:
            bot.post_payment_v1(self._Page(), "Pat Example", "75.00")
        self.assertNotIn("AT_PAYMENT_FORM", str(ctx.exception))


class EndToEndRoutingTest(unittest.TestCase):
    """The real post_payment -> the real post_payment_v1 -> a submit_payment that dies.

    Only V2 (stubbed to fail plainly, so the run falls through to V1) and the steps
    before V1's payment form are replaced. Before the wrapper, the raise escaped V1
    untagged and post_payment returned a plain FAILED for a payment TA may already
    have saved — which the morning report tells staff to post by hand.
    """

    NAMES = ("post_payment_v2", "navigate_to_billing", "settle", "select_client_v1",
             "screenshot", "scrape_allocation_date", "fill_payment_form",
             "submit_payment", "scrape_confirmation_date")

    def setUp(self):
        self._orig = {n: getattr(bot, n) for n in self.NAMES}

        def v2_plain_failure(*a, **k):
            raise Exception("Client 'Pat Example' not found in search results")

        def went_dark(page, name, dry_run=False):
            raise RuntimeError("Target page, context or browser has been closed")

        bot.post_payment_v2 = v2_plain_failure
        bot.navigate_to_billing = lambda page: None
        bot.settle = lambda page: None
        bot.select_client_v1 = lambda page, name: None
        bot.screenshot = lambda page, tag: None
        bot.scrape_allocation_date = lambda page: ("09/16/2026", True)
        bot.fill_payment_form = lambda page, amount: None
        bot.submit_payment = went_dark
        bot.scrape_confirmation_date = lambda page: None

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(bot, n, v)

    class _Page:
        def locator(self, sel):
            return _Locator(1)

        def wait_for_timeout(self, ms):
            pass

    def test_v1_raise_after_submit_is_flagged_not_failed(self):
        success, method, error, *_ = bot.post_payment(
            self._Page(), "Pat Example", "09/16/2026", "75.00")
        self.assertFalse(success)
        self.assertEqual(method, "FLAGGED", "must never come back as a plain FAILED")
        self.assertIn(bot.MAY_HAVE_POSTED_MARKER, error)


if __name__ == "__main__":
    unittest.main()
