"""Tests for message deduplication service."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.database import Base
from app.transfer.deduplication import DeduplicationService


@pytest.fixture
async def test_session():
    """Create in-memory SQLite database for deduplication testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_is_duplicate_detection(test_session: AsyncSession):
    """Verify is_duplicate detects new vs recorded mappings."""
    src_chat = -1001234567890
    dst_chat = -1009876543210
    msg_id = 100
    thread_id = 3

    # 1. Before recording, message is NOT duplicate
    is_dup, existing_id = await DeduplicationService.is_duplicate(
        session=test_session,
        source_chat_id=src_chat,
        source_message_id=msg_id,
        destination_chat_id=dst_chat,
        destination_thread_id=thread_id,
    )
    assert is_dup is False
    assert existing_id is None

    # 2. Record mapping
    await DeduplicationService.record_mapping(
        session=test_session,
        source_chat_id=src_chat,
        source_message_id=msg_id,
        destination_chat_id=dst_chat,
        destination_message_id=555,
        destination_thread_id=thread_id,
    )
    await test_session.commit()

    # 3. After recording, message IS duplicate
    is_dup, existing_id = await DeduplicationService.is_duplicate(
        session=test_session,
        source_chat_id=src_chat,
        source_message_id=msg_id,
        destination_chat_id=dst_chat,
        destination_thread_id=thread_id,
    )
    assert is_dup is True
    assert existing_id == 555

    # 4. Same source message to a DIFFERENT topic is NOT duplicate
    is_dup_diff_thread, _ = await DeduplicationService.is_duplicate(
        session=test_session,
        source_chat_id=src_chat,
        source_message_id=msg_id,
        destination_chat_id=dst_chat,
        destination_thread_id=4,  # Different thread ID
    )
    assert is_dup_diff_thread is False

