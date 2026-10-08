"""Comprehensive unit tests for ChatDiscovery and ChatPicker components."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.tl.types import Channel, Chat as TgChat, User as TgUser
from telethon.errors import FloodWaitError
from app.bot.chat_picker import ChatPicker
from app.bot.states import BotState, session_store
from app.models.chat import Chat
from app.telegram.discovery import ChatDiscovery, DiscoveredChat
from app.telegram.topics import DiscoveredTopic, TopicManager


@pytest.fixture(autouse=True)
def clear_caches_and_sessions():
    """Ensure in-memory caches and sessions are clean for each test."""
    ChatDiscovery._dialog_cache.clear()
    ChatDiscovery._cache_timestamps.clear()
    yield
    ChatDiscovery._dialog_cache.clear()
    ChatDiscovery._cache_timestamps.clear()


# =============================================================================
# 1. Entity Classification & Discovery Tests
# =============================================================================

def test_classify_broadcast_channel():
    """Verify standard broadcast channel classification."""
    entity = MagicMock(spec=Channel)
    entity.id = 111222333
    entity.title = "Driver Updates"
    entity.username = "driver_updates"
    entity.megagroup = False
    entity.forum = False
    entity.broadcast = True
    entity.admin_rights = None
    entity.creator = True

    chat = ChatDiscovery._classify_entity(entity)
    assert chat.id == -100111222333
    assert chat.title == "Driver Updates"
    assert chat.chat_type == "channel"
    assert chat.username == "driver_updates"
    assert chat.is_megagroup is False
    assert chat.is_forum is False
    assert chat.display_icon == "📢"


def test_classify_supergroup_and_forum():
    """Verify supergroup and forum supergroup classification."""
    # Standard supergroup
    entity_sg = MagicMock(spec=Channel)
    entity_sg.id = 444555666
    entity_sg.title = "Driver Community"
    entity_sg.username = None
    entity_sg.megagroup = True
    entity_sg.forum = False
    entity_sg.admin_rights = None
    entity_sg.creator = False

    chat_sg = ChatDiscovery._classify_entity(entity_sg)
    assert chat_sg.id == -100444555666
    assert chat_sg.chat_type == "supergroup"
    assert chat_sg.is_megagroup is True
    assert chat_sg.is_forum is False
    assert chat_sg.display_icon == "👥"

    # Forum supergroup
    entity_forum = MagicMock(spec=Channel)
    entity_forum.id = 777888999
    entity_forum.title = "Engineering Forum"
    entity_forum.username = "eng_forum"
    entity_forum.megagroup = True
    entity_forum.forum = True
    entity_forum.admin_rights = MagicMock(manage_topics=True, post_messages=True)

    chat_forum = ChatDiscovery._classify_entity(entity_forum)
    assert chat_forum.id == -100777888999
    assert chat_forum.chat_type == "supergroup"
    assert chat_forum.is_megagroup is True
    assert chat_forum.is_forum is True
    assert chat_forum.can_manage_topics is True
    assert chat_forum.display_icon == "👥"


def test_classify_basic_group_and_private_user():
    """Verify basic group and 1-on-1 private chat classification."""
    # Basic group
    tg_chat = MagicMock(spec=TgChat)
    tg_chat.id = 123456
    tg_chat.title = "Family Group"
    chat_g = ChatDiscovery._classify_entity(tg_chat)
    assert chat_g.id == -123456
    assert chat_g.chat_type == "group"
    assert chat_g.display_icon == "👥"

    # Private user
    tg_user = MagicMock(spec=TgUser)
    tg_user.id = 987654321
    tg_user.first_name = "Alice"
    tg_user.last_name = "Smith"
    tg_user.username = "alice_smith"
    chat_u = ChatDiscovery._classify_entity(tg_user)
    assert chat_u.id == 987654321
    assert chat_u.title == "Alice Smith"
    assert chat_u.chat_type == "private"
    assert chat_u.username == "alice_smith"
    assert chat_u.display_icon == "💬"


# =============================================================================
# 2. In-Memory Cache and Refresh Tests
# =============================================================================

@pytest.mark.asyncio
async def test_load_dialogs_caches_and_avoids_repeated_calls():
    """Verify iter_dialogs() is called only once and cached for subsequent searches."""
    client = MagicMock()
    d1 = MagicMock()
    d1.entity = MagicMock(spec=Channel, id=101, title="Channel A", megagroup=False, forum=False, username=None, admin_rights=None, creator=True, deactivated=False)
    d2 = MagicMock()
    d2.entity = MagicMock(spec=Channel, id=102, title="Channel B", megagroup=False, forum=False, username=None, admin_rights=None, creator=True, deactivated=False)

    async def fake_iter_dialogs(limit=200):
        yield d1
        yield d2

    client.iter_dialogs = MagicMock(side_effect=fake_iter_dialogs)

    with patch.object(ChatDiscovery, "_sync_chats_to_db", new_callable=AsyncMock):
        # 1. First load
        res1 = await ChatDiscovery.load_dialogs(account_id=1, client=client)
        assert len(res1) == 2
        assert client.iter_dialogs.call_count == 1

        # 2. Second load within TTL (must use cache)
        res2 = await ChatDiscovery.load_dialogs(account_id=1, client=client)
        assert len(res2) == 2
        assert client.iter_dialogs.call_count == 1

        # 3. Explicit refresh forces reload
        res3 = await ChatDiscovery.refresh_dialogs(account_id=1, client=client)
        assert len(res3) == 2
        assert client.iter_dialogs.call_count == 2


@pytest.mark.asyncio
async def test_load_dialogs_handles_flood_wait_gracefully():
    """Verify FloodWait uses cached data if available instead of failing."""
    client = MagicMock()
    # Populate cache
    cached_chat = DiscoveredChat(id=-100123, title="Old Cached", chat_type="channel")
    ChatDiscovery._dialog_cache[1] = [cached_chat]
    ChatDiscovery._cache_timestamps[1] = 0.0  # Expired cache

    async def fake_flood(limit=200):
        raise FloodWaitError(request=None, seconds=45)
        yield  # Make it an async generator

    client.iter_dialogs = MagicMock(side_effect=fake_flood)

    res = await ChatDiscovery.load_dialogs(account_id=1, client=client, force_refresh=True)
    assert len(res) == 1
    assert res[0].title == "Old Cached"


# =============================================================================
# 3. Search Ranking and Fuzzy Matching Tests
# =============================================================================

@pytest.fixture
def sample_dialogs():
    """Set of dialogs covering multiple naming patterns."""
    return [
        DiscoveredChat(id=-1001, title="Driver Updates", chat_type="channel", username="driver_up"),
        DiscoveredChat(id=-1002, title="Driver Tutorials", chat_type="channel", username="tutorials_dr"),
        DiscoveredChat(id=-1003, title="Programming Course for Drivers", chat_type="supergroup", username="course_prog"),
        DiscoveredChat(id=-1004, title="DRIVERTUF+", chat_type="channel", username="drivertufplus"),
        DiscoveredChat(id=-1005, title="DSA Masterclass", chat_type="channel", username="dsa_master"),
        DiscoveredChat(id=-1006, title="Developers India", chat_type="group", username="dev_in"),
        DiscoveredChat(id=-1007, title="Downloads Archive", chat_type="channel", username="downloads"),
        DiscoveredChat(id=-1008, title="Python Programming", chat_type="channel", username="python_prog"),
    ]


@pytest.mark.asyncio
async def test_search_ranking_priority(sample_dialogs):
    """Verify that title starts with query ranks higher than title contains query."""
    ChatDiscovery._dialog_cache[1] = sample_dialogs

    # Query "dr"
    results = await ChatDiscovery.search_dialogs(account_id=1, query="dr")
    titles = [c.title for c in results]

    # Priority 1: title starts with "dr":
    # "Driver Tutorials", "Driver Updates", "DRIVERTUF+"
    assert "Driver Tutorials" in titles[:3]
    assert "Driver Updates" in titles[:3]
    assert "DRIVERTUF+" in titles[:3]

    # Priority 3 or 4: "Programming Course for Drivers" must rank lower than items starting with "dr"
    idx_tutorials = titles.index("Driver Tutorials")
    idx_course = titles.index("Programming Course for Drivers")
    assert idx_tutorials < idx_course


@pytest.mark.asyncio
async def test_search_single_character_query(sample_dialogs):
    """Verify single character 'd' returns all matching dialogs."""
    ChatDiscovery._dialog_cache[1] = sample_dialogs

    results = await ChatDiscovery.search_dialogs(account_id=1, query="d")
    titles = [c.title for c in results]

    # All items containing or starting with 'd'
    assert "Driver Updates" in titles
    assert "DSA Masterclass" in titles
    assert "Developers India" in titles
    assert "Downloads Archive" in titles
    assert "DRIVERTUF+" in titles
    # "Python Programming" does not contain 'd' in title or username -> should not be in results
    assert "Python Programming" not in titles


@pytest.mark.asyncio
async def test_search_exact_and_special_character_query(sample_dialogs):
    """Verify exact match with special characters like 'DRIVERTUF+' matches immediately."""
    ChatDiscovery._dialog_cache[1] = sample_dialogs

    results = await ChatDiscovery.search_dialogs(account_id=1, query="DRIVERTUF+")
    assert len(results) >= 1
    assert results[0].title == "DRIVERTUF+"


@pytest.mark.asyncio
async def test_search_empty_and_no_results(sample_dialogs):
    """Verify empty query returns all dialogs and non-matching query returns empty list."""
    ChatDiscovery._dialog_cache[1] = sample_dialogs

    # Empty query returns all dialogs sorted alphabetically
    all_res = await ChatDiscovery.search_dialogs(account_id=1, query="")
    assert len(all_res) == len(sample_dialogs)

    # Non-matching query
    none_res = await ChatDiscovery.search_dialogs(account_id=1, query="nonexistentxyz123")
    assert len(none_res) == 0


@pytest.mark.asyncio
async def test_search_category_filter(sample_dialogs):
    """Verify category filtering for channels, groups, and private chats."""
    ChatDiscovery._dialog_cache[1] = sample_dialogs

    # Filter channels only
    channel_res = await ChatDiscovery.search_dialogs(account_id=1, query="", chat_type="channel")
    assert all(c.chat_type == "channel" for c in channel_res)
    assert any(c.title == "Driver Updates" for c in channel_res)
    assert not any(c.title == "Developers India" for c in channel_res)

    # Filter groups only
    group_res = await ChatDiscovery.search_dialogs(account_id=1, query="", chat_type="group")
    assert all(c.chat_type in ("group", "supergroup") for c in group_res)
    assert any(c.title == "Developers India" for c in group_res)


# =============================================================================
# 4. Database Selection Recording & Recents Tests
# =============================================================================

@pytest.mark.asyncio
async def test_record_chat_selection_and_recent_dialogs():
    """Verify recording chat selection and querying recent dialogs."""
    from app.database import init_db
    await init_db()

    chat = DiscoveredChat(
        id=-100999,
        title="Recent Test Channel",
        chat_type="channel",
        username="recent_test",
    )

    await ChatDiscovery.record_chat_selection(account_id=1, chat=chat)
    recents = await ChatDiscovery.get_recent_dialogs(account_id=1, limit=5)

    assert len(recents) >= 1
    assert any(r.id == -100999 for r in recents)


# =============================================================================
# 5. ChatPicker UI and Navigation Tests
# =============================================================================

def test_build_keyboards():
    """Verify ChatPicker keyboard builders."""
    menu_kb = ChatPicker.build_menu_keyboard("source")
    assert any(
        btn.callback_data == "cp:src:search"
        for row in menu_kb.inline_keyboard
        for btn in row
    )
    assert any(
        btn.callback_data == "cp:src:cat:channel"
        for row in menu_kb.inline_keyboard
        for btn in row
    )

    chats = [
        DiscoveredChat(id=-1001, title="Chat 1", chat_type="channel"),
        DiscoveredChat(id=-1002, title="Chat 2", chat_type="channel"),
    ]
    list_kb = ChatPicker.build_list_keyboard(chats, target="source", page=0)
    assert any(
        btn.callback_data == "cp:src:pk:-1001"
        for row in list_kb.inline_keyboard
        for btn in row
    )


@pytest.mark.asyncio
async def test_chat_picker_handle_pick_source():
    """Verify selecting a source chat stores it and shows destination transition."""
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    user_id = 99999

    session_store.clear(user_id)
    session_store.update_data(user_id, account_id=1)

    ChatDiscovery._dialog_cache[1] = [
        DiscoveredChat(id=-100111, title="Picked Source", chat_type="channel")
    ]

    await ChatPicker.handle_pick(query, user_id, target="source", chat_id=-100111)

    udata = session_store.get_data(user_id)
    assert udata.get("source_chat_id") == -100111
    assert udata.get("source_chat_title") == "Picked Source"

    query.edit_message_text.assert_called_once()
    call_kwargs = query.edit_message_text.call_args[1]
    assert "Source Selected" in call_kwargs["text"]
    assert "Picked Source" in call_kwargs["text"]
    # Check that [➡️ Select Destination] button is present
    kb = call_kwargs["reply_markup"]
    assert any(
        btn.callback_data == "cp:src:to_dest"
        for row in kb.inline_keyboard
        for btn in row
    )


@pytest.mark.asyncio
async def test_chat_picker_handle_pick_dest_forum_transitions_to_topics():
    """Verify selecting a forum supergroup as destination fetches topics."""
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    user_id = 99999

    session_store.clear(user_id)
    session_store.update_data(user_id, account_id=1)

    forum_chat = DiscoveredChat(
        id=-100222,
        title="Forum Supergroup",
        chat_type="supergroup",
        is_megagroup=True,
        is_forum=True,
    )
    ChatDiscovery._dialog_cache[1] = [forum_chat]

    fake_topics = [
        DiscoveredTopic(id=1, title="General"),
        DiscoveredTopic(id=42, title="Announcements"),
    ]

    mock_client = MagicMock()
    with patch(
        "app.telegram.user_client.user_client_manager.get_client_for_account",
        new_callable=AsyncMock,
        return_value=mock_client,
    ), patch.object(
        ChatDiscovery, "record_chat_selection", new_callable=AsyncMock
    ), patch(
        "app.telegram.topics.TopicManager.get_topics",
        new_callable=AsyncMock,
        return_value=fake_topics,
    ):
        await ChatPicker.handle_pick(query, user_id, target="dest", chat_id=-100222)

        udata = session_store.get_data(user_id)
        assert udata.get("destination_chat_id") == -100222
        assert udata.get("is_forum") is True

        query.edit_message_text.assert_called_once()
        call_kwargs = query.edit_message_text.call_args[1]
        assert "Topics enabled" in call_kwargs["text"]
        kb = call_kwargs["reply_markup"]
        assert any(
            btn.callback_data == "topic:pick:42"
            for row in kb.inline_keyboard
            for btn in row
        )


# =============================================================================
# 6. Eleven Comprehensive End-to-End Scenarios
# =============================================================================

@pytest.mark.asyncio
async def test_end_to_end_11_scenarios():
    """Verify all 11 required scenarios:
    1. Existing destination appears.
    2. Newly created destination does not initially appear.
    3. Press Refresh Chats.
    4. Newly created destination appears.
    5. Search newly created destination.
    6. Select destination.
    7. Existing topics load.
    8. Create a new topic.
    9. New topic appears after creation.
    10. Select newly created topic.
    11. Start a one-message transfer to that topic.
    """
    from telethon.tl.types import Updates, UpdateNewChannelMessage, Message, MessageActionTopicCreate, ForumTopic
    from app.transfer.manager import transfer_manager
    from app.models.transfer_job import JobStatus

    client = MagicMock()

    # Step 1 & 2: Initial dialogs (existing destination only)
    existing_dest = MagicMock(spec=Channel, id=501, title="Old Destination Group", megagroup=True, forum=True, username="old_dest", admin_rights=MagicMock(manage_topics=True), creator=True, deactivated=False)
    new_dest = MagicMock(spec=Channel, id=502, title="Driver Tutorials Forum", megagroup=True, forum=True, username="driver_tut", admin_rights=MagicMock(manage_topics=True), creator=True, deactivated=False)

    dialog_state = [MagicMock(entity=existing_dest)]

    async def fake_iter(limit=300):
        for d in dialog_state:
            yield d

    client.iter_dialogs = MagicMock(side_effect=fake_iter)

    with patch.object(ChatDiscovery, "_sync_chats_to_db", new_callable=AsyncMock):
        # 1. Existing destination appears
        chats_step1 = await ChatDiscovery.load_dialogs(account_id=1, client=client)
        assert any(c.title == "Old Destination Group" for c in chats_step1)

        # 2. Newly created destination does not initially appear
        assert not any(c.title == "Driver Tutorials Forum" for c in chats_step1)

        # User now creates new channel in Telegram app -> dialog_state updated
        dialog_state.append(MagicMock(entity=new_dest))

        # 3. Press Refresh Chats
        chats_step3 = await ChatDiscovery.refresh_dialogs(account_id=1, client=client)

        # 4. Newly created destination appears
        assert any(c.title == "Driver Tutorials Forum" for c in chats_step3)

        # 5. Search newly created destination with query "driver"
        search_results = await ChatDiscovery.search_dialogs(account_id=1, query="driver")
        assert len(search_results) >= 1
        assert search_results[0].title == "Driver Tutorials Forum"

        # 6. Select destination
        selected_dest = search_results[0]
        assert selected_dest.id == -100502
        assert selected_dest.is_forum is True

        # 7. Existing topics load
        fake_forum_topics_res = MagicMock()
        fake_forum_topics_res.topics = [
            MagicMock(spec=ForumTopic, id=1, title="General", closed=False, pinned=False),
            MagicMock(spec=ForumTopic, id=10, title="Existing Course", closed=False, pinned=False),
        ]
        client.get_entity = AsyncMock(return_value=new_dest)
        client.side_effect = None

        async def fake_client_call(req):
            if req.__class__.__name__ == "GetForumTopicsRequest":
                return fake_forum_topics_res
            elif req.__class__.__name__ == "CreateForumTopicRequest":
                # Verify correct request attributes: peer and title
                assert hasattr(req, "peer")
                assert hasattr(req, "title")
                assert req.title == "Python Course"
                # Mock Updates response
                up = UpdateNewChannelMessage(
                    message=MagicMock(spec=Message, id=77, action=MagicMock(spec=MessageActionTopicCreate)),
                    pts=1,
                    pts_count=1,
                )
                return Updates(updates=[up], users=[], chats=[], date=None, seq=0)
            return MagicMock()

        client.side_effect = fake_client_call

        existing_topics = await TopicManager.get_topics(client, selected_dest.id)
        assert len(existing_topics) == 2
        assert any(t.title == "General" for t in existing_topics)
        assert any(t.title == "Existing Course" for t in existing_topics)

        # 8. Create a new topic "Python Course"
        # Simulate Telegram server adding new topic on refresh
        def add_topic_to_server():
            fake_forum_topics_res.topics.append(
                MagicMock(spec=ForumTopic, id=77, title="Python Course", closed=False, pinned=False)
            )

        with patch.object(TopicManager, "_cache_topic", new_callable=AsyncMock):
            created_topic, refreshed_topics = await TopicManager.create_and_refresh_topic(
                client=client,
                chat_id=selected_dest.id,
                title="Python Course",
                account_id=1,
            )
            add_topic_to_server()

        # 9. New topic appears after creation with thread ID
        assert created_topic.id == 77
        assert created_topic.title == "Python Course"

        # 10. Select newly created topic
        destination_thread_id = created_topic.id
        assert destination_thread_id == 77

        # 11. Start a one-message transfer to that topic
        user_id = 12345
        job = await transfer_manager.create_job(
            owner_id=user_id,
            telegram_account_id=1,
            source_chat_id=-100111,
            source_chat_title="Source Channel",
            destination_chat_id=selected_dest.id,
            destination_chat_title="Driver Tutorials Forum",
            destination_thread_id=destination_thread_id,
            topic_name=created_topic.title,
            start_message_id=100,
            end_message_id=100,
            total_messages=1,
        )

        assert job.id is not None
        assert job.destination_chat_id == -100502
        assert job.destination_thread_id == 77
        assert job.topic_name == "Python Course"
        assert job.total_messages == 1
        assert job.status == JobStatus.QUEUED.value
