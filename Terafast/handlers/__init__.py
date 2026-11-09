"""Telegram handler registration."""
from __future__ import annotations

from pyrogram import Client


def register_handlers(app: Client) -> None:
    from handlers import batch, bots, common, login, router, targets

    common.register(app)
    targets.register(app)
    batch.register(app)
    login.register(app)
    bots.register(app)
    router.register(app)
