"""Unit and integration tests for Message Browsing, Range Selection, and Transfer Execution."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputMessagesFilterVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telegram import InlineKeyboardMarkup

from app.bot.callbacks import _handle_browse_action, _show_transfer_preview
from app.bot.handlers import text_message_handler
from app.bot.message_browser import MessageBrowser
from app.bot.states import BotState, session_store
from app.models.transfer_job import JobStatus, TransferJob
from app.transfer.copier import MessageCopier
from app.transfer.manager import TransferManager
from app.transfer.worker import TransferWorker


@pytest.fixture(autouse=True)
def clean_session():
    """Ensure session store is clean before and after each test."""
    session_store.clear(999)
    session_store.clear(1001)
    with patch("app.config.Settings.is_admin", return_value=True):
        yield
    session_store.clear(999)
    session_store.clear(1001)


def make_mock_message(
    msg_id: int,
    text: str = "",
    media_type: str = "text",
    filename: str | None = None,
    dt: datetime | None = None,
) -> Message:
    """Helper to construct mock Telethon messages."""
    msg = MagicMock(spec=Message)
    msg.id = msg_id
    msg.empty = False
    msg.action = None
    msg.message = text
    msg.text = text
    msg.date = dt or datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)
    msg.reply_to = None

    if media_type == "video":
        msg.media = MagicMock(spec=MessageMediaDocument)
        msg.video = MagicMock()
        msg.photo = None
        msg.audio = None
        msg.voice = None
        msg.gif = None
        doc = MagicMock(spec=Document)
        attr = MagicMock(spec=DocumentAttributeVideo)
        attrs = [attr]
        if filename:
            fn_attr = MagicMock(spec=DocumentAttributeFilename)
            fn_attr.file_name = filename
            attrs.append(fn_attr)
        doc.attributes = attrs
        msg.media.document = doc
        msg.document = doc
        msg.file = MagicMock()
        msg.file.name = filename
        msg.file.size = 10485760
    elif media_type == "photo":
        msg.media = MagicMock(spec=MessageMediaPhoto)
        msg.video = None
        msg.photo = MagicMock()
        msg.audio = None
        msg.voice = None
        msg.gif = None
        msg.document = None
        msg.file = MagicMock()
        msg.file.name = filename
        msg.file.size = 204800
    elif media_type == "document":
        msg.media = MagicMock(spec=MessageMediaDocument)
        msg.video = None
        msg.photo = None
        msg.audio = None
        msg.voice = None
        msg.gif = None
        doc = MagicMock(spec=Document)
        attrs = []
        if filename:
            fn_attr = MagicMock(spec=DocumentAttributeFilename)
            fn_attr.file_name = filename
            attrs.append(fn_attr)
        doc.attributes = attrs
        msg.media.document = doc
        msg.document = doc
        msg.file = MagicMock()
        msg.file.name = filename
        msg.file.size = 512000
    else:
        msg.media = None
        msg.video = None
        msg.photo = None
        msg.audio = None
        msg.voice = None
        msg.gif = None
        msg.document = None
        msg.file = None

    return msg


# =============================================================================
# 1. Message Browser Display and Metadata
# =============================================================================

@pytest.mark.asyncio
async def test_message_browser_display_page():
    """Verify MessageBrowser displays 25 messages with ID, video icon, filename, date, text in increasing order."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    # Generate 25 sample messages
    messages = [
        make_mock_message(
            1200 + i,
            text=f"Tutorial #{i + 1} details",
            media_type="video" if i % 2 == 0 else "document",
            filename=f"file_{i}.mp4" if i % 2 == 0 else f"notes_{i}.pdf",
        )
        for i in range(25)
    ]
    mock_client.get_messages = AsyncMock(return_value=messages)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        await MessageBrowser.show_browser(query, user_id, reset=True)

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args[1]
    rendered_text = call_kwargs["text"]
    reply_markup = call_kwargs["reply_markup"]

    # Verify ID, video icon, and filename
    assert "#1200" in rendered_text
    assert "#1224" in rendered_text
    assert "📹 [VIDEO]" in rendered_text
    assert "file_0.mp4" in rendered_text
    assert "📄 Doc" in rendered_text
    assert "notes_1.pdf" in rendered_text
    assert "Browse Messages — Page 1" in rendered_text
    assert "Oldest First" in rendered_text

    # Verify navigation buttons
    btn_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "🔄 Refresh" in btn_texts
    assert "Next ▶️" in btn_texts
    assert "🎬 Video: OFF" in btn_texts
    assert "⬆️ Oldest First" in btn_texts
    assert "🔍 Search" in btn_texts
    assert "🔢 Jump to ID" in btn_texts
    assert "🎯 Select Range" in btn_texts


# =============================================================================
# 2. Pagination Navigation (Next, Prev, Refresh)
# =============================================================================

@pytest.mark.asyncio
async def test_message_browser_pagination_next_and_prev():
    """Verify Next page advances offset stack and Prev page pops it in increasing order."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    # 25 messages per page in increasing order
    page_0_messages = [make_mock_message(1 + i) for i in range(25)]
    page_1_messages = [make_mock_message(26 + i) for i in range(25)]

    mock_client.get_messages = AsyncMock(side_effect=[page_0_messages, page_1_messages, page_0_messages])
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        # 1. Initial Page 0
        await MessageBrowser.show_browser(query, user_id, reset=True)
        assert session_store.get_data(user_id)["browse_page"] == 0
        assert session_store.get_data(user_id)["browse_offset_id"] == 0

        # 2. Next Page (advances to messages > max_id of page 0, which is 25)
        await MessageBrowser.handle_next(query, user_id)
        udata = session_store.get_data(user_id)
        assert udata["browse_page"] == 1
        assert udata["browse_offset_id"] == 25
        assert udata["browse_offset_stack"] == [0, 25]

        # 3. Prev Page
        await MessageBrowser.handle_prev(query, user_id)
        udata = session_store.get_data(user_id)
        assert udata["browse_page"] == 0
        assert udata["browse_offset_id"] == 0
        assert udata["browse_offset_stack"] == [0]


# =============================================================================
# 3. Video-Only Filtering
# =============================================================================

@pytest.mark.asyncio
async def test_message_browser_video_only_filter():
    """Verify video-only filter toggles, queries Telethon with video filter, preserves IDs."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
        browse_video_only=False,
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    # 3 video messages with non-sequential IDs
    video_messages = [
        make_mock_message(1210, "Vid 1", "video", "vid1.mp4"),
        make_mock_message(1230, "Vid 2", "video", "vid2.mp4"),
        make_mock_message(1250, "Vid 3", "video", "vid3.mp4"),
    ]
    mock_client.get_messages = AsyncMock(return_value=video_messages)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        # Toggle Video Only ON
        await MessageBrowser.toggle_video(query, user_id)

    udata = session_store.get_data(user_id)
    assert udata["browse_video_only"] is True
    assert udata["content_types"] == ["video"]

    # Verify Telethon was called with InputMessagesFilterVideo
    called_kwargs = mock_client.get_messages.call_args[1]
    assert isinstance(called_kwargs.get("filter"), InputMessagesFilterVideo)

    # Verify original message IDs preserved without renumbering
    rendered_text = query.edit_message_text.call_args[1]["text"]
    assert "#1210" in rendered_text
    assert "#1230" in rendered_text
    assert "#1250" in rendered_text


# =============================================================================
# 4. Jump to Message ID
# =============================================================================

@pytest.mark.asyncio
async def test_message_browser_jump_to_id():
    """Verify jumping to message ID sets offset_id appropriately for asc order."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.get_messages = AsyncMock(return_value=[make_mock_message(1245)])
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        await MessageBrowser.show_browser(query, user_id, jump_id=1245)

    called_kwargs = mock_client.get_messages.call_args[1]
    # In asc order, offset_id is jump_id - 1 so Telethon starts right at jump_id
    assert called_kwargs["offset_id"] == 1244
    assert session_store.get_data(user_id)["browse_offset_id"] == 1244


@pytest.mark.asyncio
async def test_message_browser_order_toggle():
    """Verify toggling sort order flips between ascending and descending."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
        browse_order="asc",
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.get_messages = AsyncMock(return_value=[make_mock_message(100)])
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        # Toggle from asc to desc
        await MessageBrowser.toggle_order(query, user_id)
        assert session_store.get_data(user_id)["browse_order"] == "desc"

        # Toggle from desc back to asc
        await MessageBrowser.toggle_order(query, user_id)
        assert session_store.get_data(user_id)["browse_order"] == "asc"


# =============================================================================
# 5. Search Messages & Clear Search
# =============================================================================

@pytest.mark.asyncio
async def test_message_browser_search_and_clear():
    """Verify search queries Telethon with search term and clear resets filter."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100123456789,
        source_chat_title="Python Tutorials",
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.get_messages = AsyncMock(return_value=[make_mock_message(1245, "Docker Tutorial")])
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        # 1. Search "Docker"
        await MessageBrowser.show_browser(query, user_id, search_query="Docker")
        called_kwargs = mock_client.get_messages.call_args[1]
        assert called_kwargs["search"] == "Docker"
        assert session_store.get_data(user_id)["browse_search_query"] == "Docker"

        # 2. Clear Search
        await MessageBrowser.clear_search(query, user_id)
        assert session_store.get_data(user_id)["browse_search_query"] is None


# =============================================================================
# 6. Range Input Parsing (Single ID, Range with hyphen, Range with space)
# =============================================================================

@pytest.mark.asyncio
async def test_range_input_single_id():
    """Verify entering a single ID e.g. 1245 sets start_id=1245, end_id=1245."""
    user_id = 999
    session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
    session_store.update_data(
        user_id,
        source_chat_id=-100111,
        destination_chat_id=-100222,
    )

    msg = AsyncMock()
    msg.text = "1245"
    msg.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    update.callback_query = None
    update.effective_message = msg
    update.message = msg

    await text_message_handler(update, MagicMock())

    udata = session_store.get_data(user_id)
    assert udata["start_message_id"] == 1245
    assert udata["end_message_id"] == 1245
    assert udata["range_type"] == "single"
    msg.reply_text.assert_called_once()
    assert "Single message set: Message #1245" in msg.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_range_input_range_hyphen():
    """Verify entering 1240-1250 sets start_id=1240, end_id=1250."""
    user_id = 999
    session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
    session_store.update_data(
        user_id,
        source_chat_id=-100111,
        destination_chat_id=-100222,
    )

    msg = AsyncMock()
    msg.text = "1240-1250"
    msg.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    update.callback_query = None
    update.effective_message = msg
    update.message = msg

    await text_message_handler(update, MagicMock())

    udata = session_store.get_data(user_id)
    assert udata["start_message_id"] == 1240
    assert udata["end_message_id"] == 1250
    assert udata["range_type"] == "custom"
    assert "Messages 1240 → 1250" in msg.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_range_input_range_space():
    """Verify entering '1240 1250' sets start_id=1240, end_id=1250."""
    user_id = 999
    session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
    session_store.update_data(
        user_id,
        source_chat_id=-100111,
        destination_chat_id=-100222,
    )

    msg = AsyncMock()
    msg.text = "1240 1250"
    msg.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    update.callback_query = None
    update.effective_message = msg
    update.message = msg

    await text_message_handler(update, MagicMock())

    udata = session_store.get_data(user_id)
    assert udata["start_message_id"] == 1240
    assert udata["end_message_id"] == 1250


# =============================================================================
# 7. Transfer Preview Calculation (Accessible and Video Counts)
# =============================================================================

@pytest.mark.asyncio
async def test_transfer_preview_with_message_counts():
    """Verify preview calculates accessible message count and video count."""
    user_id = 999
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100111,
        source_chat_title="My Course Channel",
        destination_chat_id=-100222,
        destination_chat_title="Target Group",
        destination_thread_id=5,
        topic_name="Tutorials Topic",
        start_message_id=1240,
        end_message_id=1250,
        browse_video_only=True,
    )

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    # 11 messages in range, 5 of which are videos
    range_messages = [
        make_mock_message(1240 + i, media_type="video" if i % 2 == 0 else "text")
        for i in range(11)
    ]

    async def mock_iter_messages(*args, **kwargs):
        for m in range_messages:
            yield m

    mock_client.iter_messages = mock_iter_messages
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client):
        await _show_transfer_preview(query, user_id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[1]["text"]

    assert "My Course Channel" in text
    assert "Target Group" in text
    assert "Tutorials Topic" in text
    assert "Messages 1240 → 1250" in text
    assert "Accessible Messages Found: *11*" in text
    assert "Video Messages Found: *6*" in text


# =============================================================================
# 8. Single-ID and Range Transfer Execution
# =============================================================================

@pytest.mark.asyncio
async def test_single_id_transfer_execution(tmp_path):
    """Verify worker only transfers the single requested message ID."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    import app.transfer.worker as tw_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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

    tw_module.get_session = mock_get_session

    async with mock_get_session() as session:
        job = TransferJob(
            owner_id=999,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Source",
            destination_chat_id=-100222,
            destination_chat_title="Dest",
            start_message_id=1245,
            end_message_id=1245,
            status=JobStatus.QUEUED.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    # Stream includes out-of-range IDs to test range boundary enforcement
    messages_stream = [
        make_mock_message(1244, text="Wrong"),
        make_mock_message(1245, text="Target Single Message"),
        make_mock_message(1246, text="Wrong"),
    ]

    async def mock_iter(*args, **kwargs):
        for m in messages_stream:
            yield m

    mock_client.iter_messages = mock_iter

    worker = TransferWorker()
    copied_ids = []

    async def mock_copy_message(*args, **kwargs):
        msg = kwargs.get("message") or args[2]
        copied_ids.append(msg.id)
        return msg.id + 1000

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client), \
         patch.object(MessageCopier, "copy_message", side_effect=mock_copy_message):
        await worker.execute_job(job_id)

    # STRICT verification: Only 1245 transferred
    assert copied_ids == [1245]

    async with mock_get_session() as session:
        from sqlalchemy import select
        res = await session.execute(select(TransferJob).where(TransferJob.id == job_id))
        completed_job = res.scalar_one()
        assert completed_job.status == JobStatus.COMPLETED.value
        assert completed_job.successful_messages == 1


@pytest.mark.asyncio
async def test_range_transfer_execution():
    """Verify worker transfers strictly messages within requested ID range [1240, 1245]."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    import app.transfer.worker as tw_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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

    tw_module.get_session = mock_get_session

    async with mock_get_session() as session:
        job = TransferJob(
            owner_id=999,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Source",
            destination_chat_id=-100222,
            destination_chat_title="Dest",
            start_message_id=1240,
            end_message_id=1245,
            status=JobStatus.QUEUED.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    # Stream includes messages before, inside, and after range
    messages_stream = [
        make_mock_message(1238),
        make_mock_message(1240),
        make_mock_message(1242),
        make_mock_message(1245),
        make_mock_message(1248),
    ]

    async def mock_iter(*args, **kwargs):
        for m in messages_stream:
            yield m

    mock_client.iter_messages = mock_iter

    worker = TransferWorker()
    copied_ids = []

    async def mock_copy_message(*args, **kwargs):
        msg = kwargs.get("message") or args[2]
        copied_ids.append(msg.id)
        return msg.id + 1000

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client), \
         patch.object(MessageCopier, "copy_message", side_effect=mock_copy_message):
        await worker.execute_job(job_id)

    # Strictly 1240, 1242, 1245 transferred; 1238 and 1248 skipped
    assert copied_ids == [1240, 1242, 1245]


# =============================================================================
# 9. Forum Topic Destination Transfer
# =============================================================================

@pytest.mark.asyncio
async def test_forum_topic_destination_transfer():
    """Verify transferring into a destination forum topic sets destination_thread_id."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    import app.transfer.worker as tw_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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

    tw_module.get_session = mock_get_session

    async with mock_get_session() as session:
        job = TransferJob(
            owner_id=999,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Source",
            destination_chat_id=-100999,
            destination_chat_title="Forum Dest",
            destination_thread_id=77,
            topic_name="Announcements",
            start_message_id=10,
            end_message_id=10,
            status=JobStatus.QUEUED.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    async def mock_iter(*args, **kwargs):
        yield make_mock_message(10, text="Forum message")

    mock_client.iter_messages = mock_iter

    worker = TransferWorker()
    passed_thread_ids = []

    async def mock_copy_message(*args, **kwargs):
        passed_thread_ids.append(kwargs.get("destination_thread_id"))
        return 9999

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client), \
         patch.object(MessageCopier, "copy_message", side_effect=mock_copy_message):
        await worker.execute_job(job_id)

    assert passed_thread_ids == [77]


# =============================================================================
# 10. Browse Callback Query Routing
# =============================================================================

@pytest.mark.asyncio
async def test_browse_callbacks_routing():
    """Verify browse callback actions update states and prompt user correctly."""
    user_id = 999
    session_store.update_data(
        user_id,
        source_chat_id=-100111,
        source_chat_title="My Channel",
    )

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    # 1. Jump callback
    await _handle_browse_action(query, user_id, "browse:jump")
    assert session_store.get_state(user_id) == BotState.WIZARD_BROWSE_JUMP
    assert "Jump to Message ID" in query.edit_message_text.call_args[1]["text"]

    # 2. Search callback
    await _handle_browse_action(query, user_id, "browse:search")
    assert session_store.get_state(user_id) == BotState.WIZARD_BROWSE_SEARCH
    assert "Search Messages" in query.edit_message_text.call_args[1]["text"]

    # 3. Enter Range callback
    await _handle_browse_action(query, user_id, "browse:enter_range")
    assert session_store.get_state(user_id) == BotState.WIZARD_RANGE_INPUT
    assert "Enter Message ID or Range" in query.edit_message_text.call_args[1]["text"]

    # 4. Back callback
    await _handle_browse_action(query, user_id, "browse:back")
    text = query.edit_message_text.call_args[1]["text"]
    assert "Source Selected" in text
    assert "My Channel" in text
    kb = query.edit_message_text.call_args[1]["reply_markup"]
    btn_labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "📂 Browse Messages" in btn_labels
    assert "🔢 Enter Message ID/Range" in btn_labels


# =============================================================================
# 11. Inaccessible / Restricted Messages Handling
# =============================================================================

@pytest.mark.asyncio
async def test_inaccessible_messages_reported_as_failed():
    """Verify inaccessible messages are recorded as failed and never marked as successful."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import Base
    from app.models.transfer_error import TransferError
    from app.transfer.copier import NonRetryableTransferError
    import app.transfer.worker as tw_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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

    tw_module.get_session = mock_get_session

    async with mock_get_session() as session:
        job = TransferJob(
            owner_id=999,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Restricted Source",
            destination_chat_id=-100222,
            destination_chat_title="Dest",
            start_message_id=50,
            end_message_id=50,
            status=JobStatus.QUEUED.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_entity = AsyncMock(return_value=MagicMock())

    async def mock_iter(*args, **kwargs):
        yield make_mock_message(50, text="Restricted Content")

    mock_client.iter_messages = mock_iter

    worker = TransferWorker()

    error_reason = "Telegram did not make the media available to this account."

    async def mock_copy_message_fail(*args, **kwargs):
        raise NonRetryableTransferError(error_reason)

    with patch("app.telegram.user_client.user_client_manager.get_active_client", return_value=mock_client), \
         patch.object(MessageCopier, "copy_message", side_effect=mock_copy_message_fail):
        await worker.execute_job(job_id)

    async with mock_get_session() as session:
        res = await session.execute(select(TransferJob).where(TransferJob.id == job_id))
        completed_job = res.scalar_one()

        # Job must NOT mark failed message as successful!
        assert completed_job.successful_messages == 0
        assert completed_job.failed_messages == 1
        assert "50" in (completed_job.failed_message_ids or "")

        err_res = await session.execute(select(TransferError).where(TransferError.transfer_job_id == job_id))
        errors = err_res.scalars().all()
        assert len(errors) == 1
        assert errors[0].source_message_id == 50
        assert errors[0].error_type == "NonRetryableTransferError"
        assert error_reason in errors[0].error_message


@pytest.mark.asyncio
async def test_browse_state_direct_range_input():
    """Verify user can type range directly while in WIZARD_BROWSE_MESSAGES state."""
    user_id = 999
    session_store.set_state(user_id, BotState.WIZARD_BROWSE_MESSAGES)
    session_store.update_data(
        user_id,
        account_id=1,
        source_chat_id=-100111,
        destination_chat_id=-100222,
    )

    message = MagicMock()
    message.reply_text = AsyncMock()
    message.from_user.id = user_id
    message.chat.id = user_id
    message.text = "#10-#25"

    context = MagicMock()

    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    update.effective_user.is_bot = False
    update.effective_chat.id = user_id
    update.message = message
    update.effective_message = message

    await text_message_handler(update, context)

    udata = session_store.get_data(user_id)
    assert udata["start_message_id"] == 10
    assert udata["end_message_id"] == 25
    assert udata["range_type"] == "custom"
    message.reply_text.assert_called_once()
    assert "Messages 10 → 25" in message.reply_text.call_args[0][0]

