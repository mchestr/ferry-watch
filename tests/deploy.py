"""The deployment as home-cluster runs it: `deploy/helmrelease.yaml` through app-template.

`deploy/helmrelease.yaml` is a copy of home-cluster's `kubernetes/apps/ferry-watch/ferry-watch/app/helmrelease.yaml`
(docs/multi-tenant/design-reference.md §7.10). Its values carry ferry-control's and ferry-web's configs and the
admission policies, so CI renders it the way Flux would and tests the result (tests/apiserver, kubeconform).

`python -m tests.deploy` prints the render, for kubeconform. It needs FW_HELM (a helm binary). FW_APP_TEMPLATE may
name a local copy of the chart instead of the OCI one; FW_HELMRELEASE another HelmRelease file (home-cluster's).
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
HELMRELEASE = Path(os.environ.get("FW_HELMRELEASE", ROOT / "deploy" / "helmrelease.yaml"))
APP_TEMPLATE = os.environ.get("FW_APP_TEMPLATE", "oci://ghcr.io/bjw-s-labs/helm/app-template")
APP_TEMPLATE_VERSION = "5.3.0"  # home-cluster's kubernetes/components/common/repos/app-template
RELEASE = "ferry-watch"
NAMESPACE = "ferry-watch"

# What a release fills in: the browser image's digest and the checkout key's public half.
BROWSER_IMAGE = "ghcr.io/mchestr/ferry-watch-browser:0.6.0@sha256:" + "2" * 64
TEST_VALUES: dict[str, Any] = {
    "ferry": {
        "browserImage": {"tag": BROWSER_IMAGE.partition(":")[2]},
        "control": {"pod": {"vncPubkeys": "k1:" + base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")}},
    }
}

# Flux's postBuild substitution rewrites ${VAR} and $VAR everywhere in the file; $$ is its escape.
_FLUX_VARIABLE = re.compile(r"(?<!\$)\$(\{|[A-Za-z_])")


def flux_variables(text: str) -> list[str]:
    """Lines Flux would substitute into (and so silently change)."""
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
    with tempfile.TemporaryDirectory(prefix="fw-deploy-") as tmp:
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
    helm = os.environ.get("FW_HELM")
    if not helm:
        sys.exit("FW_HELM isn't set")
    for doc in render(Path(helm)):
        sys.stdout.write("---\n" + json.dumps(doc) + "\n")


if __name__ == "__main__":
    main()
