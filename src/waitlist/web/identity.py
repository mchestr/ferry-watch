"""Who someone is: an OpenID Connect sign-in, verified (docs/multi-tenant/design-reference.md §6.2).

waitlist-web is a confidential OIDC client using the authorization code flow with PKCE. The SPA does the
redirect and brings back the code (in the URL fragment, so it never reaches an access log); this module
exchanges it and verifies the `id_token`: an allowed asymmetric algorithm (never `none` or HMAC), the
issuer's key, issuer, audience, expiry and the nonce. The access token is thrown away.

A user's id is computed from the verified issuer and subject, never looked up, so no database row can
point one person's sign-in at another person's browsers. GitOps aliases carry users across an IdP change.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import aiohttp
import jwt

log = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"})
CLOCK_SKEW_SECONDS = 60
DISCOVERY_TTL_SECONDS = 3600
TIMEOUT = aiohttp.ClientTimeout(total=15)


class SignInError(Exception):
    """The sign-in can't be completed. `reason` is safe to show; details are only logged."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Principal:
    issuer: str
    subject: str
    username: str | None = None
    email: str | None = None
    name: str | None = None
    groups: frozenset[str] = frozenset()


def principal_hash(issuer: str, subject: str) -> str:
    """`u` + 19 base32 characters of sha256("fw/v1|iss|sub"): 20 characters, DNS-label safe.

    "fw/v1" is from when this was Ferry Watch. Every stored user id (and the browser pods' owner label) is
    derived from it, so it stays: changing it would turn everyone into a new user.
    """
    digest = hashlib.sha256(f"fw/v1|{issuer}|{subject}".encode()).digest()
    return "u" + base64.b32encode(digest).decode().lower().rstrip("=")[:19]


def user_id(principal: Principal, aliases: Mapping[str, str]) -> str:
    h = principal_hash(principal.issuer, principal.subject)
    return aliases.get(h, h)


@dataclass(frozen=True, slots=True)
class OidcSettings:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "profile", "email", "groups")
    algorithms: frozenset[str] = frozenset({"RS256"})
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"

    def __post_init__(self) -> None:
        if not self.algorithms or not self.algorithms <= ALLOWED_ALGORITHMS:
            raise ValueError(f"id_token algorithms must be among {sorted(ALLOWED_ALGORITHMS)}")


class OidcClient:
    def __init__(self, http: aiohttp.ClientSession, settings: OidcSettings) -> None:
        self._http = http
        self.settings = settings
        self._discovery: dict[str, Any] | None = None
        self._discovered_at = 0.0
        self._keys: dict[str, Any] = {}

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None or time.monotonic() - self._discovered_at > DISCOVERY_TTL_SECONDS:
            url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
            document = await self._get_json(url)
            if document.get("issuer") != self.settings.issuer:
                raise SignInError("The identity provider's configuration names another issuer")
            self._discovery, self._discovered_at = document, time.monotonic()
        return self._discovery

    async def public_config(self) -> dict[str, Any]:
        """What the SPA needs to start a sign-in. Nothing here is secret."""
        document = await self.discovery()
        return {
            "authorization_endpoint": document["authorization_endpoint"],
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scopes": " ".join(self.settings.scopes),
        }

    async def sign_in(self, code: str, verifier: str, nonce: str) -> Principal:
        """Exchange the code and verify the id_token it brings."""
        document = await self.discovery()
        settings = self.settings
        try:
            async with self._http.post(
                document["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.redirect_uri,
                    "code_verifier": verifier,
                },
                auth=aiohttp.BasicAuth(settings.client_id, settings.client_secret),
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            ) as response:
                body = await response.json(content_type=None)
                if response.status != HTTPStatus.OK:
                    log.warning("Token exchange refused: %s", body.get("error") if isinstance(body, dict) else body)
                    raise SignInError("The identity provider refused the sign-in")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            log.warning("Token exchange failed", exc_info=True)
            raise SignInError("Couldn't reach the identity provider") from None
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str):
            raise SignInError("The identity provider sent no id_token")
        return await self.verify(id_token, nonce)

    async def verify(self, id_token: str, nonce: str) -> Principal:
        settings = self.settings
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise SignInError("A malformed id_token") from None
        algorithm = header.get("alg")
        if algorithm not in settings.algorithms:
            raise SignInError("An id_token signed with an algorithm that isn't allowed")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[algorithm],
                audience=settings.client_id,
                issuer=settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except jwt.PyJWTError as e:
            log.warning("id_token refused: %s", e)
            raise SignInError("The id_token didn't verify") from None
        if claims.get("nonce") != nonce:
            raise SignInError("The id_token is for another sign-in")
        if "azp" in claims and claims["azp"] != settings.client_id:
            raise SignInError("The id_token was issued to another client")
        if claims.get("iat", 0) > time.time() + CLOCK_SKEW_SECONDS:
            raise SignInError("The id_token is from the future")
        groups = claims.get(settings.groups_claim) or []
        return Principal(
            issuer=claims["iss"],
            subject=str(claims["sub"]),
            username=_text(claims.get(settings.username_claim)),
            email=_text(claims.get("email")),
            name=_text(claims.get("name")),
            groups=frozenset(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else frozenset(),
        )

    async def _key(self, kid: str | None) -> Any:
        if kid not in self._keys:
            await self._load_keys()  # once more, for a rotated key
        if kid is not None:
            key = self._keys.get(kid)
        else:  # an issuer with a single key may leave kid out
            key = next(iter(self._keys.values())) if len(self._keys) == 1 else None
        if key is None:
            raise SignInError("The id_token's signing key is unknown")
        return key

    async def _load_keys(self) -> None:
        document = await self.discovery()
        jwks = await self._get_json(document["jwks_uri"])
        keys = {}
        for jwk in jwks.get("keys", []):
            if jwk.get("use", "sig") != "sig":
                continue
            try:
                keys[jwk.get("kid")] = jwt.PyJWK(jwk).key
            except jwt.PyJWTError:
                log.warning("Skipping a JWKS key that can't be used")
        self._keys = keys

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._http.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT) as response:
                response.raise_for_status()
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            log.warning("Couldn't fetch %s", url, exc_info=True)
            raise SignInError("Couldn't reach the identity provider") from None
        if not isinstance(body, dict):
            raise SignInError("The identity provider sent something unexpected")
        return body


def _text(value: object, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None
