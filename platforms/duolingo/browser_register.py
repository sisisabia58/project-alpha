import re
import random
from typing import Callable, Optional, List, Dict
from patchright.sync_api import Page, sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Chromium flags that prevent renderer crashes in headless / Docker / low-memory environments.
# --disable-dev-shm-usage : use /tmp instead of /dev/shm (critical on Docker)
# --disable-gpu           : disables GPU compositing; avoids GPU process crashes
# --no-zygote             : skip the zygote process to reduce crash surface
# --no-sandbox            : required when running as root (CI / Docker)
# --disable-software-rasterizer : fall back to CPU rasterizer without crashing
# --disable-extensions    : lighter renderer startup
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
    "--no-sandbox",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
]


def _proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"server": proxy}


def _attach_crash_handler(page: Page) -> None:
    """Install a listener that turns a silent renderer crash into a readable RuntimeError."""
    def _on_crash():
        raise RuntimeError(
            "Chromium renderer crashed (Page crashed). "
            "This usually means the process ran out of shared memory or GPU resources. "
            "Ensure --disable-dev-shm-usage and --disable-gpu are set."
        )
    page.on("crash", lambda p: _on_crash())

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

    def register(self, email: str, password: str) -> dict:
        self.log(f"Starting Duolingo browser registration for: {email}")
        with sync_playwright() as pw:
            launch_opts = {
                "headless": self.headless,
                "args": _LAUNCH_ARGS,
            }
            p = _proxy_config(self.proxy)
            if p:
                launch_opts["proxy"] = p

            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=UA,
            )
            page = context.new_page()
            _attach_crash_handler(page)

            try:
                # Use networkidle so Duolingo's SPA fully boots before we locate buttons.
                # Falls back to domcontentloaded if networkidle times out (slow networks).
                try:
                    page.goto("https://www.duolingo.com/", wait_until="networkidle", timeout=60000)
                except Exception:
                    self.log("networkidle timed out — continuing on domcontentloaded")
                    page.wait_for_timeout(3000)
                
                # Check if we are already logged in or if there is a "Get started" button
                self.log("Clicking 'Get started'...")
                # FIX Bug#1: call count() on the raw locator, not on .first
                # (.first always returns count==1 even when the element is absent)
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
                page.wait_for_timeout(2000)
                
                # Handle onboarding wizard
                self._click_through_onboarding(page)
                
                # Fill registration details
                self.log("Filling profile registration form...")
                # FIX Bug#3: Duolingo now uses a birthday date-picker in many variants.
                # Expand selectors to cover date inputs and data-test attributes.
                _age_selector = (
                    'input[data-test*="age" i], '
                    'input[placeholder*="Age" i], input[placeholder*="Birthday" i], '
                    'input[placeholder*="Tuổi" i], input[placeholder*="Edad" i], '
                    'input[placeholder*="Umur" i], input[placeholder*="Idade" i], input[placeholder*="年龄" i], '
                    'input[type="date"], input[type="number"][min="1"], input[type="number"]'
                )
                age_input = page.locator(_age_selector).first
                try:
                    age_input.wait_for(state="visible", timeout=15000)
                    age_input.fill(str(random.randint(22, 45)))
                except Exception:
                    # Some A/B variants omit the age/birthday field entirely — skip gracefully
                    self.log("No age/birthday input found — skipping (form variant without age field)")
                
                name_input = page.locator('input[placeholder*="Name" i], input[placeholder*="姓名" i], input[placeholder*="Tên" i], input[placeholder*="Nombre" i], input[placeholder*="Nome" i], input[placeholder*="Nama" i]').first
                if name_input.count() and name_input.is_visible():
                    name_input.fill(email.split("@")[0])
                
                page.locator('input[type="email"]').first.fill(email)
                page.locator('input[type="password"]').first.fill(password)
                
                # Submit form
                self.log("Submitting registration profile...")
                # FIX Bug#4: same count() guard fix as Bug#1 — use raw locator for count check
                _sb_loc = page.locator('[data-test="register-button"]')
                if _sb_loc.count() > 0 and _sb_loc.first.is_visible():
                    submit_btn = _sb_loc.first
                else:
                    submit_btn = page.get_by_role("button", name=re.compile(
                        "Create profile|Create account|创建账号|确认|完成|Tạo hồ sơ|Tạo tài khoản|Buat profil|Buat akun|Crear perfil|Crear cuenta|Criar perfil|Criar conta",
                        re.I
                    )).first
                submit_btn.wait_for(state="visible", timeout=10000)
                submit_btn.click()
                
                # Wait for successful navigation to main page
                # FIX Bug#5: Duolingo may redirect to /home, /email-verification or /onboarding
                # instead of /learn — broaden pattern to accept all valid post-registration URLs
                page.wait_for_url(
                    re.compile(r"/(learn|home|email-verification|onboarding)"),
                    timeout=45000,
                )
                final_url = page.url
                self.log(f"Registration completed successfully! Landed on: {final_url}")

                # Wait for email verification if requested
                if self.verification_link_callback:
                    self.log("Requesting verification email confirmation link...")
                    confirm_url = self.verification_link_callback()
                    self.log(f"Bypassing verification by navigating directly to verification link: {confirm_url}")
                    confirm_page = context.new_page()
                    confirm_page.goto(confirm_url, wait_until="domcontentloaded", timeout=60000)
                    confirm_page.wait_for_timeout(3000)
                    confirm_page.close()

                # Gather and return session cookies
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
            except Exception as e:
                self.log(f"Duolingo registration process failed: {e}")
                raise e
            finally:
                context.close()
                browser.close()

    def redeem_code(self, email: str, password: str, cookies: List[Dict], referral_code: str) -> dict:
        self.log(f"Starting Duolingo redemption for {email} with code: {referral_code}")
        with sync_playwright() as pw:
            launch_opts = {
                "headless": self.headless,
                "args": _LAUNCH_ARGS,
            }
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
            _attach_crash_handler(page)

            try:
                # Go to redemption page
                page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                # Check if login is required
                if "/log-in" in page.url or "/register" in page.url or page.locator('input[type="email"]').count() > 0:
                    self.log("Stored cookies expired or invalid. Attempting direct login...")
                    page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
                    email_input = page.locator('input[type="email"], input[placeholder*="Email" i], input[placeholder*="username" i], input[placeholder*="邮箱" i], input[placeholder*="Tên đăng nhập" i], input[placeholder*="Nama pengguna" i]').first
                    email_input.wait_for(state="visible", timeout=15000)
                    email_input.fill(email)
                    
                    pass_input = page.locator('input[type="password"], input[placeholder*="Password" i], input[placeholder*="密码" i], input[placeholder*="mật khẩu" i], input[placeholder*="kata sandi" i], input[placeholder*="contraseña" i], input[placeholder*="senha" i]').first
                    pass_input.fill(password)
                    
                    login_btn = page.get_by_role("button", name=re.compile("Log in|登录|Đăng nhập|Masuk|Iniciar sesión|Entrar", re.I)).first
                    login_btn.click()
                    page.wait_for_url(re.compile(r"/learn"), timeout=45000)
                    page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
 
                # Fill and submit code
                self.log(f"Locating redeem code field and inputting: {referral_code}")
                code_input = page.locator('input[placeholder*="code" i], input[placeholder*="mã" i], input[placeholder*="código" i], input[type="text"]').first
                code_input.wait_for(state="visible", timeout=15000)
                code_input.fill(referral_code)
                
                redeem_btn = page.get_by_role("button", name=re.compile("Redeem|Submit|Claim|兑换|确认|Áp dụng|Klaim|Canjear|Resgatar", re.I)).first
                redeem_btn.click()
                
                self.log("Waiting for code redemption response...")
                page.wait_for_timeout(5000) # Give it some time to complete API requests

                # Navigate to Settings to verify Super subscription status
                self.log("Navigating to /settings/super to verify subscription status...")
                page.goto("https://www.duolingo.com/settings/super", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                
                page_text = page.locator("body").inner_text()
                if "Super Duolingo" in page_text:
                    self.log("Super Duolingo is ACTIVE! Claims succeeded.")
                    # Get updated cookies
                    updated_cookies = context.cookies()
                    return {"success": True, "cookies": updated_cookies}
                else:
                    self.log("Super Duolingo not found under /settings/super.")
                    # Check if there is an active banner or coupon code error on the page
                    return {"success": False, "error": "Super Duolingo subscription not active after redemption."}
            except Exception as e:
                self.log(f"Redemption flow encountered error: {e}")
                return {"success": False, "error": str(e)}
            finally:
                context.close()
                browser.close()

    def _click_through_onboarding(self, page: Page):
        self.log("Handling onboarding questionnaires...")
        # FIX Bug#2: raised from 15 → 25 to cover longer A/B onboarding variants
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

                # FIX Bug#2: call count() on the raw locator, not on .first
                # Previously, btn.count() on a .first locator always returned 1,
                # so the fallback get_by_role was never reached and
                # the else-branch wasted 1 s per step doing nothing.
                _btn_loc = page.locator('[data-test="register-button"]')
                if _btn_loc.count() > 0 and _btn_loc.first.is_visible() and _btn_loc.first.is_enabled():
                    btn = _btn_loc.first
                else:
                    btn = page.get_by_role("button", name=re.compile(
                        "Continue|Next|Confirm|Get started|继续|下一步|开始|Tiếp tục|Lanjut|Continuar|Confirmar|Bắt đầu|Mulai|Empezar|Começar",
                        re.I
                    )).first

                # After resolving btn, .first always has count==1, so only check visibility/enabled
                if btn.is_visible() and btn.is_enabled():
                    self.log(f"Step {step}: Clicking Continue/Next button...")
                    btn.click(timeout=5000, force=True)
                    page.wait_for_timeout(1500)
                else:
                    page.wait_for_timeout(1000)
            except Exception as e:
                self.log(f"Onboarding step {step} warning/waiting: {e}")
                page.wait_for_timeout(1000)
