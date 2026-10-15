"""deploy/helmrelease.yaml, home-cluster's copy (docs/multi-tenant/design-reference.md §7.10).

Rendering it needs helm and app-template: tests/apiserver and CI's deploy job do that. These checks need neither.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import msgspec
import pytest

from tests.deploy import EXTERNALSECRET, HELMRELEASE, ROOT, flux_variables, helmrelease


def test_flux_substitutes_nothing() -> None:
    assert flux_variables(HELMRELEASE.read_text()) == []
    text = "a: ${APP}\nb: $APP\nc: '^[0-9]{10}$'\nd: $$ESCAPED\ne: '{{ $x := . }}'\nf: $${X}\n"
    assert flux_variables(text) == ["a: ${APP}", "d: $$ESCAPED", "f: $${X}"]


def test_it_is_an_app_template_release_that_keeps_its_migrations() -> None:
    spec = helmrelease()["spec"]
    assert spec["chartRef"] == {"kind": "OCIRepository", "name": "app-template"}
    # Rolling back onto a migrated schema breaks the app (home-cluster's convention for migrating apps).
    assert spec["upgrade"]["strategy"]["name"] == "RetryOnFailure"


def _secret_keys(node: Any, secret: str) -> set[str]:
    """Keys of `secret` that the values read: env secretKeyRefs and secret volumes' items."""
    keys: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("secretKeyRef")
        if isinstance(ref, dict) and ref.get("name") == secret:
            keys.add(ref["key"])
        if node.get("type") == "secret" and node.get("name") == secret:
            keys |= {item["key"] for item in node.get("items", [])}
        for value in node.values():
            keys |= _secret_keys(value, secret)
    elif isinstance(node, list):
        for value in node:
            keys |= _secret_keys(value, secret)
    return keys


def test_every_key_the_pods_read_is_one_the_externalsecret_writes() -> None:
    """With a template, ESO writes only the templated keys; a key it doesn't write keeps a pod from starting."""
    external_secret = msgspec.yaml.decode(EXTERNALSECRET.read_bytes())
    secret = external_secret["spec"]["target"]["name"]
    read = _secret_keys(helmrelease()["spec"]["values"], secret)
    assert read  # the walk finds them at all
    assert read <= set(external_secret["spec"]["target"]["template"]["data"])


def test_the_streamed_viewer_is_one_of_its_three_values() -> None:
    """Read as YAML reads it: a bare `off` would be false, which both services refuse at startup (and the render
    refuses first). This repo's copy keeps the beta off; a deployment offers it by changing only this value."""
    value = helmrelease()["spec"]["values"]["ferry"]["checkout"]["streamViewer"]
    assert value in ("off", "admins", "everyone")
    if HELMRELEASE == ROOT / "deploy" / "helmrelease.yaml":
        assert value == "off"


# Who is offered the streamed viewer is each deployment's own choice: the one line the copies may differ in.
_STREAM_VIEWER = re.compile(r'^( +streamViewer: )"(?:off|admins|everyone)"$', re.MULTILINE)


@pytest.mark.skipif(not os.environ.get("FW_HOME_CLUSTER"), reason="FW_HOME_CLUSTER (a home-cluster checkout) isn't set")
@pytest.mark.parametrize("ours", [HELMRELEASE, EXTERNALSECRET], ids=lambda p: p.name)
def test_home_cluster_runs_this_copy(ours: Path) -> None:
    theirs = Path(os.environ["FW_HOME_CLUSTER"]) / "kubernetes/apps/ferry-watch/ferry-watch/app" / ours.name
    assert _STREAM_VIEWER.sub(r"\1...", theirs.read_text()) == _STREAM_VIEWER.sub(r"\1...", ours.read_text())
