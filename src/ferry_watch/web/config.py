"""ferry-web's configuration: the deployment's JSON (`ferry-web-config`) plus secrets from mounted files.

Secrets never sit in the JSON: the OIDC client secret and the checkout signing key are files (from the
ExternalSecret), named by FW_OIDC_CLIENT_SECRET_FILE and FW_CHECKOUT_KEY_FILE. The OIDC client id may come
the same way (FW_OIDC_CLIENT_ID_FILE), when the IdP generated it. Database credentials come from the libpq
variables.
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
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from ferry_watch.control.config import ConfigError, LimitsConfig, PollingConfig, poll_policy, user_limits
from ferry_watch.control.limits import Limits
from ferry_watch.polling import PollPolicy

from .accounts import IdentityPolicy
from .identity import OidcSettings

KOFI = re.compile(r"https://ko-fi\.com/[A-Za-z0-9_]{1,64}")
ED25519_KEY_BYTES = 32

__all__ = ["ConfigError", "WebConfig", "load", "new_keys", "read_secret", "signing_key"]


class _Config(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, rename="camel"):
    pass


class OidcConfig(_Config, kw_only=True):
    issuer: str
    # Or FW_OIDC_CLIENT_ID_FILE, when the IdP's generated id is kept beside the client secret.
    client_id: str = ""
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


class SmtpConfig(_Config, kw_only=True):
    host: str = "smtp-relay.default.svc.cluster.local."
    port: int = 2525
    sender: str = "ferry-watch@chestr.dev"


class WebConfig(_Config, kw_only=True):
    oidc: OidcConfig
    checkout: CheckoutConfig
    base_url: str = "https://ferry.chestr.dev"
    smtp: SmtpConfig | None = None
    vapid_subject: str = "mailto:ferry-watch@chestr.dev"
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
        if not self.oidc.client_id:
            raise ConfigError("oidc.clientId is required (in the config or FW_OIDC_CLIENT_ID_FILE)")
        if not re.fullmatch(r"https://[a-z0-9.-]+(:\d+)?", self.base_url):
            raise ConfigError("baseUrl must be an https origin")
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


def load(path: Path, *, client_id: str | None = None) -> WebConfig:
    """The config file, with the OIDC client id from its own file when there is one."""
    try:
        config = msgspec.json.decode(path.read_bytes(), type=WebConfig)
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if client_id is not None:
        config = msgspec.structs.replace(config, oidc=msgspec.structs.replace(config.oidc, client_id=client_id))
    return config.check()


def read_secret(env: Mapping[str, str], name: str) -> str:
    path = env.get(name)
    if not path:
        raise ConfigError(f"{name} is not set")
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        raise ConfigError(f"{name}: {e}") from None


def new_keys(key_id: str = "k1") -> str:
    """`ferry-web keys`: a fresh checkout key pair and VAPID key, in the forms the deployment stores them."""
    if not re.fullmatch(r"[a-z0-9]{1,16}", key_id):
        raise ConfigError("The key id must be 1-16 lowercase letters or digits")
    checkout = Ed25519PrivateKey.generate()
    vapid = ec.generate_private_key(ec.SECP256R1())
    raw = checkout.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = checkout.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return "\n".join(
        [
            "# Secrets, for the deployment's Secret (never in config):",
            f"FERRY_WATCH_CHECKOUT_KEY={_b64(raw)}",
            f"FERRY_WATCH_VAPID_KEY={_b64(vapid.private_numbers().private_value.to_bytes(32, 'big'))}",
            "# The checkout key's public half, for the browser pods (FW_VNC_PUBKEYS; not a secret):",
            f"{key_id}:{_b64(public)}",
        ]
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signing_key(text: str) -> Ed25519PrivateKey:
    """The checkout signing key: the raw 32-byte Ed25519 private key, base64url."""
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ConfigError("The checkout key isn't base64url") from None
    if len(raw) != ED25519_KEY_BYTES:
        raise ConfigError("The checkout key must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)
