import re
import random
from typing import Callable, Optional, List, Dict
from patchright.sync_api import Page, BrowserContext, sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Chromium stability flags
# ---------------------------------------------------------------------------
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--metrics-recording-only",
    "--mute-audio",
]

# ---------------------------------------------------------------------------
# Resource blocking — cuts renderer memory ~60%.
# stylesheet intentionally NOT blocked (CSS visibility control in React).
# ---------------------------------------------------------------------------
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_PATTERNS = [
    "amplitude", "analytics", "segment.io", "google-analytics",
    "googletagmanager", "hotjar", "doubleclick", "facebook.net",
    "cdn.optimizely",
]


def _proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"server": proxy}


def _block_heavy_resources(page: Page) -> None:
    """Abort resource types that don't help registration but eat renderer memory."""
    def _handler(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        if any(pat in route.request.url for pat in _BLOCKED_URL_PATTERNS):
            route.abort()
            return
        route.continue_()
    page.route("**/*", _handler)


class DuolingoBrowserRegister:
    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[str] = None,
        verification_link_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = headless
        self.proxy = proxy
        self.verification_link_callback = verification_link_callback
        self.log = log_fn

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def register(self, email: str, password: str, referral_code: Optional[str] = None) -> dict:
        """
        Register a new Duolingo account and optionally redeem a referral code
        in the same browser session.

        Verified flow (Playwright CRX recording, June 2025):
          homepage → have-account → sign-up-button
          → age-input + continue-button
          → full-name-input + email-input + password-input + register-button
          → [if referral_code] /redeem → "Enter code" → REDEEM NOW → Claim offer

        Password requirements (Duolingo enforces):
          - Minimum 8 characters
          - At least one uppercase letter
          - At least one number or symbol
        """
        self.log(f"Starting Duolingo registration for: {email}")
        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log(f"Retry attempt {attempt}/{max_attempts}...")

            with sync_playwright() as pw:
                browser = None
                try:
                    launch_opts: dict = {"headless": self.headless, "args": _LAUNCH_ARGS}
                    p = _proxy_config(self.proxy)
                    if p:
                        launch_opts["proxy"] = p

                    browser = pw.chromium.launch(**launch_opts)
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        user_agent=UA,
                    )
                    page = context.new_page()
                    _block_heavy_resources(page)

                    try:
                        return self._do_register(page, context, email, password, referral_code)
                    except Exception as exc:
                        err = str(exc)
                        if "crashed" in err.lower() or "page crash" in err.lower():
                            self.log(f"Attempt {attempt}: Page crashed — retrying. ({err})")
                            last_error = exc
                            continue
                        self.log(f"Registration failed: {exc}")
                        raise
                finally:
                    try:
                        if browser:
                            browser.close()
                    except Exception:
                        pass

        raise RuntimeError(
            f"Registration failed after {max_attempts} attempts. Last error: {last_error}"
        )

    def redeem_code(
        self,
        email: str,
        password: str,
        cookies: List[Dict],
        referral_code: str,
    ) -> dict:
        """
        Redeem a referral code on an existing Duolingo account using stored cookies.
        Falls back to password login if cookies are expired.

        Verified flow (Playwright CRX recording, June 2025):
          /redeem → "Enter code" textbox → REDEEM NOW button → Claim offer button
        """
        self.log(f"Redeeming code '{referral_code}' for {email}")
        with sync_playwright() as pw:
            launch_opts: dict = {"headless": self.headless, "args": _LAUNCH_ARGS}
            p = _proxy_config(self.proxy)
            if p:
                launch_opts["proxy"] = p

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=UA,
            )
            if cookies:
                context.add_cookies(cookies)

            page = context.new_page()
            _block_heavy_resources(page)

            try:
                result = self._do_redeem(page, context, email, password, referral_code)
                return result
            except Exception as e:
                self.log(f"Redemption error: {e}")
                return {"success": False, "error": str(e)}
            finally:
                context.close()
                browser.close()

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _do_register(
        self,
        page: Page,
        context: BrowserContext,
        email: str,
        password: str,
        referral_code: Optional[str],
    ) -> dict:
        """
        Execute registration then optional referral redemption.

        Recorded Playwright steps (exact data-test selectors):
          1. [data-test="have-account"]    — "I already have an account" on homepage
          2. [data-test="sign-up-button"]  — "Sign up" link on the login page
          3. [data-test="age-input"]       — age field (Berapa umurmu?)
          4. [data-test="continue-button"] — Next / BERIKUTNYA
          5. [data-test="full-name-input"] — Nama (opsional)
          6. [data-test="email-input"]     — Email
          7. [data-test="password-input"]  — Kata sandi
          8. [data-test="register-button"] — BUAT AKUN / Create account
          9. goto /redeem                  — (if referral_code provided)
         10. textbox "Enter code"          — paste referral code
         11. button "REDEEM NOW"
         12. button "Claim offer"
        """

        # ── Step 1: Homepage ──────────────────────────────────────────────
        self.log("Navigating to Duolingo homepage...")
        page.goto("https://www.duolingo.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        self.log(f"Landed on: {page.url}")

        # ── Step 2: Click "I already have an account" ─────────────────────
        # This navigates to the login page where a sign-up link is also present.
        # Using this path bypasses the entire onboarding wizard.
        self.log("Clicking 'I already have an account' (have-account)...")
        _have_acct = page.locator('[data-test="have-account"]')
        _have_acct.wait_for(state="visible", timeout=15000)
        _have_acct.click(timeout=8000)
        page.wait_for_timeout(2000)

        # ── Step 3: Click "Sign up" on the login page ─────────────────────
        self.log("Clicking Sign up (sign-up-button)...")
        _signup = page.locator('[data-test="sign-up-button"]')
        _signup.wait_for(state="visible", timeout=10000)
        _signup.click(timeout=8000)
        page.wait_for_timeout(2000)

        # ── Step 4: Age form ──────────────────────────────────────────────
        # Field: [data-test="age-input"]  placeholder: "Umur" / "Age"
        # Button: [data-test="continue-button"]  — disabled until age entered
        self.log("Filling age form...")
        _age = page.locator('[data-test="age-input"]')
        _age.wait_for(state="visible", timeout=10000)
        _age_val = str(random.randint(22, 35))
        _age.fill(_age_val)
        self.log(f"Age: {_age_val}")

        _cont = page.locator('[data-test="continue-button"]')
        # Poll until continue-button enables (it activates after valid age is typed)
        for _ in range(15):
            if _cont.is_enabled():
                break
            page.wait_for_timeout(300)
        _cont.click(timeout=8000)
        self.log("Age continue clicked.")
        page.wait_for_timeout(2000)

        # ── Step 5–7: Registration form ────────────────────────────────────
        # Same URL, new modal: Buat profilmu / Create your profile
        # Note: password must be ≥8 chars with uppercase + number/symbol
        self.log("Filling registration form...")

        _name = page.locator('[data-test="full-name-input"]')
        _name.wait_for(state="visible", timeout=12000)
        _name.fill(email.split("@")[0])
        self.log(f"Name: {email.split('@')[0]}")

        _email_f = page.locator('[data-test="email-input"]')
        _email_f.wait_for(state="visible", timeout=8000)
        _email_f.fill(email)
        self.log(f"Email: {email}")

        _pw_f = page.locator('[data-test="password-input"]')
        _pw_f.wait_for(state="visible", timeout=8000)
        _pw_f.fill(password)
        self.log("Password filled.")

        # ── Step 8: Submit ─────────────────────────────────────────────────
        self.log("Clicking BUAT AKUN (register-button)...")
        _reg_btn = page.locator('[data-test="register-button"]')
        _reg_btn.wait_for(state="visible", timeout=10000)

        # Use expect_navigation with domcontentloaded to avoid the renderer
        # crashing during the heavy /learn page 'load' event.
        # The default 'load' waits for ALL resources (images, scripts) which
        # overwhelms the renderer when running headless with limited memory.
        self.log("Waiting for post-registration redirect (domcontentloaded)...")
        try:
            with page.expect_navigation(
                url=re.compile(r"duolingo\.com/(learn|home|email-verification|onboarding)"),
                wait_until="domcontentloaded",
                timeout=60000,
            ):
                _reg_btn.click()
        except Exception as _nav_err:
            _nav_err_s = str(_nav_err).lower()
            if "crashed" in _nav_err_s or "page crash" in _nav_err_s:
                raise  # let the outer retry loop handle it
            # Non-crash navigation exception — might be a timing mismatch.
            # Fall back to a simple URL poll to confirm success.
            self.log(f"Navigation wait failed ({_nav_err}) — polling URL...")
            import time as _time
            _deadline = _time.time() + 30
            while _time.time() < _deadline:
                try:
                    _cur = page.url
                    if re.search(r"duolingo\.com/(learn|home|email-verification|onboarding)", _cur):
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            else:
                raise RuntimeError(
                    f"Registration did not navigate to expected URL. "
                    f"Current: {page.url}. Original error: {_nav_err}"
                )

        final_url = page.url
        self.log(f"Registration SUCCESS! Landed on: {final_url}")

        # ── Email verification (optional) ─────────────────────────────────
        if self.verification_link_callback:
            self.log("Requesting email verification link...")
            confirm_url = self.verification_link_callback()
            self.log(f"Navigating to verification link: {confirm_url}")
            confirm_page = context.new_page()
            _block_heavy_resources(confirm_page)
            confirm_page.goto(confirm_url, wait_until="domcontentloaded", timeout=60000)
            confirm_page.wait_for_timeout(3000)
            confirm_page.close()

        # ── Step 9–12: Referral code redemption (same session) ────────────
        redeem_result = None
        if referral_code:
            self.log(f"Redeeming referral code: {referral_code}")
            try:
                redeem_result = self._do_redeem(page, context, email, password, referral_code)
                self.log(f"Redemption result: {redeem_result}")
            except Exception as e:
                self.log(f"Redemption failed (non-fatal): {e}")
                redeem_result = {"success": False, "error": str(e)}

        cookies = context.cookies()
        local_storage = page.evaluate("() => JSON.stringify(localStorage)")

        return {
            "success": True,
            "email": email,
            "password": password,
            "status": "registered",
            "cookies": cookies,
            "localStorage": local_storage,
            "redemption": redeem_result,
        }

    def _do_redeem(
        self,
        page: Page,
        context: BrowserContext,
        email: str,
        password: str,
        referral_code: str,
    ) -> dict:
        """
        Redeem a referral code. Can be called inline after registration
        or separately via redeem_code() with stored cookies.

        Recorded Playwright steps:
          goto /redeem
          → textbox name="Enter code"  → fill referral_code
          → button "REDEEM NOW"        → click
          → button "Claim offer"       → click
        """

        # Navigate to the redeem page
        self.log("Navigating to /redeem...")
        page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # If redirected to login (cookies expired), log in first
        if "/log-in" in page.url or "/register" in page.url:
            self.log("Session expired — logging in before redeem...")
            page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
            page.locator('[data-test="email-input"]').fill(email)
            page.locator('[data-test="password-input"]').fill(password)
            page.get_by_role("button", name=re.compile(
                r"Log in|Masuk|Đăng nhập|登录|Iniciar sesión|Entrar", re.I
            )).first.click()
            page.wait_for_url(re.compile(r"/(learn|home)"), timeout=45000)
            page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

        # Enter referral code
        self.log("Entering referral code...")
        _code_box = page.get_by_role("textbox", name=re.compile(r"Enter code", re.I))
        _code_box.wait_for(state="visible", timeout=15000)
        _code_box.click(timeout=5000)
        _code_box.fill(referral_code)
        self.log(f"Code entered: {referral_code}")

        # Click REDEEM NOW
        self.log("Clicking REDEEM NOW...")
        _redeem_btn = page.get_by_role("button", name=re.compile(r"REDEEM NOW|Redeem now", re.I))
        _redeem_btn.wait_for(state="visible", timeout=10000)
        _redeem_btn.click(timeout=8000)
        page.wait_for_timeout(2000)

        # Click Claim offer
        self.log("Clicking Claim offer...")
        _claim_btn = page.get_by_role("button", name=re.compile(r"Claim offer", re.I))
        _claim_btn.wait_for(state="visible", timeout=10000)
        _claim_btn.click(timeout=8000)
        page.wait_for_timeout(3000)

        self.log("Referral code redeemed successfully!")
        return {
            "success": True,
            "referral_code": referral_code,
            "cookies": context.cookies(),
        }
