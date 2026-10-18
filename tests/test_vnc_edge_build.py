"""vnc-edge's build (vnc-edge/build.py): the streamed viewer's page fits its own Content-Security-Policy, and nginx
serves it with that policy (docs/multi-tenant/streamed-viewer/DESIGN.md §9)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

EDGE = Path(__file__).resolve().parents[1] / "vnc-edge"
HOST = "waitlist-checkout.chestr.dev"
APP = "https://waitlist.chestr.dev"
# C38, as DESIGN.md §5.2 words it, with the checkout host.
VIEWER_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src wss://waitlist-checkout.chestr.dev; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)
STOCK_VNC_HTML = (
    '<!DOCTYPE html><html><head><script>window.started = 1;</script><script type="module" crossorigin="anonymous" '
    'src="app/ui.js"></script></head><body></body></html>'
)


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vnc_edge_build_unit", EDGE / "build.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kasmvnc(tmp_path: Path) -> Path:
    """Just enough of KasmVNC's www directory for the build."""
    www = tmp_path / "kasmvnc"
    for name in ("assets", "app", "core"):
        (www / name).mkdir(parents=True)
    (www / "vnc.html").write_text(STOCK_VNC_HTML)
    return www


def location(conf: str, path: str) -> str:
    match = re.search(rf"location = {re.escape(path)} \{{(.*?)\n  \}}", conf, re.DOTALL)
    assert match, f"no exact-match location for {path}"
    return match.group(1)


def test_nginx_serves_the_viewer_with_its_own_policy(build: ModuleType, kasmvnc: Path, tmp_path: Path) -> None:
    built = build.build(kasmvnc, tmp_path / "out", checkout_host=HOST, app_origin=APP)
    assert built.viewer_csp == VIEWER_CSP
    conf = built.nginx_conf.read_text()
    viewer = location(conf, "/wl/viewer.html")
    # An add_header in a location replaces every one of the server's, so each is set again.
    headers = dict(re.findall(r"add_header (\S+) (.+?) always;", viewer))
    assert headers == {
        "Content-Security-Policy": f'"{VIEWER_CSP}"',
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    assert "root /usr/share/wl-vnc;" in viewer
    # The server's own headers, which vnc.html and the viewer's modules get, are the classic policy's.
    server = conf.split("location", 1)[0]
    assert f'add_header Content-Security-Policy "{built.csp}" always;' in server
    assert "add_header Cache-Control no-store always;" in server
    assert "__WL_" not in conf
    assert (built.www / "wl" / "viewer.html").read_text().count(f'href="{APP}/"') == 2


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("</body>", "<script>start()</script></body>"), "an inline <script>"),
        (("</body>", '<script type="module">start()</script></body>'), "an inline <script>"),
        (("</head>", "<style>body{}</style></head>"), "a <style> element"),
        (('<main id="stage">', '<main id="stage" style="color: red">'), "a style attribute on <main>"),
        (('<button id="type-chip"', '<button onclick="go()" id="type-chip"'), "an onclick handler on <button>"),
        (('<canvas id="screen"', '<canvas onerror="go()" id="screen"'), "an onerror handler on <canvas>"),
        (('id="status-back" href="__WL_APP_ORIGIN__/"', 'id="status-back" href=" JavaScript:go()"'), "javascript:"),
        (('<div id="wl-escape" role="alert" hidden>', '<div role="alert" hidden>'), "no #wl-escape"),
        (('id="wl-escape-classic"', 'id="escape-classic"'), "no #wl-escape-classic"),
        (('<script src="/wl/session.js"></script>', ""), "no classic <script src=/wl/session.js>"),
        (
            ('<script src="/wl/session.js"></script>', '<script type="module" src="/wl/session.js"></script>'),
            "no classic <script src=/wl/session.js>",
        ),
    ],
)
def test_the_build_refuses_a_viewer_its_policy_would_break(
    build: ModuleType, kasmvnc: Path, tmp_path: Path, change: tuple[str, str], problem: str
) -> None:
    site = tmp_path / "edge"
    shutil.copytree(EDGE / "site", site / "site")
    shutil.copyfile(EDGE / "default.conf.in", site / "default.conf.in")
    viewer = site / "site" / "wl" / "viewer.html"
    html = viewer.read_text()
    assert html.count(change[0]) == 1, change[0]
    viewer.write_text(html.replace(*change))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build, "HERE", site)
        with pytest.raises(build.BuildError, match=re.escape(problem)):
            build.build(kasmvnc, tmp_path / "out", checkout_host=HOST, app_origin=APP)


def test_the_viewer_as_it_is_fits(build: ModuleType) -> None:
    build.check_viewer_html((EDGE / "site" / "wl" / "viewer.html").read_text())


def test_each_page_script_stands_on_its_own(build: ModuleType, kasmvnc: Path, tmp_path: Path) -> None:
    """A page and its script can come from different replicas while vnc-edge rolls: the shim and the landing page's
    script each carry wl/session.js, and neither page loads it as a file of its own version."""
    built = build.build(kasmvnc, tmp_path / "out", checkout_host=HOST, app_origin=APP)
    wl = built.www / "wl"
    session = (wl / "session.js").read_text()
    for name in ("shim.js", "landing.js"):
        own = (EDGE / "site" / "wl" / name).read_text().replace("__WL_APP_ORIGIN__", APP)
        assert (wl / name).read_text() == session + own, name
    scripts = re.findall(r'<script src="([^"]+)"', (wl / "landing.html").read_text())
    assert scripts == ["/wl/landing.js"]
    assert re.findall(r'<script src="(/wl/[^"]+)"', (built.www / "vnc.html").read_text()) == ["/wl/shim.js"]


def test_the_viewer_leaves_the_screens_unsafe_areas_to_the_browser() -> None:
    """No `viewport-fit=cover`: in landscape a phone's browser then keeps the page, and with it the checkout's image
    and where taps land, clear of the notch and the rounded corners, as it does for any page. With it, the image
    was drawn under them while the viewer's own controls moved inside (found in review)."""
    html = (EDGE / "site" / "wl" / "viewer.html").read_text()
    viewport = re.search(r'<meta\s+name="viewport"\s+content="([^"]*)"', html)
    assert viewport is not None
    settings = {part.strip().split("=")[0]: part.strip().split("=")[-1] for part in viewport.group(1).split(",")}
    assert settings["width"] == "device-width"
    assert settings.get("viewport-fit", "auto") == "auto"
