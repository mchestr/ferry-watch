"""Web Push: the default alert channel (docs/multi-tenant/design-reference.md §6.7).

Messages are encrypted for the browser that subscribed (RFC 8291, `aes128gcm`), so the push service
in between (Apple's, Google's, Mozilla's) sees nothing but ciphertext, and requests are signed with the
app's VAPID key (RFC 8292). Built on `cryptography` alone.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal

import aiohttp
from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

RECORD_SIZE = 4096
VAPID_TTL_SECONDS = 12 * 3600
TIMEOUT = aiohttp.ClientTimeout(total=15)
Urgency = Literal["very-low", "low", "normal", "high"]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """An `aes128gcm` message body for one subscription (RFC 8291 §3-4). The key and salt are fresh
    for every message; they're parameters only so the RFC's example can be reproduced."""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _point(as_private.public_key())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), peer)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(ecdh_secret)
    prk = _extract(salt, ikm)
    cek = HKDFExpand(algorithm=hashes.SHA256(), length=16, info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDFExpand(algorithm=hashes.SHA256(), length=12, info=b"Content-Encoding: nonce\x00").derive(prk)
    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise ValueError("A push message must fit in one record")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 0x02: the last (only) record
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def _extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869 §2.2): HMAC-SHA256(salt, ikm)."""
    mac = hmac.HMAC(salt, hashes.SHA256())
    mac.update(ikm)
    return mac.finalize()


@dataclass(frozen=True, slots=True)
class Vapid:
    """The app's VAPID key pair (RFC 8292): push services know the app by its public key."""

    private_key: ec.EllipticCurvePrivateKey
    subject: str  # mailto: or https: contact for the push service

    @classmethod
    def from_raw(cls, text: str, subject: str) -> Vapid:
        """The private key as 32 raw bytes, base64url (as `web-push generate-vapid-keys` prints it)."""
        value = int.from_bytes(unb64(text), "big")
        return cls(ec.derive_private_key(value, ec.SECP256R1()), subject)

    @property
    def public_key(self) -> str:
        """For the browser's `applicationServerKey`."""
        return b64(_point(self.private_key.public_key()))

    def authorization(self, endpoint: str, now: float | None = None) -> str:
        parts = urllib.parse.urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int((now or time.time()) + VAPID_TTL_SECONDS),
            "sub": self.subject,
        }
        header = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        payload = b64(json.dumps(claims, separators=(",", ":")).encode())
        signed = f"{header}.{payload}".encode()
        r, s = decode_dss_signature(self.private_key.sign(signed, ec.ECDSA(hashes.SHA256())))
        signature = b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={header}.{payload}.{signature}, k={self.public_key}"


@dataclass(frozen=True, slots=True)
class Subscription:
    endpoint: str
    p256dh: str
    auth: str


class PushGone(Exception):
    """The subscription no longer exists (404/410): forget it."""


async def send(
    http: aiohttp.ClientSession,
    vapid: Vapid,
    subscription: Subscription,
    message: dict[str, Any],
    *,
    ttl: int,
    urgency: Urgency = "normal",
    topic: str | None = None,
) -> None:
    body = encrypt(
        json.dumps(message, separators=(",", ":")).encode(), unb64(subscription.p256dh), unb64(subscription.auth)
    )
    headers = {
        "Authorization": vapid.authorization(subscription.endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    }
    if topic:
        headers["Topic"] = topic[:32]  # a newer message with the same topic replaces an undelivered one
    async with http.post(subscription.endpoint, data=body, headers=headers, timeout=TIMEOUT) as response:
        if response.status in (HTTPStatus.NOT_FOUND, HTTPStatus.GONE):
            raise PushGone
        if response.status >= HTTPStatus.BAD_REQUEST:
            raise aiohttp.ClientResponseError(
                response.request_info, response.history, status=response.status, message="push refused"
            )
