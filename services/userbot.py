"""Telegram user-session login (phone + OTP + 2FA) and client cache."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

import config
import db

log = logging.getLogger("userbot")


@dataclass
class LoginFlow:
    user_id: int
    phone: str
    client: Client
    phone_code_hash: str
    created_at: float = field(default_factory=time.time)


_flows: dict[int, LoginFlow] = {}
_clients: dict[int, Client] = {}
_lock = asyncio.Lock()


def normalize_phone(raw: str) -> str:
    value = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if not value.startswith("+"):
        value = "+" + value.lstrip("+")
    return value


def flow_step(user_id: int) -> str | None:
    flow = _flows.get(user_id)
    return "code" if flow else None


def has_session(user_id: int) -> bool:
    return db.get_session(user_id) is not None


async def begin_login(user_id: int, phone: str) -> None:
    """Connect a temporary client and request the login code."""
    phone = normalize_phone(phone)
    if not re.fullmatch(r"\+\d{7,15}", phone):
        raise ValueError("Phone number galat lag raha hai. Country code ke saath bhejo, jaise +919876543210.")

    await cancel_login(user_id)
    client = Client(
        f"login_{user_id}_{int(time.time())}",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        in_memory=True,
        no_updates=True,
        sleep_threshold=30,
    )
    await client.connect()
    try:
        sent = await client.send_code(phone)
    except Exception:
        await _safe_disconnect(client)
        raise

    _flows[user_id] = LoginFlow(
        user_id=user_id,
        phone=phone,
        client=client,
        phone_code_hash=sent.phone_code_hash,
    )
    log.info("Login code requested for user %s", user_id)


async def submit_code(user_id: int, code: str) -> str:
    """Return ``"ok"`` when signed in, ``"password"`` when 2FA is required."""
    flow = _flows.get(user_id)
    if flow is None:
        raise ValueError("Login session expire ho gaya. /login dobara karo.")

    code = re.sub(r"\s+", "", code or "")
    try:
        await flow.client.sign_in(flow.phone, flow.phone_code_hash, code)
    except SessionPasswordNeeded:
        return "password"
    except PhoneCodeInvalid as exc:
        raise ValueError("OTP galat hai. Dobara bhejo.") from exc
    except PhoneCodeExpired as exc:
        raise ValueError("OTP expire ho gaya. /login dobara karo.") from exc
    except FloodWait as exc:
        raise ValueError(f"Telegram ne {exc.value}s wait karne ko kaha. Thodi der baad try karo.") from exc

    await _finish_login(flow)
    return "ok"


async def submit_password(user_id: int, password: str) -> None:
    flow = _flows.get(user_id)
    if flow is None:
        raise ValueError("Login session expire ho gaya. /login dobara karo.")
    try:
        await flow.client.check_password(password)
    except PasswordHashInvalid as exc:
        raise ValueError("2FA password galat hai.") from exc
    except FloodWait as exc:
        raise ValueError(f"Telegram ne {exc.value}s wait karne ko kaha.") from exc
    await _finish_login(flow)


async def _finish_login(flow: LoginFlow) -> None:
    session = await flow.client.export_session_string()
    db.save_session(flow.user_id, session, flow.phone)
    await _safe_disconnect(flow.client)
    _flows.pop(flow.user_id, None)
    await close_user_client(flow.user_id)
    log.info("User session stored for %s", flow.user_id)


async def cancel_login(user_id: int) -> None:
    flow = _flows.pop(user_id, None)
    if flow is not None:
        await _safe_disconnect(flow.client)


async def get_user_client(user_id: int) -> Client | None:
    """Return a connected user client for this owner, or None."""
    async with _lock:
        cached = _clients.get(user_id)
        if cached is not None:
            return cached

        session = db.get_session(user_id)
        if not session:
            return None

        client = Client(
            f"user_{user_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
            no_updates=True,
            sleep_threshold=30,
            max_concurrent_transmissions=config.TELEGRAM_TOTAL_DOWNLOAD_WORKERS,
        )
        try:
            await client.connect()
        except Exception as exc:
            log.warning("Stored user session could not connect: %r", exc)
            await _safe_disconnect(client)
            return None

        _clients[user_id] = client
        return client


async def close_user_client(user_id: int) -> None:
    async with _lock:
        client = _clients.pop(user_id, None)
    if client is not None:
        await _safe_disconnect(client)


async def close_all() -> None:
    for user_id in list(_clients):
        await close_user_client(user_id)
    for user_id in list(_flows):
        await cancel_login(user_id)


async def _safe_disconnect(client: Client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


__all__ = [
    "PhoneNumberInvalid",
    "begin_login",
    "cancel_login",
    "close_all",
    "close_user_client",
    "flow_step",
    "get_user_client",
    "has_session",
    "normalize_phone",
    "submit_code",
    "submit_password",
]
