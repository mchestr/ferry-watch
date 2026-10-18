"""deploy/helmrelease.yaml, home-cluster's copy (docs/multi-tenant/design-reference.md §7.10, design.md §8).

The first checks need neither helm nor app-template. The render checks below them need WL_HELM (and WL_APP_TEMPLATE
for a local chart), so they carry the apiserver mark: CI's apiserver job has helm, the plain python job doesn't.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any

import msgspec
import pytest

from tests.deploy import (
    EXTERNALSECRET,
    HELMRELEASE,
    NAMESPACE,
    ROOT,
    TEST_VALUES,
    flux_variables,
    helmrelease,
    merged,
    objects,
    render,
    with_provider,
)
from waitlist.control import config as control_config
from waitlist.fetcher import config as fetcher_config
from waitlist.providers.buntzen.meta import HOSTS
from waitlist.web import config as web_config


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
    value = helmrelease()["spec"]["values"]["waitlist"]["checkout"]["streamViewer"]
    assert value in ("off", "admins", "everyone")
    if HELMRELEASE == ROOT / "deploy" / "helmrelease.yaml":
        assert value == "off"


def test_providers_are_off_in_this_copy() -> None:
    """The same rule for each provider's offer (§8.5): this repo's copy offers none; home-cluster's chooses."""
    providers = helmrelease()["spec"]["values"]["waitlist"]["providers"]
    assert set(providers) == {"buntzen"}
    for provider in providers.values():
        assert provider["offer"] in ("off", "admins", "everyone")
        if HELMRELEASE == ROOT / "deploy" / "helmrelease.yaml":
            assert provider["offer"] == "off"


# Who is offered the streamed viewer and each provider is each deployment's own choice: the lines the copies may
# differ in.
_DEPLOYMENT_CHOICES = re.compile(r'^( +(?:streamViewer|offer): )"(?:off|admins|everyone)"$', re.MULTILINE)


def differences(ours: str, theirs: str) -> list[str]:
    """The lines two copies differ in, once each deployment's own choices are masked: none, for copies in step."""
    masked = [_DEPLOYMENT_CHOICES.sub(r"\1...", text) for text in (ours, theirs)]
    a, b = (text.splitlines() for text in masked)
    changed = [f"line {n}: {x!r} != {y!r}" for n, (x, y) in enumerate(zip(a, b, strict=False), 1) if x != y]
    if len(a) != len(b):
        changed.append(f"{len(a)} lines != {len(b)} lines")
    if masked[0] != masked[1] and not changed:
        changed.append("the line endings differ")
    return changed


def test_the_mask_hides_the_deployment_choices_and_nothing_else() -> None:
    ours = '      checkout:\n        streamViewer: "off"\n      buntzen:\n        offer: "off"\n        rate: 12\n'
    assert differences(ours, ours.replace('streamViewer: "off"', 'streamViewer: "admins"')) == []
    assert differences(ours, ours.replace('offer: "off"', 'offer: "everyone"')) == []
    # A third line, a trailing comment on a masked line, or a value outside the three is a difference.
    assert differences(ours, ours.replace("rate: 12", "rate: 13")) == [
        "line 5: '        rate: 12' != '        rate: 13'"
    ]
    assert differences(ours, ours.replace('offer: "off"', 'offer: "off" # admins later'))
    assert differences(ours, ours.replace('offer: "off"', "offer: off"))
    assert differences(ours, ours + "\n")
    assert differences(ours, ours.rstrip("\n")) == ["the line endings differ"]


@pytest.mark.skipif(not os.environ.get("WL_HOME_CLUSTER"), reason="WL_HOME_CLUSTER (a home-cluster checkout) isn't set")
@pytest.mark.parametrize("ours", [HELMRELEASE, EXTERNALSECRET], ids=lambda p: p.name)
def test_home_cluster_runs_this_copy(ours: Path) -> None:
    theirs = Path(os.environ["WL_HOME_CLUSTER"]) / "kubernetes/apps/waitlist/waitlist/app" / ours.name
    assert differences(ours.read_text(), theirs.read_text()) == []


# ---- the render (helm and app-template)

render_test = pytest.mark.apiserver

FETCHER_URL = f"http://waitlist-fetcher.{NAMESPACE}.svc.cluster.local.:8767"
CONFIGS = ("waitlist-control-config", "waitlist-migrate-config", "waitlist-web-config", "waitlist-fetcher-config")


@pytest.fixture(scope="module")
def helm() -> Path:
    if not os.environ.get("WL_HELM"):
        pytest.skip("WL_HELM isn't set")
    return Path(os.environ["WL_HELM"])


@pytest.fixture(scope="module")
def as_is(helm: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """This copy (every provider "off") with a release's images and key."""
    return objects(render(helm))


@pytest.fixture(scope="module")
def buntzen_on(helm: Path) -> dict[tuple[str, str], dict[str, Any]]:
    return objects(render(helm, with_provider(offer="admins")))


def config(rendered: dict[tuple[str, str], dict[str, Any]], name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(rendered[("ConfigMap", name)]["data"]["config.json"])
    return data


@render_test
@pytest.mark.parametrize("offer", ["off", "admins", "everyone"])
def test_each_offer_renders(helm: Path, offer: str) -> None:
    rendered = objects(render(helm, with_provider(offer=offer)))
    assert config(rendered, "waitlist-control-config") == config(rendered, "waitlist-migrate-config")
    for name in CONFIGS:
        assert config(rendered, name)
    assert ("Deployment", "waitlist-fetcher") in rendered  # always, even with every provider off


@render_test
@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"offer": False}, "offer must be"),  # a bare `off` in YAML
        ({"offer": "on"}, "offer must be"),
        ({"offer": None}, "offer must be"),
        ({"requestsPerMinute": 13, "fetcherPerMinute": 12}, "requestsPerMinute <= fetcherPerMinute"),
        ({"requestsPerMinute": 31, "fetcherPerMinute": 31}, "fetcherPerMinute <= 30"),
        ({"fetcherPerMinute": 31}, "fetcherPerMinute <= 30"),
        ({"requestsPerMinute": 0}, "1 <= requestsPerMinute"),
        ({"requestsPerMinute": 2.5}, "must be a whole number"),
        ({"requestsPerMinute": "12"}, "must be a whole number"),
        ({"offer": "off", "fetcherPerMinute": 60}, "fetcherPerMinute <= 30"),  # checked even when off
    ],
)
def test_a_bad_provider_value_is_refused(helm: Path, values: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        render(helm, with_provider(**values))


@render_test
@pytest.mark.parametrize(
    "by_hand",
    [
        {"control": {"fetcher": {"url": FETCHER_URL}}},
        {"control": {"httpChecks": {"buntzen": {"requestsPerMinute": 12}}}},
        {"web": {"providers": {"buntzen": {"offer": "everyone"}}}},
        {"fetcher": {"providers": {"buntzen": {"maxPerMinute": 30}}}},
        {"fetcher": {"listenPort": 9999}},
    ],
)
def test_waitlist_providers_is_the_one_source(helm: Path, by_hand: dict[str, Any]) -> None:
    """As for streamViewer: a key set by hand in one config would let the configs disagree about a provider."""
    with pytest.raises(RuntimeError, match="Set waitlist.providers only"):
        render(helm, merged(TEST_VALUES, {"waitlist": by_hand}))


@render_test
def test_off_renders_no_provider_keys(as_is: dict[tuple[str, str], dict[str, Any]]) -> None:
    """This copy, as it is: the configs are BC Ferries-only, as 0.7.1's were (acceptance criterion 5)."""
    control, migrate, web = (config(as_is, name) for name in CONFIGS[:3])
    for rendered in (control, migrate):
        assert not {"fetcher", "httpChecks"} & set(rendered)
    assert "providers" not in web
    assert config(as_is, "waitlist-fetcher-config") == {
        "listenPort": 8767,
        "upstreamTimeoutSeconds": 10,
        "maxBodyBytes": 262144,
        "providers": {},
    }


@render_test
def test_on_renders_the_contract_shapes(buntzen_on: dict[tuple[str, str], dict[str, Any]]) -> None:
    """§4.4, §4.5 and §6.7's rendered JSON, from waitlist.providers.buntzen and waitlist.fetcher."""
    for name in ("waitlist-control-config", "waitlist-migrate-config"):
        rendered = config(buntzen_on, name)
        assert rendered["fetcher"] == {"url": FETCHER_URL, "timeoutSeconds": 20}
        assert rendered["httpChecks"] == {"buntzen": {"requestsPerMinute": 12}}
    assert config(buntzen_on, "waitlist-web-config")["providers"] == {"buntzen": {"offer": "admins"}}
    assert config(buntzen_on, "waitlist-fetcher-config") == {
        "listenPort": 8767,
        "upstreamTimeoutSeconds": 10,
        "maxBodyBytes": 262144,
        "providers": {"buntzen": {"maxPerMinute": 30}},
    }
    # The URL names the Service that fronts the fetcher, on its port.
    service = buntzen_on[("Service", "waitlist-fetcher")]["spec"]
    assert service["ports"][0]["port"] == 8767
    assert service["selector"]["app.kubernetes.io/controller"] == "waitlist-fetcher"


def _load_control(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = msgspec.to_builtins(control_config.load(path))
    return loaded


def _load_web(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = msgspec.to_builtins(web_config.load(path, client_id="from-the-secret"))
    return loaded


def _load_fetcher(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = msgspec.to_builtins(fetcher_config.load(path))
    return loaded


@render_test
def test_the_off_configs_load_as_the_apps_read_them(
    as_is: dict[tuple[str, str], dict[str, Any]], tmp_path: Path
) -> None:
    for name, load in (
        ("waitlist-control-config", _load_control),
        ("waitlist-migrate-config", _load_control),
        ("waitlist-web-config", _load_web),
    ):
        (tmp_path / name).write_text(as_is[("ConfigMap", name)]["data"]["config.json"])
        load(tmp_path / name)


@render_test
@pytest.mark.parametrize("offer", ["off", "admins"])
def test_the_fetcher_config_loads(helm: Path, tmp_path: Path, offer: str) -> None:
    rendered = objects(render(helm, with_provider(offer=offer)))
    (tmp_path / "config.json").write_text(rendered[("ConfigMap", "waitlist-fetcher-config")]["data"]["config.json"])
    loaded = _load_fetcher(tmp_path / "config.json")
    assert loaded["providers"] == ({} if offer == "off" else {"buntzen": {"maxPerMinute": 30}})


@render_test
@pytest.mark.parametrize("offer", ["admins", "everyone"])
def test_the_provider_configs_load_as_the_apps_read_them(helm: Path, tmp_path: Path, offer: str) -> None:
    rendered = objects(render(helm, with_provider(offer=offer)))
    for name in ("waitlist-control-config", "waitlist-migrate-config"):
        (tmp_path / name).write_text(rendered[("ConfigMap", name)]["data"]["config.json"])
        loaded = _load_control(tmp_path / name)
        assert loaded["fetcher"] == {"url": FETCHER_URL, "timeoutSeconds": 20}
        assert loaded["httpChecks"] == {"buntzen": {"requestsPerMinute": 12}}
    (tmp_path / "web.json").write_text(rendered[("ConfigMap", "waitlist-web-config")]["data"]["config.json"])
    assert _load_web(tmp_path / "web.json")["providers"] == {"buntzen": {"offer": offer}}


# ---- waitlist-fetcher holds nothing, and talks to nothing but control and the providers' hosts


@render_test
def test_the_fetcher_has_no_token_no_database_and_no_secret(as_is: dict[tuple[str, str], dict[str, Any]]) -> None:
    deployment = as_is[("Deployment", "waitlist-fetcher")]["spec"]
    assert deployment["replicas"] == 1
    assert deployment["strategy"]["type"] == "RollingUpdate"
    pod = deployment["template"]["spec"]
    assert pod["serviceAccountName"] == "waitlist-fetcher"
    assert pod["automountServiceAccountToken"] is False
    assert as_is[("ServiceAccount", "waitlist-fetcher")]["automountServiceAccountToken"] is False
    assert pod["dnsConfig"] == {"options": [{"name": "ndots", "value": "1"}]}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    # Its volumes: its own config and /tmp. No secret, no projected token, nothing else.
    assert {v["name"]: sorted(set(v) - {"name"}) for v in pod["volumes"]} == {
        "fetcher-config": ["configMap"],
        "tmp": ["emptyDir"],
    }
    assert pod["volumes"][0]["configMap"]["name"] == "waitlist-fetcher-config"
    [container] = pod["containers"]
    assert not pod.get("initContainers")
    assert container["args"] == ["waitlist-fetcher", "run"]
    assert (
        container["image"]
        == as_is[("Deployment", "waitlist-control")]["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    assert {e["name"] for e in container["env"]} == {"WL_FETCHER_CONFIG", "WL_LOG_LEVEL"}
    assert all("valueFrom" not in e for e in container["env"])
    assert "envFrom" not in container
    assert {m["mountPath"] for m in container["volumeMounts"]} == {"/etc/waitlist-fetcher", "/tmp"}
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["livenessProbe"]["httpGet"] == {"path": "/livez", "port": 8767}
    assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8767}
    assert container["resources"] == {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "192Mi"}}
    assert container["ports"] == [{"name": "http", "containerPort": 8767}]
    # No RBAC: nothing in the render binds its ServiceAccount to anything.
    for (kind, name), o in as_is.items():
        if kind in ("RoleBinding", "ClusterRoleBinding"):
            assert all(s["name"] != "waitlist-fetcher" for s in o.get("subjects", [])), name


def _selects(selector: dict[str, Any], controller: str) -> bool:
    return bool(selector.get("matchLabels", {}).get("app.kubernetes.io/controller") == controller)


@render_test
def test_the_fetcher_policies_are_the_contracts(as_is: dict[tuple[str, str], dict[str, Any]]) -> None:
    """§8.2: in from waitlist-control's pods only; out to kube-dns for the providers' names, those names on 443,
    and never a private, CGNAT, link-local or loopback address."""
    native = as_is[("NetworkPolicy", "waitlist-fetcher")]["spec"]
    assert _selects(native["podSelector"], "waitlist-fetcher")
    assert native["policyTypes"] == ["Ingress", "Egress"]
    assert native["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/instance": "waitlist",
                            "app.kubernetes.io/controller": "waitlist-control",
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8767}],
        }
    ]
    assert not native.get("egress")
    cilium = as_is[("CiliumNetworkPolicy", "waitlist-fetcher-cilium")]["spec"]
    assert _selects(cilium["endpointSelector"], "waitlist-fetcher")
    assert "ingress" not in cilium
    dns, https = cilium["egress"]
    assert dns["toEndpoints"] == [
        {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}
    ]
    assert dns["toPorts"] == [
        {"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": [{"matchName": host} for host in HOSTS]}}
    ]
    assert https == {
        "toFQDNs": [{"matchName": host} for host in HOSTS],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    }
    [deny] = cilium["egressDeny"]
    denied = [ipaddress.ip_network(c["cidr"]) for c in deny["toCIDRSet"]]
    assert [str(n) for n in denied] == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
    ]
    assert all(not n.is_global for n in denied)


@render_test
def test_only_control_may_reach_the_fetcher_and_it_gains_nothing_else(
    as_is: dict[tuple[str, str], dict[str, Any]],
) -> None:
    control = as_is[("NetworkPolicy", "waitlist-control")]["spec"]["egress"]
    postgres, fetcher, dns = control
    assert postgres["ports"] == [{"protocol": "TCP", "port": 5432}]
    assert fetcher == {
        "to": [
            {
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/instance": "waitlist",
                        "app.kubernetes.io/controller": "waitlist-fetcher",
                    }
                }
            }
        ],
        "ports": [{"protocol": "TCP", "port": 8767}],
    }
    assert {p["port"] for p in dns["ports"]} == {53}
    # Still no internet: control's Cilium policy adds only the API server.
    assert as_is[("CiliumNetworkPolicy", "waitlist-control-cilium")]["spec"]["egress"] == [
        {"toEntities": ["kube-apiserver"], "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}]}
    ]
    # No other policy lets anything out to the fetcher.
    reaching = []
    for (kind, name), o in as_is.items():
        if kind not in ("NetworkPolicy", "CiliumNetworkPolicy"):
            continue
        for rule in o["spec"].get("egress", []):
            peers = rule.get("to", []) + rule.get("toEndpoints", [])
            if any(_selects(peer.get("podSelector", peer), "waitlist-fetcher") for peer in peers):
                reaching.append(name)
    assert reaching == ["waitlist-control"]


# ---- the admission policies reach only Waitlist's namespaces (§8.1)

CLUSTER_SCOPED = {
    "Namespace",
    "PriorityClass",
    "ClusterRole",
    "ClusterRoleBinding",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
}
POD_POLICIES = ("wl-browser-pod", "wl-browser-pod-lock")
ONLY_BROWSERS = {"matchLabels": {"kubernetes.io/metadata.name": "waitlist-browsers"}}
BOTH_NAMESPACES = {
    "matchExpressions": [
        {"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["waitlist", "waitlist-browsers"]}
    ]
}
SERVICE_ACCOUNT_PREFIXES = ("system:serviceaccount:waitlist:", "system:serviceaccount:waitlist-browsers:")


def _namespace_names(selector: dict[str, Any]) -> set[str]:
    names = {v for k, v in selector.get("matchLabels", {}).items() if k == "kubernetes.io/metadata.name"}
    for e in selector.get("matchExpressions", []):
        if e["key"] == "kubernetes.io/metadata.name":
            names |= set(e.get("values", []))
    return names


def admission_policies_reach_only_waitlist(rendered: dict[tuple[str, str], dict[str, Any]]) -> None:
    """The change that adds Waitlist also deletes Ferry Watch, and Flux may apply one before it prunes the other:
    nothing cluster-scoped may share a name with Ferry Watch's, and no admission policy may reach a ferry*
    namespace."""
    assert all(kind in CLUSTER_SCOPED for (kind, _), o in rendered.items() if "namespace" not in o["metadata"])
    cluster_scoped = [name for (kind, name) in rendered if kind in CLUSTER_SCOPED]
    assert cluster_scoped
    assert not [name for name in cluster_scoped if "ferry" in name or "fw-" in name]
    policies = {name: o["spec"] for (kind, name), o in rendered.items() if kind == "ValidatingAdmissionPolicy"}
    bindings = {name: o["spec"] for (kind, name), o in rendered.items() if kind == "ValidatingAdmissionPolicyBinding"}
    assert set(policies) == set(bindings) == {*POD_POLICIES, "wl-ns-objects"}
    for name in policies:
        assert bindings[name]["policyName"] == name
        for selector in (
            policies[name]["matchConstraints"].get("namespaceSelector", {}),
            bindings[name].get("matchResources", {}).get("namespaceSelector", {}),
        ):
            assert not [n for n in _namespace_names(selector) if n.startswith("ferry")], name
    for name in POD_POLICIES:
        assert policies[name]["matchConstraints"]["namespaceSelector"] == ONLY_BROWSERS
        assert bindings[name]["matchResources"] == {"namespaceSelector": ONLY_BROWSERS}
    ns_objects = policies["wl-ns-objects"]
    # Exactly both: it is what stops waitlist-control's ServiceAccount writing Secrets, Roles or routes in waitlist.
    assert ns_objects["matchConstraints"]["namespaceSelector"] == BOTH_NAMESPACES
    assert "matchResources" not in bindings["wl-ns-objects"]
    [validation] = ns_objects["validations"]
    for prefix in SERVICE_ACCOUNT_PREFIXES:
        assert f"'{prefix}'" in validation["expression"]


@render_test
def test_admission_policies_reach_only_waitlist(as_is: dict[tuple[str, str], dict[str, Any]]) -> None:
    admission_policies_reach_only_waitlist(as_is)


def _vap(kind: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "metadata": {"name": name}, "spec": spec}


def _policies(ns_objects: dict[str, Any] = BOTH_NAMESPACES, pods: dict[str, Any] = ONLY_BROWSERS) -> list[Any]:
    """A hand-built render of the three policies, their bindings and one ClusterRole."""
    expression = " || ".join(f"x.startsWith('{prefix}')" for prefix in SERVICE_ACCOUNT_PREFIXES)
    ns = {"matchConstraints": {"namespaceSelector": ns_objects}, "validations": [{"expression": expression}]}
    return [
        *(
            _vap("ValidatingAdmissionPolicy", n, {"matchConstraints": {"namespaceSelector": pods}})
            for n in POD_POLICIES
        ),
        *(
            _vap(
                "ValidatingAdmissionPolicyBinding", n, {"policyName": n, "matchResources": {"namespaceSelector": pods}}
            )
            for n in POD_POLICIES
        ),
        _vap("ValidatingAdmissionPolicy", "wl-ns-objects", ns),
        _vap("ValidatingAdmissionPolicyBinding", "wl-ns-objects", {"policyName": "wl-ns-objects"}),
        {"kind": "ClusterRole", "metadata": {"name": "waitlist-control-nodes"}},
    ]


def test_the_admission_rule_catches_a_policy_that_reaches_ferry_watch() -> None:
    """The check itself, on hand-built renders, so it is known to fail where it must (no helm needed)."""
    admission_policies_reach_only_waitlist(objects(_policies()))
    ferry = {"matchLabels": {"kubernetes.io/metadata.name": "ferry-watch-browsers"}}
    with_ferry = {
        "matchExpressions": [{**BOTH_NAMESPACES["matchExpressions"][0], "values": ["waitlist", "ferry-watch"]}]
    }
    only_one = {"matchExpressions": [{**BOTH_NAMESPACES["matchExpressions"][0], "values": ["waitlist-browsers"]}]}
    for render_ in (_policies(pods=ferry), _policies(ns_objects=with_ferry), _policies(ns_objects=only_one)):
        with pytest.raises(AssertionError):
            admission_policies_reach_only_waitlist(objects(render_))
    renamed = _policies()
    renamed[-1]["metadata"]["name"] = "ferry-watch-control-nodes"
    with pytest.raises(AssertionError):
        admission_policies_reach_only_waitlist(objects(renamed))
