# PRD — TeraBox Uploader Bot (Targets, Batch, Auto, User Session)

## 1. Overview

`terabox-uploader-bot` is a Telegram bot that downloads media from Telegram
chats and uploads it to TeraBox, returning a shareable link. This iteration adds
target management, resumable bulk processing, automatic new-media mirroring,
direct message-link handling and a Telegram user session for private content.

## 2. Users and goals

- Users who want to back up public or private channel/group media to TeraBox.
- Channel admins who need to migrate a large archive in one run.
- Users who want a single message link converted into a TeraBox link.

## 3. Commands

| Command | Purpose |
| --- | --- |
| `/start` | Main menu and quick help |
| `/help` | Full command reference |
| `/set` | Add a target chat + start message |
| `/targets` | Target dashboard with inline navigation |
| `/batch` | Bulk process N messages or all messages |
| `/login` | Telegram user session (phone + OTP + 2FA) |
| `/logout` | Remove the stored user session |
| `/bind` / `/change` | Bind a TeraBox cookie |
| `/status` | TeraBox quota, session and target summary |
| `/queue` | Active and waiting job counts |
| `/stop` | Cancel the caller's running jobs |
| `/cancel` | Abort the current input flow |

## 4. Feature flows

### 4.1 `/set`

1. Bot asks for a chat ID, `@username` or `t.me` link.
2. Bot resolves the chat (bot client first, user session as fallback) and shows
   the title and numeric ID.
3. Bot asks: "Download kahan se shuru karna hai?" and accepts a message link
   such as `https://t.me/SSC_CGL_DISCUSSION_GROUP_2026_27/42966`.
4. Target row stores `chat_id`, `title`, `username`, `start_message_id`,
   `start_link`, `auto_mode=0`, `last_seen_id=start_message_id-1`.
5. Multiple targets per user are supported; re-adding the same chat updates the
   start point instead of duplicating the row.

### 4.2 `/batch`

1. User picks a target from inline buttons.
2. User picks `100`, `500`, `1000`, `All` or a custom count.
3. Engine resolves the latest message ID, computes `end_id`, creates a
   `batch_jobs` row and starts a background task.
4. Messages are fetched in chunks of `BATCH_CHUNK_SIZE` (default 100) by ID.
5. Every media message goes through the shared pipeline (download → upload →
   save link). Non-media and deleted messages are skipped.
6. The cursor is persisted after every message, so a restart resumes exactly
   where it stopped. `AUTO_RESUME_BATCH=true` restarts paused jobs on boot.
7. Duplicate uploads are impossible: `uploads` has a unique index on
   `(user_id, chat_id, message_id)`.
8. FloodWait is honoured with a sleep and retry; transient errors retry with
   exponential backoff.

### 4.3 Auto mode

- Toggle per target from the target page.
- A poller runs every `AUTO_POLL_SECONDS` (default 20s), reads messages newer
  than `last_seen_id`, uploads each media file and sends the TeraBox link to the
  owner's bot chat.
- `last_seen_id` advances per chunk, so a restart does not re-upload.
- A per-target lock prevents the poller and a batch job from racing.

### 4.4 Direct link paste

- Any `t.me/<user>/<id>`, `t.me/c/<internal>/<id>`, `t.me/s/...` or
  `tg://resolve?domain=...&post=...` link is parsed without a command.
- The bot downloads that single message, uploads it and replies with the link.
- Private links (`t.me/c/...`) map to `-100<internal>` and use the user session
  when the bot is not a member.

### 4.5 `/login` and `/logout`

- `/login` asks for a phone number, sends the Telegram code, then asks for the
  OTP and, when enabled, the 2FA password.
- The exported Pyrogram session string is Fernet-encrypted in SQLite.
- `/logout` deletes the row and disconnects the cached client.
- Sensitive messages (phone, OTP, password) are deleted after use.

### 4.6 Progress

- Every download and upload renders `[██████░░░░] 60% | 12.3 MB/s | ETA: 42s`.
- Edits are throttled to `PROGRESS_EDIT_INTERVAL` (default 4s) to avoid
  Telegram flood limits. `MessageNotModified` and `FloodWait` are swallowed.

### 4.7 `/targets`

- List page: one button per target, label `📢 <title> | <uploaded>/<total> ✅`.
- Target page: title, chat ID, start link, auto state, total/uploaded/failed/
  pending counters, progress bar and buttons:
  `[🔗 Links List] [▶️ Batch Start] [🔄 Auto ON/OFF] [🗑 Remove] [⬅️ Back]`.
- Links page: 10 entries per page, each with file name, TeraBox URL and
  `↳ source:` message link. Buttons `[⬅️ Prev] [Next ➡️]`, `[📄 Export TXT]`,
  `[⬅️ Back]`.
- Export sends a `.txt` document with every link of that target.
- All navigation edits the same message.

## 5. Data model (SQLite)

- `terabox_tokens(user_id PK, token BLOB, updated_at)`
- `targets(id PK, user_id, chat_id, title, username, chat_type,
  start_message_id, start_link, auto_mode, last_seen_id, total_messages,
  uploaded_count, failed_count, skipped_count, created_at)`
  unique `(user_id, chat_id)`
- `uploads(id PK, user_id, target_id, chat_id, message_id, file_name,
  file_size, terabox_url, source_link, created_at)`
  unique `(user_id, chat_id, message_id)`
- `batch_jobs(id PK, user_id, target_id, start_id, end_id, cursor_id, total,
  processed, uploaded, failed, skipped, status, error, created_at, updated_at)`
- `user_sessions(user_id PK, session BLOB, phone, updated_at)`

## 6. Module structure

```
bot.py                  entry point, client, startup tasks
config.py               env configuration
db.py                   SQLite access layer
terabox.py              TeraBox web client (unchanged)
tgdl.py                 parallel Telegram downloader (unchanged)
handlers/               common, targets, batch, login, direct, router
services/               pipeline, batch_engine, auto_engine, userbot, progress
utils/                  formatting, links, queue, async helpers
tests/                  unit tests
```

## 7. Edge cases

| Case | Handling |
| --- | --- |
| FloodWait | Sleep `value + 2` seconds, retry |
| Bot not in chat | Fall back to user session, else actionable error |
| Session expired | Prompt `/login` again |
| Invalid link | Clear error, no crash |
| TeraBox cookie invalid | `errno=-6` message with `/bind` hint |
| Disk full | Pre-flight free-space check with buffer |
| Deleted message | Skipped, cursor advances |
| Duplicate message | Skipped, existing link returned |
| Batch restart | Cursor resume, no duplicate uploads |

## 8. Acceptance criteria

1. `/set` saves a target and start message; multiple targets coexist.
2. `/batch` processes N or all messages and resumes after a restart.
3. Auto mode uploads new media and posts the link to the bot chat.
4. Pasting a public or private message link returns a TeraBox link.
5. `/login` completes phone + OTP + 2FA and enables private downloads.
6. Progress bars update every 3–5 seconds with speed and ETA.
7. `/targets` navigates list → target → links → export in one message.
