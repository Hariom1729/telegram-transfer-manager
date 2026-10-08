"""Asyncio Job Queue and Concurrency Controller."""

import asyncio
import logging
from typing import Callable, Coroutine, Dict, List, Optional
from app.config import settings

logger = logging.getLogger(__name__)


class TransferQueue:
    """Manages an asyncio worker pool with concurrency limit."""

    def __init__(
        self,
        max_workers: Optional[int] = None,
        job_handler: Optional[Callable[[int], Coroutine]] = None,
    ) -> None:
        self.max_workers = (
            max_workers if max_workers is not None else settings.MAX_CONCURRENT_TRANSFERS
        )
        self.job_handler = job_handler
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._active_tasks: Dict[int, asyncio.Task] = {}
        self._worker_tasks: List[asyncio.Task] = []
        self._running = False
        self._lock = asyncio.Lock()

    def set_handler(self, handler: Callable[[int], Coroutine]) -> None:
        """Set the async coroutine function that processes a job ID."""
        self.job_handler = handler

    async def start(self) -> None:
        """Spawn background worker coroutines."""
        if self._running:
            return
        self._running = True
        logger.info("Starting TransferQueue with %s workers", self.max_workers)
        for i in range(self.max_workers):
            task = asyncio.create_task(self._worker_loop(i + 1))
            self._worker_tasks.append(task)

    async def stop(self) -> None:
        """Stop worker pool and wait for active jobs."""
        self._running = False
        for task in self._worker_tasks:
            task.cancel()
        for job_task in self._active_tasks.values():
            job_task.cancel()
        self._active_tasks.clear()
        self._worker_tasks.clear()

    async def enqueue(self, job_id: int) -> None:
        """Enqueue a job ID to be picked up by the next available worker."""
        logger.info("Enqueued transfer job=%s", job_id)
        await self._queue.put(job_id)

    def is_active(self, job_id: int) -> bool:
        """Check if a job is currently executing."""
        return job_id in self._active_tasks

    def get_active_job_ids(self) -> List[int]:
        """List all currently running job IDs."""
        return list(self._active_tasks.keys())

    async def _worker_loop(self, worker_index: int) -> None:
        """Worker loop continuously popping jobs from the queue."""
        logger.debug("Worker %s started", worker_index)
        while self._running:
            try:
                job_id = await self._queue.get()
                if not self.job_handler:
                    self._queue.task_done()
                    continue

                current_task = asyncio.current_task()
                async with self._lock:
                    self._active_tasks[job_id] = current_task

                logger.info("Worker %s executing job=%s", worker_index, job_id)
                try:
                    await self.job_handler(job_id)
                except asyncio.CancelledError:
                    logger.info("Job=%s cancelled", job_id)
                except Exception as e:
                    logger.error("Job=%s worker error: %s", job_id, e)
                finally:
                    async with self._lock:
                        self._active_tasks.pop(job_id, None)
                    self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Worker %s unexpected exception: %s", worker_index, e)
                await asyncio.sleep(1)


transfer_queue = TransferQueue()

