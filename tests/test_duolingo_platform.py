from __future__ import annotations
import pytest
from core.base_platform import RegisterConfig, Account, AccountStatus
from core.registry import get as get_platform, list_platforms
from platforms.duolingo.plugin import DuolingoPlatform

def test_duolingo_registry_registration(client):
    # Verify that the plugin loader automatically registered Duolingo
    resp = client.get("/api/platforms")
    assert resp.status_code == 200
    platforms = resp.json()
    names = [p["name"] for p in platforms]
    assert "duolingo" in names

    duolingo_api_info = next(p for p in platforms if p["name"] == "duolingo")
    assert duolingo_api_info["display_name"] == "Duolingo"
    assert "headless" in duolingo_api_info["supported_executors"]
    assert "headed" in duolingo_api_info["supported_executors"]

def test_duolingo_platform_actions():
    platform = DuolingoPlatform(RegisterConfig(executor_type="headless"))
    actions = platform.get_platform_actions()
    
    assert len(actions) > 0
    redeem_action = next((a for a in actions if a["id"] == "redeem_code"), None)
    assert redeem_action is not None
    assert redeem_action["label"] == "Redeem Trial Code"
    
    params = redeem_action["params"]
    assert len(params) == 1
    assert params[0]["key"] == "referral_code"
    assert params[0]["label"] == "Referral / Redeem Code"
    assert params[0]["type"] == "text"

def test_duolingo_check_valid():
    platform = DuolingoPlatform(RegisterConfig(executor_type="headless"))
    
    # Test valid account credentials
    acc_valid = Account(
        platform="duolingo",
        email="test@example.com",
        password="secretpassword",
        status=AccountStatus.REGISTERED
    )
    assert platform.check_valid(acc_valid) is True
    
    # Test empty check
    acc_invalid = Account(
        platform="duolingo",
        email="",
        password="",
        status=AccountStatus.REGISTERED
    )
    assert platform.check_valid(acc_invalid) is False

def test_duolingo_password_generation():
    platform = DuolingoPlatform(RegisterConfig(executor_type="headless"))
    
    pwd1 = platform._prepare_registration_password(None)
    assert len(pwd1) == 15
    assert isinstance(pwd1, str)
    
    pwd2 = platform._prepare_registration_password("custompass")
    assert pwd2 == "custompass"
