"""Resumable batch processor: walks message IDs from a start point and uploads media."""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client
from pyrogram.errors import FloodWait

import config
import db
from services.pipeline import (
    DiskSpaceError,
    PipelineError,
    pick_media,
    process_message,
    resolve_client,
)
from services.progress import ProgressReporter
from utils.formatting import bar, short

log = logging.getLogger("batch")

_running: dict[int, asyncio.Task] = {}
_stop_flags: set[int] = set()


def is_running(job_id: int) -> bool:
    task = _running.get(job_id)
    return task is not None and not task.done()


async def latest_message_id(client: Client, chat_id: int) -> int:
    async for message in client.get_chat_history(chat_id, limit=1):
        return int(message.id)
    return 0


async def fetch_messages(
    client: Client, chat_id: int, ids: list[int], attempts: int = 5
) -> list:
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await client.get_messages(chat_id, ids)
        except FloodWait as exc:
            last = exc
            await asyncio.sleep(int(exc.value) + 2)
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(min(2 ** (attempt - 1), 20))
    raise RuntimeError(f"Messages fetch nahi hue: {last}")


def _overall(
    target: dict,
    total: int,
    processed: int,
    uploaded: int,
    failed: int,
    skipped: int,
) -> str:
    total = max(1, int(total))
    done = min(total, int(processed))
    return (
        f"📦 **Batch** · {short(str(target['title']), 32)}\n"
        f"{bar(done, total)} · {done}/{total}\n"
        f"✅ {uploaded} | ❌ {failed} | ⏭ {skipped}"
    )


async def create_job(
    app: Client, user_id: int, target_id: int, count: int | None = None
) -> int:
    """Create a batch job and start it in the background."""
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(user_id):
        raise ValueError("Target nahi mila.")

    existing = db.get_running_batch_for_target(user_id, target_id)
    if existing is not None:
        raise ValueError(
            f"Is target ka batch already running hai (job {existing['id']}). "
            "Pehle current batch complete/pause hone do."
        )

    client = await resolve_client(app, user_id, target["chat_id"])
    latest = await latest_message_id(client, target["chat_id"])
    start_id = int(target["start_message_id"])
    if latest < start_id:
        raise ValueError("Start message ke baad koi message nahi mila.")

    if count is None:
        end_id = latest
    else:
        count = max(1, int(count))
        if config.BATCH_MAX_MESSAGES:
            count = min(count, config.BATCH_MAX_MESSAGES)
        end_id = min(start_id + count - 1, latest)

    total = end_id - start_id + 1
    job_id = db.create_batch_job(user_id, target_id, start_id, end_id, total)
    db.set_target_total(target_id, max(0, latest - start_id + 1))
    _spawn(app, job_id)
    log.info("Batch job %s created for target %s (%s messages)", job_id, target_id, total)
    return job_id


def _spawn(app: Client, job_id: int) -> None:
    task = asyncio.create_task(_run(app, job_id), name=f"batch-{job_id}")
    _running[job_id] = task
    task.add_done_callback(lambda _task: _running.pop(job_id, None))


async def _run(app: Client, job_id: int) -> None:
    job = db.get_batch_job(job_id)
    if job is None:
        return
    target = db.get_target(job["target_id"])
    if target is None:
        db.update_batch_job(job_id, status="error", error="Target removed")
        return

    user_id = int(job["user_id"])
    chat_id = int(target["chat_id"])
    end_id = int(job["end_id"])
    total = int(job["total"])
    cursor = int(job["cursor_id"])
    processed = int(job["processed"])
    uploaded = int(job["uploaded"])
    failed = int(job["failed"])
    skipped = int(job["skipped"])

    status = None
    try:
        status = await app.send_message(user_id, "⏳ Batch prepare ho raha hai...")
    except Exception:
        log.warning("Could not send batch status message to %s", user_id)
    reporter = ProgressReporter(
        status,
        header=_overall(target, total, processed, uploaded, failed, skipped),
        loop=asyncio.get_running_loop(),
    )

    try:
        client = await resolve_client(app, user_id, chat_id)
        while cursor < end_id:
            if job_id in _stop_flags:
                break

            chunk_end = min(cursor + config.BATCH_CHUNK_SIZE, end_id)
            messages = await fetch_messages(
                client, chat_id, list(range(cursor + 1, chunk_end + 1))
            )

            for message in messages:
                if job_id in _stop_flags:
                    break
                previous_cursor = cursor
                cursor = int(message.id)

                if getattr(message, "empty", False) or pick_media(message) is None:
                    processed += 1
                    skipped += 1
                    db.bump_target(target["id"], skipped=1)
                    db.update_batch_job(
                        job_id,
                        cursor_id=cursor,
                        processed=processed,
                        skipped=skipped,
                    )
                    continue

                reporter.set_header(
                    _overall(target, total, processed, uploaded, failed, skipped)
                )
                try:
                    result = await process_message(
                        app,
                        user_id,
                        chat_id,
                        message.id,
                        target=target,
                        reporter=reporter,
                    )
                except DiskSpaceError as exc:
                    cursor = previous_cursor
                    db.update_batch_job(
                        job_id,
                        status="paused",
                        cursor_id=cursor,
                        error=str(exc),
                    )
                    await reporter.finish(
                        f"⏸ **Batch paused: disk space**\n\n"
                        f"{_overall(target, total, processed, uploaded, failed, skipped)}\n\n"
                        f"{exc}\n\nSpace free karke `/batch` se Resume karo."
                    )
                    return
                except PipelineError as exc:
                    failed += 1
                    processed += 1
                    db.bump_target(target["id"], failed=1)
                    db.update_batch_job(
                        job_id,
                        cursor_id=cursor,
                        processed=processed,
                        failed=failed,
                    )
                    log.warning("Batch %s failed at %s: %s", job_id, message.id, exc)
                    continue
                except Exception:  # noqa: BLE001 - one bad message must not stop the batch
                    failed += 1
                    processed += 1
                    db.bump_target(target["id"], failed=1)
                    db.update_batch_job(
                        job_id,
                        cursor_id=cursor,
                        processed=processed,
                        failed=failed,
                    )
                    log.exception("Batch %s unexpected error at %s", job_id, message.id)
                    continue

                processed += 1
                if result.status == "uploaded":
                    uploaded += 1
                else:
                    skipped += 1
                db.update_batch_job(
                    job_id,
                    cursor_id=cursor,
                    processed=processed,
                    uploaded=uploaded,
                    skipped=skipped,
                )

            if job_id not in _stop_flags and cursor < chunk_end:
                # Deleted/empty messages may not be returned; never stall.
                cursor = chunk_end
            db.update_batch_job(job_id, cursor_id=cursor)
            await reporter.set_stage(
                _overall(target, total, processed, uploaded, failed, skipped),
                force=True,
            )
        summary = _overall(target, total, processed, uploaded, failed, skipped)
        if job_id in _stop_flags:
            db.update_batch_job(job_id, status="paused", cursor_id=cursor)
            await reporter.finish(
                f"⏸ **Batch paused**\n\n{summary}\n\n"
                "Resume ke liye /batch → target → ▶️ Resume."
            )
        else:
            db.update_batch_job(job_id, status="done", cursor_id=cursor)
            await reporter.finish(
                f"✅ **Batch complete**\n\n{summary}\n\n📁 {target['title']}"
            )

    except asyncio.CancelledError:
        db.update_batch_job(job_id, status="paused", cursor_id=cursor)
        try:
            await asyncio.shield(
                reporter.finish(
                    f"⏸ **Batch paused**\n\n"
                    f"{_overall(target, total, processed, uploaded, failed, skipped)}"
                )
            )
        except Exception:
            pass
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the owner
        db.update_batch_job(job_id, status="error", error=str(exc), cursor_id=cursor)
        log.exception("Batch %s crashed", job_id)
        try:
            await reporter.finish(f"❌ **Batch error**\n\n{exc}")
        except Exception:
            pass
    finally:
        _stop_flags.discard(job_id)


def request_stop(job_id: int) -> bool:
    task = _running.get(job_id)
    if task is None or task.done():
        return False
    _stop_flags.add(job_id)
    task.cancel()
    return True


def stop_user(user_id: int) -> int:
    stopped = 0
    for job_id in list(_running):
        job = db.get_batch_job(job_id)
        if job is not None and int(job["user_id"]) == int(user_id):
            if request_stop(job_id):
                stopped += 1
    return stopped


async def resume_job(app: Client, job_id: int) -> bool:
    job = db.get_batch_job(job_id)
    if job is None or job["status"] == "done" or is_running(job_id):
        return False
    if int(job["cursor_id"]) >= int(job["end_id"]):
        db.update_batch_job(job_id, status="done")
        return False
    db.update_batch_job(job_id, status="running", error=None)
    _spawn(app, job_id)
    return True


async def resume_pending(app: Client) -> None:
    """Resume jobs that were running when the process stopped."""
    running = [job for job in db.list_resumable_jobs() if job["status"] == "running"]
    db.mark_stale_jobs_paused()
    if not config.AUTO_RESUME_BATCH:
        return
    for job in running:
        if int(job["cursor_id"]) >= int(job["end_id"]):
            db.update_batch_job(job["id"], status="done")
            continue
        db.update_batch_job(job["id"], status="running", error=None)
        _spawn(app, job["id"])
        log.info("Resuming batch job %s from cursor %s", job["id"], job["cursor_id"])
