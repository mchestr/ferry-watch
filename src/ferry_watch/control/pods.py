"""The one shape of browser pod ferry-control creates (docs/multi-tenant/design-reference.md §7.4).

The admission policy admits nothing else, so this function and the policy are tested against each other:
every field here is either fixed or comes from the policy's parameters, and the env is exactly eight
literal values. Nothing a user typed reaches the spec except through the trip id, which is 16 hex
characters, and the owner's id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NAMESPACE = "ferry-watch-browsers"
MANAGED_BY = "ferry-watch-control"
SERVICE_ACCOUNT = "ferry-browser"
SUBDOMAIN = "browsers"  # the headless Service vnc-edge resolves pods through
DEADLINE_SECONDS = 93_600  # 26 h: activeDeadlineSeconds, and FW_DEADLINE_AT
GRACE_SECONDS = 90
HUB_TOKEN_AUDIENCE = "ferry-watch-agent"  # noqa: S105 - an audience, not a secret
HUB_TOKEN_SECONDS = 600

LABEL_TRIP = "ferry-watch.io/trip"
LABEL_OWNER = "ferry-watch.io/owner"
LABEL_POD = "ferry-watch.io/pod"
LABEL_GEN = "ferry-watch.io/gen"
LABEL_EGRESS = "ferry-watch.io/egress"
EGRESS_ALLOWLIST = "allowlist"
EGRESS_HOLD = "hold"

_TRIP_ID = re.compile(r"[a-f0-9]{16}")
_OWNER = re.compile(r"[a-z0-9]{1,63}")
_IMAGE = re.compile(r"[a-z0-9./-]+@sha256:[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class PodParams:
    """Values from the chart, rendered into both ferry-control's config and the admission policy's params."""

    image: str  # ghcr.io/mchestr/ferry-watch-browser@sha256:...
    hub_url: str
    vnc_pubkeys: str
    vnc_origin: str
    sandbox: bool = False
    cpu_request: str = "200m"
    pod_memory: str = "1536Mi"
    require_user_namespace: bool = False
    namespace: str = NAMESPACE

    def __post_init__(self) -> None:
        if not _IMAGE.fullmatch(self.image):
            raise ValueError("The browser image must be pinned by digest")


def pod_name(trip_id: str, gen: int) -> str:
    return f"fw-{trip_id}-g{gen}"


def browser_pod(trip_id: str, gen: int, owner: str, params: PodParams, *, now: float) -> dict[str, Any]:
    """The pod for generation `gen` of a trip's browser."""
    if not _TRIP_ID.fullmatch(trip_id):
        raise ValueError("Not a trip id")
    if not _OWNER.fullmatch(owner):
        raise ValueError("Not a user id")
    if gen < 1:
        raise ValueError("Generations start at 1")
    name = pod_name(trip_id, gen)
    deadline_at = int(now) + DEADLINE_SECONDS
    security: dict[str, Any] = {
        "runAsNonRoot": True,
        "runAsUser": 1001,
        "runAsGroup": 1001,
        "seccompProfile": (
            {"type": "Localhost", "localhostProfile": "profiles/fw-chromium.json"}
            if params.sandbox
            else {"type": "RuntimeDefault"}
        ),
    }
    spec: dict[str, Any] = {
        "hostname": name,
        "subdomain": SUBDOMAIN,
        "serviceAccountName": SERVICE_ACCOUNT,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "priorityClassName": "ferry-browser",
        "activeDeadlineSeconds": DEADLINE_SECONDS,
        "terminationGracePeriodSeconds": GRACE_SECONDS,
        "dnsPolicy": "ClusterFirst",
        "securityContext": security,
        "tolerations": [
            {"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 60}
            for key in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
        ],
        "containers": [
            {
                "name": "browser",
                "image": params.image,
                "env": [
                    {"name": "FW_TRIP_ID", "value": trip_id},
                    {"name": "FW_OWNER", "value": owner},
                    {"name": "FW_POD_NAME", "value": name},
                    {"name": "FW_DEADLINE_AT", "value": str(deadline_at)},
                    {"name": "FW_HUB_URL", "value": params.hub_url},
                    {"name": "FW_VNC_PUBKEYS", "value": params.vnc_pubkeys},
                    {"name": "FW_VNC_ORIGIN", "value": params.vnc_origin},
                    {"name": "FW_SANDBOX", "value": "1" if params.sandbox else "0"},
                ],
                "ports": [{"name": "vnc", "containerPort": 6080}, {"name": "probe", "containerPort": 8080}],
                "startupProbe": _probe("/livez", period=10, failures=90),
                "livenessProbe": _probe("/livez", period=30, failures=4),
                "readinessProbe": _probe("/readyz", period=15),
                "resources": {
                    "requests": {
                        "cpu": params.cpu_request,
                        "memory": params.pod_memory,
                        "ephemeral-storage": "64Mi",
                    },
                    "limits": {"cpu": "1500m", "memory": params.pod_memory, "ephemeral-storage": "256Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "terminationMessagePolicy": "File",
                "volumeMounts": [
                    {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 - the pod's own memory-backed emptyDir
                    {"name": "shm", "mountPath": "/dev/shm"},  # noqa: S108
                    {"name": "hub-token", "mountPath": "/var/run/fw", "readOnly": True},
                ],
            }
        ],
        "volumes": [
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"}},
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}},
            {
                "name": "hub-token",
                "projected": {
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "audience": HUB_TOKEN_AUDIENCE,
                                "expirationSeconds": HUB_TOKEN_SECONDS,
                                "path": "token",
                            }
                        }
                    ]
                },
            },
        ],
    }
    if params.require_user_namespace:
        spec["hostUsers"] = False
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": params.namespace,
            "labels": {
                "app.kubernetes.io/name": "ferry-browser",
                "app.kubernetes.io/managed-by": MANAGED_BY,
                LABEL_TRIP: trip_id,
                LABEL_OWNER: owner,
                LABEL_POD: name,
                LABEL_GEN: str(gen),
                LABEL_EGRESS: EGRESS_ALLOWLIST,  # only ferry-control flips it, while a cart is held
            },
            "annotations": {"fluentbit.io/exclude": "true"},
        },
        "spec": spec,
    }


def _probe(path: str, *, period: int, failures: int | None = None) -> dict[str, Any]:
    probe: dict[str, Any] = {"httpGet": {"path": path, "port": "probe"}, "periodSeconds": period}
    if failures is not None:
        probe["failureThreshold"] = failures
    return probe
