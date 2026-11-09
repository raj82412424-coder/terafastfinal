"""Multiply Telegram media download throughput.

Two bottlenecks limit kurigram downloads, both fixed here:

1. ``Client.get_session(dc_id, is_media=True)`` caches ONE media session
   (one TCP connection) per DC. Every parallel download worker funnels its
   1 MiB ``upload.GetFile`` requests through that single pipe (~1-2 MB/s).

2. All media sessions reuse the same auth key, and Telegram paces file
   downloads per auth key — extra connections on one key share one budget.

This patch replaces ``get_session`` for media sessions with a round-robin
pool of N connections per DC, EACH holding its own freshly exported auth
key (analogous to logging in from N devices, which Telegram fully allows).
Per-key download budgets then add up.

The ``stream_media`` generator keeps whatever session it was handed for its
whole life, so each worker segment lands on a different pool entry.

Configure the pool size with ``MEDIA_SESSION_POOL`` (default 12).
"""
from __future__ import annotations

import asyncio
import logging
import os

from pyrogram import raw
from pyrogram.client import Client
from pyrogram.errors import AuthBytesInvalid
from pyrogram.session import Auth, Session

log = logging.getLogger("speedup")

MEDIA_SESSION_POOL = max(1, int(os.getenv("MEDIA_SESSION_POOL", "12")))

_orig_get_session = Client.get_session


async def _create_keyed_media_session(self: Client, dc_id: int) -> Session:
    """Create a media session with its own exported auth key."""
    # Use the PROD endpoint: media_only endpoints reject fresh DH key
    # handshakes (they only serve already-authorized file requests).
    dc_option = await self.get_dc_option(
        dc_id, is_media=False, ipv6=self.ipv6, is_cdn=False
    )
    server_address, port = dc_option.ip_address, dc_option.port
    test_mode = await self.storage.test_mode()

    # Fresh DH handshake => a brand-new auth key for this connection only.
    auth_key = await Auth(self, dc_id, server_address, port, test_mode).create()
    session = Session(
        self, dc_id, server_address, port,
        auth_key, test_mode, is_media=True,
    )
    await session.start()

    # Authorize the new key: export from the home DC, import into this one.
    for _ in range(3):
        exported = await self.invoke(
            raw.functions.auth.ExportAuthorization(dc_id=dc_id)
        )
        try:
            await session.invoke(
                raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes
                )
            )
        except AuthBytesInvalid:
            continue
        else:
            return session

    await session.stop()
    raise AuthBytesInvalid(f"DC{dc_id}: import authorization failed 3 times")


def install() -> None:
    """Patch Client.get_session once; safe to call multiple times."""
    if getattr(Client, "_media_pool_installed", False):
        return

    async def pooled_get_session(
        self, dc_id=None, *, is_media=False, is_cdn=False, temporary=False, **kwargs
    ):
        if not is_media or is_cdn or temporary:
            return await _orig_get_session(
                self, dc_id, is_media=is_media, is_cdn=is_cdn,
                temporary=temporary, **kwargs,
            )

        pools = self.__dict__.setdefault("_media_session_pools", {})
        locks = self.__dict__.setdefault("_media_pool_locks", {})
        counters = self.__dict__.setdefault("_media_session_rr", {})
        gave_up = self.__dict__.setdefault("_media_pool_gave_up", set())
        lock = locks.setdefault(dc_id, asyncio.Lock())

        async with lock:
            pool = pools.setdefault(dc_id, [])
            while len(pool) < MEDIA_SESSION_POOL and dc_id not in gave_up:
                try:
                    session = await _create_keyed_media_session(self, dc_id)
                except Exception as exc:
                    log.warning(
                        "Keyed media session for DC%s failed (%r); keeping what we have",
                        dc_id, exc,
                    )
                    gave_up.add(dc_id)
                    break
                pool.append(session)
                log.info(
                    "DC%s media pool: %d/%d keyed connection(s)",
                    dc_id, len(pool), MEDIA_SESSION_POOL,
                )

        if not pool:
            # Last resort: stock single shared-key media session.
            return await _orig_get_session(
                self, dc_id, is_media=is_media, is_cdn=is_cdn, **kwargs
            )

        idx = counters.get(dc_id, 0)
        counters[dc_id] = idx + 1
        return pool[idx % len(pool)]

    Client.get_session = pooled_get_session
    Client._media_pool_installed = True
    log.info(
        "Media session pool installed: %d keyed connections per DC",
        MEDIA_SESSION_POOL,
    )


install()
