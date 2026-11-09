"""Shared download -> TeraBox upload pipeline used by every entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass

from pyrogram import Client
from pyrogram.errors import FloodWait

import config
import db
import tgdl
from services.progress import ProgressReporter
from services.userbot import get_user_client
from terabox import TeraBox, TeraboxError
from utils.async_helpers import to_thread_wait_on_cancel
from utils.formatting import human, safe_name
from utils.links import message_link

log = logging.getLogger("pipeline")

MEDIA_ATTRS = ("video", "document", "audio", "animation", "video_note", "photo")

STAGE_TEXT = {
    "hash": "🔐 File hash ban raha hai...",
    "precreate": "📡 TeraBox upload session ban raha hai...",
    "upload": "⬆️ TeraBox par upload ho raha hai...",
    "finish": "🔗 Share link ban raha hai...",
}

# Process-wide budgets shared by batch, auto and direct jobs.
download_slots = asyncio.Semaphore(config.TELEGRAM_TOTAL_DOWNLOAD_WORKERS)
upload_part_slots = threading.BoundedSemaphore(config.TERABOX_TOTAL_UPLOAD_THREADS)

_chat_client_cache: dict[tuple[int, object], Client] = {}


class PipelineError(Exception):
    """User-facing pipeline failure."""


class DiskSpaceError(PipelineError):
    """The local runtime volume cannot safely hold the next file."""


@dataclass
class ProcessResult:
    status: str  # uploaded | skipped | no_media
    link: str | None = None
    name: str | None = None
    source_link: str | None = None
    message_id: int | None = None
    error: str | None = None


def pick_media(message):
    for attr in MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is not None:
            return media
    return None


def media_size(media) -> int:
    return int(getattr(media, "file_size", 0) or 0)


def build_name(message, media) -> str:
    file_name = getattr(media, "file_name", None)
    if file_name:
        return safe_name(file_name)

    unique = getattr(media, "file_unique_id", None) or str(message.id)
    if message.video or message.animation or message.video_note:
        ext = ".mp4"
    elif message.audio:
        ext = ".mp3"
    elif message.photo:
        ext = ".jpg"
    elif getattr(message, "voice", None):
        ext = ".ogg"
    else:
        ext = ""
    return safe_name(f"{unique}{ext}")


def target_folder(target: dict | None) -> str:
    base = config.REMOTE_FOLDER
    if target and config.REMOTE_FOLDER_PER_TARGET:
        sub = safe_name(str(target.get("title") or ""))[:40].strip(" ._")
        if sub:
            return f"{base}/{sub}"
    return base


async def resolve_client(app: Client, user_id: int, chat_id) -> Client:
    """Prefer the bot client; fall back to the owner's user session.

    Positive chat ids are private chats (e.g. a watched bot's chat from /bot);
    only the owner's user session can read those, so it is tried first.
    """
    key = (int(user_id), chat_id if isinstance(chat_id, int) else str(chat_id))
    cached = _chat_client_cache.get(key)
    if cached is not None:
        return cached

    if isinstance(chat_id, int) and chat_id > 0:
        user_client = await get_user_client(user_id)
        if user_client is not None:
            try:
                await user_client.get_chat(chat_id)
                _chat_client_cache[key] = user_client
                return user_client
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "User session cannot access private chat %s (%r); trying bot client",
                    chat_id, exc,
                )

    try:
        await app.get_chat(chat_id)
        _chat_client_cache[key] = app
        return app
    except Exception as exc:
        log.info("Bot cannot access chat %s (%r); trying user session", chat_id, exc)

    user_client = await get_user_client(user_id)
    if user_client is None:
        raise PipelineError(
            "Bot is chat ka member nahi hai. Bot ko group/channel me add karo "
            "ya /login se apna Telegram account connect karo."
        )

    try:
        await user_client.get_chat(chat_id)
    except Exception as exc:
        raise PipelineError(
            f"Chat access nahi mila ({exc}). Bot ko add karo ya /login karo."
        ) from exc

    _chat_client_cache[key] = user_client
    return user_client


def invalidate_chat_cache(chat_id: int | None = None) -> None:
    if chat_id is None:
        _chat_client_cache.clear()
        return
    for key in [k for k in _chat_client_cache if k[1] == int(chat_id)]:
        _chat_client_cache.pop(key, None)


async def fetch_message(client: Client, chat_id: int, message_id: int):
    for attempt in range(4):
        try:
            return await client.get_messages(chat_id, message_id)
        except FloodWait as exc:
            await asyncio.sleep(int(exc.value) + 2)
        except Exception as exc:
            if attempt == 3:
                raise
            log.warning("get_messages retry %s for %s/%s: %r", attempt + 1, chat_id, message_id, exc)
            await asyncio.sleep(min(2 ** attempt, 15))
    return None


async def process_message(
    app: Client,
    user_id: int,
    chat_id: int,
    message_id: int,
    *,
    target: dict | None = None,
    status_message=None,
    reporter: ProgressReporter | None = None,
    source_link: str | None = None,
) -> ProcessResult:
    """Download one Telegram message and upload it to TeraBox.

    Returns ``skipped`` when the message was already uploaded, ``no_media`` when
    it has no supported attachment. Raises :class:`PipelineError` on failure.
    """
    token = db.get_token(user_id)
    if not token:
        raise PipelineError("TeraBox account bind nahi hai. /bind se apni cookie do.")

    client = await resolve_client(app, user_id, chat_id)
    if not isinstance(chat_id, int):
        chat = await client.get_chat(chat_id)
        chat_id = int(chat.id)
        _chat_client_cache[(int(user_id), chat_id)] = client

    existing = db.upload_exists(user_id, chat_id, message_id)
    if existing:
        return ProcessResult(
            "skipped",
            link=existing,
            source_link=source_link or message_link(chat_id, message_id),
            message_id=message_id,
        )

    try:
        message = await fetch_message(client, chat_id, message_id)
    except Exception:
        message = None

    if (message is None or getattr(message, "empty", False)) and client is app:
        user_client = await get_user_client(user_id)
        if user_client is not None:
            try:
                fallback = await fetch_message(user_client, chat_id, message_id)
            except Exception:
                fallback = None
            if fallback is not None and not getattr(fallback, "empty", False):
                client = user_client
                message = fallback
                _chat_client_cache[(int(user_id), int(chat_id))] = user_client

    if message is None or getattr(message, "empty", False):
        return ProcessResult("no_media", message_id=message_id)

    media = pick_media(message)
    if media is None:
        return ProcessResult("no_media", message_id=message_id)

    name = build_name(message, media)
    size = media_size(media)
    if size > config.MAX_FILE_MB * 1024 * 1024:
        raise PipelineError(
            f"File {human(size)} hai, limit {config.MAX_FILE_MB} MB. "
            "Badi file ke liye direct link bhejo."
        )

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    local = os.path.join(
        config.DOWNLOAD_DIR,
        f"{user_id}_{abs(int(chat_id))}_{message_id}_{uuid.uuid4().hex[:10]}_{name}",
    )

    if size:
        usage = shutil.disk_usage(config.DOWNLOAD_DIR)
        free = usage.free
        buffer = min(64 * 1024 * 1024, max(8 * 1024 * 1024, size // 100))
        reserve = config.MIN_FREE_MB * 1024 * 1024
        required = size + buffer + reserve
        if size + buffer > usage.total:
            raise DiskSpaceError(
                f"File {human(size)} hai, lekin runtime volume sirf {human(usage.total)} hai. "
                "Is file ke liye larger DATA_DIR volume chahiye."
            )
        if free < required:
            raise DiskSpaceError(
                f"Disk space kam hai. Available {human(free)}, required {human(required)} "
                f"(file {human(size)} + safety reserve {human(reserve)})."
            )

    loop = asyncio.get_running_loop()
    reporter = reporter or ProgressReporter(status_message, loop=loop)
    if reporter.loop is None:
        reporter.loop = loop

    source = source_link or (
        message_link(chat_id, message_id, (target or {}).get("username"))
        if int(chat_id) < 0
        else None
    )

    try:
        await reporter.set_stage("⬇️ Telegram se download ho raha hai...")

        async def download_progress(current: int, total: int, rate: float) -> None:
            await reporter.update(
                current,
                total,
                rate,
                stage="⬇️ Telegram se download ho raha hai...",
            )

        await tgdl.download_media_parallel(
            client,
            media,
            local,
            size,
            download_progress,
            config.TELEGRAM_DOWNLOAD_WORKERS,
            range_limiter=download_slots,
        )

        if not os.path.exists(local):
            raise PipelineError("Downloaded file disk par nahi mili.")

        actual_size = os.path.getsize(local)
        await reporter.set_stage(
            f"✅ Download complete ({human(actual_size)})\n⬆️ TeraBox upload shuru...",
            force=True,
        )

        tb = TeraBox(token)
        await to_thread_wait_on_cancel(tb.ensure_login)

        started = time.monotonic()

        def upload_progress(current: int, total: int) -> None:
            rate = current / max(time.monotonic() - started, 0.001)
            reporter.thread_update(
                current, total, rate, stage="⬆️ TeraBox par upload ho raha hai..."
            )

        def stage_callback(stage: str) -> None:
            text = STAGE_TEXT.get(stage)
            if text:
                asyncio.run_coroutine_threadsafe(reporter.set_stage(text), loop)

        link = await to_thread_wait_on_cancel(
            tb.upload,
            local,
            name,
            target_folder(target),
            upload_progress,
            stage_callback,
            config.TERABOX_UPLOAD_THREADS,
            upload_part_slots,
        )

        db.save_upload(
            user_id,
            target["id"] if target else None,
            chat_id,
            message_id,
            name,
            actual_size,
            link,
            source,
        )
        if target:
            db.bump_target(target["id"], uploaded=1)

        log.info("Uploaded %s/%s -> %s", chat_id, message_id, link)
        return ProcessResult(
            "uploaded",
            link=link,
            name=name,
            source_link=source,
            message_id=message_id,
        )

    except TeraboxError as exc:
        detail = str(exc)
        if any(
            marker in detail
            for marker in ("31045", "user not exists", "errno=-6", "not login", "4000023")
        ):
            raise PipelineError(
                "TeraBox cookie expire/adhoori hai. Browser me dm.1024terabox.com par "
                "fresh login karke poori Cookie copy karo aur /bind karo."
            ) from exc
        raise PipelineError(f"TeraBox error: {detail}") from exc
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Upload fail hua: {exc}") from exc
    finally:
        if os.path.exists(local):
            try:
                os.remove(local)
            except OSError:
                pass
