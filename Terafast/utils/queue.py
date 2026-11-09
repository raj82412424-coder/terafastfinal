"""FIFO job queue with per-user cancellation and generation tracking."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger("queue")


@dataclass(slots=True)
class QueueJob:
    user_id: int
    generation: int
    run: Callable[[], Awaitable[None]]
    status: object | None = None


class JobQueue:
    """Runs submitted coroutines in FIFO order with a bounded worker count."""

    def __init__(self, workers: int = 1) -> None:
        self.workers = max(1, int(workers))
        self._queue: asyncio.Queue[QueueJob] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._next_worker_id = 0
        self._active: defaultdict[int, set[asyncio.Task]] = defaultdict(set)
        self._generations: defaultdict[int, int] = defaultdict(int)
        self._pending: defaultdict[int, int] = defaultdict(int)
        self.active_jobs = 0

    def set_workers(self, workers: int) -> int:
        """Change the worker target without interrupting active jobs.

        Workers above the new target finish their current job and exit when
        idle; increasing the target starts additional workers immediately.
        """
        self.workers = max(1, int(workers))
        self.ensure_started()
        return self.workers

    def ensure_started(self) -> None:
        self._tasks = [task for task in self._tasks if not task.done()]
        while len(self._tasks) < self.workers:
            self._next_worker_id += 1
            self._tasks.append(
                asyncio.create_task(
                    self._worker(self._next_worker_id),
                    name=f"job-worker-{self._next_worker_id}",
                )
            )

    async def submit(
        self,
        user_id: int,
        run: Callable[[], Awaitable[None]],
        status: object | None = None,
    ) -> int:
        self.ensure_started()
        self._pending[user_id] += 1
        position = self._pending[user_id]
        await self._queue.put(
            QueueJob(user_id, self._generations[user_id], run, status)
        )
        return position

    def cancel_user(self, user_id: int) -> tuple[int, int]:
        self._generations[user_id] += 1
        tasks = list(self._active.get(user_id, set()))
        for task in tasks:
            task.cancel()
        pending = self._pending.get(user_id, 0)
        return len(tasks), pending

    def stats(self, user_id: int) -> dict[str, int]:
        return {
            "active": self.active_jobs,
            "waiting": self._queue.qsize(),
            "user_waiting": self._pending.get(user_id, 0),
            "workers": self.workers,
        }

    async def _worker(self, worker_id: int) -> None:
        while True:
            if worker_id > self.workers:
                return
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            self._pending[job.user_id] = max(0, self._pending[job.user_id] - 1)
            if self._pending[job.user_id] == 0:
                self._pending.pop(job.user_id, None)

            if job.generation != self._generations[job.user_id]:
                await self._safe_status(job.status, "🛑 Job stop kar diya gaya (queue se skip).")
                self._queue.task_done()
                continue

            self.active_jobs += 1
            task = asyncio.create_task(job.run(), name=f"job-{job.user_id}")
            self._active[job.user_id].add(task)
            try:
                await task
            except asyncio.CancelledError:
                if task.cancelled():
                    await self._safe_status(job.status, "🛑 Job stop kar diya gaya.")
                else:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            except Exception:
                log.exception("Worker %s failed while processing a job", worker_id)
            finally:
                self._active[job.user_id].discard(task)
                if not self._active[job.user_id]:
                    self._active.pop(job.user_id, None)
                self.active_jobs -= 1
                self._queue.task_done()

    @staticmethod
    async def _safe_status(status: object | None, text: str) -> None:
        if status is None:
            return
        try:
            await status.edit_text(text)  # type: ignore[attr-defined]
        except Exception:
            pass
