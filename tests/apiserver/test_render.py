"""What the deployment refuses to render (tests/deploy.py): helm only, no API server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.deploy import BROWSER_IMAGE, TEST_VALUES, render

pytestmark = pytest.mark.apiserver


@pytest.fixture(scope="module")
def helm() -> Path:
    if not os.environ.get("FW_HELM"):
        pytest.skip("FW_HELM isn't set")
    return Path(os.environ["FW_HELM"])


def with_values(**pod: Any) -> dict[str, Any]:
    ferry = TEST_VALUES["ferry"]
    return {"ferry": ferry | {"control": {"pod": ferry["control"]["pod"] | pod}}}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ferry": {"browserImage": {"tag": "0.6.0"}}}, "must pin the digest"),
        (with_values(vncPubkeys=""), "vncPubkeys"),
        (with_values(podMemory="1.5Gi"), "must be in Mi"),
    ],
)
def test_a_release_must_fill_in_what_the_cluster_checks(helm: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        render(helm, overrides)


def test_one_source_for_the_pod_shape(helm: Path) -> None:
    """ferry-control's pod params and the admission policy's come from the same values."""
    objects = {(o["kind"], o["metadata"]["name"]): o for o in render(helm)}
    params = objects[("ConfigMap", "ferry-browser-policy")]["data"]
    control = objects[("ConfigMap", "ferry-control-config")]["data"]["config.json"]
    assert params["allowedImages"] == BROWSER_IMAGE
    assert f'"image": "{BROWSER_IMAGE}"' in control
    for key in ("hubUrl", "vncPubkeys", "vncOrigin", "podMemory", "cpuRequest"):
        assert f'"{key}": "{params[key]}"' in control, key
    quota = objects[("ResourceQuota", "browsers")]["spec"]["hard"]
    assert quota == {"pods": "8", "requests.memory": "12288Mi", "limits.memory": "12288Mi", "requests.cpu": "1600m"}
