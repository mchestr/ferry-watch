"""Web Push encryption (RFC 8291) and VAPID (RFC 8292)."""

from __future__ import annotations

import json
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

from waitlist.web.push import Vapid, _extract, _point, b64, encrypt, unb64

# RFC 8291 §5, "Push Message Encryption Example".
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
AS_PUBLIC = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A"
    "_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def key(raw: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64(raw), "big"), ec.SECP256R1())


def test_the_rfc_example() -> None:
    as_private = key(AS_PRIVATE)
    assert b64(_point(as_private.public_key())) == AS_PUBLIC
    body = encrypt(PLAINTEXT, unb64(UA_PUBLIC), unb64(AUTH), as_private=as_private, salt=unb64(SALT))
    assert b64(body) == BODY


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """What the browser does (RFC 8291 §3.4, from the receiving side)."""
    salt, (record_size, id_length) = body[:16], struct.unpack("!IB", body[16:21])
    as_public = body[21 : 21 + id_length]
    assert record_size == 4096
    ecdh = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    info = b"WebPush: info\x00" + _point(ua_private.public_key()) + as_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=info).derive(ecdh)
    prk = _extract(salt, ikm)
    cek = HKDFExpand(algorithm=hashes.SHA256(), length=16, info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDFExpand(algorithm=hashes.SHA256(), length=12, info=b"Content-Encoding: nonce\x00").derive(prk)
    padded = AESGCM(cek).decrypt(nonce, body[21 + id_length :], None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


def test_a_browser_can_read_what_we_send() -> None:
    browser = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    message = json.dumps({"title": "Held for you", "url": "/c/3f9c0a1b2c3d4e5f"}).encode()
    first = encrypt(message, _point(browser.public_key()), auth)
    second = encrypt(message, _point(browser.public_key()), auth)
    assert first != second  # a fresh key and salt every time
    assert decrypt(first, browser, auth) == message
    assert decrypt(second, browser, auth) == message


def test_vapid_signs_for_the_push_service_origin() -> None:
    vapid = Vapid.from_raw(AS_PRIVATE, "mailto:waitlist@chestr.dev")
    assert vapid.public_key == AS_PUBLIC
    header = vapid.authorization("https://fcm.googleapis.com/fcm/send/abc123", now=1_790_000_000)
    token, key_part = header.removeprefix("vapid t=").split(", k=")
    assert key_part == AS_PUBLIC
    head, payload, signature = token.split(".")
    claims = json.loads(unb64(payload))
    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_790_000_000 + 12 * 3600,
        "sub": "mailto:waitlist@chestr.dev",
    }
    raw = unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(AS_PUBLIC))
    public.verify(der, f"{head}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if it doesn't verify
    assert json.loads(unb64(head)) == {"typ": "JWT", "alg": "ES256"}
