"""A throwaway PostgreSQL for the control plane's tests.

Starts a private cluster (unix socket only, in a temp dir) once per session, migrates a template
database, and gives each test its own copy. Set WL_TEST_PGHOST (and WL_TEST_PGPORT, WL_TEST_PGUSER) to
use an existing server instead; its user must be able to create roles and databases. Without either,
these tests skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from waitlist.control.db import Roles, grant, migrate

OWNER = "waitlist"
ROLES = Roles()
TEMPLATE = "wl_template"
_names = itertools.count(1)


@dataclass(frozen=True)
class Server:
    host: str  # a socket directory, or a hostname
    port: int
    admin: str  # a role that can create databases and roles

    def dsn(self, user: str, database: str) -> str:
        return f"postgresql://{user}@/{database}?host={self.host}&port={self.port}"


@dataclass(frozen=True)
class Database:
    server: Server
    name: str

    def dsn(self, user: str = OWNER) -> str:
        return self.server.dsn(user, self.name)

    async def connect(self, user: str = OWNER) -> asyncpg.Connection:
        return await asyncpg.connect(self.dsn(user))


def _binaries() -> Path | None:
    for version in ("17", "16", "15"):
        candidate = Path(f"/usr/lib/postgresql/{version}/bin")
        if (candidate / "initdb").exists():
            return candidate
    found = shutil.which("initdb")
    return Path(found).parent if found else None


def _as_postgres(argv: list[str]) -> list[str]:
    """initdb and postgres refuse to run as root; drop to the postgres user if we are root."""
    return ["runuser", "-u", "postgres", "--", *argv] if os.geteuid() == 0 else argv


@contextlib.contextmanager
def _private_cluster() -> Iterator[Server]:
    binaries = _binaries()
    if binaries is None:
        pytest.skip("PostgreSQL isn't installed (or set WL_TEST_PGHOST)")
    root = Path(tempfile.mkdtemp(prefix="wlpg"))
    data = root / "data"
    if os.geteuid() == 0:
        shutil.chown(root, "postgres", "postgres")
    subprocess.run(  # noqa: S603 - fixed argv
        _as_postgres([str(binaries / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"]),
        check=True,
        capture_output=True,
    )
    options = f"-k {root} -c listen_addresses='' -c fsync=off -c full_page_writes=off"
    # The server keeps whatever output it's given open, so it gets a log file rather than our pipes.
    start = [str(binaries / "pg_ctl"), "-D", str(data), "-l", str(root / "log"), "-o", options, "-w", "start"]
    subprocess.run(  # noqa: S603 - fixed argv
        _as_postgres(start),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield Server(str(root), 5432, "postgres")
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            _as_postgres([str(binaries / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"]),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(root, ignore_errors=True)


async def _prepare(server: Server) -> None:
    admin = await asyncpg.connect(server.dsn(server.admin, "postgres"))
    try:
        for role in (OWNER, ROLES.web, ROLES.control):
            with contextlib.suppress(asyncpg.DuplicateObjectError):
                await admin.execute(f"CREATE ROLE {role} LOGIN")
        await admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
        await admin.execute(f"CREATE DATABASE {TEMPLATE} OWNER {OWNER}")
    finally:
        await admin.close()
    owner = await asyncpg.connect(server.dsn(OWNER, TEMPLATE))
    try:
        await migrate(owner)
        await grant(owner, ROLES)
    finally:
        await owner.close()


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run setup on a private loop. asyncio.run() would reset the thread's current loop under pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(scope="session")
def pg_server() -> Iterator[Server]:
    host = os.environ.get("WL_TEST_PGHOST")
    if host:  # a server CI provides
        server = Server(
            host, int(os.environ.get("WL_TEST_PGPORT", "5432")), os.environ.get("WL_TEST_PGUSER", "postgres")
        )
        _run(_prepare(server))
        yield server
        return
    with _private_cluster() as server:
        _run(_prepare(server))
        yield server


@pytest.fixture
async def db(pg_server: Server) -> AsyncIterator[Database]:
    """A fresh, migrated database for one test."""
    name = f"wl_test_{os.getpid()}_{next(_names)}"
    admin = await asyncpg.connect(pg_server.dsn(pg_server.admin, "postgres"))
    try:
        await admin.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE} OWNER {OWNER}")
        yield Database(pg_server, name)
    finally:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()
