from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from waitlist.control.pods import DEADLINE_SECONDS, PodParams, browser_pod, memory_mib, pod_name

PARAMS = PodParams(
    image="ghcr.io/mchestr/waitlist-browser@sha256:" + "b" * 64,
    hub_url="ws://waitlist-control.waitlist.svc.cluster.local.:8766/agent/v1",
    vnc_pubkeys="k1:AAAA",
    vnc_origin="https://waitlist-checkout.chestr.dev",
)
TRIP = "3f9c0a1b2c3d4e5f"
OWNER = "u5yq2kz7m4c3x8a1b2d3e"


def pod(**kw: Any) -> dict[str, Any]:
    return browser_pod(TRIP, 7, OWNER, kw.pop("params", PARAMS), now=1_790_000_000, **kw)


def test_the_pod_is_the_documented_shape() -> None:
    spec = pod()
    assert spec["metadata"]["name"] == pod_name(TRIP, 7) == "wl-3f9c0a1b2c3d4e5f-g7"
    labels = spec["metadata"]["labels"]
    assert labels["waitlist.chestr.dev/egress"] == "allowlist"
    assert labels["waitlist.chestr.dev/owner"] == OWNER
    assert "ownerReferences" not in spec["metadata"]  # a bare pod
    body = spec["spec"]
    assert body["automountServiceAccountToken"] is False
    assert body["enableServiceLinks"] is False
    assert body["restartPolicy"] == "Never"
    assert body["activeDeadlineSeconds"] == DEADLINE_SECONDS
    assert body["hostname"] == spec["metadata"]["name"]
    assert "hostUsers" not in body
    (container,) = body["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert "lifecycle" not in container  # no hooks: the agent's own SIGTERM drain empties carts
    assert container["resources"]["requests"]["memory"] == container["resources"]["limits"]["memory"]


def test_exactly_eight_literal_env_vars() -> None:
    env = pod()["spec"]["containers"][0]["env"]
    assert [e["name"] for e in env] == [
        "WL_TRIP_ID",
        "WL_OWNER",
        "WL_POD_NAME",
        "WL_DEADLINE_AT",
        "WL_HUB_URL",
        "WL_VNC_PUBKEYS",
        "WL_VNC_ORIGIN",
        "WL_SANDBOX",
    ]
    assert all(set(e) == {"name", "value"} for e in env)  # no valueFrom: nothing read from secrets
    values = {e["name"]: e["value"] for e in env}
    assert values["WL_DEADLINE_AT"] == str(1_790_000_000 + DEADLINE_SECONDS)
    assert values["WL_SANDBOX"] == "0"


def test_the_only_token_is_the_hub_token() -> None:
    volumes = pod()["spec"]["volumes"]
    projected = [v for v in volumes if "projected" in v]
    assert projected == [
        {
            "name": "hub-token",
            "projected": {
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "waitlist-agent",
                            "expirationSeconds": 600,
                            "path": "token",
                        }
                    }
                ]
            },
        }
    ]
    assert not [v for v in volumes if set(v) & {"secret", "hostPath", "configMap", "persistentVolumeClaim"}]


def test_optional_hardening_is_a_values_change() -> None:
    params = replace(PARAMS, sandbox=True, require_user_namespace=True)
    body = pod(params=params)["spec"]
    assert body["hostUsers"] is False
    assert body["securityContext"]["seccompProfile"] == {
        "type": "Localhost",
        "localhostProfile": "profiles/wl-chromium.json",
    }
    assert {"name": "WL_SANDBOX", "value": "1"} in body["containers"][0]["env"]


@pytest.mark.parametrize(
    ("trip", "owner", "gen"),
    [("../etc", OWNER, 1), (TRIP, "Owner", 1), (TRIP, OWNER, 0), ("3F9C0A1B2C3D4E5F", OWNER, 1)],
)
def test_nothing_unexpected_reaches_the_spec(trip: str, owner: str, gen: int) -> None:
    with pytest.raises(ValueError, match="Not a|start at"):
        browser_pod(trip, gen, owner, PARAMS, now=0)


def test_the_image_is_pinned_by_digest() -> None:
    with pytest.raises(ValueError, match="digest"):
        PodParams(image="ghcr.io/mchestr/waitlist-browser:latest", hub_url="", vnc_pubkeys="", vnc_origin="")
    # A tag may ride along for people and Renovate; the digest still decides what runs.
    tagged = "ghcr.io/mchestr/waitlist-browser:0.6.0@sha256:" + "b" * 64
    assert PodParams(image=tagged, hub_url="", vnc_pubkeys="", vnc_origin="").image == tagged


def test_the_spec_is_stable() -> None:
    assert json.dumps(pod(), sort_keys=True) == json.dumps(pod(), sort_keys=True)


@pytest.mark.parametrize(
    ("quantity", "mib"), [("1536Mi", 1536), ("1.5Gi", 1536), ("2G", 1907), ("1073741824", 1024), ("lots", None)]
)
def test_pod_memory_in_mib(quantity: str, mib: int | None) -> None:
    assert memory_mib(quantity) == mib
