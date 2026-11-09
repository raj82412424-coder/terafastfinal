"""Throttled Telegram progress message renderer."""
from __future__ import annotations

import asyncio
import logging
import time

from pyrogram.errors import FloodWait, MessageNotModified

import config
from utils.formatting import bar, eta_label, human, speed_label

log = logging.getLogger("progress")


class ProgressReporter:
    """Edits one Telegram message with a live bar, speed and ETA.

    Edits are throttled to ``PROGRESS_EDIT_INTERVAL`` seconds. Upload workers
    call :meth:`thread_update` from a thread pool; the coroutine is scheduled on
    the owning event loop.
    """

    def __init__(
        self,
        message,
        *,
        interval: float | None = None,
        header: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.message = message
        self.interval = float(interval or config.PROGRESS_EDIT_INTERVAL)
        self.header = header
        self.stage = ""
        self.loop = loop
        self._last_edit = 0.0
        self._last_text = ""
        self._closed = False

    def set_header(self, text: str) -> None:
        self.header = text

    async def set_stage(self, text: str, *, force: bool = True) -> None:
        self.stage = text
        await self._render(force=force)

    async def update(
        self,
        current: int,
        total: int,
        speed: float | None = None,
        *,
        stage: str | None = None,
        force: bool = False,
    ) -> None:
        if stage is not None:
            self.stage = stage
        await self._render(current=current, total=total, speed=speed, force=force)

    def thread_update(
        self,
        current: int,
        total: int,
        speed: float | None = None,
        *,
        stage: str | None = None,
    ) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.update(current, total, speed, stage=stage), loop
            )
        except RuntimeError:
            pass

    async def finish(self, text: str) -> None:
        self._closed = True
        await self._edit(text)

    async def _render(
        self,
        current: int | None = None,
        total: int | None = None,
        speed: float | None = None,
        *,
        force: bool = False,
    ) -> None:
        if self._closed or self.message is None:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self.interval:
            return

        lines: list[str] = []
        if self.header:
            lines.append(self.header)
        if self.stage:
            lines.append(self.stage)
        if current is not None and total is not None:
            lines.append(bar(current, total))
            detail = f"{human(current)} / {human(total)}"
            if speed is not None:
                detail += f" | {speed_label(speed)} | {eta_label(current, total, speed)}"
            lines.append(detail)

        text = "\n".join(lines).strip()
        if not text or text == self._last_text:
            return
        self._last_edit = now
        await self._edit(text)

    async def _edit(self, text: str) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit_text(text)
            self._last_text = text
        except MessageNotModified:
            self._last_text = text
        except FloodWait as exc:
            log.warning("Progress edit flood wait %ss", exc.value)
            await asyncio.sleep(min(int(exc.value), 30))
        except Exception as exc:  # a status edit must never abort a transfer
            log.debug("Progress edit skipped: %r", exc)
