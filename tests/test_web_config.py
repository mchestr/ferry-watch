from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from ferry_watch.tickets import public_keys
from ferry_watch.web.config import ConfigError, load, new_keys, read_secret, signing_key
from ferry_watch.web.main import add_static
from ferry_watch.web.push import Vapid

MINIMAL: dict[str, Any] = {
    "oidc": {
        "issuer": "https://auth.chestr.dev",
        "clientId": "ferry-watch",
        "redirectUri": "https://ferry.chestr.dev/auth/callback",
    },
    "checkout": {"origin": "https://ferry-checkout.chestr.dev", "keyId": "k1"},
}


def write(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "web.json"
    path.write_text(json.dumps(config))
    return path


def test_minimal_config(tmp_path: Path) -> None:
    config = load(
        write(
            tmp_path,
            MINIMAL | {"identity": {"bootstrapAdmins": [{"issuer": "https://auth.chestr.dev", "username": "mike"}]}},
        )
    )
    assert config.oidc_settings("s").algorithms == frozenset({"RS256"})
    assert config.identity_policy().bootstrap_admins == frozenset({("https://auth.chestr.dev", "mike")})
    assert config.user_limits().value("maxTrips", {}) == 3
    assert config.policy().horizon.days == 30


@pytest.mark.parametrize(
    "change",
    [
        {"kofiUrl": "https://evil.example/kofi"},
        {"checkout": {"origin": "http://ferry-checkout.chestr.dev", "keyId": "k1"}},
        {"checkout": {"origin": "https://ferry-checkout.chestr.dev/path", "keyId": "k1"}},
        {"oidc": MINIMAL["oidc"] | {"algorithms": ["HS256"]}},  # never a shared-secret signature
        {"identity": {"reverifyHours": 48}},
        {"surprise": 1},
    ],
)
def test_bad_config(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | change))


def test_the_client_id_may_come_from_its_own_file(tmp_path: Path) -> None:
    oidc = {k: v for k, v in MINIMAL["oidc"].items() if k != "clientId"}
    path = write(tmp_path, MINIMAL | {"oidc": oidc})
    with pytest.raises(ConfigError, match="clientId"):
        load(path)
    assert load(path, client_id="generated-id").oidc_settings("s").client_id == "generated-id"
    # The file wins over the config, so the IdP's id is the one used.
    assert load(write(tmp_path, MINIMAL), client_id="generated-id").oidc.client_id == "generated-id"


def test_keys_come_out_in_the_forms_the_app_reads() -> None:
    lines = [line for line in new_keys("k2").splitlines() if not line.startswith("#")]
    values = dict(line.split("=", 1) for line in lines[:2])
    public = public_keys(lines[2])["k2"]
    checkout = signing_key(values["FERRY_WATCH_CHECKOUT_KEY"])
    assert checkout.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == public.public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    assert Vapid.from_raw(values["FERRY_WATCH_VAPID_KEY"], "mailto:x@example.com").public_key
    with pytest.raises(ConfigError):
        new_keys("K-1")


def test_secrets_come_from_files(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    secret = tmp_path / "key"
    secret.write_text(base64.urlsafe_b64encode(raw).decode().rstrip("=") + "\n")
    text = read_secret({"FW_CHECKOUT_KEY_FILE": str(secret)}, "FW_CHECKOUT_KEY_FILE")
    assert signing_key(text).private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()) == raw
    with pytest.raises(ConfigError):
        read_secret({}, "FW_CHECKOUT_KEY_FILE")
    with pytest.raises(ConfigError):
        signing_key("short")


async def test_the_spa_is_served_on_explicit_routes_only(tmp_path: Path, aiohttp_client: Any) -> None:
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("js")
    (tmp_path / "manifest.webmanifest").write_text("{}")
    app = web.Application()
    add_static(app, tmp_path)
    client: TestClient = await aiohttp_client(app)
    for path in ("/", "/c/3f9c0a1b2c3d4e5f", "/admin", "/auth/callback"):
        assert await (await client.get(path)).text() == "<html>app</html>"
    assert (await client.get("/assets/app.js")).status == 200
    assert (await client.get("/manifest.webmanifest")).status == 200
    assert (await client.get("/c/not-a-trip")).status == 404
    assert (await client.get("/anything-else")).status == 404
