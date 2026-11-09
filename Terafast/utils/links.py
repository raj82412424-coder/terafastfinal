"""Telegram message link parsing and building."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}
_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']+",
    re.I,
)
_SCHEME_RE = re.compile(r"tg://resolve\?[^\s<>\"']+", re.I)
_TRAILING = ").,;:!?]}\"'"


@dataclass(frozen=True)
class TelegramLink:
    raw: str
    chat: str | int
    message_id: int
    username: str | None = None
    is_private: bool = False


def _clean(raw: str) -> str:
    return raw.strip().rstrip(_TRAILING)


def parse_telegram_link(text: str) -> TelegramLink | None:
    """Parse a public, private or tg:// message link. Returns None otherwise."""
    if not text:
        return None

    scheme = _SCHEME_RE.search(text)
    if scheme:
        raw = _clean(scheme.group(0))
        query = parse_qs(urlparse(raw).query)
        domain = (query.get("domain") or [""])[0]
        post = (query.get("post") or [""])[0]
        if domain and post.isdigit():
            return TelegramLink(raw, domain, int(post), domain, False)
        return None

    match = _LINK_RE.search(text)
    if not match:
        return None

    raw = _clean(match.group(0))
    if not raw.lower().startswith("http"):
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.netloc.lower() not in _HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if parts[0] == "s":
        parts = parts[1:]
    if not parts:
        return None

    if parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return None
        return TelegramLink(raw, int(f"-100{parts[1]}"), int(parts[2]), None, True)

    if parts[0].startswith("+") or parts[0] in {
        "joinchat",
        "addstickers",
        "share",
        "proxy",
        "socks",
    }:
        return None

    if len(parts) < 2 or not parts[1].isdigit():
        return None

    username = parts[0]
    return TelegramLink(raw, username, int(parts[1]), username, False)


def message_link(chat_id: int, message_id: int, username: str | None = None) -> str:
    """Build a canonical t.me link for a stored message."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    value = int(chat_id)
    if value < 0:
        internal = abs(value) - 1_000_000_000_000
        if internal > 0:
            return f"https://t.me/c/{internal}/{message_id}"
    return f"https://t.me/c/{abs(value)}/{message_id}"
