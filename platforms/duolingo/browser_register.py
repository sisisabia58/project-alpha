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
    "--disable-gpu",           # use software renderer (SwiftShader) — no hardware GPU crash
    "--no-sandbox",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--metrics-recording-only",
    "--mute-audio",
    # ── Memory / stability ─────────────────────────────────────────────────
    # NOTE: Do NOT add --disable-webgl, --disable-3d-apis, --disable-software-rasterizer
    # or --renderer-process-limit=1 here.
    # reCAPTCHA Enterprise uses WebGL canvas fingerprinting to score the browser.
    # Blocking WebGL → reCAPTCHA score ~0.1 → Duolingo silently rejects registration.
    # With --disable-gpu, WebGL runs via SwiftShader (software) which is stable
    # and gives reCAPTCHA enough signal to score us as human.
    "--disable-gpu-compositing",         # software compositing
    "--disable-accelerated-2d-canvas",   # lower 2D canvas memory
    "--disable-webgl2",                  # WebGL2 uses extra memory; WebGL1 kept for reCAPTCHA
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


_ANIMATION_KILL_SCRIPT = """
// Throttle requestAnimationFrame to ~5 fps.
// This slows Duolingo's Lottie mascot animations enough to prevent renderer
// memory spikes during modal transitions, WITHOUT touching WebGL or canvas.
// reCAPTCHA Enterprise needs WebGL/canvas for fingerprinting — do NOT block it.
(function() {
    window.requestAnimationFrame = function(cb) {
        return setTimeout(function() { cb(performance.now()); }, 200);
    };
    window.cancelAnimationFrame = clearTimeout;
})();
"""

_ANIMATION_KILL_CSS = """
*, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
}
/* canvas intentionally NOT hidden — reCAPTCHA Enterprise uses canvas fingerprinting */
"""


def _setup_page_stability(page: Page) -> None:
    """
    Inject scripts and styles that suppress Duolingo's Lottie/WebGL animations.
    Must be called before any navigation so the init script runs on every page load.
    """
    page.add_init_script(_ANIMATION_KILL_SCRIPT)
    # Also inject CSS on every navigation (covers SPA route changes)
    page.on("domcontentloaded", lambda _: _inject_css_safe(page))


def _inject_css_safe(page: Page) -> None:
    try:
        page.add_style_tag(content=_ANIMATION_KILL_CSS)
    except Exception:
        pass  # page may be navigating; non-fatal


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
                    _setup_page_stability(page)   # kill Lottie/WebGL animations

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
            _setup_page_stability(page)

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
          1. [data-test="get-started-top"] — "GET STARTED" on homepage
          2. [data-test="funboarding-continue-button"] — Onboarding steps
          3. [data-test="close-button"]    — Close onboarding modal
          4. [data-test="create-profile-juicy"] — "BUAT PROFIL"
          5. [data-test="age-input"]       — age field (Berapa umurmu?)
          6. [data-test="continue-button"] — Next / BERIKUTNYA
          7. [data-test="full-name-input"] — Nama (opsional)
          8. [data-test="email-input"]     — Email
          9. [data-test="password-input"]  — Kata sandi
         10. [data-test="register-button"] — BUAT AKUN / Create account
         11. goto /redeem                  — (if referral_code provided)
         12. textbox "Enter code"          — paste referral code
         13. button "REDEEM NOW"
         14. button "Claim offer"
        """

        # ── Step 1: Homepage ──────────────────────────────────────────────
        self.log("Navigating to Duolingo homepage...")
        page.goto("https://www.duolingo.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)  # Allow Duolingo JS to fully initialise
        self.log(f"Landed on: {page.url}")

        # ── Steps 2–8: Onboarding state machine ──────────────────────────
        # Duolingo's onboarding flow varies by A/B test variant. Instead of
        # a rigid step sequence, we use a state machine: at each iteration
        # we check what's visible and take the right action, until we reach
        # the age-input (terminal state).
        #
        # WHY GET STARTED is required: clicking it (and advancing through the
        # onboarding) triggers Duolingo's guest user creation which sets the
        # jwt_token cookie. The registration POST to /2023-05-23/users returns
        # 401 without this JWT.

        self.log("Clicking GET STARTED (initialises guest session / JWT)...")
        _get_started = page.locator('[data-test="get-started-top"]').first
        _get_started.wait_for(state="visible", timeout=15000)
        _get_started.click(timeout=8000)
        page.wait_for_timeout(2000)

        self.log("Advancing through onboarding (state machine)...")
        _buat_profil_clicked = False
        for _sn in range(30):
            page.wait_for_timeout(1200)

            # Terminal: age form
            if page.locator('[data-test="age-input"]').is_visible():
                self.log(f"[{_sn}] Reached age form")
                break
            # Terminal: registration form (rare)
            if page.locator('[data-test="full-name-input"]').is_visible():
                self.log(f"[{_sn}] Already at registration form")
                break

            _acted = False

            # P-1: If we drifted onto /learn before registration, click the
            # Duolingo in-page X/back button (top-left corner of page).
            # The button contains a broken <img>, so we use JS to traverse
            # up to the real clickable container and dispatch a click event.
            if not _acted and not _buat_profil_clicked and "/learn" in page.url:
                self.log(f"[{_sn}] On /learn placement test — clicking back button via JS...")
                try:
                    _js_result = page.evaluate("""() => {
                        // Check a few candidate positions for the back button
                        for (const [cx, cy] of [[27,27],[32,32],[20,20],[15,15]]) {
                            let el = document.elementFromPoint(cx, cy);
                            // Traverse up to find the nearest clickable ancestor
                            let node = el;
                            while (node && node !== document.body) {
                                if (node.tagName === 'BUTTON' || node.tagName === 'A' ||
                                    node.getAttribute('role') === 'button' ||
                                    node.getAttribute('role') === 'link') {
                                    node.dispatchEvent(
                                        new MouseEvent('click', {bubbles:true, cancelable:true})
                                    );
                                    return node.tagName + '@' + cx + ',' + cy;
                                }
                                node = node.parentElement;
                            }
                        }
                        // Fallback: click the very first button in the DOM
                        const first = document.querySelector('button');
                        if (first) { first.click(); return 'first-button'; }
                        return 'none';
                    }""")
                    self.log(f"[{_sn}] JS back click result: {_js_result}")
                except Exception as _e:
                    self.log(f"[{_sn}] JS back click failed: {_e}")
                    # Last resort: Alt+Left keyboard shortcut (browser back)
                    try:
                        page.keyboard.press("Alt+ArrowLeft")
                    except Exception:
                        pass
                page.wait_for_timeout(2000)
                _acted = True

            # P0: Cookie consent / notification banners (highest priority)
            # These overlays block all other clicks if not dismissed first.
            if not _acted:
                for _dismiss_txt in [
                    "accept cookies", "reject all", "accepteer", "i agree",
                    "tolak semua", "terima semua", "setuju",
                ]:
                    _cb = page.get_by_role(
                        "button", name=re.compile(_dismiss_txt, re.I)
                    ).first
                    if _cb.is_visible():
                        self.log(f"[{_sn}] Dismissing banner ('{_dismiss_txt}') -> click")
                        _cb.click()
                        page.wait_for_timeout(1000)
                        _acted = True
                        break

            # P1: BUAT PROFIL (creates jwt_token)
            # Click ONCE with force=True to bypass overlay, then wait for age form.
            # Subsequent iterations skip P1 so we don't spam-click while the
            # page transitions from the overlay state to the age form.
            if not _acted and not _buat_profil_clicked:
                _bp = page.locator('[data-test="create-profile-juicy"]').first
                if _bp.is_visible():
                    self.log(f"[{_sn}] BUAT PROFIL -> force-click (waiting for age form...)")
                    try:
                        _bp.click(force=True, timeout=5000)
                    except Exception:
                        page.evaluate(
                            "document.querySelector('[data-test=\"create-profile-juicy\"]').click()"
                        )
                    _buat_profil_clicked = True
                    page.wait_for_timeout(4000)  # Allow transition to age form
                    _acted = True
            elif not _acted and _buat_profil_clicked:
                # Already clicked — just wait; age form will appear
                _acted = True  # suppress "Stuck" log; we're just waiting

            # P2: Language card
            if not _acted:
                _lc = page.locator('[data-test*="language-card"]').first
                if _lc.is_visible():
                    self.log(f"[{_sn}] Language card -> click")
                    _lc.click()
                    page.wait_for_timeout(1500)
                    _acted = True

            # P3: Funboarding continue
            if not _acted:
                _fc = page.locator('[data-test="funboarding-continue-button"]').first
                if _fc.is_visible() and _fc.is_enabled():
                    self.log(f"[{_sn}] Funboarding continue -> click")
                    _fc.click()
                    page.wait_for_timeout(1500)
                    _acted = True
                elif _fc.is_visible() and not _fc.is_enabled():
                    # Disabled continue: need to select a goal/reason card first
                    for _opt_sel in [
                        '[data-test*="reason"]', '[data-test*="goal"]',
                        '[data-test*="motivation"]', '[data-test*="option"]',
                    ]:
                        _opt = page.locator(_opt_sel).first
                        if _opt.is_visible():
                            self.log(f"[{_sn}] Option card (enables continue) -> click")
                            _opt.click()
                            page.wait_for_timeout(800)
                            _acted = True
                            break

            # P4: Close button (dismiss any modal to advance state)
            if not _acted:
                _cl = page.locator('[data-test="close-button"]').first
                if _cl.is_visible():
                    self.log(f"[{_sn}] Close modal -> click")
                    _cl.click()
                    page.wait_for_timeout(1500)
                    _acted = True

            # P4.5: Survey / choice pages — CONTINUE visible but disabled.
            # Two sub-cases:
            #   a) Icon-only option buttons (hdyhau survey)
            #   b) Text option buttons (proficiency: "I'm new to Spanish", etc.)
            if not _acted:
                _any_cont = page.get_by_role(
                    "button", name=re.compile(r"continue|berikutnya|lanjutkan", re.I)
                ).first
                if _any_cont.is_visible() and not _any_cont.is_enabled():
                    # (a) Icon-only buttons first
                    for _ib in page.get_by_role("button").filter(
                        has_text=re.compile(r"^\s*$")
                    ).all():
                        if _ib.is_visible():
                            self.log(f"[{_sn}] Survey option (icon) -> click")
                            _ib.click()
                            page.wait_for_timeout(800)
                            _acted = True
                            break
                    # (b) Text option buttons (not the continue button itself)
                    if not _acted:
                        _skip_re = re.compile(r"continue|berikutnya|lanjutkan|back|kembali", re.I)
                        for _tb in page.locator("button").all():
                            if not _tb.is_visible() or not _tb.is_enabled():
                                continue
                            _t = (_tb.inner_text() or "").strip()
                            if _t and not _skip_re.match(_t):
                                self.log(f"[{_sn}] Text option '{_t[:30]}' -> click")
                                _tb.click()
                                page.wait_for_timeout(800)
                                _acted = True
                                break

            # P5: Generic continue/next by button text
            if not _acted:
                for _txt in ["lanjutkan", "continue", "next", "selanjutnya", "berikutnya"]:
                    _gb = page.get_by_role("button", name=re.compile(_txt, re.I)).first
                    if _gb.is_visible() and _gb.is_enabled():
                        self.log(f"[{_sn}] '{_txt}' button -> click")
                        _gb.click()
                        page.wait_for_timeout(1500)
                        _acted = True
                        break

            if not _acted:
                try:
                    _vis = [b.inner_text()[:30] for b in page.locator("button").all()
                            if b.is_visible()]
                    if not _vis:
                        # No visible buttons at all — page may be loading or stuck
                        # on a screen with non-button elements. Go back as last resort.
                        self.log(f"[{_sn}] No visible buttons at {page.url} — going back...")
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)
                        _acted = True
                    else:
                        self.log(f"[{_sn}] Stuck. Buttons: {_vis} | URL: {page.url}")
                except Exception:
                    pass
        else:
            raise RuntimeError("Could not reach age form after 30 onboarding steps")

        # Log JWT status
        try:
            _jwt_c = {c["name"]: c["value"][:40] for c in context.cookies()
                      if c["name"] in ("jwt_token", "duo_jwt", "logged_out_uuid")}
            self.log(f"Session cookies at age form: {_jwt_c}")
        except Exception:
            pass

        # Age form
        self.log("Filling age form...")
        _age = page.locator('[data-test="age-input"]')
        _age_val = str(random.randint(22, 35))
        _age.fill(_age_val)
        self.log(f"Age: {_age_val}")

        _cont = page.locator('[data-test="continue-button"]')
        for _ in range(20):
            if _cont.is_enabled():
                break
            page.wait_for_timeout(300)
        _cont.click(timeout=8000)
        self.log("Age continue clicked.")
        page.wait_for_timeout(2000)

        # ── Steps 7–9: Registration form ───────────────────────────────────
        # Modal: Buat profilmu / Create your profile
        # Reached after: GET STARTED → onboarding → close → BUAT PROFIL → age → continue
        self.log("Filling registration form...")

        _name = page.locator('[data-test="full-name-input"]')
        _name.wait_for(state="visible", timeout=20000)
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

        # Intercept the registration API request AND response to diagnose 401
        _api_responses: list[dict] = []
        _api_requests:  list[dict] = []

        def _on_request(req):
            try:
                if "2023-05-23/users" in req.url or "api/1/user" in req.url:
                    _api_requests.append({
                        "url":     req.url,
                        "method":  req.method,
                        "headers": dict(req.headers),
                    })
            except Exception:
                pass

        def _on_response(resp):
            try:
                if "2023-05-23/users" in resp.url or "api/1/user" in resp.url:
                    try:
                        body = resp.text()
                    except Exception:
                        body = "(could not read body)"
                    _api_responses.append({
                        "url":    resp.url,
                        "status": resp.status,
                        "body":   body[:500],
                    })
            except Exception:
                pass

        page.on("request",  _on_request)
        page.on("response", _on_response)

        # Log cookies present just before clicking — 401 usually means a
        # session/CSRF cookie is missing that should have been set during
        # the sign-up flow.
        try:
            _cookies_before = {c["name"]: c["value"][:40] for c in context.cookies()
                               if c["name"] in (
                                   "jwt_token", "csrf_token", "__cf_bm",
                                   "logged_out_uuid", "session", "duo_auth",
                               )}
            self.log(f"Auth cookies before submit: {_cookies_before}")
        except Exception:
            pass

        _reg_btn.click()
        self.log("Register button clicked — waiting for API response...")

        # Duolingo's registration API sets auth cookies immediately on success
        # but does NOT always trigger a hard browser navigation.
        page.wait_for_timeout(4000)

        # ── Log captured API responses (definitive diagnosis) ─────────────
        if _api_responses:
            for _r in _api_responses:
                self.log(
                    f"Registration API {_r['status']} {_r['url'][:80]} "
                    f"→ {_r['body']}"
                )
            # ── Fail fast on API 4xx / 5xx ─────────────────────────────────
            # A 403 means Duolingo rejected the registration (bot detection,
            # expired JWT, or IP block). Don't proceed — this is NOT a success.
            for _r in _api_responses:
                if _r['status'] in (403, 401, 429, 500):
                    raise RuntimeError(
                        f"Registration API returned {_r['status']} — "
                        f"account was NOT created. Response: {_r['body'][:200]}"
                    )
        if _api_requests:
            for _req in _api_requests:
                _hdrs = {k: v[:60] for k, v in _req["headers"].items()
                         if k.lower() in ("authorization", "x-csrf-token", "cookie",
                                          "origin", "referer", "content-type")}
                self.log(f"Registration REQ headers: {_hdrs}")
        else:
            self.log(
                "WARNING: No registration API call was captured — "
                "request may have been blocked client-side (reCAPTCHA, disabled button, etc.)"
            )

        # ── Detect form validation errors ─────────────────────────────────
        _form_errors = []
        try:
            for _err_sel in [
                '[data-test="registration-error"]',
                '[data-test*="error"]',
                '[role="alert"]',
                'span[class*="text-red" i]',
                'div[class*="error" i]',
            ]:
                for _el in page.locator(_err_sel).all():
                    try:
                        if _el.is_visible():
                            _txt = (_el.inner_text() or "").strip()
                            if _txt and len(_txt) > 2:
                                _form_errors.append(_txt[:120])
                    except Exception:
                        pass
        except Exception:
            pass

        if _form_errors:
            # Log them for diagnosis then fail clearly
            self.log(f"Form validation errors: {_form_errors}")
            raise RuntimeError(f"Registration rejected by Duolingo: {_form_errors}")

        # ── Log page state for diagnosis ──────────────────────────────────
        try:
            _btns = [_b.inner_text()[:30] for _b in page.locator("button").all() if _b.is_visible()]
            self.log(f"Post-submit URL: {page.url} | visible buttons: {_btns}")
        except Exception:
            pass

        # ── Navigate to /learn to confirm auth cookies were set ───────────
        self.log("Navigating to /learn to confirm registration...")
        page.goto(
            "https://www.duolingo.com/learn",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(2000)

        final_url = page.url
        if not re.search(r"duolingo\.com/(learn|home)", final_url):
            raise RuntimeError(
                f"Registration may have failed — /learn redirected to: {final_url}. "
                f"Check form errors in logs above."
            )

        # Extra guard: if SIGN IN / CREATE ACCOUNT buttons are visible on /learn,
        # the browser is in GUEST mode — the registration did not persist.
        try:
            _guest_indicators = [
                page.get_by_role("button", name=re.compile(r"sign.?in|create.*account|create.*profile", re.I)).first,
                page.locator('[data-test="have-account"]').first,
                page.locator('[data-test="get-started-top"]').first,
            ]
            for _gi in _guest_indicators:
                if _gi.is_visible():
                    raise RuntimeError(
                        "Landed on /learn but browser is in GUEST mode "
                        "(login buttons visible) — registration was not saved. "
                        f"URL: {final_url}"
                    )
        except RuntimeError:
            raise
        except Exception:
            pass  # indicator check failed non-fatally

        self.log(f"Registration SUCCESS! Landed on: {final_url}")

        # ── Email verification (optional / non-fatal) ─────────────────────
        # Duolingo accounts are active immediately after the registration API
        # returns 200. Email verification is not required for the account to
        # work — skip gracefully if the callback times out or is unavailable.
        if self.verification_link_callback:
            self.log("Requesting email verification link...")
            try:
                confirm_url = self.verification_link_callback()
                if confirm_url:
                    self.log(f"Navigating to verification link: {confirm_url}")
                    confirm_page = context.new_page()
                    _block_heavy_resources(confirm_page)
                    confirm_page.goto(confirm_url, wait_until="domcontentloaded", timeout=60000)
                    confirm_page.wait_for_timeout(3000)
                    confirm_page.close()
                else:
                    self.log("No verification link returned — skipping (account is already active)")
            except Exception as _ve:
                self.log(f"Email verification skipped (non-fatal): {_ve}")

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
