"""Batch command and its inline flow."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
from handlers import state
from services import batch_engine
from utils.formatting import short
from utils.telegram import safe_edit

log = logging.getLogger("handlers.batch")


def _target_buttons(targets: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"📢 {short(str(target['title']), 30)}",
                callback_data=f"b:t:{target['id']}",
            )
        ]
        for target in targets
    ]
    return InlineKeyboardMarkup(buttons)


async def show_targets(app: Client, message, uid: int, *, edit: bool = False) -> None:
    targets = db.list_targets(uid)
    if not targets:
        text = "🎯 Pehle /set se target add karo."
        if edit:
            await safe_edit(message, text)
        else:
            await message.reply_text(text)
        return

    text = "📦 **Batch**\n\nKis target ka batch chalana hai?"
    keyboard = _target_buttons(targets)
    if edit:
        await safe_edit(message, text, keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def _count_menu(app: Client, query, target: dict) -> None:
    text = (
        f"📦 **{target['title']}**\n\n"
        "Kitne messages process karne hain?\n"
        "Start message se aage ginti hogi."
    )
    rows = [
        [
            InlineKeyboardButton("100", callback_data=f"b:n:{target['id']}:100"),
            InlineKeyboardButton("500", callback_data=f"b:n:{target['id']}:500"),
        ],
        [
            InlineKeyboardButton("1000", callback_data=f"b:n:{target['id']}:1000"),
            InlineKeyboardButton("♾️ All", callback_data=f"b:n:{target['id']}:0"),
        ],
        [InlineKeyboardButton("✍️ Custom", callback_data=f"b:c:{target['id']}")],
    ]
    last = db.latest_job_for_target(target["id"])
    if (
        last is not None
        and last["status"] in ("paused", "error")
        and int(last["cursor_id"]) < int(last["end_id"])
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    f"▶️ Resume ({last['processed']}/{last['total']})",
                    callback_data=f"b:r:{last['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="b:list")])
    await safe_edit(query.message, text, InlineKeyboardMarkup(rows))


async def show_count_menu(app: Client, query, target: dict) -> None:
    await _count_menu(app, query, target)


async def _start(
    app: Client, query, uid: int, target_id: int, count: int | None
) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await query.answer("Target nahi mila", show_alert=True)
        return

    await safe_edit(
        query.message, f"⏳ **{target['title']}** ka batch prepare ho raha hai..."
    )
    try:
        job_id = await batch_engine.create_job(app, uid, target_id, count)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(query.message, f"❌ Batch start nahi hua: {exc}")
        return

    job = db.get_batch_job(job_id)
    label = "all messages" if count is None else f"{count} messages"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏹ Stop batch", callback_data=f"b:s:{job_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ Targets", callback_data="t:list")],
        ]
    )
    await safe_edit(
        query.message,
        "✅ **Batch started**\n\n"
        f"📢 {target['title']}\n"
        f"📦 {label}\n"
        f"📊 Total: {job['total'] if job else '—'}\n\n"
        "Live progress alag message me aayega.",
        keyboard,
    )


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    uid = query.from_user.id

    if action == "list":
        await show_targets(app, query.message, uid, edit=True)
    elif action == "t":
        target = db.get_target(int(parts[2]))
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        await _count_menu(app, query, target)
    elif action == "n":
        target_id = int(parts[2])
        count = int(parts[3])
        await _start(app, query, uid, target_id, None if count == 0 else count)
    elif action == "c":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        state.batch_flow[uid] = {"step": "count", "target_id": target_id}
        await safe_edit(
            query.message,
            f"✍️ **{target['title']}**\n\nKitne messages? Number bhejo (jaise 2500).",
        )
    elif action == "s":
        job_id = int(parts[2])
        ok = batch_engine.request_stop(job_id)
        await query.answer("Stop request bheja" if ok else "Job already stopped")
    elif action == "r":
        job_id = int(parts[2])
        ok = await batch_engine.resume_job(app, job_id)
        await query.answer("Resume ho raha hai" if ok else "Resume nahi ho saka")


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    flow = state.batch_flow.get(uid)
    if not flow:
        return

    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.reply_text("❌ Number bhejo, jaise 2500.")
        return

    count = int(text)
    state.batch_flow.pop(uid, None)
    target = db.get_target(flow["target_id"])
    if target is None:
        await message.reply_text("❌ Target nahi mila.")
        return

    status = await message.reply_text("⏳ Batch prepare ho raha hai...")
    try:
        await batch_engine.create_job(app, uid, target["id"], count)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(status, f"❌ Batch start nahi hua: {exc}")
        return
    await safe_edit(
        status,
        f"✅ **Batch started**\n\n📢 {target['title']}\n📦 {count} messages",
    )


def register(app: Client) -> None:
    @app.on_message(filters.command("batch") & filters.private)
    async def batch_cmd(client, message):
        await show_targets(client, message, message.from_user.id)
