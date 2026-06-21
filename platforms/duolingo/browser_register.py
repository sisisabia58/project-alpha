import re
import random
from typing import Callable, Optional, List, Dict
from playwright.sync_api import Page, sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def _proxy_config(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    return {"server": proxy}

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
                "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
            }
            p = _proxy_config(self.proxy)
            if p:
                launch_opts["proxy"] = p
            
            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=UA)
            page = context.new_page()
            
            try:
                page.goto("https://www.duolingo.com/", wait_until="domcontentloaded", timeout=60000)
                
                # Check if we are already logged in or if there is a "Get started" button
                self.log("Clicking 'Get started'...")
                get_started = page.get_by_role("button", name=re.compile("Get started|开始|开始学习", re.I)).first
                get_started.click(timeout=10000)
                page.wait_for_timeout(2000)
                
                # Handle onboarding wizard
                self._click_through_onboarding(page)
                
                # Fill registration details
                self.log("Filling profile registration form...")
                age_input = page.locator('input[placeholder*="Age" i], input[type="number"]').first
                age_input.wait_for(state="visible", timeout=15000)
                age_input.fill(str(random.randint(22, 45)))
                
                name_input = page.locator('input[placeholder*="Name" i], input[placeholder*="姓名" i]').first
                if name_input.count() and name_input.is_visible():
                    name_input.fill(email.split("@")[0])
                
                page.locator('input[type="email"]').first.fill(email)
                page.locator('input[type="password"]').first.fill(password)
                
                # Submit form
                self.log("Submitting registration profile...")
                submit_btn = page.get_by_role("button", name=re.compile("Create profile|创建账号|确认|完成", re.I)).first
                submit_btn.click()
                
                # Wait for successful navigation to main page
                page.wait_for_url(re.compile(r"/learn"), timeout=45000)
                self.log("Registration completed successfully! Navigated to /learn.")

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
                "args": ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
            }
            p = _proxy_config(self.proxy)
            if p:
                launch_opts["proxy"] = p
            
            browser = pw.chromium.launch(**launch_opts)
            context = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=UA)
            
            if cookies:
                context.add_cookies(cookies)
                
            page = context.new_page()
            
            try:
                # Go to redemption page
                page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

                # Check if login is required
                if "/log-in" in page.url or "/register" in page.url or page.locator('input[type="email"]').count() > 0:
                    self.log("Stored cookies expired or invalid. Attempting direct login...")
                    page.goto("https://www.duolingo.com/log-in", wait_until="domcontentloaded", timeout=60000)
                    email_input = page.locator('input[type="email"], input[placeholder*="Email" i], input[placeholder*="邮箱" i]').first
                    email_input.wait_for(state="visible", timeout=15000)
                    email_input.fill(email)
                    
                    pass_input = page.locator('input[type="password"], input[placeholder*="Password" i], input[placeholder*="密码" i]').first
                    pass_input.fill(password)
                    
                    login_btn = page.get_by_role("button", name=re.compile("Log in|登录", re.I)).first
                    login_btn.click()
                    page.wait_for_url(re.compile(r"/learn"), timeout=45000)
                    page.goto("https://www.duolingo.com/redeem", wait_until="domcontentloaded", timeout=60000)

                # Fill and submit code
                self.log(f"Locating redeem code field and inputting: {referral_code}")
                code_input = page.locator('input[placeholder*="code" i], input[type="text"]').first
                code_input.wait_for(state="visible", timeout=15000)
                code_input.fill(referral_code)
                
                redeem_btn = page.get_by_role("button", name=re.compile("Redeem|Submit|Claim|兑换|确认", re.I)).first
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
        for step in range(15):
            try:
                # Check if we are on the final sign-up modal already
                if page.locator('input[type="email"]').count() > 0:
                    self.log("Sign-up form visible. Exiting onboarding loop.")
                    break
                
                # Check for options/selection lists and click the first option to select
                options = page.locator('button[role="radio"], ul li button, [data-test*="-card"], [data-test*="card"]').all()
                if len(options) > 0:
                    self.log(f"Step {step}: Clicking option card...")
                    options[0].click(timeout=3000, force=True)
                    page.wait_for_timeout(800)
                
                # Click Continue/Next
                btn = page.get_by_role("button", name=re.compile("Continue|Next|Confirm|Get started|继续|下一步|开始", re.I)).first
                if btn.count() and btn.is_visible() and btn.is_enabled():
                    self.log(f"Step {step}: Clicking Continue/Next button...")
                    btn.click(timeout=5000, force=True)
                    page.wait_for_timeout(1500)
                else:
                    page.wait_for_timeout(1000)
            except Exception as e:
                self.log(f"Onboarding step {step} warning/waiting: {e}")
                page.wait_for_timeout(1000)
