from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ferry_watch.tickets import (
    MAX_TOKEN_LENGTH,
    TTL_SECONDS,
    Claims,
    TicketError,
    mint,
    public_key,
    public_keys,
    verify,
)

NOW = 1_790_000_000.0
KEY = Ed25519PrivateKey.generate()
KEYS = {"k1": KEY.public_key()}
BINDING = {"sub": "user-7", "trip": "3f9c0a1b2c3d4e5f", "pod": "fw-3f9c0a1b2c3d4e5f-g1", "cart_id": "c-1"}


def ticket(**kw: object) -> str:
    return mint(KEY, "k1", now=NOW, **(BINDING | kw))  # type: ignore[arg-type]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def resign(payload: dict[str, object], key: Ed25519PrivateKey = KEY, key_id: str = "k1") -> str:
    """A ticket with these exact claims, properly signed: for the checks that come after the signature."""
    signed = f"fw1.{key_id}.{b64(json.dumps(payload).encode())}"
    return f"{signed}.{b64(key.sign(signed.encode()))}"


def raw(public: Ed25519PrivateKey) -> str:
    return b64(public.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def test_round_trip() -> None:
    claims = verify(ticket(), KEYS, now=NOW + 1)
    assert (claims.sub, claims.trip, claims.pod, claims.cart_id) == tuple(BINDING.values())
    assert (claims.iss, claims.aud) == ("fw-web", "fw-vnc")
    assert claims.exp - claims.iat == TTL_SECONDS


def test_every_ticket_is_unique() -> None:
    assert verify(ticket(), KEYS, now=NOW).jti != verify(ticket(), KEYS, now=NOW).jti


def test_another_key_cannot_forge_one() -> None:
    forged = mint(Ed25519PrivateKey.generate(), "k1", now=NOW, **BINDING)
    with pytest.raises(TicketError, match="Bad signature"):
        verify(forged, KEYS, now=NOW)


def test_unknown_key_id() -> None:
    with pytest.raises(TicketError, match="Unknown key"):
        verify(mint(KEY, "k2", now=NOW, **BINDING), KEYS, now=NOW)


def flip(text: str) -> str:
    return text[:-2] + ("AA" if text[-2:] != "AA" else "BB")


def test_tampering_breaks_the_signature() -> None:
    prefix, key_id, payload, signature = ticket().split(".")
    other = ticket(cart_id="c-2").split(".")[2]
    for tampered in (
        (prefix, key_id, other, signature),  # another ticket's claims under this signature
        (prefix, key_id, flip(payload), signature),
        (prefix, key_id, payload, flip(signature)),
    ):
        with pytest.raises(TicketError):
            verify(".".join(tampered), KEYS, now=NOW)


def test_expired() -> None:
    token = ticket()
    verify(token, KEYS, now=NOW + TTL_SECONDS - 1)
    with pytest.raises(TicketError, match="Expired"):
        verify(token, KEYS, now=NOW + TTL_SECONDS)


def test_a_long_lived_ticket_is_refused_even_when_signed() -> None:
    claims = json.loads(base64.urlsafe_b64decode(ticket().split(".")[2] + "=="))
    claims["exp"] = claims["iat"] + 3600
    with pytest.raises(TicketError, match="Bad lifetime"):
        verify(resign(claims), KEYS, now=NOW)


def test_a_ticket_from_the_future_is_refused() -> None:
    with pytest.raises(TicketError, match="Bad lifetime"):
        verify(mint(KEY, "k1", now=NOW + 30, **BINDING), KEYS, now=NOW)
    verify(mint(KEY, "k1", now=NOW + 5, **BINDING), KEYS, now=NOW)  # within the clock skew


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "fw-other"},
        {"iss": "someone"},
        {"admin": True},
        {"sub": "User 7"},
        {"jti": "short"},
    ],
)
def test_claims_are_strict(change: dict[str, object]) -> None:
    claims = json.loads(base64.urlsafe_b64decode(ticket().split(".")[2] + "=="))
    with pytest.raises(TicketError, match="Bad claims"):
        verify(resign(claims | change), KEYS, now=NOW)


@pytest.mark.parametrize(
    "token",
    ["", "fw1", "fw1.k1.e30", "fw2.k1.e30.AA", "fw1.k1.e30.AA.AA", "x" * (MAX_TOKEN_LENGTH + 1), "fw1.k1.e30.!!"],
)
def test_garbage(token: str) -> None:
    with pytest.raises(TicketError):
        verify(token, KEYS, now=NOW)


def test_mint_refuses_a_key_id_that_would_break_the_format() -> None:
    with pytest.raises(TicketError, match="Bad key id"):
        mint(KEY, "k.1", now=NOW, **BINDING)


def test_claims_type_is_exported() -> None:
    assert isinstance(verify(ticket(), KEYS, now=NOW), Claims)


# ---- keys


def test_public_keys_during_a_rotation() -> None:
    new = Ed25519PrivateKey.generate()
    keys = public_keys(f"k1:{raw(KEY)}, k2:{raw(new)}")
    assert set(keys) == {"k1", "k2"}
    verify(ticket(), keys, now=NOW)
    verify(mint(new, "k2", now=NOW, **BINDING), keys, now=NOW)


@pytest.mark.parametrize(
    "spec",
    ["", " , ", "K1:{key}", "k.1:{key}", ":{key}", "k1:", "k1:AAAA", "k1:{key}x!", "this-id-is-too-long:{key}"],
)
def test_bad_public_keys(spec: str) -> None:
    with pytest.raises(TicketError):
        public_keys(spec.format(key=raw(KEY)))


def test_public_key_is_raw_32_bytes() -> None:
    assert public_key(raw(KEY)).public_bytes(Encoding.Raw, PublicFormat.Raw) == KEY.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
