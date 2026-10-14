"""Assemble vnc-edge's static site from KasmVNC's stock web client (docs/multi-tenant/design-reference.md §7.9).

    python build.py <kasmvnc www dir> <out dir> --checkout-host ferry-checkout.chestr.dev \
        --app-origin https://ferry.chestr.dev

Writes ``<out>/www`` (served as /usr/share/fw-vnc) and ``<out>/default.conf`` (nginx). KasmVNC's files are
copied unmodified apart from one line inserted in vnc.html, which loads the shim before the client. The
Content-Security-Policy allows vnc.html's one inline script by its hash, computed here from the shipped file,
so a KasmVNC upgrade that changes the page fails the build instead of shipping a policy that doesn't fit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = '<script src="/fw/shim.js"></script>'
CLIENT_DIRS = ("assets", "app", "core")  # what nginx serves from KasmVNC, besides vnc.html
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?")
_ORIGIN = re.compile(r"https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?")
_MODULE_SCRIPT = re.compile(r'<script type="module"[^>]*></script>')
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)
_PLACEHOLDER = re.compile(r"__FW_[A-Z_]+__")


class BuildError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Built:
    www: Path
    nginx_conf: Path
    csp: str


def patch_vnc_html(html: str) -> tuple[str, str]:
    """vnc.html with the shim inserted before the client's module script, and its inline script's hash."""
    if html.count("<script") != 2:  # one inline script, one module
        raise BuildError("vnc.html should have exactly two scripts; check the CSP and the shim by hand")
    modules = _MODULE_SCRIPT.findall(html)
    inline = _INLINE_SCRIPT.findall(html)
    if len(modules) != 1 or len(inline) != 1:
        raise BuildError("vnc.html should have one module script and one inline script")
    digest = base64.b64encode(hashlib.sha256(inline[0].encode()).digest()).decode()
    return html.replace(modules[0], SHIM + modules[0], 1), f"sha256-{digest}"


def csp(script_hash: str, checkout_host: str) -> str:
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' '{script_hash}' 'wasm-unsafe-eval'",  # WebAssembly for the QOI decoder only
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            # Some Safari versions don't match wss: to 'self'. The client fetches a data: URL to probe the
            # browser's video decoders; data: never leaves the page.
            f"connect-src 'self' wss://{checkout_host} data:",
            "worker-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'none'",
        )
    )


def build(kasmvnc_www: Path, out: Path, *, checkout_host: str, app_origin: str) -> Built:
    if not _HOST.fullmatch(checkout_host):
        raise BuildError(f"Not a host name: {checkout_host!r}")
    if not _ORIGIN.fullmatch(app_origin):
        raise BuildError(f"Not an https origin: {app_origin!r}")
    www = out / "www"
    if www.exists():
        shutil.rmtree(www)
    www.mkdir(parents=True)
    vnc_html, script_hash = patch_vnc_html((kasmvnc_www / "vnc.html").read_text())
    (www / "vnc.html").write_text(vnc_html)
    for name in CLIENT_DIRS:
        shutil.copytree(kasmvnc_www / name, www / name, symlinks=False)
    values = {"__FW_APP_ORIGIN__": app_origin}
    for source in sorted((HERE / "site").rglob("*")):
        target = www / source.relative_to(HERE / "site")
        if source.is_dir():
            target.mkdir(exist_ok=True)
        elif source.suffix in (".html", ".js"):
            target.write_text(_fill(source.read_text(), values, source.name))
        else:
            shutil.copyfile(source, target)
    policy = csp(script_hash, checkout_host)
    conf = out / "default.conf"
    conf.write_text(_fill((HERE / "default.conf.in").read_text(), {"__FW_CSP__": policy}, conf.name))
    return Built(www, conf, policy)


def _fill(text: str, values: dict[str, str], name: str) -> str:
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    if leftover := _PLACEHOLDER.search(text):
        raise BuildError(f"{name}: nothing fills {leftover.group()}")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble vnc-edge's static site and nginx config.")
    parser.add_argument("kasmvnc_www", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--checkout-host", required=True)
    parser.add_argument("--app-origin", required=True)
    args = parser.parse_args()
    try:
        built = build(args.kasmvnc_www, args.out, checkout_host=args.checkout_host, app_origin=args.app_origin)
    except (BuildError, OSError) as e:
        sys.exit(f"build.py: {e}")
    sys.stdout.write(f"Content-Security-Policy: {built.csp}\n")


if __name__ == "__main__":
    main()
