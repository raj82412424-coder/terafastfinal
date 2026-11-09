"""TeraBox Uploader Bot entry point."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path

from pyrogram import Client

import config
import speedup  # noqa: F401  installs the media-session pool patch on import
from dashboard import start_dashboard
from handlers import register_handlers
from services import auto_engine, batch_engine, userbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Idle media-DC reconnects ("null packet" restart) are normal; hide the noise.
for _name in (
    "pyrogram.connection",
    "pyrogram.session.auth",
    "pyrogram.dispatcher",
    "pyrogram.session.session",
    "pyrogram.client",
):
    logging.getLogger(_name).setLevel(logging.WARNING)

config.validate_runtime_config()
os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

# Pyrogram's session contains Telegram authentication state; keep it private
# and colocated with the rest of the persistent runtime data.
os.umask(0o077)

log = logging.getLogger("bot")
_instance_lock = None


def _acquire_instance_lock() -> None:
    """Prevent duplicate bot processes sharing the same DATA_DIR and port."""
    global _instance_lock
    lock_path = config.DATA_DIR / ".bot.lock"
    _instance_lock = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(_instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(
            f"Another bot instance is already using {config.DATA_DIR}"
        ) from exc


def _cleanup_download_dir() -> None:
    """Remove interrupted runtime files before resuming persisted jobs."""
    removed = 0
    for path in Path(config.DOWNLOAD_DIR).iterdir():
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not remove stale download %s: %s", path, exc)
    if removed:
        log.info("Removed %s stale download file(s) before startup", removed)


app = Client(
    "terabox_uploader",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=str(config.DATA_DIR),
    max_concurrent_transmissions=config.TELEGRAM_TOTAL_DOWNLOAD_WORKERS,
    sleep_threshold=30,
)


async def main() -> None:
    register_handlers(app)
    await app.start()
    await batch_engine.resume_pending(app)
    auto_engine.start(app)
    log.info("Bot ready — commands: /start /set /targets /batch /bot /login")
    try:
        await asyncio.Event().wait()
    finally:
        await auto_engine.stop()
        await userbot.close_all()
        await app.stop()


if __name__ == "__main__":
    _acquire_instance_lock()
    _cleanup_download_dir()
    _, dashboard_url = start_dashboard()
    display_url = dashboard_url.split("?", 1)[0]
    suffix = " (auth token required)" if "?" in dashboard_url else ""
    print(f"Bot started · live logs: {display_url}{suffix}", flush=True)
    app.loop.run_until_complete(main())
