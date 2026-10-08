"""Transfer Manager orchestrator."""

import logging
from typing import List, Optional
from sqlalchemy import desc, select, update
from app.database import get_session
from app.models.transfer_error import TransferError
from app.models.transfer_job import JobStatus, TransferJob
from app.transfer.queue import transfer_queue
from app.transfer.worker import transfer_worker

logger = logging.getLogger(__name__)


class TransferManager:
    """Coordinates lifecycle, queueing, pause/resume, and crash recovery of transfer jobs."""

    def __init__(self) -> None:
        # Wire queue handler to worker
        transfer_queue.set_handler(self._execute_queued_job)

    async def _execute_queued_job(self, job_id: int) -> None:
        """Handler invoked by TransferQueue when a worker is free."""
        await transfer_worker.execute_job(job_id)

    async def start(self) -> None:
        """Start the transfer manager queue and recover interrupted jobs."""
        await transfer_queue.start()
        await self.recover_interrupted_jobs()

    async def stop(self) -> None:
        """Gracefully stop transfer manager queue."""
        await transfer_queue.stop()

    async def create_job(
        self,
        owner_id: int,
        telegram_account_id: int,
        source_chat_id: int,
        source_chat_title: str,
        destination_chat_id: int,
        destination_chat_title: str,
        destination_thread_id: Optional[int] = None,
        topic_name: Optional[str] = None,
        source_thread_id: Optional[int] = None,
        source_topic_name: Optional[str] = None,
        content_types: str = "all",
        duplicate_mode: str = "skip",
        start_message_id: Optional[int] = None,
        end_message_id: Optional[int] = None,
        total_messages: int = 0,
        specific_message_ids: Optional[str] = None,
    ) -> TransferJob:
        if (total_messages == 0 or total_messages is None) and start_message_id and end_message_id:
            total_messages = max(0, end_message_id - start_message_id + 1)

        async with get_session() as session:
            job = TransferJob(
                owner_id=owner_id,
                telegram_account_id=telegram_account_id,
                source_chat_id=source_chat_id,
                source_chat_title=source_chat_title,
                source_thread_id=source_thread_id,
                source_topic_name=source_topic_name,
                destination_chat_id=destination_chat_id,
                destination_chat_title=destination_chat_title,
                destination_thread_id=destination_thread_id,
                topic_name=topic_name,
                content_types=content_types,
                duplicate_mode=duplicate_mode,
                start_message_id=start_message_id,
                end_message_id=end_message_id,
                total_messages=total_messages,
                specific_message_ids=specific_message_ids,
                status=JobStatus.QUEUED.value,
            )
            session.add(job)
            await session.flush()
            job_id = job.id

        logger.info(
            "Created transfer job=%s source=%s dest=%s src_thread=%s dst_thread=%s",
            job_id,
            source_chat_id,
            destination_chat_id,
            source_thread_id,
            destination_thread_id,
        )
        return job


    async def start_job(self, job_id: int) -> bool:
        """Enqueue a newly created or ready job for execution."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if not job or job.status not in (
                JobStatus.QUEUED.value,
                JobStatus.PAUSED.value,
            ):
                return False
            job.status = JobStatus.QUEUED.value

        await transfer_queue.enqueue(job_id)
        return True

    async def pause_job(self, job_id: int) -> bool:
        """Request a running job to pause safely."""
        transfer_worker.request_pause(job_id)
        return True

    async def resume_job(self, job_id: int) -> bool:
        """Resume a paused job from its last processed message ID."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if not job or job.status != JobStatus.PAUSED.value:
                return False
            job.status = JobStatus.QUEUED.value

        await transfer_queue.enqueue(job_id)
        return True

    async def cancel_job(self, job_id: int) -> bool:
        """Cancel a running or queued job."""
        transfer_worker.request_cancel(job_id)
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job and job.status in (JobStatus.QUEUED.value, JobStatus.PAUSED.value):
                job.status = JobStatus.CANCELLED.value
        return True

    async def retry_failed_job(self, job_id: int) -> bool:
        """Retry a failed or partially completed job."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if not job:
                return False
            job.status = JobStatus.QUEUED.value
            job.failed_messages = 0

        await transfer_queue.enqueue(job_id)
        return True

    async def retry_failed_messages(self, job_id: int) -> Optional[TransferJob]:
        """Create and return a new job targeting ONLY the messages that failed in an earlier job."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            orig_job = res.scalar_one_or_none()
            if not orig_job:
                return None

            failed_ids: List[int] = []
            if orig_job.failed_message_ids:
                try:
                    failed_ids = [
                        int(x.strip())
                        for x in orig_job.failed_message_ids.split(",")
                        if x.strip().isdigit()
                    ]
                except Exception:
                    failed_ids = []

            if not failed_ids:
                # Query TransferError table as fallback
                err_stmt = select(TransferError.source_message_id).where(
                    TransferError.transfer_job_id == job_id
                )
                err_res = await session.execute(err_stmt)
                failed_ids = sorted(list(set(err_res.scalars().all())))

            if not failed_ids:
                return None

            failed_ids.sort()
            failed_ids_str = ",".join(map(str, failed_ids))

            new_job = TransferJob(
                owner_id=orig_job.owner_id,
                telegram_account_id=orig_job.telegram_account_id,
                source_chat_id=orig_job.source_chat_id,
                source_chat_title=orig_job.source_chat_title,
                source_thread_id=orig_job.source_thread_id,
                source_topic_name=orig_job.source_topic_name,
                destination_chat_id=orig_job.destination_chat_id,
                destination_chat_title=orig_job.destination_chat_title,
                destination_thread_id=orig_job.destination_thread_id,
                topic_name=orig_job.topic_name,
                content_types=orig_job.content_types,
                duplicate_mode="overwrite",  # Must allow transfer since we are explicitly retrying failed
                specific_message_ids=failed_ids_str,
                total_messages=len(failed_ids),
                status=JobStatus.QUEUED.value,
            )
            session.add(new_job)
            await session.flush()
            new_job_id = new_job.id

        logger.info(
            "Created retry failed job=%s for orig_job=%s with %s message(s): %s",
            new_job_id,
            job_id,
            len(failed_ids),
            failed_ids_str,
        )
        return new_job

    async def recover_interrupted_jobs(self) -> int:
        """Section 32: Recover any jobs that were running when the process last crashed or restarted."""
        recovered_count = 0
        async with get_session() as session:
            stmt = select(TransferJob).where(
                TransferJob.status == JobStatus.RUNNING.value
            )
            result = await session.execute(stmt)
            interrupted_jobs = list(result.scalars().all())

            for job in interrupted_jobs:
                logger.info(
                    "Recovering interrupted job=%s from message=%s",
                    job.id,
                    job.last_processed_id,
                )
                job.status = JobStatus.QUEUED.value
                recovered_count += 1

        for job in interrupted_jobs:
            await transfer_queue.enqueue(job.id)

        if recovered_count > 0:
            logger.info("Successfully recovered and enqueued %s jobs.", recovered_count)
        return recovered_count

    async def get_job(self, job_id: int) -> Optional[TransferJob]:
        """Fetch job by ID."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            return res.scalar_one_or_none()

    async def get_active_jobs(self) -> List[TransferJob]:
        """Fetch all running or queued jobs."""
        async with get_session() as session:
            stmt = (
                select(TransferJob)
                .where(
                    TransferJob.status.in_(
                        [
                            JobStatus.RUNNING.value,
                            JobStatus.QUEUED.value,
                            JobStatus.PAUSED.value,
                        ]
                    )
                )
                .order_by(desc(TransferJob.id))
            )
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def get_history(
        self, owner_id: int, limit: int = 10, offset: int = 0
    ) -> List[TransferJob]:
        """Fetch past transfer jobs for an owner."""
        async with get_session() as session:
            stmt = (
                select(TransferJob)
                .where(TransferJob.owner_id == owner_id)
                .order_by(desc(TransferJob.id))
                .limit(limit)
                .offset(offset)
            )
            res = await session.execute(stmt)
            return list(res.scalars().all())


transfer_manager = TransferManager()

