"""Target management: /set, /targets dashboard, links list and TXT export."""
from __future__ import annotations

import io
import logging
import math
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from handlers import state
from services.batch_engine import latest_message_id
from services.pipeline import resolve_client
from services.userbot import get_user_client
from utils.formatting import bar, safe_name, short
from utils.links import message_link, parse_telegram_link
from utils.telegram import safe_edit

log = logging.getLogger("handlers.targets")


def _parse_chat_ref(text: str):
    link = parse_telegram_link(text)
    if link is not None:
        return link.chat

    value = (text or "").strip()
    if value.startswith("@"):
        return value
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if "t.me/" in value:
        tail = value.rstrip("/").split("/")[-1]
        if tail and not tail.startswith("+"):
            return tail
    return value


async def _resolve_chat(app: Client, uid: int, ref):
    try:
        return await app.get_chat(ref)
    except Exception:
        client = await get_user_client(uid)
        if client is None:
            raise
        return await client.get_chat(ref)


def _target_text(target: dict) -> str:
    uploaded = db.count_uploads(target["id"])
    total = int(target["total_messages"] or 0)
    failed = int(target["failed_count"] or 0)
    skipped = int(target["skipped_count"] or 0)
    processed = uploaded + failed + skipped
    if total:
        processed = min(total, processed)
    pending = max(0, total - processed) if total else 0
    auto = "ON 🟢" if target["auto_mode"] else "OFF 🔴"
    start_link = target["start_link"] or message_link(
        target["chat_id"], target["start_message_id"], target["username"]
    )

    lines = [
        f"🎯 **{target['title']}**",
        f"🆔 `{target['chat_id']}`",
        f"🔗 Start: {start_link}",
        f"🤖 Auto mode: **{auto}**",
        "",
        f"📊 Total: {total or '—'} | ✅ {uploaded} | ❌ {failed} | ⏳ {pending}",
    ]
    if total:
        lines.append(bar(processed, total))
    return "\n".join(lines)


def _target_keyboard(target: dict) -> InlineKeyboardMarkup:
    tid = target["id"]
    auto_label = "🔄 Auto OFF" if target["auto_mode"] else "🔄 Auto ON"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Links List", callback_data=f"t:l:{tid}:0"),
                InlineKeyboardButton("▶️ Batch Start", callback_data=f"b:t:{tid}"),
            ],
            [
                InlineKeyboardButton(auto_label, callback_data=f"t:a:{tid}"),
                InlineKeyboardButton("🗑 Remove", callback_data=f"t:r:{tid}"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="t:list")],
        ]
    )


async def _refresh_total(app: Client, uid: int, target: dict) -> dict:
    if int(target["total_messages"] or 0) > 0:
        return target
    try:
        client = await resolve_client(app, uid, target["chat_id"])
        latest = await latest_message_id(client, target["chat_id"])
        total = max(0, latest - int(target["start_message_id"]) + 1)
        db.set_target_total(target["id"], total)
        return db.get_target(target["id"]) or target
    except Exception as exc:  # noqa: BLE001 - display still works without total
        log.debug("Could not refresh total for %s: %r", target["id"], exc)
        return target


async def show_targets(app: Client, message, uid: int, *, edit: bool = False) -> None:
    targets = db.list_targets(uid)
    if not targets:
        text = "🎯 Koi target save nahi hai.\n\n/set se group/channel add karo."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Add Target", callback_data="m:add")]]
        )
    else:
        buttons = []
        for target in targets:
            uploaded = db.count_uploads(target["id"])
            total = int(target["total_messages"] or 0)
            mark = "✅" if total and uploaded >= total else "⏳"
            label = f"📢 {short(str(target['title']), 26)} | {uploaded}/{total or '?'} {mark}"
            buttons.append(
                [InlineKeyboardButton(label, callback_data=f"t:v:{target['id']}")]
            )
        buttons.append([InlineKeyboardButton("➕ Add Target", callback_data="m:add")])
        text = (
            f"🎯 **Targets** ({len(targets)})\n\n"
            "Kisi target par click karke status, links aur batch dekho."
        )
        keyboard = InlineKeyboardMarkup(buttons)

    if edit:
        await safe_edit(message, text, keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def start_set_flow(app: Client, message, uid: int, *, edit: bool = False) -> None:
    state.set_flow[uid] = {"step": "chat"}
    text = (
        "🎯 **Naya target**\n\n"
        "Group/channel ka ID, @username ya link bhejo.\n\n"
        "Example: `-1001234567890` ya `@my_channel`\n\n"
        "/cancel se ruk sakte ho."
    )
    if edit:
        await safe_edit(message, text)
    else:
        await message.reply_text(text)


async def _handle_chat(app: Client, message, flow: dict, ref=None) -> None:
    uid = message.from_user.id
    raw = ref if ref is not None else (message.text or "")
    parsed = _parse_chat_ref(raw)

    try:
        chat = await _resolve_chat(app, uid, parsed)
    except Exception as exc:  # noqa: BLE001
        await message.reply_text(
            f"❌ Chat resolve nahi hua: `{exc}`\n\n"
            "Bot ko group/channel me add karo ya /login se user session banao."
        )
        return

    title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
    state.set_flow[uid] = {
        "step": "start_link",
        "chat_id": int(chat.id),
        "title": title,
        "username": chat.username,
        "chat_type": str(chat.type),
    }

    hint = (
        f"https://t.me/{chat.username}/42966"
        if chat.username
        else "https://t.me/c/3891933514/1428"
    )
    await message.reply_text(
        f"✅ Target mila: **{title}**\n🆔 `{chat.id}`\n\n"
        "Ab bhejo: **Download kahan se shuru karna hai?**\n"
        f"Us message ka link paste karo, jaise `{hint}`\n\n"
        "/cancel se ruk sakte ho."
    )


async def _handle_start_link(app: Client, message, flow: dict) -> None:
    uid = message.from_user.id
    text = (message.text or "").strip()
    link = parse_telegram_link(text)

    if link is not None:
        message_id = link.message_id
        if isinstance(link.chat, int) and link.chat != int(flow["chat_id"]):
            await message.reply_text(
                "❌ Ye link is target ka nahi hai. Same group/channel ka message link bhejo."
            )
            return
        if (
            isinstance(link.chat, str)
            and flow.get("username")
            and link.chat.lower() != str(flow["username"]).lower()
        ):
            await message.reply_text(
                "❌ Ye link is target ka nahi hai. Same group/channel ka message link bhejo."
            )
            return
    elif text.isdigit():
        message_id = int(text)
    else:
        await message.reply_text(
            "❌ Valid message link bhejo, jaise `https://t.me/username/123`"
        )
        return

    target_id = db.add_target(
        uid,
        int(flow["chat_id"]),
        str(flow["title"]),
        flow.get("username"),
        flow.get("chat_type"),
        message_id,
        text if link is not None else None,
    )
    db.set_last_seen(target_id, message_id - 1)
    state.set_flow.pop(uid, None)

    target = db.get_target(target_id)
    if target is None:
        await message.reply_text("❌ Target save nahi hua. Dobara /set karo.")
        return

    await message.reply_text(
        f"✅ **Target save ho gaya!**\n\n{_target_text(target)}",
        reply_markup=_target_keyboard(target),
    )


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    flow = state.set_flow.get(uid)
    if not flow:
        return
    if flow.get("step") == "chat":
        await _handle_chat(app, message, flow)
    elif flow.get("step") == "start_link":
        await _handle_start_link(app, message, flow)


async def _show_target(app: Client, query, uid: int, target_id: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return
    target = await _refresh_total(app, uid, target)
    await safe_edit(query.message, _target_text(target), _target_keyboard(target))


async def _show_links(app: Client, query, uid: int, target_id: int, page: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return

    page_size = config.LINKS_PAGE_SIZE
    total = db.count_uploads(target_id)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(int(page), pages - 1))
    rows = db.list_uploads(target_id, page * page_size, page_size)

    lines = [f"**Uploaded TeraBox links** — {target['title']}", ""]
    if not rows:
        lines.append("Abhi koi link upload nahi hua.")
    for index, row in enumerate(rows, start=page * page_size + 1):
        lines.append(f"{index}. 📁 {row['file_name']}")
        lines.append(f"   {row['terabox_url']}")
        lines.append(f"   ↳ source: {row['source_link'] or '—'}")
        lines.append("")
    lines.append(f"Page {page + 1}/{pages} · {total} links")

    buttons = []
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Prev", callback_data=f"t:l:{target_id}:{page - 1}"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"t:l:{target_id}:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("📄 Export TXT", callback_data=f"t:e:{target_id}")]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️ Back", callback_data=f"t:v:{target_id}")]
    )

    await safe_edit(query.message, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def _export(app: Client, query, uid: int, target_id: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return

    rows = db.list_uploads(target_id, 0, 1_000_000)
    lines = [
        f"Uploaded TeraBox links — {target['title']}",
        f"Target: {target['chat_id']}",
        f"Total: {len(rows)}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. 📁 {row['file_name']}")
        lines.append(f"   {row['terabox_url']}")
        lines.append(f"   ↳ source: {row['source_link'] or '—'}")
        lines.append("")

    data = "\n".join(lines).encode("utf-8")
    filename = f"{safe_name(str(target['title']))[:40]}_links.txt"
    try:
        await app.send_document(
            uid,
            io.BytesIO(data),
            file_name=filename,
            caption=f"📄 {len(rows)} links · {target['title']}",
        )
    except Exception as exc:  # noqa: BLE001
        await safe_edit(query.message, f"❌ Export fail hua: {exc}")
        return

    await query.answer("TXT bhej diya", show_alert=False)


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    uid = query.from_user.id

    if action == "list":
        await show_targets(app, query.message, uid, edit=True)
    elif action == "v":
        await _show_target(app, query, uid, int(parts[2]))
    elif action == "a":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        turning_on = not bool(target["auto_mode"])
        if turning_on:
            # Auto mode watches *new* media; backfill is /batch's job.
            try:
                client = await resolve_client(app, uid, target["chat_id"])
                latest = await latest_message_id(client, target["chat_id"])
                if latest:
                    db.set_last_seen(target_id, latest)
            except Exception as exc:  # noqa: BLE001 - toggle still works
                log.debug("Could not set auto baseline for %s: %r", target_id, exc)
        db.set_auto_mode(target_id, turning_on)
        target = db.get_target(target_id)
        await safe_edit(query.message, _target_text(target), _target_keyboard(target))
    elif action == "r":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        count = db.count_uploads(target_id)
        text = (
            "⚠️ **Remove target?**\n\n"
            f"📢 {target['title']}\n"
            f"🔗 {count} uploaded links bhi delete ho jayenge."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, remove", callback_data=f"t:rc:{target_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Cancel", callback_data=f"t:v:{target_id}"
                    )
                ],
            ]
        )
        await safe_edit(query.message, text, keyboard)
    elif action == "rc":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        db.delete_target(target_id)
        await show_targets(app, query.message, uid, edit=True)
    elif action == "l":
        await _show_links(app, query, uid, int(parts[2]), int(parts[3]))
    elif action == "e":
        await _export(app, query, uid, int(parts[2]))


def register(app: Client) -> None:
    @app.on_message(filters.command("set") & filters.private)
    async def set_cmd(client, message):
        if len(message.command) > 1:
            await _handle_chat(client, message, {"step": "chat"}, " ".join(message.command[1:]))
            return
        await start_set_flow(client, message, message.from_user.id)

    @app.on_message(filters.command("targets") & filters.private)
    async def targets_cmd(client, message):
        await show_targets(client, message, message.from_user.id)
