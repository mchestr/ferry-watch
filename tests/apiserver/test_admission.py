"""The chart's admission policies and RBAC on a real API server (docs/multi-tenant/design-reference.md §7.3, §7.5).

The pod ferry-control builds (pods.py, from the config the chart renders for it) must be admitted, and every
change to it denied, so the two can't drift. Then: once running, only the egress label moves, and only for
ferry-control; and no ferry identity holds a verb §7.3 doesn't list.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from typing import Any

import msgspec
import pytest

from ferry_watch.control.pods import browser_pod
from ferry_watch.web.config import WebConfig
from tests.apiserver.cluster import (
    BREAK_GLASS,
    BROWSERS,
    CONTROL_SA,
    RELEASE_NAMESPACE,
    Cluster,
    KubectlError,
    control_config,
)

pytestmark = pytest.mark.apiserver

TRIP = "3f9c0a1b2c3d4e5f"
OWNER = "u" + "a" * 19
_gens = iter(range(1, 1_000_000))


@pytest.fixture
def pod(cluster: Cluster) -> dict[str, Any]:
    """What ferry-control would create for a new generation of the trip."""
    return browser_pod(TRIP, next(_gens), OWNER, control_config(cluster).pod_params(), now=time.time())


def create(
    cluster: Cluster, pod: dict[str, Any], *, user: str = CONTROL_SA, groups: tuple[str, ...] = (), dry_run: bool = True
) -> None:
    args = ["create", "-f", "-", "-o", "name"]
    if dry_run:
        args.append("--dry-run=server")
    cluster.kubectl(*args, stdin=json.dumps(pod), user=user, groups=groups)


def denied(cluster: Cluster, pod: dict[str, Any], **kw: Any) -> str:
    with pytest.raises(KubectlError) as e:
        create(cluster, pod, **kw)
    return e.value.stderr


def container(pod: dict[str, Any]) -> dict[str, Any]:
    first: dict[str, Any] = pod["spec"]["containers"][0]
    return first


def env(pod: dict[str, Any]) -> list[dict[str, Any]]:
    return container(pod)["env"]  # type: ignore[no-any-return]


# ---- the pod ferry-control builds


def test_both_configs_load_as_the_apps_read_them(cluster: Cluster) -> None:
    """ferry-control's is loaded by the `pod` fixture; ferry-web's has its own strict loader."""
    data = cluster.get("configmap", "ferry-web-config", f"--namespace={RELEASE_NAMESPACE}")["data"]
    web = msgspec.json.decode(data["config.json"], type=WebConfig).check()
    assert web.checkout.origin == control_config(cluster).pod.vnc_origin


def test_ferry_controls_pod_is_admitted(cluster: Cluster, pod: dict[str, Any]) -> None:
    create(cluster, pod)


def test_a_break_glass_person_may_create_the_same_pod(cluster: Cluster, pod: dict[str, Any]) -> None:
    create(cluster, pod, user="mike", groups=(BREAK_GLASS, "system:masters"))


def test_nobody_else_may_even_with_every_verb(cluster: Cluster, pod: dict[str, Any]) -> None:
    assert "Only ferry-control creates browser pods" in denied(cluster, pod, user="mike", groups=("system:masters",))


_DELETE = object()
_LABELS = ("metadata", "labels")
_C = ("spec", "containers", 0)


def _at(pod: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    target: Any = pod
    for key in path:
        target = target[key]
    return target


def _set(*path: str | int, value: Any) -> Callable[[dict[str, Any]], None]:
    """Set the field at `path` (or remove it, with _DELETE), creating the map that holds it if need be."""

    def change(pod: dict[str, Any]) -> None:
        *parents, last = path
        outer = _at(pod, tuple(parents[:-1]))
        key = parents[-1]
        holder = outer[int(key)] if isinstance(outer, list) else outer.setdefault(key, {})
        if value is _DELETE:
            del holder[last]
        else:
            holder[last] = value

    return change


def _append(*path: str | int, item: Any) -> Callable[[dict[str, Any]], None]:
    def change(pod: dict[str, Any]) -> None:
        _at(pod, path).append(item)

    return change


def _env(name: str, value: str | None) -> Callable[[dict[str, Any]], None]:
    def change(pod: dict[str, Any]) -> None:
        items = [e for e in env(pod) if e["name"] != name]
        if value is not None:
            items.append({"name": name, "value": value})
        container(pod)["env"] = items

    return change


OTHER_IMAGE = "ghcr.io/mchestr/ferry-watch-browser@sha256:" + "9" * 64
# Pod Security admission and the ServiceAccount plugin see some changes before the policy does; either refusal will do.
PSA = "violates PodSecurity"
CHANGES: dict[str, tuple[Callable[[dict[str, Any]], None], str | tuple[str, ...]]] = {
    "another image": (_set(*_C, "image", value=OTHER_IMAGE), "allowed image"),
    "an image by tag": (_set(*_C, "image", value="ghcr.io/mchestr/ferry-watch-browser:latest"), "allowed image"),
    "a command": (_set(*_C, "command", value=["sh", "-c", "id"]), "command"),
    "a lifecycle hook": (_set(*_C, "lifecycle", value={"preStop": {"exec": {"command": ["true"]}}}), "hooks"),
    "envFrom": (_set(*_C, "envFrom", value=[{"configMapRef": {"name": "x"}}]), "inputs"),
    "an extra env var": (_env("NODE_OPTIONS", "--inspect"), "environment"),
    "a missing env var": (_env("FW_SANDBOX", None), "environment"),
    "another hub": (_env("FW_HUB_URL", "ws://evil.example/agent/v1"), "environment"),
    "another owner in env": (_env("FW_OWNER", "u" + "b" * 19), "environment"),
    "an env value from a secret": (
        _append(*_C, "env", item={"name": "FW_X", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}),
        "environment",
    ),
    "a hostPath volume": (
        _append("spec", "volumes", item={"name": "host", "hostPath": {"path": "/"}}),
        ("volumes", PSA),
    ),
    "a bigger /tmp": (_set("spec", "volumes", 0, "emptyDir", "sizeLimit", value="2Gi"), "volumes"),
    "a token for another audience": (
        _set(
            "spec",
            "volumes",
            2,
            "projected",
            "sources",
            0,
            "serviceAccountToken",
            "audience",
            value="https://kubernetes.default.svc",
        ),
        "hub token",
    ),
    "a sub-path mount": (_set(*_C, "volumeMounts", 0, "subPath", value="x"), "sub-paths"),
    "an API token": (_set("spec", "automountServiceAccountToken", value=True), "fixed fields"),
    "another service account": (
        _set("spec", "serviceAccountName", value="default"),
        ("fixed fields", "error looking up service account"),
    ),
    "a longer deadline": (_set("spec", "activeDeadlineSeconds", value=10**6), "fixed fields"),
    "no deadline": (_set("spec", "activeDeadlineSeconds", value=_DELETE), "fixed fields"),
    "another hostname": (_set("spec", "hostname", value="fw-0000000000000000-g1"), "fixed fields"),
    "a node selector": (_set("spec", "nodeSelector", value={"kubernetes.io/hostname": "m0"}), "never use"),
    "a host alias": (
        _set("spec", "hostAliases", value=[{"ip": "1.2.3.4", "hostnames": ["www.bcferries.com"]}]),
        "never use",
    ),
    "a DNS config": (_set("spec", "dnsConfig", value={"nameservers": ["1.1.1.1"]}), "never use"),
    "an init container": (
        _set("spec", "initContainers", value=[{"name": "x", "image": "busybox"}]),
        ("never use", PSA),
    ),
    "more tolerations": (
        _append("spec", "tolerations", item={"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}),
        "tolerations",
    ),
    "a writable root": (_set(*_C, "securityContext", "readOnlyRootFilesystem", value=False), "read-only"),
    "a container-level uid": (_set(*_C, "securityContext", "runAsUser", value=2000), "override nothing"),
    "a sysctl": (
        _set("spec", "securityContext", "sysctls", value=[{"name": "net.ipv4.ping_group_range", "value": "0 1"}]),
        "security context",
    ),
    "another seccomp profile": (
        _set("spec", "securityContext", "seccompProfile", value={"type": "Localhost", "localhostProfile": "x.json"}),
        "security context",
    ),
    "more memory": (_set(*_C, "resources", "limits", "memory", value="2Gi"), "resources"),
    "an exec probe": (_set(*_C, "livenessProbe", value={"exec": {"command": ["true"]}, "periodSeconds": 30}), "probe"),
    "a probe of another host": (_set(*_C, "readinessProbe", "httpGet", "host", value="10.0.0.1"), "probe"),
    "another port": (_append(*_C, "ports", item={"name": "debug", "containerPort": 9222}), "ports"),
    "an extra label": (_set(*_LABELS, "extra", value="x"), "labels"),
    "hold egress at create": (_set(*_LABELS, "ferry-watch.io/egress", value="hold"), "labels"),
    "a label for another trip": (_set(*_LABELS, "ferry-watch.io/trip", value="0" * 16), "labels"),
    "a Multus annotation": (_set("metadata", "annotations", "k8s.v1.cni.cncf.io/networks", value="x"), "annotations"),
    "an owner reference": (
        _set(
            "metadata",
            "ownerReferences",
            value=[
                {"apiVersion": "v1", "kind": "ConfigMap", "name": "x", "uid": "00000000-0000-0000-0000-000000000000"}
            ],
        ),
        "owners",
    ),
    "a finalizer": (_set("metadata", "finalizers", value=["example.com/keep"]), "owners"),
    "another name": (_set("metadata", "name", value="browser-1"), "fw-<trip>-g<gen>"),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_any_change_is_denied(cluster: Cluster, pod: dict[str, Any], change: str) -> None:
    mutate, expected = CHANGES[change]
    pod = copy.deepcopy(pod)
    mutate(pod)
    refusal = denied(cluster, pod)
    assert any(message in refusal for message in ((expected,) if isinstance(expected, str) else expected)), refusal


# ---- once it's running


@pytest.fixture
def running(cluster: Cluster, pod: dict[str, Any]) -> str:
    create(cluster, pod, dry_run=False)
    return str(pod["metadata"]["name"])


def _patch(cluster: Cluster, name: str, patch: dict[str, Any], **kw: Any) -> None:
    cluster.kubectl(
        "patch", "pod", name, f"--namespace={BROWSERS}", "--type=merge", f"--patch={json.dumps(patch)}", **kw
    )


def test_ferry_control_flips_the_egress_label_and_nothing_else(cluster: Cluster, running: str) -> None:
    _patch(cluster, running, {"metadata": {"labels": {"ferry-watch.io/egress": "hold"}}}, user=CONTROL_SA)
    _patch(cluster, running, {"metadata": {"labels": {"ferry-watch.io/egress": "allowlist"}}}, user=CONTROL_SA)
    for patch in (
        {"metadata": {"labels": {"ferry-watch.io/egress": "open"}}},
        {"metadata": {"labels": {"ferry-watch.io/owner": "u" + "b" * 19}}},
        {"metadata": {"labels": {"ferry-watch.io/egress": "hold", "extra": "x"}}},
        {"metadata": {"annotations": {"kubernetes.io/egress-bandwidth": "1G"}}},
        {"spec": {"activeDeadlineSeconds": 60}},
    ):
        with pytest.raises(KubectlError, match="can't be changed or entered|Forbidden|forbidden"):
            _patch(cluster, running, patch, user=CONTROL_SA)


def test_nobody_else_flips_it(cluster: Cluster, running: str) -> None:
    with pytest.raises(KubectlError, match="can't be changed or entered"):
        _patch(
            cluster,
            running,
            {"metadata": {"labels": {"ferry-watch.io/egress": "hold"}}},
            user="mike",
            groups=("system:masters",),
        )
    _patch(
        cluster,
        running,
        {"metadata": {"labels": {"ferry-watch.io/egress": "hold"}}},
        user="mike",
        groups=("system:masters", BREAK_GLASS),
    )


def test_nobody_gets_a_shell(cluster: Cluster, running: str) -> None:
    with pytest.raises(KubectlError, match="can't be changed or entered"):
        cluster.kubectl(
            "exec", running, f"--namespace={BROWSERS}", "--", "true", user="mike", groups=("system:masters",)
        )


# ---- RBAC (§7.3)

SERVICE_ACCOUNTS = {
    name: f"system:serviceaccount:{RELEASE_NAMESPACE}:{name}"
    for name in ("ferry-web", "ferry-control", "vnc-edge", "ferry-migrate")
} | {"ferry-browser": f"system:serviceaccount:{BROWSERS}:ferry-browser"}

NEVER = [
    ("get", "secrets"),
    ("list", "secrets"),
    ("create", "pods/exec"),
    ("create", "pods/attach"),
    ("create", "pods/portforward"),
    ("get", "pods/log"),
    ("update", "pods"),
    ("deletecollection", "pods"),
    ("update", "pods/ephemeralcontainers"),
    ("create", "pods/eviction"),
    ("update", "pods/status"),
    ("create", "deployments"),
    ("create", "jobs"),
    ("create", "services"),
    ("get", "services/proxy"),
    ("create", "serviceaccounts/token"),
    ("update", "configmaps"),
    ("create", "networkpolicies"),
    ("create", "roles"),
    ("create", "rolebindings"),
    ("bind", "clusterroles"),
    ("escalate", "roles"),
    ("impersonate", "users"),
    ("create", "resourcequotas"),
    ("create", "validatingadmissionpolicies"),
    ("delete", "namespaces"),
    ("create", "events"),
]


@pytest.mark.parametrize("identity", sorted(SERVICE_ACCOUNTS))
def test_no_ferry_identity_holds_a_forbidden_verb(cluster: Cluster, identity: str) -> None:
    user = SERVICE_ACCOUNTS[identity]
    held = [
        f"{verb} {resource} in {ns}"
        for ns in (RELEASE_NAMESPACE, BROWSERS)
        for verb, resource in NEVER
        if cluster.can(user, verb, resource, ns)
    ]
    assert held == []


def test_only_ferry_control_touches_pods_and_only_in_the_browser_namespace(cluster: Cluster) -> None:
    control = SERVICE_ACCOUNTS["ferry-control"]
    for verb in ("create", "delete", "get", "list", "watch", "patch"):
        assert cluster.can(control, verb, "pods", BROWSERS), verb
    assert not cluster.can(control, "create", "pods", RELEASE_NAMESPACE)
    assert cluster.can(control, "create", "tokenreviews.authentication.k8s.io")
    assert not cluster.can(control, "create", "subjectaccessreviews.authorization.k8s.io")
    assert cluster.can(control, "list", "nodes")
    for name in ("ferry-web", "vnc-edge", "ferry-migrate", "ferry-browser"):
        for verb in ("create", "get", "list", "patch", "delete"):
            assert not cluster.can(SERVICE_ACCOUNTS[name], verb, "pods", BROWSERS), (name, verb)


def test_rbac_slips_are_caught_by_ns_objects(cluster: Cluster) -> None:
    """Defence in depth (M2): even a RoleBinding someone adds by mistake doesn't let a ferry identity write."""
    ns = f"--namespace={RELEASE_NAMESPACE}"
    cluster.kubectl("create", "role", "oops", "--verb=create", "--resource=configmaps", ns)
    cluster.kubectl(
        "create", "rolebinding", "oops", "--role=oops", f"--serviceaccount={RELEASE_NAMESPACE}:ferry-web", ns
    )
    try:
        with pytest.raises(KubectlError, match="can't change objects in its namespaces"):
            cluster.kubectl("create", "configmap", "x", ns, user=SERVICE_ACCOUNTS["ferry-web"])
    finally:
        cluster.kubectl("delete", "rolebinding,role", "oops", ns)
