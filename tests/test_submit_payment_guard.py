"""submit_payment must not record a save it cannot place inside TherapyAppointment.

postiq#12. After "Save Payment", submit_payment() accepted either signal as proof TA saved the
payment: the page reaching networkidle, or the Save Payment control being gone. poll_square then
records the Square payment id as posted. But both signals are equally true on a login redirect,
an error page or a blank page -- and a payment recorded as posted that TA never saved is never
retried. That is the one error this system cannot detect afterwards.

Drives the REAL submit_payment with a fake page. playwright and dotenv are stubbed at import, so
neither a browser nor either package is needed to run this: python3 -m unittest discover tests
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
import bot_v2  # noqa: E402

TA = "https://portal.therapyappointment.com/index.cfm"


class _Locator:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class FakePage:
    """`networkidle`: whether the post-save wait settles. `save_button_after`: how many
    "Save Payment" controls remain on the page after the click (0 = the form is gone)."""

    def __init__(self, url, networkidle=True, save_button_after=0):
        self.url = url
        self._idle = networkidle
        self._after = save_button_after

    def click(self, sel):
        pass

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=None):
        if not self._idle:
            raise bot_v2.PlaywrightTimeout("networkidle")

    def locator(self, sel):
        return _Locator(self._after)


class SubmitPaymentGuard(unittest.TestCase):
    NAME = "Pat Example"  # de-identified

    def setUp(self):
        self._shot = bot_v2.screenshot
        bot_v2.screenshot = lambda *a, **k: None

    def tearDown(self):
        bot_v2.screenshot = self._shot

    # Not a save: the page left TA, or landed on its sign-in route.
    def test_quiet_login_redirect_is_not_a_save(self):
        self.assertFalse(bot_v2.submit_payment(FakePage(f"{TA}/public:auth?fw1pk=1"), self.NAME))

    def test_blank_page_is_not_a_save(self):
        self.assertFalse(bot_v2.submit_payment(FakePage("about:blank"), self.NAME))

    def test_foreign_host_is_not_a_save(self):
        self.assertFalse(bot_v2.submit_payment(FakePage("https://example.com/error"), self.NAME))

    def test_form_gone_on_the_login_page_is_not_a_save(self):
        page = FakePage(f"{TA}/public:auth", networkidle=False, save_button_after=0)
        self.assertFalse(bot_v2.submit_payment(page, self.NAME))

    # Still a save: both signals, on a TA page -- the guard must not cost a real one.
    def test_quiet_page_inside_ta_is_still_a_save(self):
        self.assertTrue(bot_v2.submit_payment(FakePage(f"{TA}/billing:payment/receipt"), self.NAME))

    def test_form_gone_inside_ta_is_still_a_save(self):
        page = FakePage(f"{TA}/client:profile", networkidle=False, save_button_after=0)
        self.assertTrue(bot_v2.submit_payment(page, self.NAME))

    # TA serves parts of its app from sub-hosts; a save landing on one is still a save.
    def test_ta_subhost_is_still_a_save(self):
        page = FakePage("https://api.portal.therapyappointment.com/n/dashboard/billing")
        self.assertTrue(bot_v2.submit_payment(page, self.NAME))

    # A look-alike domain is not TA.
    def test_lookalike_domain_is_not_a_save(self):
        page = FakePage("https://portal.therapyappointment.com.evil.example/x")
        self.assertFalse(bot_v2.submit_payment(page, self.NAME))

    # And the existing refusal is unchanged: nothing settled, form still up.
    def test_unconfirmed_save_inside_ta_is_still_refused(self):
        page = FakePage(f"{TA}/client:profile", networkidle=False, save_button_after=1)
        self.assertFalse(bot_v2.submit_payment(page, self.NAME))


if __name__ == "__main__":
    unittest.main()
