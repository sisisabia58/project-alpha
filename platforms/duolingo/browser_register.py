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
# --no-zygote omitted — no-op on Windows, can harm Linux renderer stability.
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
# Resource blocking — images/media/fonts are the memory hogs.
# stylesheet is intentionally NOT blocked: Duolingo uses CSS classes to
# control visibility; without CSS all locator.wait_for(visible) calls hang.
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
    """Block resource types / URLs that don't help registration but eat memory."""
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
                    _block_heavy_resources(page)

                    try:
                        result = self._do_register(page, context, email, password)
                        return result
                    except Exception as exc:
                        err = str(exc)
                        if "crashed" in err.lower() or "page crash" in err.lower():
                            self.log(f"Attempt {attempt}: Page crashed — retrying. ({err})")
                            last_error = exc
                            continue
                        self.log(f"Duolingo registration process failed: {exc}")
                        raise
                finally:
                    try:
                        if browser:
                            browser.close()
                    except Exception:
                        pass

        raise RuntimeError(
            f"Duolingo registration failed after {max_attempts} attempts. "
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
                page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                if "/log-in" in page.url or "/register" in page.url or page.locator('input[type="email"]').count() > 0:
                    self.log("Stored cookies expired — logging in directly...")
                    page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
                    page.locator(
                        'input[type="email"], input[placeholder*="Email" i], input[placeholder*="username" i]'
                    ).first.fill(email)
                    page.locator('input[type="password"]').first.fill(password)
                    page.get_by_role("button", name=re.compile(
                        "Log in|登录|Đăng nhập|Masuk|Iniciar sesión|Entrar", re.I
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
                    "Redeem|Submit|Claim|兑换|Áp dụng|Klaim|Canjear|Resgatar", re.I
                )).first.click()
                page.wait_for_timeout(5000)

                page.goto("https://www.duolingo.com/settings/super", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                if "Super Duolingo" in page.locator("body").inner_text():
                    self.log("Super Duolingo is ACTIVE!")
                    return {"success": True, "cookies": context.cookies()}
                else:
                    return {"success": False, "error": "Super Duolingo not active after redemption."}
            except Exception as e:
                self.log(f"Redemption flow error: {e}")
                return {"success": False, "error": str(e)}
            finally:
                context.close()
                browser.close()

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _do_register(self, page: Page, context: BrowserContext, email: str, password: str) -> dict:
        """
        Execute the full Duolingo registration flow.

        Confirmed flow (from user screenshots, June 2025):
          1. en.duolingo.com          → homepage  → click GET STARTED
          2. /register                → "I want to learn" language cards → click one
          3. /register                → "What language do you speak?" list → click one
          4. /welcome                 → Duo mascot animation (multiple screens) → click LANJUTKAN
          5. /welcome?welcomeStep=... → "Dari mana kamu tahu?" option grid → click one → LANJUTKAN
          6. /learn                   → "Berapa banyak?" modal → click ✕ to close
          7. /learn                   → main page → click BUAT PROFIL
          8. /learn?isLoggingIn=true  → "Berapa umurmu?" age field → fill + click BERIKUTNYA
          9. /learn?isLoggingIn=true  → "Buat profilmu" form → fill Nama/Email/Kata sandi → BUAT AKUN
        """
        # ── Navigate to /register ─────────────────────────────────────────
        self.log("Navigating to Duolingo registration page...")
        page.goto("https://www.duolingo.com/register", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        self.log(f"Landed on: {page.url}")

        # If redirected to homepage, click GET STARTED
        if "/register" not in page.url and "/onboarding" not in page.url:
            self.log("Redirected to homepage — clicking GET STARTED...")
            _gs = page.locator('[data-test="get-started"]')
            if _gs.count() > 0 and _gs.first.is_visible():
                _gs.first.click(timeout=10000)
            else:
                _gs2 = page.get_by_role("button", name=re.compile(
                    r"Get started|开始|开始学习|Bắt đầu|Mulai|Empezar|Começar", re.I
                )).first
                try:
                    _gs2.wait_for(state="visible", timeout=10000)
                    _gs2.click(timeout=10000)
                except Exception:
                    self.log("GET STARTED not found — continuing anyway")
            page.wait_for_timeout(1500)

        # ── PHASE 1: Click through onboarding wizard until /learn ─────────
        # Handles: language grid, native language list, /welcome animation
        # screens, /welcome?welcomeStep=hdyhau "where did you hear" screen.
        self._click_through_onboarding(page)
        self.log(f"Onboarding complete — now on: {page.url}")

        # ── PHASE 2: Close the knowledge-level modal on /learn ────────────
        # Screenshot: "Berapa banyak bahasa Inggris yang kamu tahu?" popup
        # with an ✕ button at top-left. Must be dismissed before BUAT PROFIL.
        self.log("Phase 2: Closing level-selection modal...")
        _closed = False
        for _dt_close in ["close-button", "modal-close", "dismiss-button"]:
            _cl = page.locator(f'[data-test="{_dt_close}"]')
            if _cl.count() > 0:
                try:
                    _cl.first.wait_for(state="visible", timeout=1500)
                    _cl.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    _closed = True
                    break
                except Exception:
                    pass

        if not _closed:
            for _b in page.locator("button").all():
                try:
                    _txt = (_b.inner_text() or "").strip()
                    if _txt in ("×", "✕", "✗", "X", "x", "❌") and _b.is_visible():
                        self.log(f"Closing modal via '{_txt}' button...")
                        _b.click(timeout=3000)
                        page.wait_for_timeout(1000)
                        _closed = True
                        break
                except Exception:
                    continue

        if not _closed:
            for _aria in ['button[aria-label*="close" i]', 'button[aria-label*="tutup" i]']:
                _cl = page.locator(_aria)
                if _cl.count() > 0 and _cl.first.is_visible():
                    try:
                        _cl.first.click(timeout=3000)
                        page.wait_for_timeout(1000)
                        _closed = True
                        break
                    except Exception:
                        pass

        if not _closed:
            self.log("No close button found for level modal — continuing anyway")

        page.wait_for_timeout(1500)

        # ── PHASE 3: Click "BUAT PROFIL" on the /learn main page ─────────
        # Screenshot: right panel shows "Buat profil untuk menyimpan progresmu!"
        # green "BUAT PROFIL" button + blue "MASUK" button.
        self.log("Phase 3: Clicking BUAT PROFIL...")
        _buat_profil = page.get_by_role("button", name=re.compile(
            r"Buat profil|Create profile|Create account|Sign up|Daftar|"
            r"Tạo hồ sơ|Tạo tài khoản|Criar perfil|Crear perfil",
            re.I
        )).first
        try:
            _buat_profil.wait_for(state="visible", timeout=10000)
            _buat_profil.click(timeout=5000)
            self.log("BUAT PROFIL clicked.")
            page.wait_for_timeout(2000)
        except Exception as e:
            self.log(f"BUAT PROFIL not found ({e}) — checking if age form already appeared")

        # ── PHASE 4: Age form ─────────────────────────────────────────────
        # URL: /learn?isLoggingIn=true
        # Screenshot: "Berapa umurmu?" | input placeholder "Umur" | button "BERIKUTNYA"
        self.log("Phase 4: Filling age form (Berapa umurmu?)...")
        _age_input = page.locator(
            'input[placeholder*="Umur" i], '
            'input[placeholder*="Age" i], '
            'input[placeholder*="Tuổi" i], '
            'input[placeholder*="Edad" i], '
            'input[data-test*="age" i], '
            'input[type="number"]'
        ).first
        try:
            _age_input.wait_for(state="visible", timeout=8000)
            _age_val = str(random.randint(22, 35))
            _age_input.fill(_age_val)
            self.log(f"Filled age: {_age_val}")

            # BERIKUTNYA = Next in Indonesian — enabled after a valid age is typed
            _next_btn = page.get_by_role("button", name=re.compile(
                r"Berikutnya|Next|Selanjutnya|Continue|Lanjut|Tiếp theo|Siguiente",
                re.I
            )).first
            for _ in range(10):
                if _next_btn.is_enabled():
                    break
                page.wait_for_timeout(400)
            _next_btn.click(timeout=5000)
            self.log("BERIKUTNYA clicked.")
            page.wait_for_timeout(2000)
        except Exception as e:
            self.log(f"Age form: {e} — continuing to registration form")

        # ── PHASE 5: Registration form ────────────────────────────────────
        # URL: /learn?isLoggingIn=true  (same URL, different modal)
        # Screenshot fields: Nama (opsional) | Email | Kata sandi
        # Submit button: BUAT AKUN
        self.log("Phase 5: Filling registration form (Buat profilmu)...")

        # Wait for the Email field (placeholder "Email" in all locales on this form)
        _email_input = page.locator(
            'input[placeholder="Email"], '
            'input[placeholder*="Email" i], '
            'input[type="email"], '
            'input[autocomplete*="email" i], '
            'input[name*="email" i]'
        ).first
        try:
            _email_input.wait_for(state="visible", timeout=15000)
        except Exception as e:
            # Diagnostic log before failing
            try:
                _btns = [_b.inner_text()[:40] for _b in page.locator("button").all() if _b.is_visible()]
                _inps = [(_i.get_attribute("placeholder") or _i.get_attribute("type") or "?")
                         for _i in page.locator("input").all() if _i.is_visible()]
                self.log(f"Diagnosis — URL:{page.url} | buttons:{_btns} | inputs:{_inps}")
            except Exception:
                pass
            raise RuntimeError(
                f"Email input not found on {page.url}. "
                f"Duolingo may have changed its registration flow. Error: {e}"
            )

        # Fill Nama (opsional) if visible
        _name_input = page.locator(
            'input[placeholder*="Nama" i], input[placeholder*="Name" i]'
        ).first
        try:
            if _name_input.is_visible():
                _name_input.fill(email.split("@")[0])
                self.log(f"Filled name: {email.split('@')[0]}")
        except Exception:
            pass

        # Fill Email
        _email_input.fill(email)
        self.log(f"Filled email: {email}")

        # Fill Kata sandi (Password)
        _pass_input = page.locator(
            'input[type="password"], '
            'input[placeholder*="Kata sandi" i], '
            'input[placeholder*="Password" i]'
        ).first
        _pass_input.fill(password)
        self.log("Filled password.")

        # Click BUAT AKUN (Create Account)
        self.log("Clicking BUAT AKUN...")
        _buat_akun = page.get_by_role("button", name=re.compile(
            r"Buat akun|Create account|Sign up|Register|Daftar|"
            r"Tạo tài khoản|Criar conta|Crear cuenta|注册",
            re.I
        )).first
        _buat_akun.wait_for(state="visible", timeout=10000)
        _buat_akun.click()

        # ── Wait for post-registration landing ────────────────────────────
        # After BUAT AKUN, Duolingo redirects to /learn (no isLoggingIn param)
        self.log("Waiting for registration to complete...")
        page.wait_for_url(
            re.compile(r"duolingo\.com/(learn$|learn\?(?!isLoggingIn)|home|email-verification|onboarding)"),
            timeout=45000,
        )
        final_url = page.url
        self.log(f"Registration completed successfully! Landed on: {final_url}")

        # Email verification (optional)
        if self.verification_link_callback:
            self.log("Requesting verification link...")
            confirm_url = self.verification_link_callback()
            self.log(f"Navigating to verification link: {confirm_url}")
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
        """
        Click through the Duolingo onboarding wizard until we reach /learn.

        Confirmed screens (Phase 1 + Phase 2 screenshots):
          - /register  : "I want to learn" language grid → click card → (no continue needed, auto-advances)
          - /register  : "What language do you speak?" list → click li item
          - /welcome   : Duo mascot animations → click LANJUTKAN (multiple times)
          - /welcome?welcomeStep=hdyhau : "Dari mana kamu tahu?" option grid
                         → click any option → click LANJUTKAN
          - /learn     : level modal appears → EXIT LOOP (Phase 2 handles it)
        """
        self.log("Handling onboarding questionnaires...")
        prev_url = page.url
        stall_steps = 0

        for step in range(40):
            try:
                current_url = page.url

                # ── Exit when we reach the main app ──────────────────────────
                # /learn is the Duolingo main page — onboarding is complete.
                # The level-selection modal on /learn is handled by _do_register.
                if "/learn" in current_url:
                    self.log(f"Step {step}: Reached /learn — onboarding complete.")
                    break

                # Also exit if email input is already visible (rare fast-track)
                if page.locator('input[type="email"]').count() > 0:
                    self.log("Email input visible — exiting onboarding loop.")
                    break

                # Log URL changes for debugging
                if current_url != prev_url:
                    self.log(f"Step {step}: → {current_url}")
                    prev_url = current_url
                    stall_steps = 0
                    page.wait_for_timeout(2000)  # let React settle after navigation

                # ── Select an option card or list item ────────────────────────
                # Covers: language grid cards, native language list, "where did
                # you hear" option buttons, level-selection list items.
                clicked_option = False
                for sel in [
                    'button[role="radio"]',
                    '[data-test*="-card"]:not([data-test*="register"])',
                    '[data-test*="card"]:not([data-test*="register"])',
                    'ul li a',         # native language list: <ul><li><a>
                    'ul li button',    # some option lists: <ul><li><button>
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
                    page.wait_for_timeout(400)

                # ── Find and click Continue / LANJUTKAN ───────────────────────
                clicked_continue = False

                # Priority 1: data-test attributes
                for dt in ["register-button", "onboarding-next", "continue-button",
                           "next-button", "submit-button"]:
                    loc = page.locator(f'[data-test="{dt}"]')
                    if loc.count() > 0:
                        try:
                            loc.first.wait_for(state="visible", timeout=1500)
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

                # Priority 2: role=button with known labels
                # LANJUTKAN = Indonesian "Continue" (confirmed in Phase 1 screenshots)
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

                # Priority 3: any visible enabled button with text > 3 chars
                if not clicked_continue:
                    _skip_labels = {"", "x", "close", "skip", "×", "✕", "masuk", "log in"}
                    for b in page.locator("button").all():
                        try:
                            if b.is_visible() and b.is_enabled():
                                label = (b.inner_text() or "").strip()
                                if label and label.lower() not in _skip_labels and len(label) > 3:
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
