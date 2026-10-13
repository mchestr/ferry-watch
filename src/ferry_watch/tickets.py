"""Checkout tickets: the one-minute pass that lets a user's browser into their own pod's VNC.

The dashboard (ferry-web) mints a ticket when the user taps "Check out", only for the cart's owner and
only while the cart is held. The browser pod verifies it against public keys pinned in its own spec, and
checks that it names this pod, this trip, this cart and this pod's owner (design-reference.md §5.5).
Nothing else can open a pod's VNC: not the operator, and not another user who guesses the path.

Format: ``fw1.<key id>.<payload>.<signature>``, with the payload base64url JSON and the signature an
Ed25519 signature over ``fw1.<key id>.<payload>``. It is deliberately not a JWT: there is no algorithm
field to negotiate, so there is nothing to confuse.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from collections.abc import Mapping
from typing import Annotated, Final, Literal

import msgspec
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "fw1"
AUDIENCE: Final = "fw-vnc"
ISSUER: Final = "fw-web"
TTL_SECONDS = 60
MAX_TOKEN_LENGTH = 1024
CLOCK_SKEW_SECONDS = 5

_KEY_ID = re.compile(r"[a-z0-9]{1,16}")
Ident = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9-]{1,64}$")]


class TicketError(ValueError):
    """The ticket is malformed, forged, expired, or for something else."""


class Claims(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    iss: Literal["fw-web"] = ISSUER
    aud: Literal["fw-vnc"] = AUDIENCE
    sub: Ident  # the owner's user id
    trip: Ident
    pod: Ident
    cart_id: Ident
    jti: Annotated[str, msgspec.Meta(pattern=r"^[A-Za-z0-9_-]{16,64}$")]
    iat: int
    exp: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise TicketError("Bad encoding") from None


def mint(
    key: Ed25519PrivateKey, key_id: str, *, sub: str, trip: str, pod: str, cart_id: str, now: float | None = None
) -> str:
    if not _KEY_ID.fullmatch(key_id):
        raise TicketError("Bad key id")
    issued = int(time.time() if now is None else now)
    claims = Claims(
        sub=sub,
        trip=trip,
        pod=pod,
        cart_id=cart_id,
        jti=secrets.token_urlsafe(16),
        iat=issued,
        exp=issued + TTL_SECONDS,
    )
    signed = f"{PREFIX}.{key_id}.{_b64(msgspec.json.encode(claims))}"
    return f"{signed}.{_b64(key.sign(signed.encode()))}"


def verify(token: str, keys: Mapping[str, Ed25519PublicKey], *, now: float | None = None) -> Claims:
    """The ticket's claims, if it is genuine and current. Binding it to a pod is the caller's job."""
    if len(token) > MAX_TOKEN_LENGTH:
        raise TicketError("Too long")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != PREFIX:
        raise TicketError("Not a ticket")
    _, key_id, payload, signature = parts
    key = keys.get(key_id)
    if key is None:
        raise TicketError("Unknown key")
    try:
        key.verify(_unb64(signature), f"{PREFIX}.{key_id}.{payload}".encode())
    except InvalidSignature:
        raise TicketError("Bad signature") from None
    try:
        claims = msgspec.json.decode(_unb64(payload), type=Claims)
    except (msgspec.ValidationError, msgspec.DecodeError):
        raise TicketError("Bad claims") from None
    current = time.time() if now is None else now
    if claims.exp - claims.iat > TTL_SECONDS or claims.iat > current + CLOCK_SKEW_SECONDS:
        raise TicketError("Bad lifetime")
    if current >= claims.exp:
        raise TicketError("Expired")
    return claims


def public_key(text: str) -> Ed25519PublicKey:
    """A raw 32-byte Ed25519 public key, base64url (how pods get them, pinned in their spec)."""
    raw = _unb64(text)
    if len(raw) != 32:  # an Ed25519 public key is 32 bytes
        raise TicketError("Bad public key")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_keys(spec: str) -> dict[str, Ed25519PublicKey]:
    """Keys from ``FW_VNC_PUBKEYS``: ``<id>:<key>[,<id>:<key>]`` (two during a rotation)."""
    keys: dict[str, Ed25519PublicKey] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        key_id, _, text = item.partition(":")
        if not _KEY_ID.fullmatch(key_id):
            raise TicketError("Bad key id")
        keys[key_id] = public_key(text)
    if not keys:
        raise TicketError("No keys")
    return keys
