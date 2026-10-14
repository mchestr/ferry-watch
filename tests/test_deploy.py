"""deploy/helmrelease.yaml, home-cluster's copy (docs/multi-tenant/design-reference.md §7.10).

Rendering it needs helm and app-template: tests/apiserver and CI's deploy job do that. These checks need neither.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.deploy import HELMRELEASE, flux_variables, helmrelease


def test_flux_substitutes_nothing() -> None:
    assert flux_variables(HELMRELEASE.read_text()) == []
    assert flux_variables("a: ${APP}\nb: $APP\nc: '^[0-9]{10}$'\nd: $$ESCAPED\n") == ["a: ${APP}", "b: $APP"]


def test_it_is_an_app_template_release_that_keeps_its_migrations() -> None:
    spec = helmrelease()["spec"]
    assert spec["chartRef"] == {"kind": "OCIRepository", "name": "app-template"}
    # Rolling back onto a migrated schema breaks the app (home-cluster's convention for migrating apps).
    assert spec["upgrade"]["strategy"]["name"] == "RetryOnFailure"


@pytest.mark.skipif(not os.environ.get("FW_HOME_CLUSTER"), reason="FW_HOME_CLUSTER (a home-cluster checkout) isn't set")
def test_home_cluster_runs_this_copy() -> None:
    theirs = Path(os.environ["FW_HOME_CLUSTER"]) / "kubernetes/apps/ferry-watch/ferry-watch/app/helmrelease.yaml"
    assert theirs.read_text() == HELMRELEASE.read_text()
