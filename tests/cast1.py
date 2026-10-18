"""The wl.cast1 conformance pack (tests/fixtures/cast1/README.md), as the agent's tests load it."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

PACK = Path(__file__).parent / "fixtures" / "cast1"


def load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((PACK / f"{name}.json").read_text("utf-8"))
    return doc


def agent_cases(name: str, array: str = "cases") -> Iterator[Any]:
    """The cases of one array the agent runs (all but `only: "viewer"`), as pytest params named by their ids."""
    for case in load(name)[array]:
        if case.get("only") != "viewer":
            yield pytest.param(case, id=case["id"])


@dataclass
class PackKnown:
    """messages.json's `context`: the images and the field map the session holds (cast_protocol.Known)."""

    sizes: dict[int, tuple[int, int]] = field(default_factory=dict)
    latest_of: dict[int, tuple[int, int]] = field(default_factory=dict)
    kinds: dict[tuple[int, int], str] = field(default_factory=dict)
    newest_seq: int = 0

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> PackKnown:
        known = cls()
        for image in context["images"]:  # oldest first: the last one of a page is its latest
            known.sizes[image["seq"]] = (image["w"], image["h"])
            known.latest_of[image["page"]] = (image["w"], image["h"])
            known.newest_seq = max(known.newest_seq, image["seq"])
        for f in context["fields"]:
            known.kinds[(context["rev"], f["id"])] = f["k"]
        return known

    def image(self, seq: int) -> tuple[int, int] | None:
        return self.sizes.get(seq)

    def newest(self) -> int:
        return self.newest_seq

    def latest(self, page: int) -> tuple[int, int] | None:
        return self.latest_of.get(page)

    def field_kind(self, rev: int, field: int) -> str | None:
        return self.kinds.get((rev, field))
