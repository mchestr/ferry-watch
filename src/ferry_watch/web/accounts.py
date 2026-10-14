"""Who may use Ferry Watch, and their sessions (docs/multi-tenant/design-reference.md §6.2.2, §6.2.6-6.2.8).

Admission is invite-only. A verified sign-in maps to a user id computed from (issuer, subject); an active
user with that id is signed in, an open invitation or a bootstrap admin creates them, and anyone else is
recorded as "unmatched" and told their access is being set up. A cached identity that disagrees with the
computed id refuses the sign-in.

Sessions carry no cookie: the app hands out an opaque 256-bit bearer token once and stores only its
SHA-256. They expire after 7 days idle or 30 days, and must be re-verified with the identity provider
every 24 hours.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import asyncpg

from .identity import Principal, user_id

log = logging.getLogger(__name__)

IDLE = timedelta(days=7)
ABSOLUTE = timedelta(days=30)
TOUCH_EVERY = timedelta(minutes=5)  # last use is written at most this often
INVITE_DAYS = 30
SETTING_UP = "Your access is being set up"

Unauthorized = Literal["missing", "expired", "reverify", "revoked"]


class Refused(Exception):
    """A sign-in that doesn't let the person in. The reason is shown to them."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    aliases: Mapping[str, str] = field(default_factory=dict)  # principal hash -> existing uid (GitOps)
    bootstrap_admins: frozenset[tuple[str, str]] = frozenset()  # (issuer, username)
    invite_claim: Literal["preferred_username", "email"] = "preferred_username"
    auto_approve_groups: frozenset[str] = frozenset()
    reverify: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if not timedelta(hours=1) <= self.reverify <= timedelta(hours=24):
            raise ValueError("Re-verification must be every 1 to 24 hours")


@dataclass(frozen=True, slots=True)
class SignedIn:
    uid: str
    username: str
    is_admin: bool
    token_hash: bytes


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class Accounts:
    def __init__(self, pool: asyncpg.Pool, policy: IdentityPolicy, *, clock: Callable[[], float] = time.time) -> None:
        self._pool = pool
        self._policy = policy
        self._clock = clock

    # ---- admission

    async def admit(self, principal: Principal) -> str:
        """The user this sign-in is, creating them from an invitation if there is one. Raises Refused."""
        uid = user_id(principal, self._policy.aliases)
        now = _ts(self._clock())
        refusal: str | None = None
        async with self._pool.acquire() as conn, conn.transaction():
            # A refusal is decided in here but raised after the commit, so its record is kept.
            cached = await conn.fetchval(
                "SELECT uid FROM identities WHERE iss = $1 AND sub = $2", principal.issuer, principal.subject
            )
            user = await conn.fetchrow("SELECT state FROM users WHERE id = $1 FOR UPDATE", uid)
            if cached is not None and cached != uid:
                log.error("Identity %s maps to %s, not %s: refused", principal.subject, cached, uid)
                await _audit(conn, "system", "identity", "sign_in_refused", uid, "cached identity mismatch")
                refusal = SETTING_UP
            elif user is None and not await self._create(conn, uid, principal, now):
                refusal = SETTING_UP
            elif user is not None and user["state"] != "active":
                refusal = "Your account is suspended"
            else:
                await conn.execute(
                    "INSERT INTO identities (iss, sub, uid, username, email, last_groups, first_seen, last_seen)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $7) ON CONFLICT (iss, sub) DO UPDATE SET"
                    " username = $4, email = $5, last_groups = $6, last_seen = $7",
                    principal.issuer,
                    principal.subject,
                    uid,
                    principal.username,
                    principal.email,
                    sorted(principal.groups),
                    now,
                )
                await conn.execute("UPDATE users SET last_seen_at = $2 WHERE id = $1", uid, now)
        if refusal is not None:
            raise Refused(refusal)
        return uid

    async def _create(
        self, conn: asyncpg.pool.PoolConnectionProxy, uid: str, principal: Principal, now: datetime
    ) -> bool:
        """Create the user if an invitation, a bootstrap entry or a group allows it; else record them unmatched."""
        policy = self._policy
        claim = principal.username if policy.invite_claim == "preferred_username" else principal.email
        invite = None
        if claim:
            invite = await conn.fetchrow(
                "SELECT id, limits FROM invites WHERE used_at IS NULL AND expires_at > $1 AND match_claim = $2"
                " AND lower(match_value) = lower($3) ORDER BY created_at LIMIT 1 FOR UPDATE",
                now,
                policy.invite_claim,
                claim,
            )
        admin = principal.username is not None and (principal.issuer, principal.username) in policy.bootstrap_admins
        approved = bool(principal.groups & policy.auto_approve_groups)
        if invite is None and not admin and not approved:
            await conn.execute(
                "INSERT INTO unmatched_identities (iss, sub, preferred_username, email, first_seen, last_seen)"
                " VALUES ($1, $2, $3, $4, $5, $5) ON CONFLICT (iss, sub) DO UPDATE SET"
                " preferred_username = $3, email = $4, last_seen = $5",
                principal.issuer,
                principal.subject,
                principal.username,
                principal.email,
                now,
            )
            return False
        await conn.execute(
            "INSERT INTO users (id, username, email, display, is_admin, created_at) VALUES ($1, $2, $3, $4, $5, $6)",
            uid,
            principal.username or uid,
            principal.email,
            principal.name,
            admin,
            now,
        )
        if invite is not None:
            await conn.execute(
                "UPDATE invites SET used_by_uid = $2, used_at = $3 WHERE id = $1", invite["id"], uid, now
            )
            for key, value in (json.loads(invite["limits"]) or {}).items():
                await conn.execute(
                    "INSERT INTO user_limits (user_id, key, value, set_by, reason) VALUES ($1, $2, $3, 'invite', $4)",
                    uid,
                    key,
                    int(value),
                    f"invite {invite['id']}",
                )
        how = "invite" if invite is not None else "bootstrap admin" if admin else "auto-approved group"
        await _audit(conn, "system", "user", "user_created", uid, how)
        log.info("New user %s (%s)", uid, how)
        return True

    # ---- sessions

    async def create_session(self, uid: str, principal: Principal, label: str | None) -> str:
        """A new session; the token is returned once and never stored."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        await self._pool.execute(
            "INSERT INTO sessions (token_hash, user_id, iss, sub, client_label, created_at, last_used_at, verified_at,"
            " idle_expires_at, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $6, $6, $7, $8)",
            token_hash(token),
            uid,
            principal.issuer,
            principal.subject,
            (label or "")[:64] or None,
            _ts(now),
            _ts(now + IDLE.total_seconds()),
            _ts(now + ABSOLUTE.total_seconds()),
        )
        return token

    async def resolve(self, token: str | None) -> SignedIn | Unauthorized:
        """The signed-in user behind a bearer token, or why there isn't one."""
        if not token:
            return "missing"
        return await self.resolve_hash(token_hash(token))

    async def resolve_hash(self, digest: bytes) -> SignedIn | Unauthorized:
        """As `resolve`, for a session already identified by its hash (an open dashboard socket)."""
        row = await self._pool.fetchrow(
            "SELECT s.user_id, s.revoked_at, s.idle_expires_at, s.expires_at, s.verified_at, s.last_used_at,"
            " u.state, u.username, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = $1",
            digest,
        )
        now = self._clock()
        if row is None or row["revoked_at"] is not None or row["state"] != "active":
            return "revoked" if row is not None else "missing"
        if now >= row["idle_expires_at"].timestamp() or now >= row["expires_at"].timestamp():
            return "expired"
        if now >= row["verified_at"].timestamp() + self._policy.reverify.total_seconds():
            return "reverify"
        if now - row["last_used_at"].timestamp() >= TOUCH_EVERY.total_seconds():
            await self._pool.execute(
                "UPDATE sessions SET last_used_at = $2, idle_expires_at = least(expires_at, $3) WHERE token_hash = $1",
                digest,
                _ts(now),
                _ts(now + IDLE.total_seconds()),
            )
        return SignedIn(row["user_id"], row["username"], row["is_admin"], digest)

    async def owner_of(self, token: str) -> str | None:
        """Whose token this is, even if it's due for re-verification (to rotate it after one)."""
        owner: str | None = await self._pool.fetchval(
            "SELECT user_id FROM sessions WHERE token_hash = $1 AND revoked_at IS NULL", token_hash(token)
        )
        return owner

    async def revoke(self, *, token: str | None = None, uid: str | None = None, reason: str) -> int:
        """Revoke one session or all of a user's. Returns how many."""
        if token is not None:
            result = await self._pool.execute(
                "UPDATE sessions SET revoked_at = $2, revoked_reason = $3 WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash(token),
                _ts(self._clock()),
                reason,
            )
        else:
            result = await self._pool.execute(
                "UPDATE sessions SET revoked_at = $2, revoked_reason = $3 WHERE user_id = $1 AND revoked_at IS NULL",
                uid,
                _ts(self._clock()),
                reason,
            )
        return int(result.split()[-1])


async def _audit(
    conn: asyncpg.pool.PoolConnectionProxy, actor_kind: str, target_kind: str, action: str, target: str, reason: str
) -> None:
    await conn.execute(
        "INSERT INTO audit_log (actor_kind, actor, action, target_kind, target_id, reason)"
        " VALUES ($1, 'ferry-web', $2, $3, $4, $5)",
        actor_kind,
        action,
        target_kind,
        target,
        reason,
    )
