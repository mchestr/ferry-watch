"""The import-linter contracts keep up with the providers (docs/waitlist/design.md §2.5): a third provider can't slip
its site parser past the fence, or start importing another provider."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ROOT / "src" / "waitlist" / "providers"


def _contract(name: str) -> dict[str, Any]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    (contract,) = (c for c in config["tool"]["importlinter"]["contracts"] if c["name"] == name)
    return dict(contract)


def _packages() -> list[str]:
    return sorted(p.name for p in PROVIDERS.iterdir() if p.is_dir() and (p / "__init__.py").exists())


def test_every_client_is_fenced() -> None:
    fence = _contract("Only waitlist-fetcher runs a provider's client (it parses what a site sends)")
    clients = sorted(f"waitlist.providers.{p.parent.name}.client" for p in PROVIDERS.glob("*/client.py"))
    assert clients  # Buntzen's, at least
    assert set(clients) <= set(fence["forbidden_modules"])
    assert {"waitlist.web", "waitlist.control", "waitlist.agent"} <= set(fence["source_modules"])


def test_every_provider_is_independent_of_the_others() -> None:
    independence = _contract("Providers don't know about each other")
    modules = {f"waitlist.providers.{name}" for name in _packages()} | {
        f"waitlist.providers.{p.stem}" for p in PROVIDERS.glob("*.py") if p.stem not in ("__init__", "base")
    }
    assert modules == set(independence["modules"])
    assert independence["type"] == "independence"


def test_no_provider_package_imports_its_client() -> None:
    """control and web import waitlist.providers, and import-linter follows transitive imports."""
    for name in _packages():
        text = (PROVIDERS / name / "__init__.py").read_text()
        assert ".client" not in text, name
