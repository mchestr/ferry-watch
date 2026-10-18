"""Render Waitlist's PNG icons from web/public/icon.svg (docs/waitlist/design.md §7.8).

    uv run --extra browser python web/scripts/render-icons.py

It draws the SVG in the repo's Playwright Chromium and writes, beside it in web/public/:

- icon-192.png, icon-512.png: the icon as drawn, its rounded corners transparent;
- icon-maskable-192.png, icon-maskable-512.png: the tile's blue to the edges and the bell at 80 % (20 % padding),
  so no launcher's mask cuts into the bell;
- apple-touch-icon.png (180 px): the blue to the edges, since iOS rounds the corners itself.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

PUBLIC = Path(__file__).resolve().parent.parent / "public"
SOURCE = PUBLIC / "icon.svg"

# file: (size in px, the blue to the edges, the bell's scale)
ICONS: dict[str, tuple[int, bool, float]] = {
    "icon-192.png": (192, False, 1.0),
    "icon-512.png": (512, False, 1.0),
    "icon-maskable-192.png": (192, True, 0.8),
    "icon-maskable-512.png": (512, True, 0.8),
    "apple-touch-icon.png": (180, True, 1.0),
}


def variant(svg: str, *, size: int, full_bleed: bool, scale: float) -> str:
    """The icon drawn at `size` px: square to the edges when `full_bleed`, the bell scaled about the centre."""
    out = svg.replace("<svg ", f'<svg width="{size}" height="{size}" style="display:block" ', 1)
    if full_bleed:
        out = re.sub(r'\srx="[^"]*"', "", out, count=1)
    if scale != 1.0:
        out = out.replace("<g ", f'<g transform="translate(16 16) scale({scale}) translate(-16 -16)" ', 1)
    return out


async def render() -> None:
    svg = SOURCE.read_text(encoding="utf-8").strip()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(device_scale_factor=1)
        for name, (size, full_bleed, scale) in ICONS.items():
            await page.set_viewport_size({"width": size, "height": size})
            drawn = variant(svg, size=size, full_bleed=full_bleed, scale=scale)
            await page.set_content(f'<html><body style="margin:0;background:transparent">{drawn}</body></html>')
            await page.screenshot(
                path=PUBLIC / name, omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size}
            )
        await browser.close()


if __name__ == "__main__":
    asyncio.run(render())
