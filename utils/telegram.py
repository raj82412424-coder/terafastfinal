"""Telegram message helpers that tolerate Pyrogram/kurigram API differences."""
from __future__ import annotations

import logging

from pyrogram.errors import MessageNotModified

log = logging.getLogger("telegram")

try:  # kurigram/pyrogram forks renamed disable_web_page_preview
    from pyrogram.types import LinkPreviewOptions

    NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except Exception:  # pragma: no cover - older clients
    NO_PREVIEW = {"disable_web_page_preview": True}


async def safe_edit(message, text: str, reply_markup=None) -> None:
    """Edit a message, falling back to a reply when editing is impossible."""
    if message is None:
        return

    for kwargs in (
        {**NO_PREVIEW, "reply_markup": reply_markup},
        {"reply_markup": reply_markup},
    ):
        try:
            await message.edit_text(text, **kwargs)
            return
        except MessageNotModified:
            return
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001 - fall through to reply
            log.debug("edit_text failed: %r", exc)
            break

    try:
        await message.reply_text(text, reply_markup=reply_markup, **NO_PREVIEW)
    except TypeError:
        await message.reply_text(text, reply_markup=reply_markup)
    except Exception as exc:  # noqa: BLE001
        log.debug("reply_text failed: %r", exc)


async def safe_reply(message, text: str, reply_markup=None) -> None:
    try:
        await message.reply_text(text, reply_markup=reply_markup, **NO_PREVIEW)
    except TypeError:
        await message.reply_text(text, reply_markup=reply_markup)
