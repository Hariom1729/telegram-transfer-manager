"""Tests for Transfer Manager, State Transitions, and Worker logic."""

from unittest.mock import MagicMock
import pytest
from telethon.tl.custom.message import Message
from telethon.tl.types import MessageActionChatCreate, MessageMediaPhoto
from app.models.transfer_job import JobStatus
from app.transfer.copier import MessageCopier
from app.transfer.manager import TransferManager
from app.transfer.progress import ProgressTracker
from app.utils.formatting import format_progress_bar
from app.utils.validators import (
    validate_chat_identifier,
    validate_message_range,
    validate_phone_number,
)


def test_progress_bar_formatting():
    """Verify progress bar calculation and output."""
    bar_0 = format_progress_bar(0, 100, length=10)
    assert "░░░░░░░░░░ 0%" in bar_0

    bar_50 = format_progress_bar(50, 100, length=10)
    assert "█████░░░░░ 50%" in bar_50

    bar_100 = format_progress_bar(100, 100, length=10)
    assert "██████████ 100%" in bar_100


def test_progress_tracker_status_message():
    """Verify section 20 progress message template."""
    tracker = ProgressTracker(
        job_id=1042,
        source_title="Source Channel",
        destination_title="Target Group",
        topic_name="Programming",
        total_messages=1560,
    )
    tracker.processed_messages = 1284
    tracker.successful_messages = 1250
    tracker.skipped_messages = 27
    tracker.failed_messages = 7

    status_text = tracker.format_status_message()
    assert "Transfer #1042" in status_text
    assert "Source:\n📢 Source Channel" in status_text
    assert "Destination:\n👥 Target Group" in status_text
    assert "Topic:\n🧵 Programming" in status_text
    assert "1,284 / 1,560" in status_text
    assert "✅ Success: 1,250" in status_text
    assert "⏭ Skipped: 27" in status_text
    assert "❌ Failed: 7" in status_text


def test_content_filter_classification():
    """Verify content filter correctly identifies photos, texts, and service messages."""
    # Text message
    text_msg = MagicMock(spec=Message)
    text_msg.action = None
    text_msg.media = None
    text_msg.photo = None
    text_msg.video = None
    text_msg.audio = None
    text_msg.voice = None
    text_msg.gif = None
    text_msg.document = None
    text_msg.file = None
    text_msg.text = "Hello world"
    text_msg.message = "Hello world"

    assert MessageCopier.classify_message_content(text_msg) == "text"
    assert MessageCopier.should_transfer_message(text_msg, ["text"]) is True
    assert MessageCopier.should_transfer_message(text_msg, ["photo"]) is False
    assert MessageCopier.should_transfer_message(text_msg, ["all"]) is True

    # Photo message
    photo_msg = MagicMock(spec=Message)
    photo_msg.action = None
    photo_msg.media = MagicMock(spec=MessageMediaPhoto)
    photo_msg.photo = MagicMock()
    photo_msg.video = None
    photo_msg.audio = None
    photo_msg.voice = None
    photo_msg.gif = None
    photo_msg.document = None
    photo_msg.file = None
    photo_msg.text = "Photo caption"
    photo_msg.message = "Photo caption"

    assert MessageCopier.classify_message_content(photo_msg) == "photo"
    assert MessageCopier.should_transfer_message(photo_msg, ["photo"]) is True
    assert MessageCopier.should_transfer_message(photo_msg, ["text"]) is False

    # Service message should always be skipped
    service_msg = MagicMock(spec=Message)
    service_msg.action = MagicMock(spec=MessageActionChatCreate)
    assert MessageCopier.should_transfer_message(service_msg, ["all"]) is False


def test_validators():
    """Verify input validators for phone, chat identifiers, and message ranges."""
    # Phone numbers
    valid, phone = validate_phone_number("+12025550143")
    assert valid is True
    assert phone == "+12025550143"

    invalid, _ = validate_phone_number("12345")
    assert invalid is False

    # Chat identifiers
    is_id, val = validate_chat_identifier("-1001234567890")
    assert is_id is True
    assert val == -1001234567890

    is_user, uval = validate_chat_identifier("@my_telegram_channel")
    assert is_user is True
    assert uval == "@my_telegram_channel"

    # Message range
    is_rng_valid, _ = validate_message_range(1, 155)
    assert is_rng_valid is True

    is_rng_inv, err = validate_message_range(200, 100)
    assert is_rng_inv is False
    assert "greater" in err


@pytest.mark.asyncio
async def test_job_lifecycle_state_transitions():
    """Verify pause, resume, and cancel job status modifications."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.database import Base
    import app.transfer.manager as tm_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    original_get_session = tm_module.get_session
    tm_module.get_session = mock_get_session

    try:
        manager = tm_module.TransferManager()
        # 1. Create job
        job = await manager.create_job(
            owner_id=123,
            telegram_account_id=None,
            source_chat_id=-1001,
            source_chat_title="Src",
            destination_chat_id=-1002,
            destination_chat_title="Dst",
        )
        assert job.status == JobStatus.QUEUED.value

        # 2. Simulate running to paused
        async with mock_get_session() as s:
            from app.models.transfer_job import TransferJob
            from sqlalchemy import select
            res = await s.execute(select(TransferJob).where(TransferJob.id == job.id))
            j = res.scalar_one()
            j.status = JobStatus.PAUSED.value

        # 3. Resume job
        resumed = await manager.resume_job(job.id)
        assert resumed is True
        fetched = await manager.get_job(job.id)
        assert fetched.status == JobStatus.QUEUED.value

        # 4. Cancel job
        cancelled = await manager.cancel_job(job.id)
        assert cancelled is True
        fetched2 = await manager.get_job(job.id)
        assert fetched2.status == JobStatus.CANCELLED.value

    finally:
        tm_module.get_session = original_get_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_jobs_recovery_on_startup():
    """Verify section 32: RUNNING jobs on startup are recovered to QUEUED."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.database import Base
    import app.transfer.manager as tm_module
    from app.models.transfer_job import TransferJob

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    original_get_session = tm_module.get_session
    tm_module.get_session = mock_get_session

    try:
        # Seed an interrupted running job
        async with mock_get_session() as s:
            stuck_job = TransferJob(
                owner_id=999,
                source_chat_id=-1001,
                destination_chat_id=-1002,
                status=JobStatus.RUNNING.value,
                last_processed_id=45,
            )
            s.add(stuck_job)

        manager = tm_module.TransferManager()
        recovered_count = await manager.recover_interrupted_jobs()
        assert recovered_count == 1

        # Check job status is now QUEUED
        recovered_job = await manager.get_job(stuck_job.id)
        assert recovered_job.status == JobStatus.QUEUED.value
        assert recovered_job.last_processed_id == 45

    finally:
        tm_module.get_session = original_get_session
        await engine.dispose()


@pytest.mark.asyncio
async def test_download_to_local_media_and_text(tmp_path):
    """Verify download_to_local saves media and text messages properly."""
    client = MagicMock()

    # 1. Media message
    media_file = tmp_path / "photo.jpg"
    media_file.write_bytes(b"fake-image-bytes")

    async def mock_download_media(msg, file):
        return str(media_file)

    client.download_media = mock_download_media

    media_msg = MagicMock(spec=Message)
    media_msg.id = 101
    media_msg.media = MagicMock()
    media_msg.message = "Beautiful Landscape"

    out_dir = tmp_path / "downloads"
    res = await MessageCopier.download_to_local(client, media_msg, out_dir)
    assert res == str(media_file)
    # Check caption file
    caption_file = media_file.with_suffix(".jpg.caption.txt")
    assert caption_file.exists()
    assert caption_file.read_text(encoding="utf-8") == "Beautiful Landscape"

    # 2. Text message
    text_msg = MagicMock(spec=Message)
    text_msg.id = 102
    text_msg.media = None
    text_msg.message = "Important notes here"

    res_txt = await MessageCopier.download_to_local(client, text_msg, out_dir)
    assert res_txt is not None
    assert (out_dir / "msg_102.txt").exists()
    assert (out_dir / "msg_102.txt").read_text(encoding="utf-8") == "Important notes here"


def test_progress_tracker_completed_skipped_note():
    """Verify informative note is included when all messages are skipped on completion."""
    tracker = ProgressTracker(
        job_id=43,
        source_title="AI Bootcamp",
        destination_title="💾 Downloads/AI Bootcamp/",
        total_messages=5,
    )
    tracker.processed_messages = 5
    tracker.successful_messages = 0
    tracker.skipped_messages = 5
    tracker.failed_messages = 0

    msg = tracker.format_status_message(status_label="COMPLETED")
    assert "All 5 message(s) were skipped because they were already transferred/downloaded" in msg
    assert "To download new messages:" in msg


@pytest.mark.asyncio
async def test_create_job_with_source_topic():
    """Verify creating a job with source forum topic records source_thread_id and source_topic_name."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    import app.transfer.manager as tm_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    original_get_session = tm_module.get_session
    tm_module.get_session = mock_get_session

    try:
        manager = tm_module.TransferManager()
        job = await manager.create_job(
            owner_id=123,
            telegram_account_id=1,
            source_chat_id=-100100,
            source_chat_title="My Forum Source",
            source_thread_id=55,
            source_topic_name="Physics",
            destination_chat_id=-100200,
            destination_chat_title="Dest Group",
            destination_thread_id=77,
            topic_name="General",
        )

        assert job.source_thread_id == 55
        assert job.source_topic_name == "Physics"
        assert job.destination_thread_id == 77
        assert job.topic_name == "General"
    finally:
        tm_module.get_session = original_get_session
        await engine.dispose()


def test_progress_bar_completed_is_100_percent():
    """Verify that a completed job always shows 100% progress bar, even if total_messages was 0."""
    tracker = ProgressTracker(
        job_id=71,
        source_title="Source Group",
        destination_title="Target Group",
        total_messages=0,  # "All messages" was selected
    )
    tracker.processed_messages = 155
    tracker.successful_messages = 150
    tracker.skipped_messages = 1
    tracker.failed_messages = 4

    status_text = tracker.format_status_message(status_label="COMPLETED")
    assert "100%" in status_text
    assert "███████████████ 100%" in status_text
    assert "Processed: 155" in status_text
    assert "✅ Success: 150" in status_text
    assert "❌ Failed: 4" in status_text


def test_build_job_completion_keyboard():
    """Verify that completion keyboard includes retransfer button when there are failed messages."""
    from app.bot.keyboards import build_job_completion_keyboard

    # No failed messages
    kb_clean = build_job_completion_keyboard(job_id=1, failed_count=0)
    flat_clean = [btn.callback_data for row in kb_clean.inline_keyboard for btn in row]
    assert not any("job_retry_failed" in cb for cb in flat_clean)

    # 4 failed messages
    kb_failed = build_job_completion_keyboard(job_id=1, failed_count=4)
    flat_failed = [btn.callback_data for row in kb_failed.inline_keyboard for btn in row]
    assert any("job_retry_failed:1" in cb for cb in flat_failed)
    # Check button text
    btn_text = [btn.text for row in kb_failed.inline_keyboard for btn in row if "job_retry_failed:1" in btn.callback_data][0]
    assert "Retransfer 4 Failed Msg" in btn_text


@pytest.mark.asyncio
async def test_retry_failed_messages_creates_targeted_job():
    """Verify retry_failed_messages creates a new TransferJob targeting only the failed message IDs."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    import app.transfer.manager as tm_module
    from app.models.transfer_job import TransferJob, JobStatus

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    original_get_session = tm_module.get_session
    tm_module.get_session = mock_get_session

    try:
        manager = tm_module.TransferManager()
        # Create an original job with failed messages
        orig_job = await manager.create_job(
            owner_id=101,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Orig Source",
            destination_chat_id=-100222,
            destination_chat_title="Orig Dest",
            total_messages=155,
        )

        async with mock_get_session() as session:
            db_j = await session.get(TransferJob, orig_job.id)
            db_j.failed_messages = 4
            db_j.failed_message_ids = "12,15,48,99"
            db_j.status = JobStatus.COMPLETED.value
            await session.commit()

        # Call retry_failed_messages
        retry_job = await manager.retry_failed_messages(orig_job.id)
        assert retry_job is not None
        assert retry_job.id != orig_job.id
        assert retry_job.owner_id == 101
        assert retry_job.source_chat_id == -100111
        assert retry_job.destination_chat_id == -100222
        assert retry_job.duplicate_mode == "overwrite"
        assert retry_job.specific_message_ids == "12,15,48,99"
        assert retry_job.total_messages == 4
        assert retry_job.status == JobStatus.QUEUED.value
    finally:
        tm_module.get_session = original_get_session
        await engine.dispose()





