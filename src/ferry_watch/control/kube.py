"""The few Kubernetes calls ferry-control makes (docs/multi-tenant/design-reference.md §6.5, §6.6, §7.3).

ferry-control's RBAC allows exactly these: TokenReview (to bind an agent's socket to its pod), and
list/watch/create/delete/patch of pods in the browser namespace. Nothing reads secrets, execs into a
pod or fetches logs. The `Cluster` protocol keeps the rest of the control plane testable with a fake.

A small client over aiohttp rather than a full Kubernetes library: it needs five calls, and the in-cluster
ServiceAccount token is re-read for each, since the kubelet rotates it.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from .pods import LABEL_EGRESS, MANAGED_BY, NAMESPACE

log = logging.getLogger(__name__)

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
AGENT_USERNAME = "system:serviceaccount:ferry-watch-browsers:ferry-browser"
EXTRA_POD_UID = "authentication.kubernetes.io/pod-uid"
EXTRA_POD_NAME = "authentication.kubernetes.io/pod-name"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class KubeError(Exception):
    """The API server refused or failed a call."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status


@dataclass(frozen=True, slots=True)
class PodIdentity:
    """Who presented a token: a browser pod, as the API server vouches for it."""

    pod_uid: str
    pod_name: str


@dataclass(frozen=True, slots=True)
class ObservedPod:
    name: str
    uid: str
    labels: dict[str, str] = field(default_factory=dict)
    phase: str = "Pending"  # Pending, Running, Succeeded, Failed, Unknown
    node: str | None = None
    deleting: bool = False
    unschedulable: bool = False
    termination_message: str | None = None  # one enum line, written by the agent

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> ObservedPod:
        meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
        conditions = status.get("conditions") or []
        unschedulable = any(
            c.get("type") == "PodScheduled" and c.get("status") == "False" and c.get("reason") == "Unschedulable"
            for c in conditions
        )
        message = None
        for container in status.get("containerStatuses") or []:
            terminated = (container.get("state") or {}).get("terminated") or {}
            message = (terminated.get("message") or "").strip()[:100] or message
        return cls(
            name=meta["name"],
            uid=meta["uid"],
            labels=dict(meta.get("labels") or {}),
            phase=status.get("phase", "Pending"),
            node=spec.get("nodeName"),
            deleting=meta.get("deletionTimestamp") is not None,
            unschedulable=unschedulable,
            termination_message=message,
        )


class Cluster(Protocol):
    async def review_token(self, token: str, audience: str) -> PodIdentity | None: ...
    async def list_pods(self) -> list[ObservedPod]: ...
    async def create_pod(self, spec: dict[str, Any]) -> ObservedPod: ...
    async def delete_pod(self, name: str) -> None: ...
    async def set_egress(self, name: str, value: str) -> None: ...


def pod_identity(review: dict[str, Any]) -> PodIdentity | None:
    """The browser pod a TokenReview vouches for, or None if it doesn't vouch for one."""
    status = review.get("status") or {}
    if not status.get("authenticated"):
        return None
    user = status.get("user") or {}
    if user.get("username") != AGENT_USERNAME:
        return None
    extra = user.get("extra") or {}
    uids, names = extra.get(EXTRA_POD_UID) or [], extra.get(EXTRA_POD_NAME) or []
    if len(uids) != 1 or len(names) != 1:
        return None
    return PodIdentity(pod_uid=uids[0], pod_name=names[0])


class KubeCluster:
    """The API server, from inside the cluster, as ferry-control's ServiceAccount."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        *,
        server: str = "https://kubernetes.default.svc",
        namespace: str = NAMESPACE,
        account_dir: Path = SERVICE_ACCOUNT_DIR,
    ) -> None:
        self._http = http
        self._server = server.rstrip("/")
        self._namespace = namespace
        self._account_dir = account_dir
        self._ssl = ssl.create_default_context(cafile=str(account_dir / "ca.crt"))

    async def _call(self, method: str, path: str, *, json: Any = None, content_type: str | None = None) -> Any:
        token = (self._account_dir / "token").read_text().strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        async with self._http.request(
            method, self._server + path, json=json, headers=headers, ssl=self._ssl, timeout=TIMEOUT
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= HTTPStatus.BAD_REQUEST:
                raise KubeError(response.status, str((body or {}).get("reason", response.reason)))
            return body

    async def review_token(self, token: str, audience: str) -> PodIdentity | None:
        review = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [audience]},
        }
        return pod_identity(await self._call("POST", "/apis/authentication.k8s.io/v1/tokenreviews", json=review))

    async def list_pods(self) -> list[ObservedPod]:
        selector = f"app.kubernetes.io/managed-by={MANAGED_BY}"
        body = await self._call("GET", f"/api/v1/namespaces/{self._namespace}/pods?labelSelector={selector}")
        return [ObservedPod.from_api(item) for item in body.get("items", [])]

    async def create_pod(self, spec: dict[str, Any]) -> ObservedPod:
        return ObservedPod.from_api(await self._call("POST", f"/api/v1/namespaces/{self._namespace}/pods", json=spec))

    async def delete_pod(self, name: str) -> None:
        try:
            await self._call("DELETE", f"/api/v1/namespaces/{self._namespace}/pods/{name}")
        except KubeError as e:
            if e.status != HTTPStatus.NOT_FOUND:  # already gone is fine
                raise

    async def set_egress(self, name: str, value: str) -> None:
        patch = {"metadata": {"labels": {LABEL_EGRESS: value}}}
        await self._call(
            "PATCH",
            f"/api/v1/namespaces/{self._namespace}/pods/{name}",
            json=patch,
            content_type="application/strategic-merge-patch+json",
        )
