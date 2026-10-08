"""Tests for session resilience, locking, storage error isolation, and pre-validation."""

import asyncio
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.bot.chat_picker import ChatPicker
from app.config import settings
from app.models.transfer_job import JobStatus, TransferJob
from app.telegram.discovery import DiscoveredChat
from app.telegram.user_client import (
    SessionStorageError,
    UserClientManager,
)
from app.transfer.worker import TransferWorker


@pytest.fixture(autouse=True)
async def isolated_test_db():
    """Ensure tests run against an isolated in-memory database."""
    from app.database import Base
    import app.telegram.user_client as uc_module
    import app.transfer.worker as tw_module
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from contextlib import asynccontextmanager

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

    orig_uc = uc_module.get_session
    orig_tw = tw_module.get_session
    uc_module.get_session = mock_get_session
    tw_module.get_session = mock_get_session
    try:
        yield mock_get_session
    finally:
        uc_module.get_session = orig_uc
        tw_module.get_session = orig_tw
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_storage_error_does_not_deactivate_account(isolated_test_db):
    """Verify sqlite3.OperationalError raises SessionStorageError and preserves is_active."""
    from app.models.telegram_account import TelegramAccount
    from app.models.user import User

    manager = UserClientManager()

    # Create active account in DB
    async with isolated_test_db() as session:
        user = User(telegram_id=12345, is_admin=True)
        session.add(user)
        await session.flush()
        account = TelegramAccount(
            user_id=user.id,
            phone_number="+919565292019",
            session_name="account_1",
            is_active=True,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_client.connect = AsyncMock(side_effect=sqlite3.OperationalError("attempt to write a readonly database"))
    mock_client.disconnect = AsyncMock()

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client), \
         patch.object(settings, "API_ID", 12345), \
         patch.object(settings, "API_HASH", "mock_hash"):

        with pytest.raises(SessionStorageError) as exc_info:
            await manager.get_client_for_account(account_id)

        assert "attempt to write a readonly database" in str(exc_info.value)

    # Verify account is still marked active in DB
    async with isolated_test_db() as session:
        from sqlalchemy import select
        res = await session.execute(select(TelegramAccount).where(TelegramAccount.id == account_id))
        acc = res.scalar_one_or_none()
        assert acc is not None
        assert acc.is_active is True


@pytest.mark.asyncio
async def test_single_client_instance_returned_concurrently(isolated_test_db):
    """Verify multiple concurrent calls to get_client_for_account return the exact same instance."""
    from app.models.telegram_account import TelegramAccount
    from app.models.user import User

    manager = UserClientManager()

    async with isolated_test_db() as session:
        user = User(telegram_id=12345, is_admin=True)
        session.add(user)
        await session.flush()
        account = TelegramAccount(
            user_id=user.id,
            phone_number="+919565292019",
            session_name="account_1",
            is_active=True,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client) as client_cls, \
         patch.object(settings, "API_ID", 12345), \
         patch.object(settings, "API_HASH", "mock_hash"):

        # Run 5 concurrent calls
        c1, c2, c3, c4, c5 = await asyncio.gather(
            manager.get_client_for_account(account_id),
            manager.get_client_for_account(account_id),
            manager.get_client_for_account(account_id),
            manager.get_client_for_account(account_id),
            manager.get_client_for_account(account_id),
        )

        assert c1 is c2 is c3 is c4 is c5
        # Only instantiated once
        assert client_cls.call_count == 1


@pytest.mark.asyncio
async def test_legacy_session_file_migration(isolated_test_db, tmp_path):
    """Verify legacy user_{phone}.session file is automatically copied to account_{id}.session."""
    from app.models.telegram_account import TelegramAccount
    from app.models.user import User

    manager = UserClientManager()

    # Place legacy session file in temporary session directory
    legacy_file = tmp_path / "user_919565292019.session"
    legacy_file.write_text("LEGACY_AUTH_DATA")

    target_session_file = tmp_path / "account_1.session"
    assert not target_session_file.exists()

    async with isolated_test_db() as session:
        user = User(telegram_id=12345, is_admin=True)
        session.add(user)
        await session.flush()
        account = TelegramAccount(
            user_id=user.id,
            phone_number="+919565292019",
            session_name="account_1",
            is_active=True,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client), \
         patch.object(settings, "SESSION_DIRECTORY", str(tmp_path)), \
         patch.object(settings, "API_ID", 12345), \
         patch.object(settings, "API_HASH", "mock_hash"):

        client = await manager.get_client_for_account(account_id)
        assert client is not None

        # Verify file was migrated
        assert target_session_file.exists()
        assert target_session_file.read_text() == "LEGACY_AUTH_DATA"


@pytest.mark.asyncio
async def test_search_inplace_edit_when_bot_provided():
    """Verify ChatPicker.handle_search_query edits message in place without spamming."""
    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.chat_id = 998877
    mock_msg.edit_message_text = AsyncMock()

    sample = [
        DiscoveredChat(id=-1001, title="Python Course", chat_type="channel", username="py_course", can_post=True),
        DiscoveredChat(id=-1002, title="Cooking Recipes", chat_type="channel", username="cooking", can_post=True),
    ]

    with patch("app.bot.chat_picker.session_store.get_data", return_value={"account_id": 1}), \
         patch("app.telegram.discovery.ChatDiscovery.load_dialogs", new_callable=AsyncMock), \
         patch("app.telegram.discovery.ChatDiscovery.search_dialogs", new_callable=AsyncMock, return_value=[sample[0]]), \
         patch("app.telegram.user_client.user_client_manager.get_client_for_account", new_callable=AsyncMock) as mock_get_client:

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_get_client.return_value = mock_client

        await ChatPicker.handle_search_query(
            message_or_query=mock_msg,
            user_id=12345,
            target="source",
            query_text="py",
            page=0,
            edit_message_id=456,
            bot=mock_bot,
        )

        # Bot edit_message_text was called on the prompt message ID
        mock_bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_bot.edit_message_text.call_args.kwargs
        assert call_kwargs["chat_id"] == 998877
        assert call_kwargs["message_id"] == 456
        assert "Found 1 matching chats" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_transfer_worker_validating_state_and_failure_reporting(isolated_test_db):
    """Verify worker enters VALIDATING state and reports failures to tracker."""
    worker = TransferWorker()

    async with isolated_test_db() as session:
        job = TransferJob(
            owner_id=12345,
            source_chat_id=-100111,
            source_chat_title="Source A",
            destination_chat_id=-100222,
            destination_chat_title="Dest B",
            total_messages=10,
            status=JobStatus.QUEUED.value,
            telegram_account_id=999,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    progress_callback = AsyncMock()
    worker.register_progress_callback(job_id, progress_callback)

    # Mock user_client_manager to return disconnected client
    with patch("app.transfer.worker.user_client_manager.get_active_client", new_callable=AsyncMock, return_value=None):
        await worker.execute_job(job_id)

    # Job must be marked FAILED
    async with isolated_test_db() as session:
        from sqlalchemy import select
        res = await session.execute(select(TransferJob).where(TransferJob.id == job_id))
        failed_job = res.scalar_one_or_none()
        assert failed_job.status == JobStatus.FAILED.value
        assert "inactive or disconnected" in failed_job.error_summary

    # Progress callback was notified with failure
    assert progress_callback.await_count >= 1
    last_call_text = progress_callback.call_args[0][0]
    assert "FAILED" in last_call_text or "Reason" in last_call_text
