"""Transfer Worker execution engine."""

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Callable, Coroutine, Dict, List, Optional
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient
from telethon.tl.types import Channel
from app.database import get_session
from app.models.transfer_error import TransferError
from app.models.transfer_job import JobStatus, TransferJob
from app.telegram.user_client import user_client_manager
from app.transfer.copier import message_copier
from app.transfer.deduplication import DeduplicationService
from app.transfer.progress import ProgressTracker
from app.transfer.retry import NonRetryableTransferError, retry_executor
from app.utils.formatting import format_duration
from app.utils.paths import ensure_download_directory, sanitize_folder_name

logger = logging.getLogger(__name__)


class TransferWorker:
    """Executes a single TransferJob to completion or interruption."""

    def __init__(self) -> None:
        self._pause_flags: Dict[int, bool] = {}
        self._cancel_flags: Dict[int, bool] = {}
        self._active_trackers: Dict[int, ProgressTracker] = {}
        self._progress_callbacks: Dict[int, Callable[[str, any], Coroutine]] = {}

    def register_progress_callback(
        self, job_id: int, callback: Callable[[str, any], Coroutine]
    ) -> None:
        """Register a Telegram message edit callback for real-time UI updates."""
        self._progress_callbacks[job_id] = callback

    def request_pause(self, job_id: int) -> None:
        """Signal a running job to pause gracefully at the next safe boundary."""
        self._pause_flags[job_id] = True

    def request_cancel(self, job_id: int) -> None:
        """Signal a running job to cancel immediately."""
        self._cancel_flags[job_id] = True

    def get_tracker(self, job_id: int) -> Optional[ProgressTracker]:
        """Get live tracker for a running job."""
        return self._active_trackers.get(job_id)

    async def execute_job(self, job_id: int) -> None:
        """Main execution loop for a transfer job with robust pre-validation and state progression."""
        self._pause_flags[job_id] = False
        self._cancel_flags[job_id] = False

        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if not job:
                logger.error("Job %s not found in database", job_id)
                return

        logger.info(
            "Transfer starting job=%s source=%s destination=%s",
            job.id,
            job.source_chat_id,
            job.destination_chat_id,
        )

        # 1. Setup Progress Tracker immediately so user sees state changes
        tracker_cb = self._progress_callbacks.get(job_id)
        tracker = ProgressTracker(
            job_id=job.id,
            source_title=job.source_chat_title or str(job.source_chat_id),
            destination_title=job.destination_chat_title
            or str(job.destination_chat_id),
            topic_name=job.topic_name,
            total_messages=job.total_messages or 0,
            update_callback=tracker_cb,
        )
        tracker.processed_messages = job.processed_messages
        tracker.successful_messages = job.successful_messages
        tracker.skipped_messages = job.skipped_messages
        tracker.failed_messages = job.failed_messages
        self._active_trackers[job_id] = tracker

        # 2. Mark VALIDATING state in database and UI
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            db_job = res.scalar_one_or_none()
            if db_job:
                db_job.status = JobStatus.VALIDATING.value
                await session.commit()

        await tracker.update(force=True, status_label="VALIDATING")

        # 3. Pre-validation: Retrieve persistent MTProto client
        client = None
        if job.telegram_account_id:
            try:
                client = await user_client_manager.get_active_client(
                    job.telegram_account_id
                )
            except Exception as e:
                await self._fail_job(job_id, f"Session storage error: {e}")
                return

        # 4. Pre-validation: Verify client connection and authorization
        if not client or not client.is_connected():
            await self._fail_job(
                job_id,
                "Telegram account session is inactive or disconnected. Re-connect account in Connected Accounts.",
            )
            return

        try:
            if not await client.is_user_authorized():
                await self._fail_job(
                    job_id,
                    "Telegram account session has expired or is unauthorized. Please re-authenticate.",
                )
                return
        except Exception as e:
            await self._fail_job(job_id, f"Authentication check error: {e}")
            return

        is_local_download = (job.destination_chat_id == 0)
        local_dest_dir: Optional[Path] = None
        source_entity = None
        dest_entity = None

        # 5. Pre-validation: Resolve source entity and accessibility
        try:
            source_entity = await client.get_entity(job.source_chat_id)
        except Exception as e:
            await self._fail_job(
                job_id,
                f"Cannot access source chat ({job.source_chat_title or job.source_chat_id}): {e}",
            )
            return

        # 6. Pre-validation: Resolve destination entity and permissions
        if is_local_download:
            raw_title = job.source_chat_title or f"chat_{job.source_chat_id}"
            safe_folder = sanitize_folder_name(raw_title)
            local_dest_dir = ensure_download_directory(safe_folder)
            tracker.destination_title = f"Downloads/{safe_folder}/"
        else:
            try:
                dest_entity = await client.get_entity(job.destination_chat_id)
            except Exception as e:
                await self._fail_job(
                    job_id,
                    f"Cannot access destination chat ({job.destination_chat_title or job.destination_chat_id}): {e}",
                )
                return

            # Verify send permissions
            is_creator = getattr(dest_entity, "creator", False)
            admin_rights = getattr(dest_entity, "admin_rights", None)
            default_banned = getattr(dest_entity, "default_banned_rights", None)

            if not is_creator:
                if isinstance(dest_entity, Channel) and not getattr(dest_entity, "megagroup", False):
                    # Broadcast channel: must have post_messages permission
                    if not admin_rights or not getattr(admin_rights, "post_messages", False):
                        await self._fail_job(
                            job_id,
                            "Your account does not have permission to post messages in the destination channel.",
                        )
                        return
                elif default_banned and getattr(default_banned, "send_messages", False):
                    await self._fail_job(
                        job_id,
                        "Your account is restricted from sending messages in the destination chat.",
                    )
                    return

        # 7. Validation succeeded: Transition to RUNNING state
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            db_job = res.scalar_one_or_none()
            if db_job:
                db_job.status = JobStatus.RUNNING.value
                if not db_job.started_at:
                    db_job.started_at = datetime.now(timezone.utc)
                await session.commit()

        await tracker.update(force=True, status_label="RUNNING")
        tracker.failed_messages = job.failed_messages
        self._active_trackers[job_id] = tracker

        # Parse allowed content types
        raw_types = (job.content_types or "all").lower().split(",")
        allowed_types = [t.strip() for t in raw_types if t.strip()]

        start_time = time.time()
        start_msg_id = (
            job.last_processed_id
            if (job.last_processed_id and job.last_processed_id > 0)
            else (job.start_message_id or 0)
        )
        end_msg_id = job.end_message_id

        # Fetch messages in ascending order (chronological)
        iter_kwargs = {
            "entity": source_entity,
            "reverse": True,
        }
        if start_msg_id and start_msg_id > 0:
            iter_kwargs["min_id"] = max(0, start_msg_id - 1)
        if end_msg_id and end_msg_id > 0:
            iter_kwargs["max_id"] = end_msg_id + 1
        elif job.total_messages and job.total_messages > 0 and not start_msg_id:
            iter_kwargs["limit"] = job.total_messages

        try:
            async for msg in client.iter_messages(**iter_kwargs):
                # 1. Check for Cancel
                if self._cancel_flags.get(job_id):
                    await self._cancel_job(job_id)
                    await tracker.update(force=True, status_label="CANCELLED")
                    return

                # 2. Check for Pause
                if self._pause_flags.get(job_id):
                    await self._pause_job(job_id, msg.id)
                    await tracker.update(force=True, status_label="PAUSED")
                    return

                # 3. Content filter check
                if not message_copier.should_transfer_message(msg, allowed_types):
                    tracker.processed_messages += 1
                    tracker.skipped_messages += 1
                    await self._persist_step(
                        job_id,
                        last_id=msg.id,
                        processed=tracker.processed_messages,
                        successful=tracker.successful_messages,
                        skipped=tracker.skipped_messages,
                        failed=tracker.failed_messages,
                    )
                    await tracker.update()
                    continue

                # 4. Duplicate detection check
                if job.duplicate_mode == "skip":
                    async with get_session() as session:
                        is_dup, _ = await DeduplicationService.is_duplicate(
                            session=session,
                            source_chat_id=job.source_chat_id,
                            source_message_id=msg.id,
                            destination_chat_id=job.destination_chat_id,
                            destination_thread_id=job.destination_thread_id,
                        )
                    if is_dup:
                        tracker.processed_messages += 1
                        tracker.skipped_messages += 1
                        await self._persist_step(
                            job_id,
                            last_id=msg.id,
                            processed=tracker.processed_messages,
                            successful=tracker.successful_messages,
                            skipped=tracker.skipped_messages,
                            failed=tracker.failed_messages,
                        )
                        await tracker.update()
                        continue

                # 5. Perform Transfer / Local Download with Retry and FloodWait
                cur_msg = msg
                if is_local_download:
                    async def do_copy(m=cur_msg):
                        res = await message_copier.download_to_local(
                            client=client,
                            message=m,
                            download_dir=local_dest_dir,
                        )
                        return m.id if res else 0
                else:
                    async def do_copy(m=cur_msg):
                        return await message_copier.copy_message(
                            client=client,
                            destination_entity=dest_entity,
                            message=m,
                            destination_thread_id=job.destination_thread_id,
                            job_id=job.id,
                            source_entity=source_entity,
                        )

                try:
                    dest_msg_id = await retry_executor.execute(
                        operation=do_copy,
                        job_id=job.id,
                        message_id=msg.id,
                    )

                    # Success: record mapping
                    if dest_msg_id:
                        async with get_session() as session:
                            await DeduplicationService.record_mapping(
                                session=session,
                                source_chat_id=job.source_chat_id,
                                source_message_id=msg.id,
                                destination_chat_id=job.destination_chat_id,
                                destination_message_id=dest_msg_id,
                                destination_thread_id=job.destination_thread_id,
                                transfer_job_id=job.id,
                            )
                        tracker.successful_messages += 1
                        logger.info(
                            "Message processed job=%s source_message=%s dest_message=%s",
                            job.id,
                            msg.id,
                            dest_msg_id,
                        )
                    else:
                        tracker.skipped_messages += 1

                except Exception as err:
                    # Message failed: record error
                    logger.error(
                        "Message failed job=%s message=%s error=%s",
                        job.id,
                        msg.id,
                        err,
                    )
                    tracker.failed_messages += 1
                    async with get_session() as session:
                        err_rec = TransferError(
                            transfer_job_id=job.id,
                            source_message_id=msg.id,
                            error_type=type(err).__name__,
                            error_message=str(err),
                            retry_count=1,
                        )
                        session.add(err_rec)

                tracker.processed_messages += 1
                await self._persist_step(
                    job_id,
                    last_id=msg.id,
                    processed=tracker.processed_messages,
                    successful=tracker.successful_messages,
                    skipped=tracker.skipped_messages,
                    failed=tracker.failed_messages,
                )
                await tracker.update()

            # Finished loop
            duration = time.time() - start_time
            await self._complete_job(job_id, tracker, duration)

        except Exception as fatal_e:
            logger.error("Job %s encountered fatal loop error: %s", job_id, fatal_e)
            await self._fail_job(job_id, str(fatal_e))
        finally:
            self._active_trackers.pop(job_id, None)

    async def _persist_step(
        self,
        job_id: int,
        last_id: int,
        processed: int,
        successful: int,
        skipped: int,
        failed: int,
    ) -> None:
        """Persist intermediate counters to database."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job:
                job.last_processed_id = last_id
                job.processed_messages = processed
                job.successful_messages = successful
                job.skipped_messages = skipped
                job.failed_messages = failed

    async def _pause_job(self, job_id: int, last_msg_id: int) -> None:
        """Persist paused state."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job:
                job.status = JobStatus.PAUSED.value
                job.last_processed_id = last_msg_id
        logger.info("Transfer paused job=%s at message=%s", job_id, last_msg_id)

    async def _cancel_job(self, job_id: int) -> None:
        """Persist cancelled state."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job:
                job.status = JobStatus.CANCELLED.value
                job.completed_at = datetime.now(timezone.utc)
        logger.info("Transfer cancelled job=%s", job_id)

    async def _fail_job(self, job_id: int, reason: str) -> None:
        """Persist failed state."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job:
                job.status = JobStatus.FAILED.value
                job.completed_at = datetime.now(timezone.utc)
                job.error_summary = reason
        logger.error("Transfer failed job=%s reason=%s", job_id, reason)

        fail_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
        )
        tracker = self._active_trackers.get(job_id)
        if tracker:
            await tracker.update(
                force=True,
                status_label="FAILED",
                custom_keyboard=fail_kb,
                error_detail=reason,
            )
        elif job_id in self._progress_callbacks:
            cb = self._progress_callbacks[job_id]
            try:
                await cb(f"❌ *Transfer #{job_id} (FAILED)*\n\n⚠️ *Reason:* _{reason}_", fail_kb)
            except Exception:
                pass

    async def _complete_job(
        self, job_id: int, tracker: ProgressTracker, duration: float
    ) -> None:
        """Persist completed state and trigger completion notice."""
        async with get_session() as session:
            res = await session.execute(
                select(TransferJob).where(TransferJob.id == job_id)
            )
            job = res.scalar_one_or_none()
            if job:
                job.status = JobStatus.COMPLETED.value
                job.completed_at = datetime.now(timezone.utc)
                job.processed_messages = tracker.processed_messages
                job.successful_messages = tracker.successful_messages
                job.skipped_messages = tracker.skipped_messages
                job.failed_messages = tracker.failed_messages

        logger.info(
            "Transfer completed job=%s success=%s skipped=%s failed=%s duration=%0.1fs",
            job_id,
            tracker.successful_messages,
            tracker.skipped_messages,
            tracker.failed_messages,
            duration,
        )

        done_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📋 Transfer History", callback_data="nav:history"
                    ),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ]
            ]
        )
        await tracker.update(
            force=True,
            status_label="COMPLETED",
            custom_keyboard=done_kb,
        )


transfer_worker = TransferWorker()

