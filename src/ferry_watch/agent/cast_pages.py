"""The pages of the checkout's browser context, as a stream session lists them (PROTOCOL.md §5.2).

Ids from 1 in opening order, never reused within a session; at most 8 listed (a ninth page is neither listed nor
shown); the main one is the cart's own page, which the viewer can't close. Each listed page gets its own CDP
session, without Page.enable (the dialog keeper has its own, DESIGN §6.1), and its title is read from
`Target.getTargetInfo`, which Chromium caps, never from the page.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .cast_cdp import Cdp, CdpError, Worlds, open_session
from .cast_protocol import MAX_TITLE, PageInfo, Pages, clean, origin_of

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

MAX_PAGES: Final = 8
MAX_PAGE_ID: Final = 65535


@dataclass(eq=False, slots=True)
class PageEntry:
    """A page of the checkout's context the session lists."""

    id: int
    page: Page
    main: bool
    cdp: Cdp | None = None
    worlds: Worlds | None = None
    title: str = ""
    size: tuple[int, int] | None = None  # its latest screencast frame's
    dialogs: int = 0  # open on it

    @property
    def blocked(self) -> bool:
        """A JavaScript dialog is open on it: it answers almost no CDP until it's answered."""
        return self.dialogs > 0


class PageList:
    """The checkout context's pages, in opening order: ids from 1, never reused; at most 8 listed (more are
    neither listed nor shown); the front one is the newest unless the viewer chose another."""

    def __init__(self, context: BrowserContext, main: Page) -> None:
        self._context = context
        self._main = main
        self.listed: list[PageEntry] = []
        self.front: PageEntry | None = None
        self._next_id = 1
        self._opening = asyncio.Lock()

    def track(self, page: Page) -> PageEntry | None:
        if (known := self.of(page)) is not None:
            return known
        if len(self.listed) >= MAX_PAGES or self._next_id > MAX_PAGE_ID or page.is_closed():
            return None
        entry = PageEntry(self._next_id, page, page is self._main)
        self._next_id += 1
        self.listed.append(entry)
        return entry

    def remove(self, page: Page) -> PageEntry | None:
        entry = self.of(page)
        if entry is not None:
            self.listed.remove(entry)
            if self.front is entry:
                self.front = None
        return entry

    def of(self, page: Page) -> PageEntry | None:
        return next((e for e in self.listed if e.page is page), None)

    def by_id(self, page_id: int) -> PageEntry | None:
        return next((e for e in self.listed if e.id == page_id), None)

    def newest(self) -> PageEntry | None:
        return self.listed[-1] if self.listed else None

    async def cdp(self, entry: PageEntry) -> Cdp:
        """The page's own CDP session (opened once; no Page.enable: the dialog keeper has its own)."""
        async with self._opening:  # one session a page, however many ask at once
            if entry.cdp is None:
                try:
                    session = await open_session(self._context, entry.page)
                except Exception as e:  # noqa: BLE001 - the page went
                    raise CdpError(f"no CDP session for a page ({type(e).__name__})") from None
                entry.cdp = Cdp(session)
            return entry.cdp

    async def message(self) -> Pages | None:
        """The `pages` message, with each page's title read now (Target.getTargetInfo: Chromium caps it); None
        once no page is left (the session is ending)."""
        for entry in list(self.listed):
            with contextlib.suppress(CdpError):
                info = await (await self.cdp(entry)).send("Target.getTargetInfo")
                entry.title = clean(str(info.get("targetInfo", {}).get("title", "")), MAX_TITLE)
        if not self.listed:
            return None
        front = self.front.id if self.front is not None else self.listed[0].id
        listed = [PageInfo(id=e.id, title=e.title, origin=origin_of(e.page.url), main=e.main) for e in self.listed]
        return Pages(front=front, pages=listed)
