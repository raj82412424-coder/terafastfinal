"""Small async helpers used by the upload pipeline."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def to_thread_wait_on_cancel(
    func: Callable[..., T], *args: Any, **kwargs: Any
) -> T:
    """Run blocking work in a thread without deleting its input on cancellation.

    ``asyncio.to_thread`` cannot kill a native thread. Waiting for it to settle
    before the caller's ``finally`` block removes downloaded files avoids a
    race where the worker is still reading a deleted file.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        raise


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on_error: Callable[[BaseException, int], None] | None = None,
) -> T:
    """Retry an awaitable with exponential backoff, honouring FloodWait."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if on_error is not None:
                on_error(exc, attempt)
            if attempt >= attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            flood = getattr(exc, "value", None)
            if exc.__class__.__name__ in ("FloodWait", "SlowmodeWait") and flood:
                delay = max(delay, float(flood) + 2)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
