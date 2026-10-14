"""ferry-web's configuration: the chart's JSON (`ferry-web-config`) plus secrets from mounted files.

Secrets never sit in the JSON: the OIDC client secret and the checkout signing key are files (from the
ExternalSecret), named by FW_OIDC_CLIENT_SECRET_FILE and FW_CHECKOUT_KEY_FILE. Database credentials come
from the libpq variables.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

import msgspec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ferry_watch.control.config import ConfigError, LimitsConfig, PollingConfig, poll_policy, user_limits
from ferry_watch.control.limits import Limits
from ferry_watch.polling import PollPolicy

from .accounts import IdentityPolicy
from .identity import OidcSettings

KOFI = re.compile(r"https://ko-fi\.com/[A-Za-z0-9_]{1,64}")
ED25519_KEY_BYTES = 32

__all__ = ["ConfigError", "WebConfig", "load", "read_secret", "signing_key"]


class _Config(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, rename="camel"):
    pass


class OidcConfig(_Config, kw_only=True):
    issuer: str
    client_id: str
    redirect_uri: str
    scopes: list[str] = msgspec.field(default_factory=lambda: ["openid", "profile", "email", "groups"])
    algorithms: list[str] = msgspec.field(default_factory=lambda: ["RS256"])
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"


class AdminConfig(_Config, kw_only=True):
    issuer: str
    username: str


class IdentityConfig(_Config, kw_only=True):
    bootstrap_admins: list[AdminConfig] = []
    aliases: dict[str, str] = {}
    invite_claim: Literal["preferred_username", "email"] = "preferred_username"
    auto_approve_groups: list[str] = []
    reverify_hours: Annotated[int, msgspec.Meta(ge=1, le=24)] = 24


class CheckoutConfig(_Config, kw_only=True):
    origin: str
    key_id: Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9]{1,16}$")]


class WebConfig(_Config, kw_only=True):
    oidc: OidcConfig
    checkout: CheckoutConfig
    identity: IdentityConfig = msgspec.field(default_factory=IdentityConfig)
    polling: PollingConfig = msgspec.field(default_factory=PollingConfig)
    limits: LimitsConfig = msgspec.field(default_factory=LimitsConfig)
    kofi_url: str | None = None
    listen_port: int = 8765
    static_dir: str | None = None

    def oidc_settings(self, client_secret: str) -> OidcSettings:
        o = self.oidc
        return OidcSettings(
            issuer=o.issuer,
            client_id=o.client_id,
            client_secret=client_secret,
            redirect_uri=o.redirect_uri,
            scopes=tuple(o.scopes),
            algorithms=frozenset(o.algorithms),
            username_claim=o.username_claim,
            groups_claim=o.groups_claim,
        )

    def identity_policy(self) -> IdentityPolicy:
        i = self.identity
        return IdentityPolicy(
            aliases=dict(i.aliases),
            bootstrap_admins=frozenset((a.issuer, a.username) for a in i.bootstrap_admins),
            invite_claim=i.invite_claim,
            auto_approve_groups=frozenset(i.auto_approve_groups),
            reverify=timedelta(hours=i.reverify_hours),
        )

    def policy(self) -> PollPolicy:
        return poll_policy(self.polling)

    def user_limits(self) -> Limits:
        return user_limits(self.limits)

    def check(self) -> WebConfig:
        if self.kofi_url is not None and not KOFI.fullmatch(self.kofi_url):
            raise ConfigError("kofiUrl must be https://ko-fi.com/<name>")
        if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?", self.checkout.origin):
            raise ConfigError("checkout.origin must be an https origin")
        try:
            self.oidc_settings("check")
            self.identity_policy()
            self.policy()
            self.user_limits()
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return self


def load(path: Path) -> WebConfig:
    try:
        return msgspec.json.decode(path.read_bytes(), type=WebConfig).check()
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None


def read_secret(env: Mapping[str, str], name: str) -> str:
    path = env.get(name)
    if not path:
        raise ConfigError(f"{name} is not set")
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        raise ConfigError(f"{name}: {e}") from None


def signing_key(text: str) -> Ed25519PrivateKey:
    """The checkout signing key: the raw 32-byte Ed25519 private key, base64url."""
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ConfigError("The checkout key isn't base64url") from None
    if len(raw) != ED25519_KEY_BYTES:
        raise ConfigError("The checkout key must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)
