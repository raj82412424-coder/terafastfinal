# TeraBox Uploader Telegram Bot

Telegram channel/group ka media download karke TeraBox par upload karta hai aur
shareable link deta hai. Is version me targets dashboard, resumable batch,
auto mode, direct message-link handling aur private content ke liye Telegram
user session add kiya gaya hai.

## Features

- **`/set`** — target group/channel + start message save karo. Multiple targets.
- **`/bot`** — dusra bot track karo (ID / @username / t.me link). Jab wo bot
  video bhejega, automatically download + TeraBox upload hoti hai, live
  progress bar ke saath link bot chat me aata hai. `/bot list`,
  `/bot remove <id/@username>` se manage karo.
- **`/batch`** — start point se `100` / `500` / `1000` / `all` / custom messages
  process karo. FloodWait retry, cursor resume aur duplicate prevention.
- **Auto mode** — per-target ON/OFF. Nayi media automatically upload hoti hai
  aur TeraBox link bot chat me aata hai.
- **Direct link paste** — koi command nahi. `t.me/...` link paste karo, link
  wapas milega (public aur private dono).
- **`/login` / `/logout`** — phone + OTP + 2FA se Telegram user session.
  Private groups/channels isi session se download hote hain.
- **Live progress** — `[██████░░░░] 60% | 12.3 MB/s | ETA: 42s`, 3–5 sec me ek edit.
- **`/targets`** — inline dashboard: status page, auto toggle, links list
  (10 per page), TXT export. Sab navigation ek hi message edit karta hai.
- Parallel Telegram download (1 MiB ranges, resume) + parallel TeraBox upload
  (4 MiB parts, host failover).
- Encrypted SQLite storage for TeraBox cookies and Telegram session strings.

## Commands

| Command | Description |
| --- | --- |
| `/start` | Main menu |
| `/help` | Command reference |
| `/set` | Add target chat + start message |
| `/targets` | Target dashboard |
| `/batch` | Bulk download + upload |
| `/bot` | Bot watch — dusre bot ki videos auto-upload |
| `/parallel` / `/pr` | Runtime max parallel jobs ko 1–4 set karo |
| `/login` | Telegram user session (phone/OTP/2FA) |
| `/logout` | Remove user session |
| `/bind` / `/change` | Bind TeraBox cookie |
| `/status` | TeraBox quota, session, targets |
| `/delete` | Delete TeraBox config |
| `/queue` | Queue status |
| `/stop` | Cancel your running jobs |
| `/cancel` | Abort current input flow |

## Setup

```bash
cp .env.example .env
# .env me API_ID, API_HASH, BOT_TOKEN bharo
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

`DATA_DIR` default `./data` rakho, taaki encryption key, SQLite DB aur downloads
persistent ek hi directory me rahen. First start par `DATA_DIR/.encryption_key`
restrictive permissions ke saath generate hoti hai. Existing encrypted tokens
ke liye wahi purani key preserve karna zaroori hai.

### TeraBox cookie

1. Browser me `dm.1024terabox.com` par login karo.
2. F12 → Network → koi request → **Cookie** header copy karo.
3. Bot me `/bind` karke poori cookie paste karo.

Cookie Fernet-encrypted SQLite me save hoti hai aur message turant delete hota hai.

### Private groups/channels

1. `/login` → phone number (`+919876543210`).
2. Telegram OTP code.
3. 2FA password (agar enabled ho).

Session string encrypted save hoti hai. `/logout` se delete hoti hai. Bot ko
public channel me add karna optional hai — user session fallback khud use hota hai.

## Docker

```bash
cp .env.example .env
# .env me secrets bharo
docker compose up -d --build
docker compose logs -f terabox-bot
```

Compose persistent named volume `/app/data` mount karta hai. Koi web port open nahi hota.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `API_ID`, `API_HASH`, `BOT_TOKEN` | — | Telegram credentials (required) |
| `DATA_DIR` | `./data` | Persistent state directory |
| `ENCRYPTION_KEY` | auto | Fernet key for cookies/sessions |
| `MAX_PARALLEL` | `1` | FIFO queue workers |
| `TELEGRAM_DOWNLOAD_WORKERS` | `3` | Per-job download workers |
| `TELEGRAM_TOTAL_DOWNLOAD_WORKERS` | `6` | Global download stream cap |
| `TERABOX_UPLOAD_THREADS` | `6` | Per-job upload threads |
| `TERABOX_TOTAL_UPLOAD_THREADS` | `8` | Global upload part cap |
| `TERABOX_MAX_RETRIES` | `8` | Part upload retries |
| `MAX_FILE_MB` | `4096` | Telegram file size guard |
| `MIN_FREE_MB` | `512` | Minimum disk space kept free during downloads |
| `MAX_URL_GB` | `10` | yt-dlp download limit |
| `REMOTE_FOLDER` | `TG_Uploads` | TeraBox base folder |
| `REMOTE_FOLDER_PER_TARGET` | `true` | Per-target subfolder |
| `BATCH_CHUNK_SIZE` | `100` | Messages fetched per request |
| `BATCH_MAX_MESSAGES` | `0` | Hard cap per batch (0 = none) |
| `AUTO_RESUME_BATCH` | `true` | Resume running jobs on boot |
| `AUTO_POLL_SECONDS` | `20` | Auto mode poll interval |
| `PROGRESS_EDIT_INTERVAL` | `4` | Progress edit throttle |
| `LINKS_PAGE_SIZE` | `10` | Links per page |

## Data model

- `targets` — chat, start message, auto mode, last seen, counters.
- `uploads` — unique `(user_id, chat_id, message_id)`; duplicate upload impossible.
- `batch_jobs` — start/end/cursor and counters for resume.
- `terabox_tokens`, `user_sessions` — Fernet-encrypted blobs.

## Project layout

```
bot.py                  entry point
config.py               env configuration
db.py                   SQLite layer
terabox.py              TeraBox web client
tgdl.py                 parallel Telegram downloader
handlers/               common, targets, batch, login, direct, router
services/               pipeline, batch_engine, auto_engine, userbot, progress
utils/                  formatting, links, queue, telegram, async helpers
tests/                  unit tests
```

## Checks

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Notes

- TeraBox ka official public upload API nahi hai; `terabox.py` website ke current
  web endpoints use karta hai. Website change hone par TeraBox-side update ki
  zaroorat pad sakti hai.
- Telegram bot/media route ki real size limit provider par depend karti hai.
  `MAX_FILE_MB` sirf local guard hai.
- Batch "N messages" ka matlab start message ID se N IDs ka range hai; deleted
  messages skip ho jaate hain.
