from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.base_sms import HERO_SMS_DEFAULT_COUNTRY, HERO_SMS_DEFAULT_SERVICE, HeroSmsProvider
from infrastructure.provider_settings_repository import ProviderSettingsRepository

router = APIRouter(prefix="/sms", tags=["sms"])


class HeroSmsQueryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    country: str = ""
    proxy: str = ""


def _saved_herosms_config() -> dict:
    return ProviderSettingsRepository().resolve_runtime_settings("sms", "herosms", {})


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _provider_from_payload(payload: HeroSmsQueryRequest | None = None) -> HeroSmsProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_herosms_config()
    api_key = str(payload.api_key or saved.get("herosms_api_key") or "").strip()
    return HeroSmsProvider(
        api_key=api_key,
        default_service=str(payload.service or saved.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or HERO_SMS_DEFAULT_COUNTRY),
        max_price=_safe_float(saved.get("herosms_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


@router.get("/herosms/countries")
def herosms_countries():
    try:
        return {"countries": _provider_from_payload().get_countries()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/herosms/services")
def herosms_services(country: str = ""):
    try:
        return {"services": _provider_from_payload(HeroSmsQueryRequest(country=country)).get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/balance")
def herosms_balance(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        return {"balance": provider.get_balance()}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/prices")
def herosms_prices(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        country = str(body.country or provider.default_country or HERO_SMS_DEFAULT_COUNTRY)
        return {"prices": provider.get_prices(service=service, country=country)}
    except Exception as exc:
        raise HTTPException(502, str(exc))


class HeroSmsBestCountryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    proxy: str = ""
    min_stock: int = 20
    max_price: float = 0
    top_n: int = 10


@router.post("/herosms/top-countries")
def herosms_top_countries(body: HeroSmsBestCountryRequest | None = None):
    """获取按价格排序的国家列表（含价格和库存）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        rows = provider.get_top_countries(service=service)
        # 只返回有库存的
        rows = [r for r in rows if (r.get("count") or 0) > 0]
        if body.top_n > 0:
            rows = rows[:body.top_n]
        return {"countries": rows, "service": service}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/herosms/best-country")
def herosms_best_country(body: HeroSmsBestCountryRequest | None = None):
    """自动选择最优国家（价格最低 + 库存充足）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        best = provider.get_best_country(
            service=service,
            min_stock=body.min_stock,
            max_price=body.max_price,
        )
        if best:
            # 获取详细信息
            rows = provider.get_top_countries(service=service)
            detail = next((r for r in rows if str(r.get("country")) == str(best)), None)
            return {
                "country": best,
                "detail": detail,
                "service": service,
            }
        return {"country": None, "detail": None, "service": service}
    except Exception as exc:
        raise HTTPException(502, str(exc))
