"""接码服务基类 + SMS-Activate 实现。"""
from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    @abstractmethod
    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        """Rent a phone number for the given service.

        Args:
            service: Service code (e.g. "cursor", "openai").
            country: Country code (e.g. "us", "ru"). Provider-specific.

        Returns:
            SmsActivation with activation_id and phone_number.
        """
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        """Wait for and return the SMS verification code.

        Args:
            activation_id: The activation ID from get_number().
            timeout: Max seconds to wait.

        Returns:
            The verification code string, or empty string on timeout.
        """
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True


# ---------------------------------------------------------------------------
# SMS-Activate implementation (https://sms-activate.guru)
# ---------------------------------------------------------------------------

# Service code mapping: platform name -> sms-activate service code
# Full list: https://sms-activate.guru/en/api2
SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",       # "Other" — Cursor doesn't have a dedicated code
    "chatgpt": "dr",      # OpenAI / ChatGPT
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

# Country code mapping: short name -> sms-activate country ID
SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "br": "73",
    "default": "0",  # Russia (cheapest)
}


class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    def __init__(self, api_key: str, *, default_country: str = ""):
        self.api_key = api_key
        self.default_country = default_country or "ru"

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(self.BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        """Get account balance."""
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = SMS_ACTIVATE_COUNTRIES.get(
            country or self.default_country,
            SMS_ACTIVATE_COUNTRIES["default"],
        )

        result = self._request("getNumber", service=service_code, country=country_id)

        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            activation_id = parts[1]
            phone = parts[2]
            return SmsActivation(
                activation_id=activation_id,
                phone_number=phone,
                country=country or self.default_country,
            )

        if "NO_NUMBERS" in result:
            raise RuntimeError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise RuntimeError("SMS-Activate: 余额不足")
        raise RuntimeError(f"SMS-Activate getNumber failed: {result}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._request("getStatus", id=activation_id)

            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                # Code was sent but needs retry
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""

            # Unknown status, keep waiting
            time.sleep(3)

        # Timeout — cancel the activation
        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config."""
    if provider_key == "sms_activate":
        api_key = config.get("sms_activate_api_key", "")
        if not api_key:
            raise RuntimeError("SMS-Activate 未配置 API Key")
        return SmsActivateProvider(
            api_key=api_key,
            default_country=config.get("sms_activate_country", ""),
        )
    raise RuntimeError(f"未知的接码服务: {provider_key}")


def create_phone_callbacks(
    provider_key: str,
    config: dict,
    *,
    service: str,
    country: str = "",
    log_fn=None,
) -> tuple:
    """Create (phone_callback, cleanup) tuple for browser registration.

    phone_callback is called twice by CursorBrowserRegister:
      1st call: returns the phone number
      2nd call: returns the SMS code

    Returns:
        (phone_callback, cleanup_fn)
    """
    log = log_fn or logger.info
    provider = create_sms_provider(provider_key, config)
    activation: Optional[SmsActivation] = None
    call_count = 0

    def phone_callback() -> str:
        nonlocal activation, call_count
        call_count += 1

        if call_count == 1:
            # First call: get a phone number
            log(f"正在从 {provider_key} 获取手机号...")
            activation = provider.get_number(service=service, country=country)
            log(f"获取到手机号: {activation.phone_number}")
            return activation.phone_number

        if call_count == 2 and activation:
            # Second call: wait for SMS code
            log("等待短信验证码...")
            code = provider.get_code(activation.activation_id, timeout=120)
            if code:
                log(f"收到验证码: {code}")
                provider.report_success(activation.activation_id)
            else:
                log("⚠️ 未收到验证码")
            return code

        return ""

    def cleanup():
        if activation:
            try:
                provider.cancel(activation.activation_id)
            except Exception:
                pass

    return phone_callback, cleanup
