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
# Note: --no-zygote is intentionally omitted — it's a no-op on Windows and
# can cause renderer instability on some Linux configurations.
# ---------------------------------------------------------------------------
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",       # use /tmp instead of /dev/shm (Docker / low /dev/shm)
    "--disable-gpu",                  # disable GPU compositing — avoids GPU process crashes
    "--no-sandbox",                   # required when running as root (CI / Docker)
    "--disable-software-rasterizer",  # prevent fallback rasterizer from crashing
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-hang-monitor",         # don't let the hang monitor kill a slow renderer
    "--disable-prompt-on-repost",
    "--metrics-recording-only",
    "--mute-audio",
]

# ---------------------------------------------------------------------------
# Resource types and URL patterns to block.
# Blocking images / fonts / media cuts renderer memory significantly.
# NOTE: stylesheet is intentionally NOT blocked — Duolingo's React SPA uses
# CSS classes to control visibility (display/opacity). Without CSS, buttons
# never become 'visible' and all locator.wait_for() calls time out.
# ---------------------------------------------------------------------------
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCKED_URL_PATTERNS = [
    "amplitude",
    "analytics",
    "segment.io",
    "google-analytics",
    "googletagmanager",
    "hotjar",
    "doubleclick",
    "facebook.net",
    "cdn.optimizely",
]


def _proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"server": proxy}


def _block_heavy_resources(page: Page) -> None:
    """
    Intercept requests and abort resource types that are not needed for
    registration but consume significant renderer memory (images, fonts,
    media, analytics scripts).
    """
    def _handler(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        url = route.request.url
        if any(pat in url for pat in _BLOCKED_URL_PATTERNS):
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, email: str, password: str) -> dict:
        self.log(f"Starting Duolingo browser registration for: {email}")
        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.log(f"Retry attempt {attempt}/{max_attempts} after previous crash...")

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

                    # Block heavy resources BEFORE any navigation to reduce memory peak
                    _block_heavy_resources(page)

                    try:
                        result = self._do_register(page, context, email, password)
                        return result
                    except Exception as exc:
                        error_str = str(exc)
                        # "Page crashed" or "Chromium renderer crashed" → retry with fresh browser
                        if "crashed" in error_str.lower() or "page crash" in error_str.lower():
                            self.log(f"Attempt {attempt}: Page crashed — will retry with fresh browser. ({error_str})")
                            last_error = exc
                            continue
                        # Any other error → surface immediately, no point retrying
                        self.log(f"Duolingo registration process failed: {exc}")
                        raise
                finally:
                    try:
                        if browser:
                            browser.close()
                    except Exception:
                        pass

        # All attempts exhausted due to crash
        raise RuntimeError(
            f"Duolingo registration failed after {max_attempts} attempts due to persistent page crash. "
            f"Last error: {last_error}"
        )

    def redeem_code(self, email: str, password: str, cookies: List[Dict], referral_code: str) -> dict:
        self.log(f"Starting Duolingo redemption for {email} with code: {referral_code}")
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
                # Go to redemption page
                page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                # Check if login is required
                if "/log-in" in page.url or "/register" in page.url or page.locator('input[type="email"]').count() > 0:
                    self.log("Stored cookies expired or invalid. Attempting direct login...")
                    page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
                    email_input = page.locator(
                        'input[type="email"], input[placeholder*="Email" i], input[placeholder*="username" i], '
                        'input[placeholder*="邮箱" i], input[placeholder*="Tên đăng nhập" i], input[placeholder*="Nama pengguna" i]'
                    ).first
                    email_input.wait_for(state="visible", timeout=15000)
                    email_input.fill(email)

                    pass_input = page.locator(
                        'input[type="password"], input[placeholder*="Password" i], input[placeholder*="密码" i], '
                        'input[placeholder*="mật khẩu" i], input[placeholder*="kata sandi" i], '
                        'input[placeholder*="contraseña" i], input[placeholder*="senha" i]'
                    ).first
                    pass_input.fill(password)

                    login_btn = page.get_by_role("button", name=re.compile("Log in|登录|Đăng nhập|Masuk|Iniciar sesión|Entrar", re.I)).first
                    login_btn.click()
                    page.wait_for_url(re.compile(r"/(learn|home)"), timeout=45000)
                    page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)

                # Fill and submit code
                self.log(f"Locating redeem code field and inputting: {referral_code}")
                code_input = page.locator(
                    'input[placeholder*="code" i], input[placeholder*="mã" i], '
                    'input[placeholder*="código" i], input[type="text"]'
                ).first
                code_input.wait_for(state="visible", timeout=15000)
                code_input.fill(referral_code)

                redeem_btn = page.get_by_role("button", name=re.compile(
                    "Redeem|Submit|Claim|兑换|确认|Áp dụng|Klaim|Canjear|Resgatar", re.I
                )).first
                redeem_btn.click()

                self.log("Waiting for code redemption response...")
                page.wait_for_timeout(5000)

                # Navigate to Settings to verify Super subscription status
                self.log("Navigating to /settings/super to verify subscription status...")
                page.goto("https://www.duolingo.com/settings/super", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                page_text = page.locator("body").inner_text()
                if "Super Duolingo" in page_text:
                    self.log("Super Duolingo is ACTIVE! Claims succeeded.")
                    updated_cookies = context.cookies()
                    return {"success": True, "cookies": updated_cookies}
                else:
                    self.log("Super Duolingo not found under /settings/super.")
                    return {"success": False, "error": "Super Duolingo subscription not active after redemption."}
            except Exception as e:
                self.log(f"Redemption flow encountered error: {e}")
                return {"success": False, "error": str(e)}
            finally:
                context.close()
                browser.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_register(self, page: Page, context: BrowserContext, email: str, password: str) -> dict:
        """Execute the full registration flow on an already-configured page."""
        # Navigate directly to /register to skip the homepage "Get started" button.
        # This avoids the brittle homepage button selector entirely.
        self.log("Navigating directly to Duolingo registration page...")
        page.goto("https://www.duolingo.com/register", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # Duolingo may redirect /register → homepage or /onboarding depending on locale/A-B test.
        # If we land back on the homepage, fall back to clicking "Get started".
        current_url = page.url
        self.log(f"Landed on: {current_url}")

        if "/register" not in current_url and "/onboarding" not in current_url:
            # We're on the homepage — try to click the entry button
            self.log("Redirected to homepage — looking for entry button...")
            _gs_loc = page.locator('[data-test="get-started"], [data-test="have-account"]')
            if _gs_loc.count() > 0 and _gs_loc.first.is_visible():
                _gs_loc.first.click(timeout=10000)
            else:
                entry_btn = page.get_by_role("button", name=re.compile(
                    "Get started|Sign up|Register|Create account|"
                    "开始|开始学习|Bắt đầu|Mulai|Empezar|Começar|Registrarse|Cadastrar",
                    re.I
                )).first
                try:
                    entry_btn.wait_for(state="visible", timeout=10000)
                    entry_btn.click(timeout=10000)
                except Exception:
                    self.log("Entry button not found on homepage — attempting onboarding flow anyway")
            page.wait_for_timeout(1500)

        # Handle onboarding wizard (skipped automatically if /register lands on the form directly)
        self._click_through_onboarding(page)

        # ── Diagnostic: log what's actually on the page ──────────────────────
        # This helps us understand the /welcome page structure
        self.log(f"Current page: {page.url} | title: {page.title()}")
        try:
            _btn_texts = []
            for _b in page.locator("button, a[role='button'], [role='button']").all():
                try:
                    if _b.is_visible():
                        _t = (_b.inner_text() or "").strip()[:60]
                        if _t:
                            _btn_texts.append(_t)
                except Exception:
                    pass
            self.log(f"Visible interactive elements: {_btn_texts}")
            _inp_info = []
            for _inp in page.locator("input").all():
                try:
                    if _inp.is_visible():
                        _inp_info.append(_inp.get_attribute("type") or "text")
                except Exception:
                    pass
            self.log(f"Visible inputs: {_inp_info}")
        except Exception:
            pass

        # ── Strategy: reveal the email/password form ──────────────────────────
        # Duolingo's /welcome page may show:
        #   A) Social login options + "Sign up with email" button → click it
        #   B) A welcome screen with just "Continue" → click it, then reach the form
        #   C) The form is already rendered (less common)
        _clicked_entry = False

        # Try 1: any element whose text contains "email" (broadest match)
        for _sel in [
            'button:has-text("email")',
            'a:has-text("email")',
            '[data-test*="email"]',
            'button[data-test*="email"]',
        ]:
            try:
                _loc = page.locator(_sel).first
                if _loc.count() > 0 and _loc.is_visible():
                    self.log(f"Clicking email signup element ({_sel})...")
                    _loc.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    _clicked_entry = True
                    break
            except Exception:
                continue

        # Try 2: by role=button or role=link with any "email" in the accessible name
        if not _clicked_entry:
            for _role in ("button", "link"):
                try:
                    _loc = page.get_by_role(_role, name=re.compile(r"email", re.I)).first
                    if _loc.is_visible():
                        self.log(f"Clicking email signup ({_role} by role)...")
                        _loc.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        _clicked_entry = True
                        break
                except Exception:
                    continue

        # Try 3: click any "Continue" button on the welcome page (transitional screen)
        if not _clicked_entry:
            self.log("No email button found — clicking Continue on welcome screen...")
            for _dt in ["register-button", "continue-button", "next-button"]:
                _loc = page.locator(f'[data-test="{_dt}"]')
                if _loc.count() > 0:
                    try:
                        _loc.first.wait_for(state="visible", timeout=2000)
                        _loc.first.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        _clicked_entry = True
                        break
                    except Exception:
                        pass
            if not _clicked_entry:
                _cont = page.get_by_role("button", name=re.compile(
                    r"Continue|Next|Get started|OK|Start|Done|Lanjut|Tiếp tục|Continuar|Começar",
                    re.I
                )).first
                try:
                    if _cont.is_visible():
                        _cont.click(timeout=5000)
                        page.wait_for_timeout(2000)
                        _clicked_entry = True
                except Exception:
                    pass

        # Try 4: navigate directly to the email registration path
        if not _clicked_entry:
            self.log("Trying direct navigation to email sign-up form...")
            page.goto("https://www.duolingo.com/register?referrer=https://www.duolingo.com/welcome",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

        # ── Wait for the email input to appear ────────────────────────────────
        self.log("Filling profile registration form...")
        # Duolingo may use type="email" OR type="text" with autocomplete="email"
        _email_input_sel = (
            'input[type="email"], '
            'input[autocomplete*="email" i], '
            'input[name*="email" i], '
            'input[placeholder*="email" i], '
            'input[placeholder*="邮箱" i], '
            'input[placeholder*="Email" i]'
        )
        _email_input = page.locator(_email_input_sel).first
        try:
            _email_input.wait_for(state="visible", timeout=15000)
        except Exception as e:
            # Log page state one more time for final diagnosis
            try:
                _final_btns = [_b.inner_text()[:40] for _b in page.locator("button").all() if _b.is_visible()]
                _final_inps = [_i.get_attribute("type") or "?" for _i in page.locator("input").all() if _i.is_visible()]
                self.log(f"Final page state — URL: {page.url} | buttons: {_final_btns} | inputs: {_final_inps}")
            except Exception:
                pass
            raise RuntimeError(
                f"Email input never appeared on {page.url}. "
                f"Duolingo may have changed its registration flow. Original error: {e}"
            )

        # Optional: age / birthday field (some variants omit it)
        _age_selector = (
            'input[data-test*="age" i], '
            'input[placeholder*="Age" i], input[placeholder*="Birthday" i], '
            'input[placeholder*="Tuổi" i], input[placeholder*="Edad" i], '
            'input[placeholder*="Umur" i], input[placeholder*="Idade" i], input[placeholder*="年龄" i], '
            'input[type="date"], input[type="number"][min="1"], input[type="number"]'
        )
        age_input = page.locator(_age_selector).first
        try:
            age_input.wait_for(state="visible", timeout=4000)
            age_input.fill(str(random.randint(22, 45)))
        except Exception:
            self.log("No age/birthday input found — skipping")

        # Optional: name field
        name_input = page.locator(
            'input[placeholder*="Name" i], input[placeholder*="姓名" i], input[placeholder*="Tên" i], '
            'input[placeholder*="Nombre" i], input[placeholder*="Nome" i], input[placeholder*="Nama" i]'
        ).first
        if name_input.count() and name_input.is_visible():
            name_input.fill(email.split("@")[0])

        # Fill email and password (email input already confirmed visible above)
        _email_input.fill(email)
        page.locator('input[type="password"]').first.fill(password)

        # Submit form
        self.log("Submitting registration profile...")
        _sb_loc = page.locator('[data-test="register-button"]')
        if _sb_loc.count() > 0 and _sb_loc.first.is_visible():
            submit_btn = _sb_loc.first
        else:
            submit_btn = page.get_by_role("button", name=re.compile(
                "Create profile|Create account|创建账号|确认|完成|Tạo hồ sơ|Tạo tài khoản|"
                "Buat profil|Buat akun|Crear perfil|Crear cuenta|Criar perfil|Criar conta",
                re.I
            )).first
        submit_btn.wait_for(state="visible", timeout=10000)
        submit_btn.click()

        # Wait for post-registration URL — accept /learn, /home, /email-verification, /onboarding
        page.wait_for_url(
            re.compile(r"/(learn|home|email-verification|onboarding)"),
            timeout=45000,
        )
        final_url = page.url
        self.log(f"Registration completed successfully! Landed on: {final_url}")

        # Handle email verification if a callback was provided
        if self.verification_link_callback:
            self.log("Requesting verification email confirmation link...")
            confirm_url = self.verification_link_callback()
            self.log(f"Bypassing verification by navigating directly to verification link: {confirm_url}")
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

    def _click_through_onboarding(self, page: Page):
        self.log("Handling onboarding questionnaires...")
        prev_url = page.url
        stall_steps = 0

        # URLs that signal we have reached the sign-up / profile form.
        # NOTE: /welcome is intentionally NOT here — it is a Duo mascot animation
        # sequence (Phase 1 screenshots confirmed) with a LANJUTKAN button.
        # Only exit when we see actual form inputs or a dedicated form URL.
        _FORM_URL_MARKERS = ("/profile", "/name", "/age", "/signup", "/create-profile")

        for step in range(35):
            try:
                current_url = page.url

                # ── Exit: sign-up form reached (email input visible) ──────────
                if page.locator('input[type="email"]').count() > 0:
                    self.log("Sign-up form visible. Exiting onboarding loop.")
                    break

                # ── Exit: URL reached a known form / profile page ─────────────
                if any(marker in current_url for marker in _FORM_URL_MARKERS):
                    self.log(f"Reached form page: {current_url} — waiting for email input...")
                    try:
                        page.locator('input[type="email"]').wait_for(state="visible", timeout=5000)
                    except Exception:
                        pass
                    self.log("Exiting onboarding loop (form URL detected).")
                    break

                # Log URL transitions so we know where we are
                if current_url != prev_url:
                    self.log(f"Step {step}: Navigated to {current_url}")
                    prev_url = current_url
                    stall_steps = 0
                    # Wait 2s for React to settle and render new page content
                    # before querying for option cards or Continue buttons
                    page.wait_for_timeout(2000)

                # ── Select an option card or list item ──────────────────────────
                # Covers: grid language cards, native language list items, radio buttons.
                # Do NOT use force=True — bypasses React synthetic event handlers.
                clicked_option = False
                for sel in [
                    'button[role="radio"]',
                    '[data-test*="-card"]:not([data-test*="register"])',
                    '[data-test*="card"]:not([data-test*="register"])',
                    'ul li a',
                    'ul li button',
                    '[aria-checked="false"]',
                ]:
                    for opt in page.locator(sel).all():
                        try:
                            if opt.is_visible() and opt.is_enabled():
                                self.log(f"Step {step}: Clicking option ({sel})...")
                                opt.click(timeout=4000)
                                page.wait_for_timeout(600)
                                clicked_option = True
                                break
                        except Exception:
                            continue
                    if clicked_option:
                        break

                if clicked_option:
                    page.wait_for_timeout(400)  # brief settle after React state update

                # ── Find and click Continue / Next ────────────────────────────
                clicked_continue = False

                # Priority 1: known data-test attributes
                for dt in ["register-button", "onboarding-next", "continue-button", "next-button", "submit-button"]:
                    loc = page.locator(f'[data-test="{dt}"]')
                    if loc.count() > 0:
                        try:
                            loc.first.wait_for(state="visible", timeout=1500)
                            # Poll up to 3s for button to become enabled after card selection
                            for _ in range(6):
                                if loc.first.is_enabled():
                                    break
                                page.wait_for_timeout(500)
                            if loc.first.is_enabled():
                                self.log(f"Step {step}: Clicking [data-test={dt}]...")
                                loc.first.click(timeout=5000)
                                page.wait_for_timeout(1400)
                                clicked_continue = True
                                stall_steps = 0
                                break
                        except Exception:
                            pass

                # Priority 2: role=button with known labels (incl. LANJUTKAN = Indonesian Continue)
                if not clicked_continue:
                    btn = page.get_by_role("button", name=re.compile(
                        r"Continue|Next|Confirm|OK|Done|Submit|Got it|Start|"
                        r"Lanjutkan|Lanjut|Mulai|Bắt đầu|Tiếp tục|"
                        r"继续|下一步|开始|完成|确认|"
                        r"Continuar|Confirmar|Empezar|Começar",
                        re.I
                    )).first
                    try:
                        if btn.is_visible():
                            for _ in range(6):
                                if btn.is_enabled():
                                    break
                                page.wait_for_timeout(500)
                            if btn.is_enabled():
                                self.log(f"Step {step}: Clicking Continue (by role)...")
                                btn.click(timeout=5000)
                                page.wait_for_timeout(1400)
                                clicked_continue = True
                                stall_steps = 0
                    except Exception:
                        pass

                # Priority 3: any visible enabled button longer than 3 chars (last resort)
                if not clicked_continue:
                    for b in page.locator("button").all():
                        try:
                            if b.is_visible() and b.is_enabled():
                                label = (b.inner_text() or "").strip()
                                skip = {"", "x", "close", "skip", "×", "✕"}
                                if label and label.lower() not in skip and len(label) > 3:
                                    self.log(f"Step {step}: Fallback click: '{label}'")
                                    b.click(timeout=5000)
                                    page.wait_for_timeout(1400)
                                    clicked_continue = True
                                    stall_steps = 0
                                    break
                        except Exception:
                            continue

                # ── Stall detection ───────────────────────────────────────────
                if not clicked_option and not clicked_continue:
                    stall_steps += 1
                    self.log(f"Step {step}: Nothing to click (stall #{stall_steps})")
                    if stall_steps >= 5:
                        self.log("Onboarding stalled — exiting loop early")
                        break
                    page.wait_for_timeout(1200)

            except Exception as e:
                self.log(f"Onboarding step {step} error: {e}")
                page.wait_for_timeout(1000)
