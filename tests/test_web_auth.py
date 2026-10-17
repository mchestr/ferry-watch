"""waitlist-web's sign-in against a fake OIDC provider, and its cookie-less sessions, on real Postgres."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
import asyncpg
import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.postgres import ROLES, Database
from waitlist.web.accounts import Accounts, IdentityPolicy
from waitlist.web.auth import ACCOUNTS, OIDC, require_session, routes, signed_in
from waitlist.web.identity import OidcClient, OidcSettings, principal_hash

CLIENT_ID = "waitlist"
SECRET = "client-secret-" + "x" * 32
VERIFIER = "v" * 43


@dataclass
class FakeIdP:
    """Discovery, a token endpoint and JWKS: enough of Authelia for the code flow."""

    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    issuer: str = ""
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    exchanged: list[str] = field(default_factory=list)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/.well-known/openid-configuration", self.discovery)
        app.router.add_post("/api/oidc/token", self.token)
        app.router.add_get("/jwks.json", self.jwks)
        return app

    async def discovery(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/api/oidc/authorization",
                "token_endpoint": f"{self.issuer}/api/oidc/token",
                "jwks_uri": f"{self.issuer}/jwks.json",
            }
        )

    async def jwks(self, _: web.Request) -> web.Response:
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return web.json_response({"keys": [public | {"kid": "k1", "use": "sig", "alg": "RS256"}]})

    async def token(self, request: web.Request) -> web.Response:
        auth = aiohttp.BasicAuth.decode(request.headers["Authorization"])
        form = await request.post()
        grant = self.codes.pop(str(form["code"]), None)
        if (auth.login, auth.password) != (CLIENT_ID, SECRET) or grant is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(str(form["code_verifier"]).encode()).digest()).rstrip(b"=")
        if challenge.decode() != grant.pop("_challenge"):
            return web.json_response({"error": "invalid_grant"}, status=400)
        self.exchanged.append(str(form["code"]))
        return web.json_response({"access_token": "ignored", "id_token": grant.pop("_id_token")})

    def code_for(self, sub: str, *, nonce: str = "n-1", headers: dict[str, Any] | None = None, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "sub": sub,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "preferred_username": sub,
            "email": f"{sub}@example.com",
            "groups": ["waitlist"],
        } | claims
        if headers and headers.get("alg") == "HS256":
            token = jwt.encode(payload, SECRET, algorithm="HS256")
        else:
            token = jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "k1"} | (headers or {}))
        code = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
        self.codes[code] = {"_id_token": token, "_challenge": challenge}
        return code


@dataclass
class Rig:
    idp: FakeIdP
    client: TestClient
    db: Database
    clock: list[float]

    async def sign_in(self, code: str, *, nonce: str = "n-1", token: str | None = None) -> aiohttp.ClientResponse:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.post(
            "/auth/session",
            json={"code": code, "code_verifier": VERIFIER, "state": "s", "nonce": nonce},
            headers=headers,
        )

    async def token(self, sub: str) -> str:
        response = await self.sign_in(self.idp.code_for(sub))
        assert response.status == 200, await response.text()
        return str((await response.json())["token"])

    async def me(self, token: str) -> aiohttp.ClientResponse:
        return await self.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    async def sql(self, query: str, *args: Any) -> list[asyncpg.Record]:
        conn = await self.db.connect()
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def invite(self, username: str) -> None:
        await self.sql(
            "INSERT INTO invites (id, match_claim, match_value, limits, expires_at)"
            " VALUES ($1, 'preferred_username', $2, '{\"maxTrips\": 5}', now() + interval '30 days')",
            secrets.token_hex(8),
            username,
        )


@pytest.fixture
async def rig(
    db: Database, aiohttp_server: Callable[..., Any], aiohttp_client: Callable[..., Any]
) -> AsyncIterator[Rig]:
    idp = FakeIdP()
    idp_server: TestServer = await aiohttp_server(idp.app())
    idp.issuer = str(idp_server.make_url("")).rstrip("/")
    pool = await asyncpg.create_pool(db.dsn(ROLES.web), min_size=1, max_size=4)
    clock = [time.time()]
    async with aiohttp.ClientSession() as http:
        app = web.Application(middlewares=[require_session])
        app[OIDC] = OidcClient(
            http,
            OidcSettings(issuer=idp.issuer, client_id=CLIENT_ID, client_secret=SECRET, redirect_uri="https://x/cb"),
        )
        app[ACCOUNTS] = Accounts(
            pool, IdentityPolicy(bootstrap_admins=frozenset({(idp.issuer, "owner")})), clock=lambda: clock[0]
        )
        app.router.add_routes(routes())

        async def me(request: web.Request) -> web.Response:
            user = signed_in(request)
            return web.json_response({"uid": user.uid, "admin": user.is_admin})

        app.router.add_get("/api/me", me)
        client: TestClient = await aiohttp_client(app)
        try:
            yield Rig(idp, client, db, clock)
        finally:
            await pool.close()


# ---- admission


async def test_an_invited_person_signs_in(rig: Rig) -> None:
    await rig.invite("ann")
    response = await rig.sign_in(rig.idp.code_for("ann"))
    assert response.status == 200
    assert "Set-Cookie" not in response.headers  # never a cookie
    body = await response.json()
    uid = principal_hash(rig.idp.issuer, "ann")
    assert body["user"] == uid
    me = await rig.me(body["token"])
    assert await me.json() == {"uid": uid, "admin": False}
    assert [r["value"] for r in await rig.sql("SELECT value FROM user_limits WHERE key = 'maxTrips'")] == [5]
    assert (await rig.sql("SELECT used_by_uid FROM invites"))[0]["used_by_uid"] == uid
    stored = await rig.sql("SELECT token_hash FROM sessions")
    assert stored[0]["token_hash"] == hashlib.sha256(body["token"].encode()).digest()  # only the hash is kept


async def test_nobody_else_gets_in(rig: Rig) -> None:
    response = await rig.sign_in(rig.idp.code_for("mallory"))
    assert response.status == 403
    assert (await response.json())["error"] == "Your access is being set up"
    assert [r["preferred_username"] for r in await rig.sql("SELECT preferred_username FROM unmatched_identities")] == [
        "mallory"
    ]
    assert await rig.sql("SELECT * FROM users") == []


async def test_the_bootstrap_admin(rig: Rig) -> None:
    me = await rig.me(await rig.token("owner"))
    assert (await me.json())["admin"] is True


async def test_a_suspended_user_is_refused(rig: Rig) -> None:
    await rig.invite("ann")
    token = await rig.token("ann")
    await rig.sql("UPDATE users SET state = 'suspended'")
    assert (await rig.me(token)).status == 401
    assert (await rig.sign_in(rig.idp.code_for("ann"))).status == 403


async def test_a_cached_identity_that_disagrees_is_refused(rig: Rig) -> None:
    await rig.invite("ann")
    await rig.token("ann")
    await rig.sql("INSERT INTO users (id, username) VALUES ('bbbbbbbbbbbbbbbbbbbb', 'bob')")
    await rig.sql("UPDATE identities SET uid = 'bbbbbbbbbbbbbbbbbbbb'")
    assert (await rig.sign_in(rig.idp.code_for("ann"))).status == 403


# ---- the id_token


@pytest.mark.parametrize(
    "tamper",
    [
        {"nonce": "another"},
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
        {"azp": "someone-else"},
        {"headers": {"alg": "HS256"}},  # "signed" with the client secret
        {"headers": {"kid": "unknown"}},
    ],
)
async def test_id_tokens_are_verified(rig: Rig, tamper: dict[str, Any]) -> None:
    await rig.invite("ann")
    code = rig.idp.code_for("ann", **tamper)
    response = await rig.sign_in(code)
    assert response.status == 400
    assert await rig.sql("SELECT * FROM sessions") == []


async def test_a_reused_or_made_up_code_fails(rig: Rig) -> None:
    await rig.invite("ann")
    code = rig.idp.code_for("ann")
    assert (await rig.sign_in(code)).status == 200
    assert (await rig.sign_in(code)).status == 400
    assert (await rig.sign_in("made-up")).status == 400


# ---- sessions


async def test_api_calls_need_a_session(rig: Rig) -> None:
    response = await rig.client.get("/api/me")
    assert response.status == 401
    assert await response.json() == {"reason": "missing"}
    assert (await rig.me("not-a-token")).status == 401


async def test_sessions_expire_and_are_reverified(rig: Rig) -> None:
    await rig.invite("ann")
    token = await rig.token("ann")
    rig.clock[0] += timedelta(hours=25).total_seconds()
    response = await rig.me(token)
    assert (response.status, await response.json()) == (401, {"reason": "reverify"})
    # The SPA signs in again, quietly, with its current token: that token is rotated out.
    response = await rig.sign_in(rig.idp.code_for("ann"), token=token)
    fresh = (await response.json())["token"]
    assert (await rig.me(fresh)).status == 200
    assert (await (await rig.me(token)).json())["reason"] == "revoked"
    rig.clock[0] += timedelta(days=8).total_seconds()
    assert (await (await rig.me(fresh)).json())["reason"] == "expired"  # 7 days idle


async def test_logout_and_logout_everywhere(rig: Rig) -> None:
    await rig.invite("ann")
    phone, laptop, tablet = await rig.token("ann"), await rig.token("ann"), await rig.token("ann")
    await rig.client.post("/auth/logout", headers={"Authorization": f"Bearer {phone}"})
    assert (await rig.me(phone)).status == 401
    assert (await rig.me(laptop)).status == 200
    response = await rig.client.post("/auth/logout-everywhere", headers={"Authorization": f"Bearer {laptop}"})
    assert await response.json() == {"revoked": 2}
    assert (await rig.me(tablet)).status == 401


async def test_the_spa_s_sign_in_settings(rig: Rig) -> None:
    response = await rig.client.get("/auth/config")
    body = await response.json()
    assert body["client_id"] == CLIENT_ID
    assert body["authorization_endpoint"].endswith("/api/oidc/authorization")
    assert SECRET not in await response.text()


async def test_bad_requests(rig: Rig) -> None:
    response = await rig.client.post("/auth/session", json={"code": "x", "code_verifier": "short", "nonce": "n"})
    assert response.status == 400
    response = await rig.client.post("/auth/session", data="code=x")
    assert response.status == 415


def test_ids_are_dns_safe_and_stable() -> None:
    uid = principal_hash("https://auth.chestr.dev", "1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    assert len(uid) == 20
    assert uid.isalnum()
    assert uid.islower()
    assert uid == principal_hash("https://auth.chestr.dev", "1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    assert uid != principal_hash("https://other.example", "1b4e28ba-2fa1-11d2-883f-0016d3cca427")
