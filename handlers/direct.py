"""Direct message links, forwarded media and yt-dlp URL downloads."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time

import yt_dlp
from pyrogram import Client

import config
import db
from handlers import batch, bots, common, login, state, targets
from services.pipeline import (
    PipelineError,
    process_message,
    upload_part_slots,
)
from services.progress import ProgressReporter
from terabox import TeraBox
from utils.async_helpers import to_thread_wait_on_cancel
from utils.formatting import human, safe_name
from utils.links import parse_telegram_link
from utils.telegram import safe_edit

log = logging.getLogger("handlers.direct")

URL_RE = re.compile(r"https?://\S+")


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    text = (message.text or "").strip()

    if uid in state.parallel_flow:
        await common.handle_parallel_text(app, message)
        return
    if uid in state.set_flow:
        await targets.handle_text(app, message)
        return
    if uid in state.login_flow:
        await login.handle_text(app, message)
        return
    if uid in state.batch_flow:
        await batch.handle_text(app, message)
        return
    if uid in state.bot_flow:
        await bots.handle_text(app, message)
        return
    if uid in state.waiting_for_token:
        await common.handle_token(app, message, text)
        return

    link = parse_telegram_link(text)
    if link is not None:
        await _process_link(app, message, link)
        return

    match = URL_RE.search(text)
    if match:
        await _process_url(app, message, match.group(0))
        return

    await message.reply_text(
        "Kya karna hai?\n\n"
        "• Telegram message link paste karo — TeraBox link milega\n"
        "• Video/document bhejo — upload ho jayega\n"
        "• /set — naya target add karo\n"
        "• /targets — dashboard\n"
        "• /help — saare commands"
    )


async def _process_link(app: Client, message, link) -> None:
    uid = message.from_user.id
    if not db.get_token(uid):
        await message.reply_text("❌ Pehle TeraBox account bind karo: /bind")
        return

    status = await message.reply_text("⏳ Telegram message check ho raha hai...")
    try:
        result = await process_message(
            app,
            uid,
            link.chat,
            link.message_id,
            status_message=status,
            source_link=link.raw,
        )
    except PipelineError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Direct link failed")
        await safe_edit(status, f"❌ Error: {exc}")
        return

    if result.status == "uploaded":
        await safe_edit(
            status,
            "✅ **Upload complete**\n\n"
            f"📁 `{result.name}`\n"
            f"🔗 {result.link}\n"
            f"↳ source: {result.source_link}",
        )
    elif result.status == "skipped":
        await safe_edit(
            status,
            f"♻️ Ye message pehle upload ho chuka hai.\n🔗 {result.link}",
        )
    else:
        await safe_edit(status, "ℹ️ Is message me koi media file nahi mili.")


async def handle_media(app: Client, message) -> None:
    uid = message.from_user.id
    if not db.get_token(uid):
        await message.reply_text("❌ Pehle TeraBox account bind karo: /bind")
        return

    status = await message.reply_text("⏳ Queue me add ho raha hai...")
    position = await common.queue.submit(
        uid, lambda: _run_media(app, message, status), status
    )
    if position > 1:
        await safe_edit(status, f"⏳ Queue me waiting...\n📌 Position: {position}")


async def _run_media(app: Client, message, status) -> None:
    uid = message.from_user.id
    try:
        result = await process_message(
            app, uid, message.chat.id, message.id, status_message=status
        )
    except PipelineError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Media upload failed")
        await safe_edit(status, f"❌ Error: {exc}")
        return

    if result.status == "uploaded":
        await safe_edit(
            status,
            f"✅ **Upload complete**\n\n📁 `{result.name}`\n🔗 {result.link}",
        )
    elif result.status == "skipped":
        await safe_edit(status, f"♻️ Pehle upload ho chuka hai.\n🔗 {result.link}")
    else:
        await safe_edit(status, "ℹ️ Koi media file nahi mili.")


async def _process_url(app: Client, message, url: str) -> None:
    uid = message.from_user.id
    if not db.get_token(uid):
        await message.reply_text("❌ Pehle TeraBox account bind karo: /bind")
        return

    status = await message.reply_text("⏳ Queue me add ho raha hai...")
    position = await common.queue.submit(
        uid, lambda: _run_url(app, message, url, status), status
    )
    if position > 1:
        await safe_edit(status, f"⏳ Queue me waiting...\n📌 Position: {position}")


async def _run_url(app: Client, message, url: str, status) -> None:
    uid = message.from_user.id
    token = db.get_token(uid)
    if not token:
        await safe_edit(status, "❌ TeraBox account bind nahi hai.")
        return

    folder = os.path.join(config.DOWNLOAD_DIR, f"{uid}_{message.id}_url")
    os.makedirs(folder, exist_ok=True)
    reporter = ProgressReporter(status, loop=asyncio.get_running_loop())

    try:
        await reporter.set_stage("⬇️ Link se download ho raha hai...")
        last = [0.0]

        def hook(data):
            if data.get("status") != "downloading":
                return
            now = time.time()
            if now - last[0] < 3:
                return
            last[0] = now
            current = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            rate = float(data.get("speed") or 0)
            reporter.thread_update(
                current, total, rate, stage="⬇️ Link se download ho raha hai..."
            )

        options = {
            "outtmpl": os.path.join(folder, "%(title).120B.%(ext)s"),
            "progress_hooks": [hook],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "concurrent_fragment_downloads": config.URL_FRAGMENT_WORKERS,
            "http_chunk_size": 10 * 1024 * 1024,
            "merge_output_format": "mp4",
            "max_filesize": config.MAX_URL_GB * 1024**3,
        }

        def download():
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)

        await to_thread_wait_on_cancel(download)

        files = [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, name))
            and not name.endswith((".part", ".ytdl"))
        ]
        if not files:
            raise RuntimeError("Link se file download nahi hui (size limit ya unsupported link).")

        local = max(files, key=os.path.getsize)
        name = safe_name(os.path.basename(local))
        await reporter.set_stage(
            f"✅ Download complete ({human(os.path.getsize(local))})\n"
            "⬆️ TeraBox upload shuru...",
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

        link = await to_thread_wait_on_cancel(
            tb.upload,
            local,
            name,
            config.REMOTE_FOLDER,
            upload_progress,
            None,
            config.TERABOX_UPLOAD_THREADS,
            upload_part_slots,
        )
        await reporter.finish(
            f"✅ **Upload complete**\n\n📁 `{name}`\n🔗 {link}"
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("URL upload failed")
        await safe_edit(status, f"❌ Error: {exc}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
