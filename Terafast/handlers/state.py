"""In-memory conversation state shared by the text router and handlers."""
from __future__ import annotations

# /set flow: {"step": "chat"} or {"step": "start_link", "chat_id": ..., ...}
set_flow: dict[int, dict] = {}

# /login flow: {"step": "phone" | "code" | "password"}
login_flow: dict[int, dict] = {}

# /batch custom count flow: {"step": "count", "target_id": ...}
batch_flow: dict[int, dict] = {}

# /bot flow: {"step": "ref"} — waiting for a bot ID/@username/link.
bot_flow: dict[int, dict] = {}

# Users waiting to paste a TeraBox cookie.
waiting_for_token: set[int] = set()

# Users waiting to enter a runtime max-parallel value.
parallel_flow: set[int] = set()


def clear(user_id: int) -> None:
    set_flow.pop(user_id, None)
    login_flow.pop(user_id, None)
    batch_flow.pop(user_id, None)
    bot_flow.pop(user_id, None)
    waiting_for_token.discard(user_id)
    parallel_flow.discard(user_id)
