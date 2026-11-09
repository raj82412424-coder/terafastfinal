"""Single entry point for text, media and callback updates."""
from __future__ import annotations

import logging

from pyrogram import Client, filters

from handlers import batch, bots, common, direct, login, targets

log = logging.getLogger("handlers.router")

COMMANDS = [
    "start",
    "help",
    "set",
    "targets",
    "batch",
    "bot",
    "login",
    "logout",
    "bind",
    "change",
    "delete",
    "delete_config",
    "cancel",
    "status",
    "config",
    "queue",
    "parallel",
    "pr",
    "stop",
    "stopall",
]


def register(app: Client) -> None:
    @app.on_message(filters.private & filters.text & ~filters.command(COMMANDS))
    async def text_router(client, message):
        await direct.handle_text(client, message)

    @app.on_message(
        filters.private
        & (
            filters.video
            | filters.document
            | filters.audio
            | filters.animation
            | filters.video_note
            | filters.photo
        )
    )
    async def media_router(client, message):
        await direct.handle_media(client, message)

    @app.on_callback_query()
    async def callback_router(client, query):
        data = query.data or ""
        try:
            if data.startswith("t:"):
                await targets.handle_callback(client, query)
            elif data.startswith("b:"):
                await batch.handle_callback(client, query)
            elif data.startswith("m:"):
                await common.handle_callback(client, query)
            elif data.startswith("l:"):
                await login.handle_callback(client, query)
            elif data.startswith("w:"):
                await bots.handle_callback(client, query)
        except Exception:  # noqa: BLE001 - one bad callback must not kill the bot
            log.exception("Callback failed: %s", data)
        finally:
            try:
                await query.answer()
            except Exception:
                pass
