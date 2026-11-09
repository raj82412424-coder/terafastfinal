"""Bot watch: track another bot's videos and auto-upload them to TeraBox.

/bot se user kisi dusre bot ko register karta hai (ID, @username ya t.me link).
Us bot ki aane wali videos automatically download + TeraBox upload hoti hain
live progress bar ke saath, aur link bot chat me aata hai.
"""
from __future__ import annotations

import logging
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
from handlers import state
from services.batch_engine import latest_message_id
from services.userbot import get_user_client
from utils.telegram import safe_edit

log = logging.getLogger("handlers.bots")

BOT_ADD_PROMPT = (
    "🤖 **Bot Watch**\n\n"
    "Kaunsa bot track karna hai? Us bot ka **ID**, **@username** ya **link** bhejo:\n\n"
    "• `7040617509`\n"
    "• `@FreeVideoEditorbot`\n"
    "• `https://t.me/FreeVideoEditorbot`\n\n"
    "Register karte hi jab bhi wo bot aapko video bhejega, wo automatically "
    "download + TeraBox upload ho jayegi aur yahin **progress bar** ke saath "
    "link milega.\n\n"
    "📋 `/bot list` — tracked bots\n"
    "🗑 `/bot remove <id/username>`\n"
    "/cancel se ruk sakte ho."
)

_PARSE_USAGE = (
    "❌ Samajh nahi aaya. Inme se koi ek format bhejo:\n\n"
    "• `7040617509`\n"
    "• `@FreeVideoEditorbot`\n"
    "• `https://t.me/FreeVideoEditorbot`"
)

_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z0-9_]+)",
    re.I,
)
_UNWANTED_PATHS = {"c", "s", "joinchat", "share", "proxy", "socks", "addstickers"}


def _parse_bot_ref(raw: str):
    """Return ("id", int) or ("username", str); None when unparseable."""
    value = (raw or "").strip().strip("`").strip()
    if not value:
        return None

    match = _LINK_RE.search(value)
    if match:
        name = match.group(1)
        if name.lower() in _UNWANTED_PATHS or name.startswith("+"):
            return None
        return ("username", name)

    if value.startswith("@"):
        name = value[1:]
        if re.fullmatch(r"[A-Za-z0-9_]{3,}", name):
            return ("username", name)
        return None

    if re.fullmatch(r"\d{5,}", value):
        return ("id", int(value))

    return None


async def _resolve_bot(app: Client, uid: int, kind: str, value):
    """Resolve the watched bot via the user session, then the bot client."""
    ref = value if kind == "id" else f"@{value}"
    user_client = await get_user_client(uid)
    if user_client is not None:
        try:
            return await user_client.get_users(ref)
        except Exception as exc:  # noqa: BLE001
            log.debug("User session could not resolve %s: %r", ref, exc)
    try:
        return await app.get_users(ref)
    except Exception as exc:  # noqa: BLE001
        log.debug("Bot client could not resolve %s: %r", ref, exc)
    return None


def _display_name(user) -> str:
    name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    ).strip()
    return name or (getattr(user, "username", None) or str(user.id))


async def _register(app: Client, message, uid: int, kind: str, value) -> None:
    if db.get_session(uid) is None:
        await message.reply_text(
            "❌ Bot watch ke liye **Telegram login** zaroori hai — "
            "bot ki bheji videos aapke account ke through padhi jati hain.\n\n"
            "Pehle /login karo, phir /bot."
        )
        return

    wait = await message.reply_text("⏳ Bot check ho raha hai...")
    user = await _resolve_bot(app, uid, kind, value)
    if user is None:
        await safe_edit(
            wait,
            "❌ Bot resolve nahi hua. ID/username/link check karo.\n\n"
            "Agar bot sahi hai, pehle us bot ko Telegram me ek baar /start karo "
            "(taaki aapka account us bot ko jaanta ho), phir /bot dobara bhejo.",
        )
        return

    bot_id = int(user.id)
    bot_username = (getattr(user, "username", None) or "").strip() or None
    name = _display_name(user)
    is_bot = bool(getattr(user, "is_bot", False))

    user_client = await get_user_client(uid)
    if user_client is None:
        await safe_edit(wait, "❌ User session connect nahi ho payi. Dobara /login karo.")
        return

    try:
        latest = await latest_message_id(user_client, bot_id)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(
            wait,
            f"❌ Is bot ka chat read nahi ho paya: `{exc}`\n\n"
            "Bot ko pehle /start karo, phir /bot se add karo.",
        )
        return

    start_link = f"https://t.me/{bot_username}" if bot_username else None
    target_id = db.add_target(
        uid,
        bot_id,
        name,
        bot_username,
        "bot",
        max(1, latest or 1),
        start_link,
        bot_watch=1,
    )
    db.set_auto_mode(target_id, True)
    if latest:
        # Future-only: the poller starts from the next incoming message.
        db.set_last_seen(target_id, latest)
    db.add_bot_source(uid, bot_id, bot_username, name, target_id)
    state.bot_flow.pop(uid, None)
    log.info("Bot watch registered: user %s -> bot %s (%s)", uid, bot_id, bot_username)

    emoji = "🤖" if is_bot else "👤"
    lines = [
        "✅ **Bot watch active!**",
        "",
        f"{emoji} **{name}**" + ("" if is_bot else " _(user account, bot nahi)_"),
        f"🆔 `{bot_id}`",
    ]
    if bot_username:
        lines.append(f"🔗 t.me/{bot_username}")
    lines += [
        "",
        "Ab jab bhi ye bot aapko video/media bhejega:",
        "⬇️ Automatic download — live progress bar",
        "⬆️ TeraBox upload — live progress bar",
        "🔗 TeraBox link yahan milega",
        "",
        "📊 /targets me is bot ka dashboard, links list aur batch milega.",
        "Purani videos ke liye: /targets → is bot → ▶️ Batch Start.",
    ]
    await safe_edit(
        wait,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎯 Targets", callback_data="m:targets")]]
        ),
    )


async def show_list(app: Client, message, uid: int, *, edit: bool = False) -> None:
    sources = db.list_bot_sources(uid)
    if not sources:
        text = (
            "🤖 **Bot Watch**\n\n"
            "Abhi koi bot track nahi ho raha.\n\n/bot se add karo — "
            "bot ki har video automatically TeraBox upload hogi."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Add Bot", callback_data="w:add")]]
        )
    else:
        lines = [f"🤖 **Tracked Bots** ({len(sources)})", ""]
        buttons = []
        for src in sources:
            count = db.count_uploads(src["target_id"]) if src["target_id"] else 0
            handle = f"@{src['bot_username']}" if src["bot_username"] else "—"
            lines.append(
                f"{src['display_name']}\n"
                f"   {handle} · 🆔 `{src['bot_id']}` · ✅ {count} uploaded"
            )
            label = f"🗑 {src['bot_username'] or src['display_name']}"
            buttons.append(
                [InlineKeyboardButton(label, callback_data=f"w:r:{src['id']}")]
            )
        lines += [
            "",
            "Naya bot add karne ke liye /bot bhejo.",
        ]
        buttons.append([InlineKeyboardButton("➕ Add Bot", callback_data="w:add")])
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(buttons)

    if edit:
        await safe_edit(message, text, keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def _remove_by_ref(app: Client, message, uid: int, ref: str) -> None:
    source = db.find_bot_source(uid, ref)
    if source is None:
        await message.reply_text(
            "❌ Ye bot tracked nahi hai. /bot list se check karo."
        )
        return

    db.delete_bot_source(source["id"])
    if source["target_id"]:
        db.delete_target(int(source["target_id"]))
    log.info("Bot watch removed: user %s -> bot %s", uid, source["bot_id"])
    await message.reply_text(
        f"🗑 **{source['display_name']}** ka bot watch band ho gaya.\n"
        "Us bot ki ab nayi videos auto-upload nahi hongi."
    )


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    if not state.bot_flow.get(uid):
        return
    parsed = _parse_bot_ref(message.text or "")
    if parsed is None:
        await message.reply_text(_PARSE_USAGE)
        return
    kind, value = parsed
    await _register(app, message, uid, kind, value)


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    uid = query.from_user.id

    if action == "add":
        state.clear(uid)
        state.bot_flow[uid] = {"step": "ref"}
        await safe_edit(query.message, BOT_ADD_PROMPT)
    elif action == "r" and len(parts) >= 3:
        source_id = int(parts[2])
        source = _own_source(uid, source_id)
        if source is None:
            await safe_edit(query.message, "❌ Ye bot tracked nahi hai.")
            return
        count = db.count_uploads(source["target_id"]) if source["target_id"] else 0
        await safe_edit(
            query.message,
            "⚠️ **Bot watch remove karna hai?**\n\n"
            f"🤖 {source['display_name']}\n"
            f"🆔 `{source['bot_id']}`\n"
            f"🔗 {count} uploaded links bhi delete ho jayenge.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, remove", callback_data=f"w:rc:{source_id}"
                        )
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data="w:list")],
                ]
            ),
        )
    elif action == "rc" and len(parts) >= 3:
        source_id = int(parts[2])
        source = _own_source(uid, source_id)
        if source is not None:
            db.delete_bot_source(source_id)
            if source["target_id"]:
                db.delete_target(int(source["target_id"]))
            log.info("Bot watch removed: user %s -> bot %s", uid, source["bot_id"])
        await show_list(app, query.message, uid, edit=True)
    elif action == "list":
        await show_list(app, query.message, uid, edit=True)


def _own_source(uid: int, source_id: int) -> dict | None:
    for source in db.list_bot_sources(uid):
        if int(source["id"]) == int(source_id):
            return source
    return None


def register(app: Client) -> None:
    @app.on_message(filters.command("bot") & filters.private)
    async def bot_cmd(client, message):
        uid = message.from_user.id
        args = message.command[1:]

        if not args:
            state.clear(uid)
            state.bot_flow[uid] = {"step": "ref"}
            await message.reply_text(BOT_ADD_PROMPT)
            return

        sub = args[0].lower()
        if sub == "list":
            await show_list(client, message, uid)
            return
        if sub == "remove":
            if len(args) < 2:
                await message.reply_text(
                    "🗑 Usage: `/bot remove 7040617509` ya "
                    "`/bot remove @FreeVideoEditorbot`"
                )
                return
            await _remove_by_ref(client, message, uid, " ".join(args[1:]))
            return

        parsed = _parse_bot_ref(" ".join(args))
        if parsed is None:
            await message.reply_text(_PARSE_USAGE)
            return
        kind, value = parsed
        await _register(client, message, uid, kind, value)
