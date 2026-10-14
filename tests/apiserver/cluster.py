"""A real kube-apiserver (envtest's binaries) with the deployment applied, for the admission and RBAC tests.

Set FW_ENVTEST_BIN to a directory holding etcd, kube-apiserver and kubectl (controller-tools' envtest
release for the cluster's Kubernetes minor), and FW_HELM to a helm binary. Without them these tests skip.
CI sets both (.github/workflows/ci.yml). The deployment is deploy/helmrelease.yaml rendered through
app-template (tests/deploy.py).

There is no kubelet, scheduler or controller-manager: pods are admitted and then sit Pending, and the
fixture fills in the ResourceQuotas' status, which the quota controller would otherwise do.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ferry_watch.control.config import ControlConfig
from ferry_watch.control.pods import browser_pod
from tests.deploy import NAMESPACE, render

RELEASE_NAMESPACE = NAMESPACE
BROWSERS = "ferry-watch-browsers"
CONTROL_SA = f"system:serviceaccount:{RELEASE_NAMESPACE}:ferry-control"
BREAK_GLASS = "ferry-watch:break-glass"
# Cilium and Gateway API kinds aren't installed here; kubeconform checks those (CI's deploy job).
NOT_INSTALLED = {"CiliumNetworkPolicy", "HTTPRoute"}


class KubectlError(Exception):
    def __init__(self, stderr: str) -> None:
        super().__init__(stderr)
        self.stderr = stderr


@dataclass(frozen=True)
class Cluster:
    kubectl_bin: Path
    kubeconfig: Path

    def kubectl(self, *args: str, stdin: str | None = None, user: str | None = None, groups: Sequence[str] = ()) -> str:
        argv = [str(self.kubectl_bin), f"--kubeconfig={self.kubeconfig}", *args]
        if user is not None:
            argv.append(f"--as={user}")
        argv += [f"--as-group={group}" for group in groups]
        done = subprocess.run(  # noqa: S603 - our own argv
            argv, input=stdin, capture_output=True, text=True, timeout=30, check=False
        )
        if done.returncode != 0:
            raise KubectlError(done.stderr)
        return done.stdout

    def get(self, *args: str) -> Any:
        return json.loads(self.kubectl("get", *args, "-o", "json"))

    def can(self, user: str, verb: str, resource: str, namespace: str | None = None) -> bool:
        """`kubectl auth can-i`, which exits 1 for "no": read the answer rather than the exit status."""
        resource, _, subresource = resource.partition("/")  # "pods/exec" alone would mean the pod named exec
        argv = [str(self.kubectl_bin), f"--kubeconfig={self.kubeconfig}", "auth", "can-i", verb, resource]
        argv.append(f"--as={user}")
        if subresource:
            argv.append(f"--subresource={subresource}")
        if namespace is not None:
            argv.append(f"--namespace={namespace}")
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603 - our own argv
        answer = done.stdout.strip()
        if answer not in ("yes", "no"):
            raise KubectlError(done.stderr)
        return answer == "yes"


def control_config(cluster: Cluster) -> ControlConfig:
    """ferry-control's config as the deployment rendered it."""
    data = cluster.get("configmap", "ferry-control-config", f"--namespace={RELEASE_NAMESPACE}")["data"]
    return msgspec.json.decode(data["config.json"], type=ControlConfig).check()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(check: Any, what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(Exception):
            if check():
                return
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} didn't come up")
        time.sleep(0.25)  # noqa: TID251 - a synchronous, session-wide fixture


def _start(argv: list[str], log: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        return subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603 - our own argv


@contextlib.contextmanager
def running_cluster() -> Iterator[Cluster]:
    bin_dir, helm = os.environ.get("FW_ENVTEST_BIN"), os.environ.get("FW_HELM")
    if not bin_dir or not helm:
        pytest.skip("FW_ENVTEST_BIN and FW_HELM aren't set")
    binaries = Path(bin_dir)
    processes: list[subprocess.Popen[bytes]] = []
    with tempfile.TemporaryDirectory(prefix="fw-apiserver-") as tmp:
        work = Path(tmp)
        etcd_port, peer_port, api_port = _free_port(), _free_port(), _free_port()
        processes.append(
            _start(
                [
                    str(binaries / "etcd"),
                    f"--data-dir={work / 'etcd'}",
                    f"--listen-client-urls=http://127.0.0.1:{etcd_port}",
                    f"--advertise-client-urls=http://127.0.0.1:{etcd_port}",
                    f"--listen-peer-urls=http://127.0.0.1:{peer_port}",
                    "--unsafe-no-fsync",
                ],
                work / "etcd.log",
            )
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (work / "sa.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        (work / "sa.pub").write_bytes(
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        token = secrets.token_hex(16)
        (work / "tokens.csv").write_text(f'{token},admin,admin,"system:masters"\n')
        processes.append(
            _start(
                [
                    str(binaries / "kube-apiserver"),
                    f"--etcd-servers=http://127.0.0.1:{etcd_port}",
                    "--bind-address=127.0.0.1",
                    "--advertise-address=127.0.0.1",
                    f"--secure-port={api_port}",
                    f"--cert-dir={work / 'certs'}",
                    f"--token-auth-file={work / 'tokens.csv'}",
                    "--authorization-mode=RBAC",
                    "--service-cluster-ip-range=10.0.0.0/24",
                    "--service-account-issuer=https://kubernetes.default.svc.cluster.local",
                    f"--service-account-key-file={work / 'sa.pub'}",
                    f"--service-account-signing-key-file={work / 'sa.key'}",
                    "--allow-privileged=false",
                ],
                work / "apiserver.log",
            )
        )
        kubeconfig = work / "kubeconfig"
        kubeconfig.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [
                        {
                            "name": "envtest",
                            "cluster": {
                                "server": f"https://127.0.0.1:{api_port}",
                                "insecure-skip-tls-verify": True,
                            },
                        }
                    ],
                    "users": [{"name": "admin", "user": {"token": token}}],
                    "contexts": [{"name": "envtest", "context": {"cluster": "envtest", "user": "admin"}}],
                    "current-context": "envtest",
                }
            )
        )
        cluster = Cluster(binaries / "kubectl", kubeconfig)
        try:
            _wait(lambda: cluster.kubectl("get", "--raw", "/readyz") == "ok", "kube-apiserver")
            _install(cluster, Path(helm))
            yield cluster
        except BaseException:
            for name in ("apiserver.log", "etcd.log"):
                with contextlib.suppress(OSError):
                    print(f"---- {name}\n" + (work / name).read_text()[-4000:])  # noqa: T201 - for a failed setup
            raise
        finally:
            for process in reversed(processes):
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
                if process.poll() is None:
                    process.kill()


def _install(cluster: Cluster, helm: Path) -> None:
    """The deployment as helm-controller would apply it: hooks too (the Job just sits there)."""
    objects = [doc for doc in render(helm) if doc["kind"] not in NOT_INSTALLED]
    rendered = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects})
    cluster.kubectl("create", "namespace", RELEASE_NAMESPACE)
    cluster.kubectl("apply", "--server-side", "-f", "-", stdin=rendered)
    # The policies load asynchronously: wait until fw-browser-pod refuses ferry-control's own pod from an admin.
    pod = browser_pod("0" * 16, 1, "u" + "a" * 19, control_config(cluster).pod_params(), now=time.time())

    def enforcing() -> bool:
        try:
            cluster.kubectl("create", "--dry-run=server", "-f", "-", stdin=json.dumps(pod))
        except KubectlError as e:
            return "fw-browser-pod" in e.stderr
        return False

    _wait(enforcing, "the admission policies")
    # No quota controller here: give each quota the status it would compute for an empty namespace.
    for quota in cluster.get("resourcequotas", f"--namespace={BROWSERS}")["items"]:
        hard = quota["spec"]["hard"]
        status = {"status": {"hard": hard, "used": dict.fromkeys(hard, "0")}}
        cluster.kubectl(
            "patch",
            "resourcequota",
            quota["metadata"]["name"],
            f"--namespace={BROWSERS}",
            "--subresource=status",
            "--type=merge",
            f"--patch={json.dumps(status)}",
        )
