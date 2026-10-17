"""What the deployment refuses to render (tests/deploy.py): helm only, no API server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.deploy import BROWSER_IMAGE, TEST_VALUES, render
from waitlist.control import config as control_config
from waitlist.web import config as web_config

pytestmark = pytest.mark.apiserver


@pytest.fixture(scope="module")
def helm() -> Path:
    if not os.environ.get("WL_HELM"):
        pytest.skip("WL_HELM isn't set")
    return Path(os.environ["WL_HELM"])


def with_values(**pod: Any) -> dict[str, Any]:
    values = TEST_VALUES["waitlist"]
    return TEST_VALUES | {"waitlist": values | {"control": {"pod": values["control"]["pod"] | pod}}}


def with_stream_viewer(value: Any) -> dict[str, Any]:
    return TEST_VALUES | {"waitlist": TEST_VALUES["waitlist"] | {"checkout": {"streamViewer": value}}}


def with_copy(top: str, service: str, value: Any) -> dict[str, Any]:
    """checkout.streamViewer `top`, and a copy of it in one service's own config values."""
    values = with_stream_viewer(top)["waitlist"]
    copy = {"checkout": {"streamViewer": value}}
    if service == "control":
        return TEST_VALUES | {"waitlist": values | {"control": values["control"] | copy}}
    return TEST_VALUES | {"waitlist": values | {"web": copy}}


def with_tag(controller: str, tag: str) -> dict[str, Any]:
    controllers = TEST_VALUES["controllers"]
    return TEST_VALUES | {"controllers": controllers | {controller: {"containers": {"app": {"image": {"tag": tag}}}}}}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            TEST_VALUES | {"waitlist": TEST_VALUES["waitlist"] | {"browserImage": {"tag": "0.6.0"}}},
            "must pin its digest",
        ),
        (with_tag("waitlist-control", "0.6.0"), "must pin its digest"),
        (with_tag("vnc-edge", "0.6.0"), "must pin its digest"),
        (with_values(vncPubkeys=""), "vncPubkeys"),
        (with_values(podMemory="1.5Gi"), "must be in Mi"),
        (with_values(cpuRequest="1"), "millicores"),
        (with_stream_viewer(False), "streamViewer"),  # a bare `off` in YAML
        (with_stream_viewer("on"), "streamViewer"),
        (with_stream_viewer(None), "streamViewer"),
        # One source (found in review: with "off" rendering no key, a copy under control filled the gap, so pods
        # kept cast_allowed while waitlist-web showed the beta as off). Any copy is refused, matching or not.
        (with_copy("off", "control", "everyone"), "waitlist.checkout.streamViewer only"),
        (with_copy("off", "web", "everyone"), "waitlist.checkout.streamViewer only"),
        (with_copy("admins", "control", "admins"), "waitlist.checkout.streamViewer only"),
    ],
)
def test_a_release_must_fill_in_what_the_cluster_checks(helm: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        render(helm, overrides)


def test_one_source_for_the_pod_shape(helm: Path) -> None:
    """waitlist-control's pod params and the admission policy's come from the same values."""
    objects = {(o["kind"], o["metadata"]["name"]): o for o in render(helm)}
    params = objects[("ConfigMap", "waitlist-browser-policy")]["data"]
    control = objects[("ConfigMap", "waitlist-control-config")]["data"]["config.json"]
    assert params["allowedImages"] == BROWSER_IMAGE
    assert f'"image": "{BROWSER_IMAGE}"' in control
    for key in ("hubUrl", "vncPubkeys", "vncOrigin", "podMemory", "cpuRequest"):
        assert f'"{key}": "{params[key]}"' in control, key
    quota = objects[("ResourceQuota", "browsers")]["spec"]["hard"]
    assert quota == {"pods": "8", "requests.memory": "12288Mi", "limits.memory": "12288Mi", "requests.cpu": "1600m"}


@pytest.mark.parametrize("value", [None, "off", "admins", "everyone"])
def test_one_source_for_who_may_stream(helm: Path, tmp_path: Path, value: str | None) -> None:
    """waitlist-web (the Settings switch and stream links) and waitlist-control (each pod's cast_allowed) read the same
    value, and each starts with its config. None: the deploy file's own, the kill switch."""
    objects = {
        (o["kind"], o["metadata"]["name"]): o
        for o in render(helm, None if value is None else with_stream_viewer(value))
    }
    control, web = tmp_path / "control.json", tmp_path / "web.json"
    control.write_text(objects[("ConfigMap", "waitlist-control-config")]["data"]["config.json"])
    web.write_text(objects[("ConfigMap", "waitlist-web-config")]["data"]["config.json"])
    assert control_config.load(control).hub_settings().stream_viewer == (value or "off")
    assert web_config.load(web, client_id="waitlist").checkout.stream_viewer == (value or "off")
    migrate = objects[("ConfigMap", "waitlist-migrate-config")]["data"]["config.json"]
    assert migrate == control.read_text()  # what the db hook reads


@pytest.mark.parametrize("value", [None, "off"])
def test_off_renders_the_configs_as_0_6_4_did(helm: Path, value: str | None) -> None:
    """0.6.4's configs refuse unknown keys: with the kill switch off, the 0.7.0 values render none, so rolling the
    images back to 0.6.4 (without the values) leaves waitlist-web, waitlist-control and the db hook starting."""
    objects = {
        (o["kind"], o["metadata"]["name"]): o
        for o in render(helm, None if value is None else with_stream_viewer(value))
    }
    configs = [
        json.loads(objects[("ConfigMap", name)]["data"]["config.json"])
        for name in ("waitlist-control-config", "waitlist-migrate-config", "waitlist-web-config")
    ]
    control, migrate, web = configs
    assert "checkout" not in control
    assert "checkout" not in migrate
    assert set(web["checkout"]) == {"origin"}
