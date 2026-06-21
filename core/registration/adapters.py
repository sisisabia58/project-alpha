from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .models import RegistrationCapability, RegistrationContext, RegistrationArtifacts, RegistrationResult


@dataclass(slots=True)
class OtpSpec:
    keyword: str = ""
    timeout: int | None = None
    code_pattern: str | None = None
    wait_message: str = "Waiting for verification code..."
    success_label: str = "Verification code"


@dataclass(slots=True)
class LinkSpec:
    keyword: str = ""
    timeout: int | None = None
    wait_message: str = "Waiting for verification link email..."
    success_label: str = "Verification link"
    preview_chars: int = 80


@dataclass(slots=True)
class BrowserRegistrationAdapter:
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    browser_worker_builder: Callable[[RegistrationContext, RegistrationArtifacts], Any] | None = None
    browser_register_runner: Callable[[Any, RegistrationContext, RegistrationArtifacts], Any] | None = None
    oauth_runner: Callable[[RegistrationContext], Any] | None = None
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    otp_spec: OtpSpec | None = None
    link_spec: LinkSpec | None = None
    use_captcha_for_mailbox: bool = False
    preflight: Callable[[RegistrationContext], None] | None = None


@dataclass(slots=True)
class ProtocolMailboxAdapter:
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    worker_builder: Callable[[RegistrationContext, RegistrationArtifacts], Any]
    register_runner: Callable[[Any, RegistrationContext, RegistrationArtifacts], Any]
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    otp_spec: OtpSpec | None = None
    link_spec: LinkSpec | None = None
    use_captcha: bool = False
    use_executor: bool = False
    preflight: Callable[[RegistrationContext], None] | None = None


@dataclass(slots=True)
class ProtocolOAuthAdapter:
    oauth_runner: Callable[[RegistrationContext], Any]
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    preflight: Callable[[RegistrationContext], None] | None = None
