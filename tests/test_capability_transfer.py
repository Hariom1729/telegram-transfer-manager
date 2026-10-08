"""Tests for Capability-based Transfer Strategy, Media Routing, Diagnostics, and Queue Resilience."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telethon import types
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel, DocumentAttributeVideo, MessageMediaDocument

from app.database import Base
from app.models.transfer_job import JobStatus, TransferJob
from app.models.transfer_error import TransferError
from app.transfer.copier import MessageCopier, TransferDiagnostics
from app.transfer.retry import NonRetryableTransferError
from app.transfer.worker import TransferWorker


@pytest.mark.asyncio
async def test_normal_text_message_transfers_natively():
    """Verify normal text message sends natively via send_message and logs native diagnostics."""
    client = MagicMock()
    client.send_message = AsyncMock(return_value=MagicMock(id=1001))
    client.download_media = AsyncMock()

    msg = MagicMock(spec=Message)
    msg.id = 1
    msg.media = None
    msg.message = "Hello, this is a plain text message"
    msg.entities = None
    msg.web_preview = False

    captured_diags = []

    def on_diag(diag: TransferDiagnostics):
        captured_diags.append(diag)

    res_id = await MessageCopier.copy_message(
        client=client,
        destination_entity=-1002,
        message=msg,
        job_id=42,
        on_diagnostics=on_diag,
    )

    assert res_id == 1001
    assert client.send_message.call_count == 1
    assert client.download_media.call_count == 0

    assert len(captured_diags) == 1
    assert captured_diags[0].method == "native"
    assert captured_diags[0].file_size == len(msg.message.encode("utf-8"))
    assert captured_diags[0].total_time > 0


@pytest.mark.asyncio
async def test_normal_unrestricted_video_transfers_natively():
    """Verify unrestricted video attempts native transfer first and succeeds without downloading to disk."""
    client = MagicMock()
    client.send_file = AsyncMock(return_value=MagicMock(id=2002))
    client.download_media = AsyncMock()

    msg = MagicMock(spec=Message)
    msg.id = 2
    msg.media = MagicMock(spec=MessageMediaDocument)
    msg.noforwards = False
    msg.restricted = False
    msg.restriction_reason = None
    msg.message = "Unrestricted video clip"
    msg.entities = None
    msg.chat = MagicMock(noforwards=False, restricted=False, restriction_reason=None)
    msg.file = MagicMock(size=1024 * 1024 * 5)  # 5 MB

    captured_diags = []
    res_id = await MessageCopier.copy_message(
        client=client,
        destination_entity=-1002,
        message=msg,
        job_id=43,
        on_diagnostics=lambda d: captured_diags.append(d),
    )

    assert res_id == 2002
    assert client.send_file.call_count == 1
    # Crucial: download_media was NEVER called because native transfer worked directly!
    assert client.download_media.call_count == 0

    assert len(captured_diags) == 1
    assert captured_diags[0].method == "native"
    assert captured_diags[0].file_size == 1024 * 1024 * 5
    assert captured_diags[0].file_size_mb == 5.0


@pytest.mark.asyncio
async def test_restricted_video_skips_native_and_uses_download_upload(tmp_path):
    """Verify restricted video (noforwards=True) routes directly to authorized download/upload pipeline."""
    client = MagicMock()

    async def mock_dl(m, file):
        p = Path(file)
        p.write_bytes(b"A" * (1024 * 1024 * 2))
        return str(p)

    client.download_media = mock_dl
    client.send_file = AsyncMock(return_value=MagicMock(id=3003))

    msg = MagicMock(spec=Message)
    msg.id = 3
    msg.chat_id = -100999
    msg.media = MagicMock(spec=MessageMediaDocument)
    # Flagged as noforwards (protected)
    msg.noforwards = True
    msg.restricted = False
    msg.restriction_reason = None
    msg.video = MagicMock()
    msg.message = "Protected video clip"
    msg.entities = None
    msg.document = MagicMock()
    msg.document.attributes = [MagicMock(spec=DocumentAttributeVideo)]

    captured_diags = []
    with patch("app.transfer.copier.get_temp_download_directory", return_value=tmp_path):
        res_id = await MessageCopier.copy_message(
            client=client,
            destination_entity=-1002,
            message=msg,
            job_id=44,
            on_diagnostics=lambda d: captured_diags.append(d),
        )

    assert res_id == 3003
    # send_file was called once (for the upload of the downloaded file)
    assert client.send_file.call_count == 1
    upload_call_kwargs = client.send_file.call_args.kwargs
    # Verified: sent the uploaded handle/file, not the original restricted media reference
    assert isinstance(upload_call_kwargs["file"], (str, types.InputFile, types.InputFileBig))
    assert upload_call_kwargs["supports_streaming"] is True

    # Temp files cleaned up
    remaining = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(remaining) == 0

    # Diagnostics recorded
    assert len(captured_diags) == 1
    diag = captured_diags[0]
    assert diag.method == "download_upload"
    assert diag.file_size == 1024 * 1024 * 2
    assert diag.file_size_mb == 2.0
    assert diag.download_time > 0
    assert diag.upload_time > 0
    assert diag.download_speed > 0
    assert diag.upload_speed > 0


@pytest.mark.asyncio
async def test_native_forward_rejected_falls_back_to_download_upload(tmp_path):
    """Verify when native send/forward fails, it gracefully falls back to authorized download/upload."""
    client = MagicMock()
    # First send_file (native) fails; second send_file (after download) succeeds
    client.send_file = AsyncMock()
    client.send_file.side_effect = [
        Exception("Native send_file rejected by Telegram"),
        MagicMock(id=4004),
    ]
    client.forward_messages = AsyncMock(side_effect=Exception("ChatForwardsRestrictedError"))

    async def mock_dl(m, file):
        p = Path(file)
        p.write_bytes(b"B" * 50000)
        return str(p)

    client.download_media = mock_dl

    msg = MagicMock(spec=Message)
    msg.id = 4
    msg.chat_id = -100888
    msg.media = MagicMock(spec=MessageMediaDocument)
    msg.noforwards = False
    msg.restricted = False
    msg.restriction_reason = None
    msg.message = "Message with rejected native forward"
    msg.entities = None
    msg.document = None

    captured_diags = []
    with patch("app.transfer.copier.get_temp_download_directory", return_value=tmp_path):
        res_id = await MessageCopier.copy_message(
            client=client,
            destination_entity=-1002,
            message=msg,
            job_id=45,
            on_diagnostics=lambda d: captured_diags.append(d),
        )

    assert res_id == 4004
    assert client.send_file.call_count == 2
    assert client.forward_messages.call_count == 1

    # Diagnostics recorded
    assert len(captured_diags) == 1
    diag = captured_diags[0]
    assert diag.method == "download_upload"
    assert diag.file_size == 50000

    # Temp files cleaned up
    remaining = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_inaccessible_media_raises_non_retryable_clear_error(tmp_path):
    """Verify when Telegram denies media retrieval, a clear NonRetryableTransferError is raised."""
    client = MagicMock()
    # download_media returns None (Telegram denied/empty media)
    client.download_media = AsyncMock(return_value=None)
    client.send_file = AsyncMock(side_effect=Exception("Direct disallowed"))
    client.forward_messages = AsyncMock(side_effect=Exception("Forward disallowed"))

    msg = MagicMock(spec=Message)
    msg.id = 5
    msg.chat_id = -100777
    msg.media = MagicMock(spec=MessageMediaDocument)
    msg.noforwards = True
    msg.message = "Inaccessible media"
    msg.entities = None

    with patch("app.transfer.copier.get_temp_download_directory", return_value=tmp_path):
        with pytest.raises(NonRetryableTransferError) as exc_info:
            await MessageCopier.copy_message(
                client=client,
                destination_entity=-1002,
                message=msg,
                job_id=46,
            )

    assert "Telegram did not make the media available to this account." in str(exc_info.value)
    # Verify temp files cleaned up even on failure
    remaining = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_queue_resilience_multi_message_does_not_crash(tmp_path):
    """Verify that when one message fails or falls back, the queue continues processing remaining messages."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    with patch("app.transfer.worker.get_session", mock_get_session):

        # Create job
        async with mock_get_session() as s:
            job = TransferJob(
                owner_id=12345,
                telegram_account_id=1,
                source_chat_id=-1001,
                source_chat_title="Source Chat",
                destination_chat_id=-1002,
                destination_chat_title="Dest Chat",
                content_types="all",
                duplicate_mode="allow",
                total_messages=3,
                status=JobStatus.QUEUED.value,
            )
            s.add(job)
            await s.flush()
            job_id = job.id

        # Prepare 3 messages:
        # Msg 1: Plain text -> succeeds
        msg1 = MagicMock(spec=Message)
        msg1.id = 101
        msg1.media = None
        msg1.action = None
        msg1.message = "Text message 1"
        msg1.entities = None
        msg1.web_preview = False

        # Msg 2: Media that Telegram denies -> fails with clear error
        msg2 = MagicMock(spec=Message)
        msg2.id = 102
        msg2.media = MagicMock(spec=MessageMediaDocument)
        msg2.action = None
        msg2.noforwards = True
        msg2.message = "Media message 2 (denied by Telegram)"
        msg2.entities = None

        # Msg 3: Protected media -> falls back to download/upload -> succeeds
        msg3 = MagicMock(spec=Message)
        msg3.id = 103
        msg3.media = MagicMock(spec=MessageMediaDocument)
        msg3.action = None
        msg3.noforwards = True
        msg3.video = MagicMock()
        msg3.message = "Media message 3 (protected but accessible)"
        msg3.entities = None
        msg3.document = None

        # Mock client
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.is_user_authorized = AsyncMock(return_value=True)

        source_channel = MagicMock(spec=Channel, id=-1001, title="Source Chat", creator=False, default_banned_rights=None, noforwards=True)
        dest_channel = MagicMock(spec=Channel, id=-1002, title="Dest Chat", creator=True, default_banned_rights=None)

        async def mock_get_entity(ident):
            if ident == -1001:
                return source_channel
            return dest_channel

        client.get_entity = mock_get_entity

        # Mock iter_messages yielding the 3 messages
        async def mock_iter_messages(**kwargs):
            for m in [msg1, msg2, msg3]:
                yield m

        client.iter_messages = mock_iter_messages

        # Mock client actions
        client.send_message = AsyncMock(return_value=MagicMock(id=5001))

        async def mock_dl(m, file):
            if m.id == 102:
                # Denied!
                return None
            p = Path(file)
            p.write_bytes(b"content-for-103")
            return str(p)

        client.download_media = mock_dl
        client.send_file = AsyncMock(return_value=MagicMock(id=5003))

        with patch("app.transfer.worker.user_client_manager.get_active_client", AsyncMock(return_value=client)), \
             patch("app.transfer.copier.get_temp_download_directory", return_value=tmp_path):

            worker = TransferWorker()
            await worker.execute_job(job_id)

        # Inspect resulting job status in DB
        async with mock_get_session() as s:
            from sqlalchemy import select
            res = await s.execute(select(TransferJob).where(TransferJob.id == job_id))
            final_job = res.scalar_one()

            assert final_job.status == JobStatus.COMPLETED.value
            assert final_job.processed_messages == 3
            assert final_job.successful_messages == 2
            assert final_job.failed_messages == 1

            # Check recorded transfer error for message 102
            err_res = await s.execute(select(TransferError).where(TransferError.transfer_job_id == job_id))
            errors = err_res.scalars().all()
            assert len(errors) == 1
            assert errors[0].source_message_id == 102
            assert "Telegram did not make the media available to this account." in errors[0].error_message

    await engine.dispose()
