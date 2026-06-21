import random
import string
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, LinkSpec, RegistrationResult
from core.registry import register

@register
class DuolingoPlatform(BasePlatform):
    name = "duolingo"
    display_name = "Duolingo"
    version = "1.0.0"
    
    supported_executors = ["headless", "headed"]
    supported_identity_modes = ["mailbox"]
    
    capabilities = ["query_state", "redeem_code"]
    
    def get_platform_actions(self) -> list:
        return [
            {"id": "query_state", "label": "查询账号状态", "params": []},
            {
                "id": "redeem_code",
                "label": "Redeem Trial Code",
                "params": [
                    {"key": "referral_code", "label": "Referral / Redeem Code", "type": "text"},
                ],
                "sync": False,
            }
        ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str:
        if password:
            return password
        # Duolingo requires: ≥8 chars, at least 1 uppercase, 1 number or symbol
        upper = random.choice(string.ascii_uppercase)
        digits = random.choices(string.digits, k=2)
        symbols = random.choices("!@#$%", k=1)
        rest = random.choices(string.ascii_letters + string.digits, k=9)
        chars = [upper] + digits + symbols + rest
        random.shuffle(chars)
        return "".join(chars)

    def _map_duolingo_result(self, result: dict) -> RegistrationResult:
        status_val = result.get("status", "registered")
        try:
            status = AccountStatus(status_val)
        except Exception:
            status = AccountStatus.REGISTERED
            
        return RegistrationResult(
            email=result["email"],
            password=result["password"],
            token=result.get("session_token", ""),
            status=status,
            extra={
                "cookies": result.get("cookies", []),
                "localStorage": result.get("localStorage", {}),
            }
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_duolingo_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.duolingo.browser_register", fromlist=["DuolingoBrowserRegister"]
            ).DuolingoBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                verification_link_callback=artifacts.verification_link_callback,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.register(
                email=ctx.identity.email,
                password=ctx.password,
                referral_code=getattr(ctx, "referral_code", None),
            ),
            link_spec=LinkSpec(
                keyword="duolingo",
                wait_message="Waiting for Duolingo verification email...",
                success_label="Verification Link"
            ),
        )

    def check_valid(self, account: Account) -> bool:
        # If the account has session cookies, we assume valid for check_valid fallback, or check user profile.
        # Simple local/token check for baseline validation.
        return bool(account.email and (account.password or account.extra.get("cookies")))

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "redeem_code":
            from platforms.duolingo.browser_register import DuolingoBrowserRegister
            referral_code = str(params.get("referral_code") or "").strip()
            if not referral_code:
                return {"ok": False, "error": "Referral code is required"}

            worker = DuolingoBrowserRegister(
                headless=(self.config.executor_type == "headless"),
                proxy=self.config.proxy if self.config else None,
                log_fn=self.log
            )
            result = worker.redeem_code(
                email=account.email,
                password=account.password,
                cookies=account.extra.get("cookies", []),
                referral_code=referral_code
            )
            if result.get("success"):
                account.status = AccountStatus.SUBSCRIBED
                # Update cookies
                if "cookies" in result:
                    account.extra["cookies"] = result["cookies"]
                return {"ok": True, "data": {"message": "Successfully claimed Super Duolingo!", "status": "subscribed"}}
            return {"ok": False, "error": result.get("error", "Failed to redeem code")}

        raise NotImplementedError(f"Unknown action: {action_id}")
