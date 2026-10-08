"""Tests for database models, session management, and constraints."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.database import Base
from app.models.chat import Chat
from app.models.message_mapping import MessageMapping
from app.models.telegram_account import TelegramAccount
from app.models.topic import Topic
from app.models.transfer_job import JobStatus, TransferJob
from app.models.user import User


@pytest.fixture
async def test_session():
    """Create an in-memory SQLite database for isolated unit testing."""
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
async def test_user_creation_and_query(test_session: AsyncSession):
    """Verify User model creation and lookup."""
    user = User(
        telegram_id=123456789,
        username="testadmin",
        first_name="Admin",
        is_admin=True,
    )
    test_session.add(user)
    await test_session.commit()

    res = await test_session.execute(
        select(User).where(User.telegram_id == 123456789)
    )
    queried = res.scalar_one_or_none()
    assert queried is not None
    assert queried.username == "testadmin"
    assert queried.is_admin is True


@pytest.mark.asyncio
async def test_transfer_job_creation(test_session: AsyncSession):
    """Verify TransferJob lifecycle properties."""
    job = TransferJob(
        owner_id=123456789,
        source_chat_id=-1001111111111,
        source_chat_title="Source Channel",
        destination_chat_id=-1002222222222,
        destination_chat_title="Dest Group",
        destination_thread_id=5,
        topic_name="Announcements",
        content_types="all",
        status=JobStatus.QUEUED.value,
        total_messages=50,
    )
    test_session.add(job)
    await test_session.commit()

    assert job.id is not None
    assert job.status == JobStatus.QUEUED.value
    assert job.destination_thread_id == 5


@pytest.mark.asyncio
async def test_message_mapping_unique_constraint(test_session: AsyncSession):
    """Verify that duplicate message mappings are rejected by unique constraint."""
    mapping1 = MessageMapping(
        source_chat_id=-1001111111111,
        source_message_id=42,
        destination_chat_id=-1002222222222,
        destination_thread_id=5,
        destination_message_id=101,
    )
    test_session.add(mapping1)
    await test_session.commit()

    # Attempt to insert same source and destination mapping
    mapping2 = MessageMapping(
        source_chat_id=-1001111111111,
        source_message_id=42,
        destination_chat_id=-1002222222222,
        destination_thread_id=5,
        destination_message_id=102,
    )
    test_session.add(mapping2)
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()

