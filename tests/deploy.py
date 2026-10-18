"""The deployment as home-cluster runs it: `deploy/helmrelease.yaml` through app-template.

`deploy/helmrelease.yaml` (and `externalsecret.yaml`) are copies of home-cluster's
`kubernetes/apps/waitlist/waitlist/app/helmrelease.yaml` (and `externalsecret.yaml`)
(docs/multi-tenant/design-reference.md §7.10, docs/waitlist/design.md §8.5). Its values carry waitlist-control's,
waitlist-web's and waitlist-fetcher's configs and the admission policies, so CI renders it the way Flux would and tests
the result (tests/test_deploy.py, tests/apiserver, kubeconform).

`python -m tests.deploy` prints the render, for kubeconform. It needs WL_HELM (a helm binary). WL_APP_TEMPLATE may
name a local copy of the chart instead of the OCI one; WL_HELMRELEASE another HelmRelease file (home-cluster's).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import msgspec

ROOT = Path(__file__).resolve().parents[1]
HELMRELEASE = Path(os.environ.get("WL_HELMRELEASE", ROOT / "deploy" / "helmrelease.yaml"))
EXTERNALSECRET = HELMRELEASE.with_name("externalsecret.yaml")  # the `waitlist` Secret the pods read
APP_TEMPLATE = os.environ.get("WL_APP_TEMPLATE", "oci://ghcr.io/bjw-s-labs/helm/app-template")
APP_TEMPLATE_VERSION = "5.3.0"  # home-cluster's kubernetes/components/common/repos/app-template
RELEASE = "waitlist"
NAMESPACE = "waitlist"

# What a release fills in: each image's digest and the checkout key's public half.
BROWSER_IMAGE = "ghcr.io/mchestr/waitlist-browser:0.6.0@sha256:" + "2" * 64
CONTROL_TAG = "0.6.0@sha256:" + "1" * 64
EDGE_TAG = "0.6.0@sha256:" + "3" * 64


def _image(container: str, tag: str) -> dict[str, Any]:
    return {"containers": {container: {"image": {"tag": tag}}}}


TEST_VALUES: dict[str, Any] = {
    "waitlist": {
        "browserImage": {"tag": BROWSER_IMAGE.partition(":")[2]},
        "control": {"pod": {"vncPubkeys": "k1:" + base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")}},
    },
    "controllers": {
        "waitlist-web": _image("app", CONTROL_TAG),
        "waitlist-control": _image("app", CONTROL_TAG),
        "waitlist-fetcher": _image("app", CONTROL_TAG),
        "db": _image("migrate", CONTROL_TAG),
        "vnc-edge": _image("app", EDGE_TAG),
    },
}


def merged(base: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """`base` with `change` merged in, map by map (as helm merges two --values files): a copy, never `base` itself."""
    out = dict(base)
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


def with_provider(provider: str = "buntzen", **values: Any) -> dict[str, Any]:
    """TEST_VALUES with waitlist.providers.<provider> changed: offer "admins" unless `values` says otherwise. This
    repo's copy has every provider "off" (§8.5), so a test that needs one on renders through this."""
    return merged(TEST_VALUES, {"waitlist": {"providers": {provider: {"offer": "admins"} | values}}})


def objects(documents: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The render, by (kind, name)."""
    return {(o["kind"], o["metadata"]["name"]): o for o in documents}


# Flux's postBuild substitution (fluxcd/pkg/envsubst) rewrites ${VAR} and turns the escape $$ into $; a bare
# $name is left alone.
_FLUX_VARIABLE = re.compile(r"\$\{|\$\$")


def flux_variables(text: str) -> list[str]:
    """Lines Flux's substitution would change."""
    return [line.strip() for line in text.splitlines() if _FLUX_VARIABLE.search(line)]


def helmrelease() -> dict[str, Any]:
    doc: dict[str, Any] = msgspec.yaml.decode(HELMRELEASE.read_bytes())
    return doc


def render(helm: Path, overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The objects helm-controller would apply, hooks included (TEST_VALUES unless `overrides` is given)."""
    text = HELMRELEASE.read_text()
    if found := flux_variables(text):
        raise ValueError(f"Flux would substitute into: {found}")
    values = helmrelease()["spec"]["values"]
    chart = [APP_TEMPLATE] if Path(APP_TEMPLATE).is_dir() else [APP_TEMPLATE, f"--version={APP_TEMPLATE_VERSION}"]
    with tempfile.TemporaryDirectory(prefix="wl-deploy-") as tmp:
        (Path(tmp) / "values.json").write_text(json.dumps(values))
        (Path(tmp) / "test.json").write_text(json.dumps(TEST_VALUES if overrides is None else overrides))
        done = subprocess.run(  # noqa: S603 - our own argv
            [
                str(helm),
                "template",
                RELEASE,
                *chart,
                f"--namespace={NAMESPACE}",
                f"--values={Path(tmp) / 'values.json'}",
                f"--values={Path(tmp) / 'test.json'}",
                # helm template has no cluster to ask; home-cluster serves this version.
                "--api-versions=gateway.networking.k8s.io/v1/HTTPRoute",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return _documents(done.stdout)


def _documents(stream: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for chunk in re.split(r"^---\s*$", stream, flags=re.MULTILINE):
        doc = msgspec.yaml.decode(chunk) if chunk.strip() else None
        if doc:
            documents.append(doc)
    return documents


def main() -> None:
    helm = os.environ.get("WL_HELM")
    if not helm:
        sys.exit("WL_HELM isn't set")
    for doc in render(Path(helm)):
        sys.stdout.write("---\n" + json.dumps(doc) + "\n")


if __name__ == "__main__":
    main()
