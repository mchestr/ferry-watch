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

from waitlist.tickets import public_keys
from waitlist.web.config import ConfigError, checkout_key, load, new_keys, read_secret, signing_key
from waitlist.web.main import add_static
from waitlist.web.push import Vapid

MINIMAL: dict[str, Any] = {
    "oidc": {
        "issuer": "https://auth.chestr.dev",
        "clientId": "waitlist",
        "redirectUri": "https://waitlist.chestr.dev/auth/callback",
    },
    "checkout": {"origin": "https://waitlist-checkout.chestr.dev", "keyId": "k1"},
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


def test_the_streamed_viewer_is_off_unless_the_deployment_offers_it(tmp_path: Path) -> None:
    assert load(write(tmp_path, MINIMAL)).checkout.stream_viewer == "off"  # the kill switch is the default
    for value in ("off", "admins", "everyone"):
        checkout = MINIMAL["checkout"] | {"streamViewer": value}
        assert load(write(tmp_path, MINIMAL | {"checkout": checkout})).checkout.stream_viewer == value


@pytest.mark.parametrize(
    "change",
    [
        {"kofiUrl": "https://evil.example/kofi"},
        {"checkout": {"origin": "http://waitlist-checkout.chestr.dev", "keyId": "k1"}},
        {"checkout": {"origin": "https://waitlist-checkout.chestr.dev/path", "keyId": "k1"}},
        # Only the three values; YAML reads a bare `off` as false, which must stop startup, not mean anything.
        *({"checkout": MINIMAL["checkout"] | {"streamViewer": v}} for v in ("on", "Admins", False, True, None)),
        {"oidc": MINIMAL["oidc"] | {"algorithms": ["HS256"]}},  # never a shared-secret signature
        {"identity": {"reverifyHours": 48}},
        {"surprise": 1},
    ],
)
def test_bad_config(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | change))


def test_providers_are_off_unless_the_deployment_offers_them(tmp_path: Path) -> None:
    assert load(write(tmp_path, MINIMAL)).offers() == {"buntzen": "off"}
    for offer in ("off", "admins", "everyone"):
        config = load(write(tmp_path, MINIMAL | {"providers": {"buntzen": {"offer": offer}}}))
        assert config.offers() == {"buntzen": offer}
    assert load(write(tmp_path, MINIMAL | {"providers": {"buntzen": {}}})).offers() == {"buntzen": "off"}


@pytest.mark.parametrize(
    "providers",
    [
        {"bcferries": {"offer": "everyone"}},  # always offered: not the config's to say
        {"bcparks": {"offer": "admins"}},  # not a provider this build knows
        *({"buntzen": {"offer": v}} for v in ("on", "Admins", False, None)),  # YAML's bare off is false: refused
        {"buntzen": {"offer": "admins", "requestsPerMinute": 12}},  # waitlist-control's key
    ],
)
def test_bad_provider_offers(tmp_path: Path, providers: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL | {"providers": providers}))


def test_alert_only_watches_are_a_limit(tmp_path: Path) -> None:
    config = load(write(tmp_path, MINIMAL | {"limits": {"defaults": {"maxAlertOnlyWatches": 2}}}))
    assert config.user_limits().value("maxAlertOnlyWatches", {}) == 2


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
    key_id, checkout = checkout_key(values["WAITLIST_CHECKOUT_KEY"], "k1")
    assert key_id == "k2"  # the id travels with the key, so a rotation is one change
    assert checkout.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) == public.public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    assert Vapid.from_raw(values["WAITLIST_VAPID_KEY"], "mailto:x@example.com").public_key
    with pytest.raises(ConfigError):
        new_keys("K-1")


def test_the_checkout_key_carries_its_id_or_takes_the_configs() -> None:
    raw = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    assert checkout_key(f"k7:{raw}", "k1")[0] == "k7"
    assert checkout_key(raw, "k1")[0] == "k1"
    for bad in (f"K7:{raw}", f":{raw}", "k7:short"):
        with pytest.raises(ConfigError):
            checkout_key(bad, "k1")


def test_secrets_come_from_files(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    secret = tmp_path / "key"
    secret.write_text(base64.urlsafe_b64encode(raw).decode().rstrip("=") + "\n")
    text = read_secret({"WL_CHECKOUT_KEY_FILE": str(secret)}, "WL_CHECKOUT_KEY_FILE")
    assert signing_key(text).private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()) == raw
    with pytest.raises(ConfigError):
        read_secret({}, "WL_CHECKOUT_KEY_FILE")
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


async def test_the_spa_serves_the_watch_routes(tmp_path: Path, aiohttp_client: Any) -> None:
    """docs/waitlist/design.md §6.8: the dashboard, New watch (with a provider), a watch's page and the checkout."""
    (tmp_path / "index.html").write_text("<html>app</html>")
    app = web.Application()
    add_static(app, tmp_path)
    client: TestClient = await aiohttp_client(app)
    for path in (
        "/",
        "/new",
        "/new/buntzen",
        "/new/bcferries",
        "/w/3f9c0a1b2c3d4e5f",
        "/c/3f9c0a1b2c3d4e5f",
        "/settings",
    ):
        response = await client.get(path)
        assert (response.status, await response.text()) == (200, "<html>app</html>"), path
        assert response.headers["Cache-Control"] == "no-cache"
    for path in ("/new/Buntzen", "/new/b", "/new/" + "x" * 33, "/w/not-a-watch", "/w/3F9C0A1B2C3D4E5F", "/watches"):
        assert (await client.get(path)).status == 404, path
