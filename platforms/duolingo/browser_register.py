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
# Blocking images / fonts / media / analytics cuts renderer memory by ~60-70%
# on heavy SPAs like Duolingo — the single biggest cause of Page crashed.
# ---------------------------------------------------------------------------
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
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
        # Navigate — domcontentloaded is safer than networkidle for crash-prone pages
        page.goto("https://www.duolingo.com/", wait_until="domcontentloaded", timeout=60000)
        # Brief pause so React mounts its root components before we query the DOM
        page.wait_for_timeout(2000)

        # Click "Get started"
        self.log("Clicking 'Get started'...")
        _gs_loc = page.locator('[data-test="get-started"]')
        if _gs_loc.count() > 0 and _gs_loc.first.is_visible():
            get_started = _gs_loc.first
        else:
            get_started = page.get_by_role("button", name=re.compile(
                "Get started|开始|开始学习|Bắt đầu|Mulai|Empezar|Começar",
                re.I
            )).first
        get_started.wait_for(state="visible", timeout=15000)
        get_started.click(timeout=10000)
        page.wait_for_timeout(1500)

        # Handle onboarding wizard
        self._click_through_onboarding(page)

        # Fill registration details
        self.log("Filling profile registration form...")
        _age_selector = (
            'input[data-test*="age" i], '
            'input[placeholder*="Age" i], input[placeholder*="Birthday" i], '
            'input[placeholder*="Tuổi" i], input[placeholder*="Edad" i], '
            'input[placeholder*="Umur" i], input[placeholder*="Idade" i], input[placeholder*="年龄" i], '
            'input[type="date"], input[type="number"][min="1"], input[type="number"]'
        )
        age_input = page.locator(_age_selector).first
        try:
            age_input.wait_for(state="visible", timeout=12000)
            age_input.fill(str(random.randint(22, 45)))
        except Exception:
            self.log("No age/birthday input found — skipping (form variant without age field)")

        name_input = page.locator(
            'input[placeholder*="Name" i], input[placeholder*="姓名" i], input[placeholder*="Tên" i], '
            'input[placeholder*="Nombre" i], input[placeholder*="Nome" i], input[placeholder*="Nama" i]'
        ).first
        if name_input.count() and name_input.is_visible():
            name_input.fill(email.split("@")[0])

        page.locator('input[type="email"]').first.fill(email)
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
        for step in range(25):
            try:
                # Exit early when sign-up form is visible
                if page.locator('input[type="email"]').count() > 0:
                    self.log("Sign-up form visible. Exiting onboarding loop.")
                    break

                # Click the first selectable option card if any are present
                options = page.locator(
                    'button[role="radio"], ul li button, [data-test*="-card"], [data-test*="card"]'
                ).all()
                if len(options) > 0:
                    self.log(f"Step {step}: Clicking option card...")
                    options[0].click(timeout=3000, force=True)
                    page.wait_for_timeout(800)

                # Resolve the Continue / Next button
                _btn_loc = page.locator('[data-test="register-button"]')
                if _btn_loc.count() > 0 and _btn_loc.first.is_visible() and _btn_loc.first.is_enabled():
                    btn = _btn_loc.first
                else:
                    btn = page.get_by_role("button", name=re.compile(
                        "Continue|Next|Confirm|Get started|继续|下一步|开始|Tiếp tục|Lanjut|"
                        "Continuar|Confirmar|Bắt đầu|Mulai|Empezar|Começar",
                        re.I
                    )).first

                if btn.is_visible() and btn.is_enabled():
                    self.log(f"Step {step}: Clicking Continue/Next button...")
                    btn.click(timeout=5000, force=True)
                    page.wait_for_timeout(1500)
                else:
                    page.wait_for_timeout(1000)
            except Exception as e:
                self.log(f"Onboarding step {step} warning/waiting: {e}")
                page.wait_for_timeout(1000)
