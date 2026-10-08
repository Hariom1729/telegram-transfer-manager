"""Comprehensive tests for OS-aware download directories, collision prevention,
1-character suggestion search, ranking, 15-result pagination, and isolated search state.
"""

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.tl.custom.message import Message
from telethon.tl.types import Channel

from app.bot.chat_picker import ChatPicker, PAGE_SIZE, RESULTS_PER_PAGE
from app.bot.states import session_store
from app.telegram.discovery import ChatDiscovery, DiscoveredChat
from app.transfer.copier import MessageCopier
from app.utils.paths import (
    ensure_download_directory,
    get_download_directory,
    get_temp_download_directory,
    get_unique_filepath,
    sanitize_filename,
    sanitize_folder_name,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure in-memory caches and test user session are clear."""
    ChatDiscovery._dialog_cache.clear()
    ChatDiscovery._cache_timestamps.clear()
    yield
    ChatDiscovery._dialog_cache.clear()
    ChatDiscovery._cache_timestamps.clear()


# =============================================================================
# TEST 1: Download Path, Temp Storage, Filename Sanitization & Collision Prevention
# =============================================================================

def test_download_directory_and_temp_directory_not_in_project():
    """Verify download directory is in user's OS Downloads folder and temp is in OS temp dir."""
    dl_dir = get_download_directory()
    temp_dir = get_temp_download_directory()

    # Must be absolute paths
    assert dl_dir.is_absolute()
    assert temp_dir.is_absolute()

    # Must be ~/Downloads on macOS/Linux
    assert str(dl_dir).endswith("Downloads")

    # Temp dir must be inside system temp directory, NOT in project root
    sys_temp = Path(tempfile.gettempdir())
    assert sys_temp in temp_dir.parents or temp_dir == sys_temp


def test_filename_sanitization_and_collision_prevention(tmp_path):
    """Verify preserving valid characters/underscores, sanitizing invalid chars, and avoiding overwrite."""
    # 1. Sanitization
    assert sanitize_filename("my_video:1080p?.mp4") == "my_video_1080p_.mp4"
    assert sanitize_filename("regular_video.mp4") == "regular_video.mp4"
    assert sanitize_filename("") == "unnamed_file"

    # 2. Collision prevention (video.mp4 -> video (1).mp4 -> video (2).mp4)
    file1 = get_unique_filepath(tmp_path, "video.mp4")
    assert file1.name == "video.mp4"
    file1.write_text("v1")

    file2 = get_unique_filepath(tmp_path, "video.mp4")
    assert file2.name == "video (1).mp4"
    file2.write_text("v2")

    file3 = get_unique_filepath(tmp_path, "video.mp4")
    assert file3.name == "video (2).mp4"


@pytest.mark.asyncio
async def test_transfer_temporary_files_are_cleaned_up(tmp_path):
    """Verify temporary stream files used during transfer are deleted upon completion."""
    client = MagicMock()

    async def mock_download_media(msg, file):
        p = Path(file)
        p.write_bytes(b"downloaded")
        return str(p)

    client.download_media = mock_download_media
    client.send_file = AsyncMock(return_value=MagicMock(id=999))
    # Fail direct send to trigger fallback
    client.send_file.side_effect = [Exception("Direct send disallowed"), MagicMock(id=999)]
    client.forward_messages = AsyncMock(side_effect=Exception("Forward disallowed"))

    msg = MagicMock(spec=Message)
    msg.id = 55
    msg.chat_id = -100111
    msg.media = MagicMock()
    msg.message = "Restricted Channel Media"
    msg.entities = None
    msg.document = None

    with patch("app.transfer.copier.get_temp_download_directory", return_value=tmp_path):
        res_id = await MessageCopier.copy_message(client, destination_entity=-100222, message=msg)
        assert res_id == 999

    # Verify no temporary files remain in the temp directory
    remaining = [p for p in tmp_path.iterdir() if p.is_file()]
    assert len(remaining) == 0


# =============================================================================
# TEST 2 & 3 & 4: 1-Character Search, Partial Matches, Both Title & Username, Ranking
# =============================================================================

@pytest.fixture
def sample_chats():
    return [
        DiscoveredChat(id=-1001, title="Python Course", chat_type="channel", username="python_course"),
        DiscoveredChat(id=-1002, title="Python Developers", chat_type="channel", username="py_devs"),
        DiscoveredChat(id=-1003, title="DevOps Community", chat_type="group", username="devops_hub"),
        DiscoveredChat(id=-1004, title="Developer Hub", chat_type="supergroup", username="developer_hub"),
        DiscoveredChat(id=-1005, title="Learn Python", chat_type="channel", username="learn_py"),
        DiscoveredChat(id=-1006, title="App Architecture", chat_type="channel", username="app_arch"),
        DiscoveredChat(id=-1007, title="Asian Cooking Recipes", chat_type="channel", username="asian_cooking"),
        DiscoveredChat(id=-1008, title="Apple Fans", chat_type="channel", username="apple_fans"),
    ]


@pytest.mark.asyncio
async def test_search_single_character_query(sample_chats):
    """TEST 2: Verify single character 'a' returns all chats containing 'a' in title or username."""
    ChatDiscovery._dialog_cache[1] = sample_chats

    res = await ChatDiscovery.search_dialogs(account_id=1, query="a")
    titles = [c.title for c in res]

    # "Apple Fans", "App Architecture", "Learn Python", "Asian Cooking Recipes" all have 'a'
    assert "Apple Fans" in titles
    assert "App Architecture" in titles
    assert "Learn Python" in titles
    assert "Asian Cooking Recipes" in titles

    # "Apple Fans" or "App Architecture" starts with 'a' -> should appear before "Learn Python"
    apple_idx = titles.index("Apple Fans")
    learn_idx = titles.index("Learn Python")
    assert apple_idx < learn_idx


@pytest.mark.asyncio
async def test_search_partial_query_and_ranking(sample_chats):
    """TEST 3: Verify partial query 'py' ranks title prefix matches above substring matches."""
    ChatDiscovery._dialog_cache[1] = sample_chats

    res = await ChatDiscovery.search_dialogs(account_id=1, query="py")
    titles = [c.title for c in res]

    assert "Python Course" in titles
    assert "Python Developers" in titles
    assert "Learn Python" in titles
    assert "Cooking Recipes" not in titles

    # "Python Course" (starts with py) ranked before "Learn Python" (word starts with py)
    assert titles.index("Python Course") < titles.index("Learn Python")
    assert titles.index("Python Developers") < titles.index("Learn Python")


@pytest.mark.asyncio
async def test_search_matches_both_title_and_username(sample_chats):
    """TEST 4: Verify search query 'dev' matches both chat.title and chat.username."""
    ChatDiscovery._dialog_cache[1] = sample_chats

    res = await ChatDiscovery.search_dialogs(account_id=1, query="dev")
    titles = [c.title for c in res]

    # Matches title: Developer Hub, DevOps Community
    assert "Developer Hub" in titles
    assert "DevOps Community" in titles

    # Matches username: Python Developers (username: py_devs contains dev)
    assert "Python Developers" in titles


# =============================================================================
# TEST 5: Empty Search Results UI
# =============================================================================

@pytest.mark.asyncio
async def test_search_empty_results_ui():
    """TEST 5: Verify non-matching search shows proper empty state with refresh/search buttons."""
    mock_msg = MagicMock()
    mock_msg.edit_message_text = AsyncMock()

    with patch("app.bot.chat_picker.session_store.get_data", return_value={"account_id": 1}), \
         patch("app.telegram.discovery.ChatDiscovery.load_dialogs", new_callable=AsyncMock), \
         patch("app.telegram.discovery.ChatDiscovery.search_dialogs", new_callable=AsyncMock, return_value=[]), \
         patch("app.telegram.user_client.user_client_manager.get_client_for_account", new_callable=AsyncMock):

        await ChatPicker.handle_search_query(
            message_or_query=mock_msg,
            user_id=123,
            target="source",
            query_text="xyz987nothing",
            page=0,
        )

        mock_msg.edit_message_text.assert_called_once()
        kwargs = mock_msg.edit_message_text.call_args.kwargs
        assert "No chats found" in kwargs["text"]
        assert "xyz987nothing" in kwargs["text"]

        kb = kwargs["reply_markup"]
        btn_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "cp:src:refresh" in btn_data
        assert "cp:src:search" in btn_data
        assert "cp:src:menu" in btn_data


# =============================================================================
# TEST 6 & 7: 15 Results Per Page Pagination
# =============================================================================

def test_page_size_constant():
    """TEST 6: Verify default RESULTS_PER_PAGE and PAGE_SIZE are 15."""
    assert PAGE_SIZE == 15
    assert RESULTS_PER_PAGE == 15


@pytest.mark.asyncio
async def test_browse_without_searching_15_results_per_page():
    """TEST 6: Verify browsing without a query displays 15 items on page 1."""
    many_chats = [
        DiscoveredChat(id=-1000 - i, title=f"Channel {i:02d}", chat_type="channel")
        for i in range(1, 36)  # 35 chats total
    ]

    mock_msg = MagicMock()
    mock_msg.edit_message_text = AsyncMock()

    with patch("app.bot.chat_picker.session_store.get_data", return_value={"account_id": 1}), \
         patch("app.telegram.discovery.ChatDiscovery.load_dialogs", new_callable=AsyncMock), \
         patch("app.telegram.discovery.ChatDiscovery.search_dialogs", new_callable=AsyncMock, return_value=many_chats), \
         patch("app.telegram.user_client.user_client_manager.get_client_for_account", new_callable=AsyncMock):

        # Page 1 (index 0)
        await ChatPicker.show_category(mock_msg, user_id=123, target="source", category="channel", page=0)

        kwargs = mock_msg.edit_message_text.call_args.kwargs
        assert "Showing 1–15 of 35 (Page 1/3)" in kwargs["text"]

        kb = kwargs["reply_markup"]
        # Exactly 15 chat rows + 1 nav row + 1 action row + 1 back row
        chat_buttons = [
            row[0] for row in kb.inline_keyboard
            if row[0].callback_data.startswith("cp:src:pk:")
        ]
        assert len(chat_buttons) == 15

        # Check pagination buttons: Next is present, Previous is NOT present on Page 1
        nav_buttons = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "cp:src:pg:1" in nav_buttons
        assert not any(d.startswith("cp:src:pg:-") for d in nav_buttons)


@pytest.mark.asyncio
async def test_search_with_greater_than_15_matches_pagination():
    """TEST 7: Verify search results with 35 matches paginate 15 per page."""
    search_chats = [
        DiscoveredChat(id=-1000 - i, title=f"Python Group {i:02d}", chat_type="supergroup")
        for i in range(1, 36)
    ]

    mock_msg = MagicMock()
    mock_msg.edit_message_text = AsyncMock()

    with patch("app.bot.chat_picker.session_store.get_data", return_value={"account_id": 1}), \
         patch("app.telegram.discovery.ChatDiscovery.load_dialogs", new_callable=AsyncMock), \
         patch("app.telegram.discovery.ChatDiscovery.search_dialogs", new_callable=AsyncMock, return_value=search_chats), \
         patch("app.telegram.user_client.user_client_manager.get_client_for_account", new_callable=AsyncMock):

        # Page 1 (0..14)
        await ChatPicker.handle_search_query(mock_msg, user_id=123, target="source", query_text="python", page=0)
        p1_kwargs = mock_msg.edit_message_text.call_args.kwargs
        assert "Found 35 matching chats (Page 1/3)" in p1_kwargs["text"]
        assert "Showing: 1–15" in p1_kwargs["text"]

        # Page 2 (15..29)
        await ChatPicker.handle_search_query(mock_msg, user_id=123, target="source", query_text="python", page=1)
        p2_kwargs = mock_msg.edit_message_text.call_args.kwargs
        assert "Found 35 matching chats (Page 2/3)" in p2_kwargs["text"]
        assert "Showing: 16–30" in p2_kwargs["text"]
        p2_btns = [btn.callback_data for row in p2_kwargs["reply_markup"].inline_keyboard for btn in row]
        assert "cp:src:pg:0" in p2_btns  # Previous button
        assert "cp:src:pg:2" in p2_btns  # Next button

        # Page 3 (30..34)
        await ChatPicker.handle_search_query(mock_msg, user_id=123, target="source", query_text="python", page=2)
        p3_kwargs = mock_msg.edit_message_text.call_args.kwargs
        assert "Found 35 matching chats (Page 3/3)" in p3_kwargs["text"]
        assert "Showing: 31–35" in p3_kwargs["text"]
        p3_btns = [btn.callback_data for row in p3_kwargs["reply_markup"].inline_keyboard for btn in row]
        assert "cp:src:pg:1" in p3_btns  # Previous button
        assert "cp:src:pg:3" not in p3_btns  # No next button on final page


# =============================================================================
# TEST 8: Search State Isolation & Active Query Preservation
# =============================================================================

def test_source_and_destination_search_state_isolation():
    """TEST 8: Verify Source and Destination queries are isolated in state."""
    user_id = 8888

    session_store.clear(user_id)

    # Set Source search query
    ChatPicker.update_target_state(user_id, "source", query="python", page=1, view="search")

    # Set Destination search query
    ChatPicker.update_target_state(user_id, "dest", query="archive_backup", page=0, view="search")

    src_state = ChatPicker.get_target_state(user_id, "source")
    dst_state = ChatPicker.get_target_state(user_id, "dest")

    assert src_state["query"] == "python"
    assert src_state["page"] == 1

    assert dst_state["query"] == "archive_backup"
    assert dst_state["page"] == 0


# =============================================================================
# TEST 9: Refresh Chats Reruns Search and Preserves Query
# =============================================================================

@pytest.mark.asyncio
async def test_refresh_preserves_query_and_resets_page():
    """TEST 9: Verify pressing refresh refreshes cache, keeps query, and resets to Page 1."""
    user_id = 9999
    session_store.clear(user_id)
    session_store.update_data(user_id, account_id=1)
    ChatPicker.update_target_state(user_id, "source", query="crypto", page=2, view="search")

    mock_query = MagicMock()
    mock_query.answer = AsyncMock()
    mock_query.edit_message_text = AsyncMock()

    mock_client = MagicMock()

    with patch("app.telegram.discovery.ChatDiscovery.refresh_dialogs", new_callable=AsyncMock) as mock_refresh, \
         patch("app.telegram.discovery.ChatDiscovery.search_dialogs", new_callable=AsyncMock, return_value=[]) as mock_search, \
         patch("app.telegram.user_client.user_client_manager.get_client_for_account", new_callable=AsyncMock, return_value=mock_client):

        await ChatPicker.refresh_dialogs(mock_query, user_id=user_id, target="source")

        # 1. Cache was refreshed from Telethon
        mock_refresh.assert_awaited_once_with(1, mock_client)

        # 2. Search was rerun with the same query "crypto"
        mock_search.assert_awaited_once()
        assert mock_search.call_args.kwargs["query"] == "crypto"

        # 3. State page was reset to 0
        state = ChatPicker.get_target_state(user_id, "source")
        assert state["page"] == 0
        assert state["query"] == "crypto"


# =============================================================================
# TEST 10: Select Search Result
# =============================================================================

@pytest.mark.asyncio
async def test_select_search_result_exact_entity():
    """TEST 10: Verify selecting a search result stores exact chat ID and transitions."""
    user_id = 7777
    session_store.clear(user_id)
    session_store.update_data(user_id, account_id=1)

    chosen_chat = DiscoveredChat(id=-100998877, title="My Special Channel", chat_type="channel")
    ChatDiscovery._dialog_cache[1] = [chosen_chat]

    mock_query = MagicMock()
    mock_query.edit_message_text = AsyncMock()

    await ChatPicker.handle_pick(mock_query, user_id, target="source", chat_id=-100998877)

    udata = session_store.get_data(user_id)
    assert udata.get("source_chat_id") == -100998877
    assert udata.get("source_chat_title") == "My Special Channel"
    assert "Source Selected" in mock_query.edit_message_text.call_args.kwargs["text"]
