"""动态代理 IP 提供者 — 从第三方 API 动态获取代理。

支持两种模式:
  1. 静态代理: 从数据库读取固定代理列表（现有逻辑）
  2. 动态代理: 从第三方 API 实时获取代理 IP

动态代理 provider 通过 provider_settings 配置，和邮箱/验证码 provider 一样
在前端"全局配置"页管理。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BaseProxyProvider(ABC):
    """动态代理提供者基类。"""

    @abstractmethod
    def get_proxy(self) -> Optional[str]:
        """获取一个代理 URL，格式: http://host:port 或 http://user:pass@host:port。
        返回 None 表示无可用代理。"""
        ...


class ApiExtractProvider(BaseProxyProvider):
    """通用 API 提取模式 — 调用一个 URL 返回代理 IP 列表。

    适用于大多数代理商的"API 提取"接口，返回格式通常是:
      - 每行一个 IP:PORT
      - 或 JSON 数组
    """

    def __init__(
        self,
        *,
        api_url: str,
        protocol: str = "http",
        username: str = "",
        password: str = "",
        timeout: int = 10,
    ):
        self.api_url = api_url
        self.protocol = protocol or "http"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._cache: list[str] = []
        self._lock = threading.Lock()

    def _fetch(self) -> list[str]:
        """从 API 获取代理列表。"""
        try:
            resp = requests.get(self.api_url, timeout=self.timeout)
            resp.raise_for_status()
            text = resp.text.strip()
        except Exception as exc:
            logger.warning(f"[ProxyProvider] API 请求失败: {exc}")
            return []

        # Try JSON first
        try:
            import json
            data = json.loads(text)
            if isinstance(data, list):
                return [self._normalize(str(item)) for item in data if item]
            if isinstance(data, dict):
                # Common patterns: {"data": [...], "proxies": [...], "list": [...]}
                for key in ("data", "proxies", "list", "proxy_list", "result"):
                    items = data.get(key)
                    if isinstance(items, list):
                        return [self._normalize(str(item)) for item in items if item]
        except (json.JSONDecodeError, ValueError):
            pass

        # Fall back to line-by-line parsing
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return [self._normalize(line) for line in lines if self._looks_like_proxy(line)]

    def _looks_like_proxy(self, line: str) -> bool:
        """Check if a line looks like a proxy address."""
        # Match IP:PORT, HOST:PORT, or protocol://... patterns
        if line.startswith(("http://", "https://", "socks5://", "socks4://")):
            return True
        return bool(re.match(r'^[\w.\-]+:\d+', line))

    def _normalize(self, raw: str) -> str:
        """Normalize a raw proxy string to a full URL."""
        raw = raw.strip()
        if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
            return raw
        # Add protocol and optional auth
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{raw}"
        return f"{self.protocol}://{raw}"

    def get_proxy(self) -> Optional[str]:
        with self._lock:
            if not self._cache:
                self._cache = self._fetch()
            if self._cache:
                return self._cache.pop(0)
        return None


class RotatingProxyProvider(BaseProxyProvider):
    """固定入口旋转代理 — 每次请求自动分配不同 IP。

    适用于提供固定网关地址的代理商（如 BrightData、Oxylabs、IPRoyal 等），
    格式通常是: http://user:pass@gate.provider.com:port
    每次通过该网关发出的请求会自动使用不同的出口 IP。
    """

    def __init__(self, *, gateway_url: str):
        self.gateway_url = gateway_url

    def get_proxy(self) -> Optional[str]:
        return self.gateway_url if self.gateway_url else None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_proxy_provider(provider_key: str, config: dict) -> BaseProxyProvider:
    """根据 provider_key 和配置创建代理提供者。"""
    if provider_key == "api_extract":
        api_url = config.get("proxy_api_url", "")
        if not api_url:
            raise RuntimeError("动态代理未配置 API URL")
        return ApiExtractProvider(
            api_url=api_url,
            protocol=config.get("proxy_protocol", "http"),
            username=config.get("proxy_username", ""),
            password=config.get("proxy_password", ""),
        )

    if provider_key == "rotating_gateway":
        gateway = config.get("proxy_gateway_url", "")
        if not gateway:
            raise RuntimeError("旋转代理未配置网关地址")
        return RotatingProxyProvider(gateway_url=gateway)

    raise RuntimeError(f"未知的代理 provider: {provider_key}")


def get_dynamic_proxy(extra: dict | None = None) -> Optional[str]:
    """尝试从配置的动态代理 provider 获取代理。

    如果未配置动态代理，返回 None（回退到静态代理池）。
    """
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list(provider_type="proxy")
        for setting in settings:
            if not setting.enabled:
                continue
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                proxy = provider.get_proxy()
                if proxy:
                    return proxy
            except Exception as exc:
                logger.debug(f"[ProxyProvider] {setting.provider_key} 获取失败: {exc}")
                continue
    except Exception:
        pass
    return None
