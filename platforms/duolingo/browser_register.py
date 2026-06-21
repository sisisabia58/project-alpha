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
# Chromium stability flags — keeps headless renderer alive on memory-limited hosts
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
# stylesheet intentionally NOT blocked (CSS needed for React visibility).
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
    """Abort resources that don't help registration but eat renderer memory."""
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

    def register(self, email: str, password: str) -> dict:
        self.log(f"Starting Duolingo browser registration for: {email}")
        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log(f"Retry attempt {attempt}/{max_attempts} after crash...")

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
                        return self._do_register(page, context, email, password)
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

    def redeem_code(self, email: str, password: str, cookies: List[Dict], referral_code: str) -> dict:
        self.log(f"Starting Duolingo code redemption for {email}: {referral_code}")
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
                page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                if "/log-in" in page.url or "/register" in page.url or page.locator('input[type="email"]').count() > 0:
                    self.log("Cookies expired — logging in...")
                    page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
                    page.locator('[data-test="email-input"], input[type="email"]').first.fill(email)
                    page.locator('[data-test="password-input"], input[type="password"]').first.fill(password)
                    page.get_by_role("button", name=re.compile(
                        r"Log in|Masuk|Đăng nhập|登录|Iniciar sesión|Entrar", re.I
                    )).first.click()
                    page.wait_for_url(re.compile(r"/(learn|home)"), timeout=45000)
                    page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)

                code_input = page.locator(
                    'input[placeholder*="code" i], input[placeholder*="mã" i], '
                    'input[placeholder*="código" i], input[type="text"]'
                ).first
                code_input.wait_for(state="visible", timeout=15000)
                code_input.fill(referral_code)

                page.get_by_role("button", name=re.compile(
                    r"Redeem|Submit|Claim|Klaim|兑换|Áp dụng|Canjear|Resgatar", re.I
                )).first.click()
                page.wait_for_timeout(5000)

                page.goto("https://www.duolingo.com/settings/super", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                if "Super Duolingo" in page.locator("body").inner_text():
                    self.log("Super Duolingo ACTIVE!")
                    return {"success": True, "cookies": context.cookies()}
                return {"success": False, "error": "Super Duolingo not active after redemption."}
            except Exception as e:
                self.log(f"Redemption error: {e}")
                return {"success": False, "error": str(e)}
            finally:
                context.close()
                browser.close()

    # ──────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────

    def _do_register(self, page: Page, context: BrowserContext, email: str, password: str) -> dict:
        """
        Execute the full Duolingo registration flow.

        Flow verified via Playwright CRX recording (June 2025):

          1. Homepage           → click [data-test="get-started-top"]
          2. /register          → click [data-test*="language-card"] (first card)
          3. /welcome           → click [data-test="funboarding-continue-button"] × N
          4. /welcome?step=...  → click ← back button (empty-text button)  ← skips survey
          5. /learn             → click [data-test="close-button"]           ← closes level modal
          6. /learn             → click [data-test="create-profile-juicy"]
          7. /learn?isLoggingIn → fill  [data-test="age-input"] + click [data-test="continue-button"]
          8. /learn?isLoggingIn → fill  [data-test="full-name-input"]
                                   fill  [data-test="email-input"]
                                   fill  [data-test="password-input"]
                                   click [data-test="register-button"]
        """

        # ── Step 1: Homepage → GET STARTED ────────────────────────────────
        self.log("Navigating to Duolingo homepage...")
        page.goto("https://www.duolingo.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        self.log(f"Landed on: {page.url}")

        self.log("Clicking GET STARTED (get-started-top)...")
        _gs = page.locator('[data-test="get-started-top"]')
        try:
            _gs.wait_for(state="visible", timeout=15000)
            _gs.click(timeout=10000)
        except Exception:
            # Fallback: try the main CTA button
            self.log("get-started-top not found — trying get-started...")
            _gs2 = page.locator('[data-test="get-started"]')
            if _gs2.count() > 0 and _gs2.first.is_visible():
                _gs2.first.click(timeout=10000)
            else:
                page.get_by_role("button", name=re.compile(
                    r"Get started|Mulai|Empezar|Começar|Bắt đầu|开始", re.I
                )).first.click(timeout=10000)
        page.wait_for_timeout(2000)

        # ── Step 2: Language grid → click first card ──────────────────────
        # Recorded: [data-test="flag-english language-card"]
        # We use a wildcard to pick any available language card
        self.log("Selecting language to learn (first available card)...")
        _card = page.locator('[data-test*="language-card"]').first
        try:
            _card.wait_for(state="visible", timeout=15000)
            _card.click(timeout=8000)
            self.log(f"Language card clicked.")
        except Exception as e:
            raise RuntimeError(f"Language card not found: {e}")
        page.wait_for_timeout(2000)

        # ── Optional: Native language list ────────────────────────────────
        # Some locales show "What language do you speak?" before funboarding.
        # If it appears, click the first list item and continue.
        try:
            _native = page.locator('ul li a, ul li button').first
            if _native.is_visible():
                self.log("Native language selection found — clicking first option...")
                _native.click(timeout=3000)
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # ── Step 3: Funboarding LANJUTKAN screens ────────────────────────
        # Recorded: funboarding-continue-button clicked twice (animation screens).
        # We loop until it's gone (handles variable number of animation screens).
        self.log("Clicking through funboarding screens (LANJUTKAN)...")
        for _i in range(8):
            try:
                _fbd = page.locator('[data-test="funboarding-continue-button"]')
                _fbd.first.wait_for(state="visible", timeout=3000)
                if _fbd.first.is_enabled():
                    self.log(f"LANJUTKAN #{_i + 1}...")
                    _fbd.first.click(timeout=5000)
                    page.wait_for_timeout(1500)
                else:
                    break
            except Exception:
                break  # Button gone — funboarding complete

        # ── Step 4: Skip "Dari mana kamu tahu?" survey ───────────────────
        # Recorded: getByRole('button').filter({ hasText: /^$/ })
        # The ← back arrow button has no visible text (icon only).
        # Clicking it navigates directly to /learn, skipping the survey.
        self.log("Clicking back (←) to skip survey screen...")
        try:
            _back = page.get_by_role("button").filter(has_text=re.compile(r"^$")).first
            if _back.is_visible():
                self.log("Back button (empty text) clicked.")
                _back.click(timeout=5000)
                page.wait_for_timeout(1500)
            else:
                # Fallback: navigate to /learn directly
                self.log("Back button not visible — navigating to /learn directly...")
                page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
        except Exception as e:
            self.log(f"Back button: {e} — navigating to /learn directly...")
            page.goto("https://www.duolingo.com/learn", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

        # ── Step 5: Close level-selection modal on /learn ─────────────────
        # Recorded: [data-test="close-button"] (the ✕ on "Berapa banyak?" modal)
        self.log("Closing level-selection modal (close-button)...")
        try:
            _close = page.locator('[data-test="close-button"]')
            _close.first.wait_for(state="visible", timeout=10000)
            _close.first.click(timeout=5000)
            self.log("Level modal closed.")
            page.wait_for_timeout(1500)
        except Exception as e:
            self.log(f"close-button not found ({e}) — continuing")

        # ── Step 6: Click "BUAT PROFIL" ───────────────────────────────────
        # Recorded: [data-test="create-profile-juicy"]
        self.log("Clicking BUAT PROFIL (create-profile-juicy)...")
        _buat_profil = page.locator('[data-test="create-profile-juicy"]')
        try:
            _buat_profil.wait_for(state="visible", timeout=12000)
            _buat_profil.click(timeout=5000)
            self.log("BUAT PROFIL clicked.")
            page.wait_for_timeout(2000)
        except Exception as e:
            raise RuntimeError(f"create-profile-juicy not found on {page.url}: {e}")

        # ── Step 7: Age form ──────────────────────────────────────────────
        # URL: /learn?isLoggingIn=true
        # Recorded: [data-test="age-input"].fill("23") → [data-test="continue-button"].click()
        self.log("Filling age form...")
        _age = page.locator('[data-test="age-input"]')
        try:
            _age.wait_for(state="visible", timeout=8000)
            _age_val = str(random.randint(22, 35))
            _age.fill(_age_val)
            self.log(f"Age: {_age_val}")

            # continue-button enables only after a valid age is entered
            _cont = page.locator('[data-test="continue-button"]')
            for _ in range(10):
                if _cont.is_enabled():
                    break
                page.wait_for_timeout(400)
            _cont.click(timeout=5000)
            self.log("BERIKUTNYA clicked.")
            page.wait_for_timeout(2000)
        except Exception as e:
            self.log(f"Age form: {e} — continuing to registration form")

        # ── Step 8: Registration form ─────────────────────────────────────
        # URL: /learn?isLoggingIn=true (same URL, new modal)
        # Recorded selectors:
        #   [data-test="full-name-input"]  ← Nama (opsional)
        #   [data-test="email-input"]      ← Email
        #   [data-test="password-input"]   ← Kata sandi
        #   [data-test="register-button"]  ← BUAT AKUN
        self.log("Filling registration form (Buat profilmu)...")

        # Wait for the form to appear (name input is the first field)
        _name = page.locator('[data-test="full-name-input"]')
        try:
            _name.wait_for(state="visible", timeout=15000)
            _name.fill(email.split("@")[0])
            self.log(f"Name: {email.split('@')[0]}")
        except Exception as e:
            self.log(f"full-name-input: {e}")

        _email_field = page.locator('[data-test="email-input"]')
        try:
            _email_field.wait_for(state="visible", timeout=8000)
            _email_field.fill(email)
            self.log(f"Email: {email}")
        except Exception as e:
            self.log(f"email-input: {e}")

        _pw_field = page.locator('[data-test="password-input"]')
        try:
            _pw_field.wait_for(state="visible", timeout=8000)
            _pw_field.fill(password)
            self.log("Password filled.")
        except Exception as e:
            self.log(f"password-input: {e}")

        # Submit
        self.log("Clicking BUAT AKUN (register-button)...")
        _reg_btn = page.locator('[data-test="register-button"]')
        _reg_btn.wait_for(state="visible", timeout=10000)
        _reg_btn.click()

        # ── Wait for post-registration success ────────────────────────────
        # After BUAT AKUN: redirects to /learn (no isLoggingIn), /home, or /email-verification
        self.log("Waiting for registration success redirect...")
        page.wait_for_url(
            re.compile(r"duolingo\.com/(learn$|learn\?(?!isLoggingIn)|home|email-verification|onboarding)"),
            timeout=45000,
        )
        final_url = page.url
        self.log(f"Registration SUCCESS! Landed on: {final_url}")

        # Email verification (optional callback)
        if self.verification_link_callback:
            self.log("Requesting email verification link...")
            confirm_url = self.verification_link_callback()
            self.log(f"Navigating to: {confirm_url}")
            confirm_page = context.new_page()
            _block_heavy_resources(confirm_page)
            confirm_page.goto(confirm_url, wait_until="domcontentloaded", timeout=60000)
            confirm_page.wait_for_timeout(3000)
            confirm_page.close()

        cookies = context.cookies()
        local_storage = page.evaluate("() => JSON.stringify(localStorage)")

        return {
            "success": True,
            "email": email,
            "password": password,
            "status": "registered",
            "cookies": cookies,
            "localStorage": local_storage,
        }
