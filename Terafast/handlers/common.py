"""Core commands: start, help, status, TeraBox binding, queue and stop."""
from __future__ import annotations

import logging
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from handlers import state
from services import batch_engine, userbot
from services.pipeline import invalidate_chat_cache
from terabox import TeraBox
from utils.async_helpers import to_thread_wait_on_cancel
from utils.formatting import human
from utils.queue import JobQueue
from utils.telegram import safe_edit

log = logging.getLogger("handlers.common")

queue = JobQueue(config.MAX_PARALLEL)

BIND_HELP = (
    "🔑 **TeraBox cookie bhejo.**\n\n"
    "Kaise milega:\n"
    "1. Browser me dm.1024terabox.com par login karo\n"
    "2. F12 → Network tab kholo\n"
    "3. Koi bhi request par click karke **Cookie** header copy karo\n"
    "4. Poori Cookie yahan paste karo\n\n"
    "💡 ndus + browserid + ndut_fmt sab include karo.\n"
    "Cookie milte hi message delete ho jayega. /cancel se ruk sakte ho."
)

PARALLEL_HELP = (
    "⚙️ **Max parallel set karo**\n\n"
    "Kitne upload/download jobs ek saath chalne chahiye?\n"
    "Sirf **1**, **2**, **3** ya **4** bhejo.\n"
    "Example: `3`"
)

HELP_TEXT = (
    "📖 **Commands**\n\n"
    "**Targets**\n"
    "/set — naya group/channel add karo\n"
    "/targets — dashboard, links list, TXT export\n"
    "/batch — bulk download + upload\n"
    "/bot — dusre bot ki videos auto-upload (bot watch)\n\n"
    "**Account**\n"
    "/bind — TeraBox cookie\n"
    "/status — quota aur session\n"
    "/delete — TeraBox config delete\n"
    "/login — Telegram user session (private chats)\n"
    "/logout — session remove\n\n"
    "**Jobs**\n"
    "/parallel ya /pr — max parallel jobs 1–4 set karo\n"
    "/queue — queue status\n"
    "/stop — apne jobs cancel karo\n"
    "/cancel — current input flow cancel\n\n"
    "**Direct**\n"
    "• Telegram message link paste karo\n"
    "• Video/document bhejo\n"
    "• Direct URL bhejo (yt-dlp)"
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎯 Targets", callback_data="m:targets"),
                InlineKeyboardButton("➕ Add Target", callback_data="m:add"),
            ],
            [
                InlineKeyboardButton("🤖 Bot Watch", callback_data="m:bots"),
                InlineKeyboardButton("🔑 TeraBox Account", callback_data="m:tb"),
            ],
            [
                InlineKeyboardButton("👤 Telegram Login", callback_data="m:login"),
                InlineKeyboardButton("📊 Status", callback_data="m:status"),
            ],
        ]
    )


def _normalize_cookie(raw: str) -> str:
    token = (raw or "").strip().strip("`").replace("\r\n", "\n")
    if token.lower().startswith("cookie:"):
        token = token.split(":", 1)[1].strip()

    if ";" not in token and "\n" not in token:
        token = re.sub(r"\s+", "", token)
        if token.lower().startswith("ndus="):
            token = token[5:]
        return token

    cookies: list[str] = []
    for line in re.split(r"[;\n]+", token):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            cookies.append(f"{key}={value}")
    return "; ".join(cookies)


async def handle_token(app: Client, message, raw: str) -> None:
    uid = message.from_user.id
    token = _normalize_cookie(raw)
    if not token or len(token) < 15:
        await message.reply_text(
            "❌ Cookie adhoori lag rahi hai. Poori TeraBox Cookie bhejo."
        )
        return

    try:
        await message.delete()
    except Exception:
        pass

    wait = await app.send_message(uid, "⏳ Token verify ho raha hai...")
    try:
        quota = await to_thread_wait_on_cancel(TeraBox(token).validate)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        await safe_edit(
            wait,
            "❌ Token verify nahi hua. Check karo ki browser me "
            "dm.1024terabox.com logged in hai.\n"
            f"`{exc}`\n\n/change se dobara poori cookie bhejo.",
        )
        return

    db.save_token(uid, token)
    state.waiting_for_token.discard(uid)
    invalidate_chat_cache()
    await safe_edit(
        wait,
        "✅ Account bind ho gaya!\n"
        f"💾 Used: {human(quota.get('used', 0))} / {human(quota.get('total', 0))}\n\n"
        "Ab /set se target add karo.",
        main_menu(),
    )


async def send_status(app: Client, message, uid: int, *, edit: bool = False) -> None:
    lines = ["📊 **Status**", ""]

    token = db.get_token(uid)
    if token:
        try:
            quota = await to_thread_wait_on_cancel(TeraBox(token).validate)
            lines.append(
                f"🔑 TeraBox: ✅ {human(quota.get('used', 0))} / {human(quota.get('total', 0))}"
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"🔑 TeraBox: ⚠️ {exc}")
    else:
        lines.append("🔑 TeraBox: ❌ bind nahi hai (/bind)")

    lines.append(
        f"👤 Telegram session: {'✅ active' if db.get_session(uid) else '❌ none (/login)'}"
    )
    lines.append(f"🎯 Targets: {len(db.list_targets(uid))}")

    stats = queue.stats(uid)
    lines.append(
        f"📋 Queue: {stats['active']} active · {stats['waiting']} waiting"
    )
    lines.append(f"⚙️ Max parallel: {stats['workers']}")

    text = "\n".join(lines)
    if edit:
        await safe_edit(message, text, main_menu())
    else:
        await message.reply_text(text, reply_markup=main_menu())


def _tb_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Change", callback_data="m:tb:change"),
                InlineKeyboardButton("🗑 Delete", callback_data="m:tb:delete"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="m:back")],
        ]
    )


def _tb_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, delete", callback_data="m:tb:delete_yes"),
                InlineKeyboardButton("⬅️ Cancel", callback_data="m:tb:delete_no"),
            ]
        ]
    )


async def send_tb_account(
    app: Client, message, uid: int, *, edit: bool = False
) -> None:
    """Show connection details without ever displaying the saved cookie."""
    token = db.get_token(uid)
    if token:
        state.waiting_for_token.discard(uid)
    if not token:
        state.waiting_for_token.add(uid)
        text = BIND_HELP
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="m:back")]]
        )
        if edit:
            await safe_edit(message, text, keyboard)
        else:
            await message.reply_text(text, reply_markup=keyboard)
        return

    lines = [
        "🔑 **TeraBox Account**",
        "",
        "✅ Connection: **Bound**",
        "🔒 Cookie: Saved securely (encrypted)",
    ]
    try:
        quota = await to_thread_wait_on_cancel(TeraBox(token).validate)
        lines.extend(
            [
                f"📦 Storage: {human(quota.get('used', 0))} / "
                f"{human(quota.get('total', 0))}",
                "🟢 Status: **Connected**",
            ]
        )
    except Exception:  # noqa: BLE001 - do not expose cookie/API details
        lines.extend(
            [
                "🟡 Status: **Bound, refresh failed**",
                "Quota details abhi fetch nahi ho paaye. Change se cookie update karo.",
            ]
        )

    text = "\n".join(lines)
    if edit:
        await safe_edit(message, text, _tb_account_keyboard())
    else:
        await message.reply_text(text, reply_markup=_tb_account_keyboard())


async def handle_parallel_text(app: Client, message) -> None:
    await _apply_parallel(message, message.text or "")


async def _apply_parallel(message, raw: str) -> None:
    uid = message.from_user.id
    value_text = (raw or "").strip()
    if value_text not in {"1", "2", "3", "4"}:
        await message.reply_text("❌ Sirf 1, 2, 3 ya 4 bhejo. /cancel se ruk sakte ho.")
        return

    value = int(value_text)
    previous = queue.workers
    persisted = config.set_max_parallel(value)
    queue.set_workers(value)
    state.parallel_flow.discard(uid)
    saved = "💾 Restart ke baad bhi saved rahega." if persisted else "⚠️ Runtime me set hua; .env save nahi ho saka."
    await message.reply_text(
        f"✅ Max parallel **{value}** set ho gaya.\n"
        f"Pehle: {previous} · Ab: {value}\n"
        f"Active jobs interrupt nahi honge; extra workers idle hote hi adjust honge.\n\n{saved}"
    )


def register(app: Client) -> None:
    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client, message):
        bound = db.get_token(message.from_user.id) is not None
        text = (
            "👋 **TeraBox Uploader Bot**\n\n"
            "Telegram media download karke TeraBox par upload karta hai.\n\n"
            "**Setup**\n"
            "1️⃣ /bind — TeraBox cookie\n"
            "2️⃣ /set — target group/channel + start message\n"
            "3️⃣ /batch — bulk upload\n\n"
            "**Direct**\n"
            "Koi bhi Telegram message link paste karo — TeraBox link wapas milega.\n\n"
            "Private groups ke liye /login karo."
        )
        if bound:
            text += "\n\n✅ TeraBox account bind hai."
        await message.reply_text(text, reply_markup=main_menu())

    @app.on_message(filters.command("help") & filters.private)
    async def help_cmd(client, message):
        await message.reply_text(HELP_TEXT, reply_markup=main_menu())

    @app.on_message(filters.command(["parallel", "pr"]) & filters.private)
    async def parallel_cmd(client, message):
        uid = message.from_user.id
        state.clear(uid)
        if len(message.command) > 1:
            await _apply_parallel(message, message.command[1])
            return
        state.parallel_flow.add(uid)
        stats = queue.stats(uid)
        await message.reply_text(
            f"{PARALLEL_HELP}\n\nCurrent: **{stats['workers']}**"
        )

    @app.on_message(filters.command(["bind", "change"]) & filters.private)
    async def bind_cmd(client, message):
        if len(message.command) > 1:
            await handle_token(client, message, " ".join(message.command[1:]))
            return
        state.waiting_for_token.add(message.from_user.id)
        await message.reply_text(BIND_HELP)

    @app.on_message(filters.command(["delete", "delete_config"]) & filters.private)
    async def delete_cmd(client, message):
        state.waiting_for_token.discard(message.from_user.id)
        ok = db.delete_token(message.from_user.id)
        await message.reply_text(
            "🗑 TeraBox config delete ho gaya." if ok else "Koi config saved nahi tha.",
            reply_markup=main_menu(),
        )

    @app.on_message(filters.command("cancel") & filters.private)
    async def cancel_cmd(client, message):
        uid = message.from_user.id
        state.clear(uid)
        await userbot.cancel_login(uid)
        await message.reply_text("Cancelled.")

    @app.on_message(filters.command(["status", "config"]) & filters.private)
    async def status_cmd(client, message):
        await send_status(client, message, message.from_user.id)

    @app.on_message(filters.command("queue") & filters.private)
    async def queue_cmd(client, message):
        stats = queue.stats(message.from_user.id)
        await message.reply_text(
            "📋 **Queue**\n\n"
            f"▶️ Active: {stats['active']}\n"
            f"⏳ Waiting (all users): {stats['waiting']}\n"
            f"👤 Your waiting jobs: {stats['user_waiting']}\n"
            f"⚙️ Workers: {stats['workers']}"
        )

    @app.on_message(filters.command(["stop", "stopall"]) & filters.private)
    async def stop_cmd(client, message):
        uid = message.from_user.id
        state.clear(uid)
        stopped = batch_engine.stop_user(uid)
        active, pending = queue.cancel_user(uid)
        await message.reply_text(
            "🛑 Stop request bheja.\n\n"
            f"📦 Batch jobs: {stopped}\n"
            f"▶️ Active jobs: {active}\n"
            f"⏳ Waiting skipped: {pending}"
        )


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    uid = query.from_user.id

    if data == "m:targets":
        from handlers import targets

        await targets.show_targets(app, query.message, uid, edit=True)
    elif data == "m:add":
        from handlers import targets

        await targets.start_set_flow(app, query.message, uid, edit=True)
    elif data == "m:tb":
        await send_tb_account(app, query.message, uid, edit=True)
    elif data == "m:tb:change":
        state.waiting_for_token.add(uid)
        await safe_edit(
            query.message,
            BIND_HELP,
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="m:tb")]]
            ),
        )
    elif data == "m:tb:delete":
        if db.get_token(uid) is None:
            await send_tb_account(app, query.message, uid, edit=True)
            return
        await safe_edit(
            query.message,
            "⚠️ **TeraBox account delete karna hai?**\n\n"
            "Saved cookie permanently remove ho jayegi. Upload jobs par iska effect nahi hoga, "
            "lekin next upload se pehle dobara bind karna padega.",
            _tb_delete_keyboard(),
        )
    elif data == "m:tb:delete_yes":
        state.waiting_for_token.discard(uid)
        deleted = db.delete_token(uid)
        invalidate_chat_cache()
        await safe_edit(
            query.message,
            "🗑 TeraBox account delete ho gaya." if deleted else "Koi TeraBox account saved nahi tha.",
            main_menu(),
        )
    elif data == "m:tb:delete_no":
        await send_tb_account(app, query.message, uid, edit=True)
    elif data == "m:back":
        state.waiting_for_token.discard(uid)
        await safe_edit(query.message, "🏠 **Main menu**", main_menu())
    elif data == "m:login":
        from handlers import login

        await login.show_login(app, query.message, uid, edit=True)
    elif data == "m:bots":
        from handlers import bots

        await bots.show_list(app, query.message, uid, edit=True)
    elif data == "m:status":
        await send_status(app, query.message, uid, edit=True)
