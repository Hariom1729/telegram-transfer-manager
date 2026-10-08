"""Unit tests for the Telethon MTProto Authentication Flow."""

import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User as TelethonUser
from app.telegram.user_client import UserClientManager


@pytest.fixture(autouse=True)
async def isolated_test_db():
    """Ensure tests run against an isolated in-memory SQLite database."""
    from app.database import Base
    import app.telegram.user_client as uc_module
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

    original_get_session = uc_module.get_session
    uc_module.get_session = mock_get_session
    try:
        yield
    finally:
        uc_module.get_session = original_get_session
        await engine.dispose()


@pytest.fixture
def user_client_mgr():
    """Create a fresh instance of UserClientManager for testing."""
    return UserClientManager()


@pytest.mark.asyncio
async def test_start_login_stores_client_keyed_by_user_id(user_client_mgr):
    """Sequence 1: Start login stores client and phone_code_hash keyed by telegram_bot_user_id."""
    user_id = 987654321
    phone = "+12025550199"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "mock_hash_abc123"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client):
        success, hash_val, status = await user_client_mgr.start_login(
            telegram_bot_user_id=user_id, phone_number=phone
        )

        assert success is True
        assert hash_val == "mock_hash_abc123"
        assert status == "CODE_REQUESTED"

        # State is stored keyed by user_id
        pending = user_client_mgr.get_pending_auth(user_id)
        assert pending is not None
        assert pending["client"] is mock_client
        assert pending["phone"] == phone
        assert pending["phone_code_hash"] == "mock_hash_abc123"


@pytest.mark.asyncio
async def test_sequence_immediate_code_entry_authenticates_successfully(user_client_mgr):
    """Sequence 1: Enter code immediately -> Authentication successful using exact same client."""
    user_id = 12345
    phone = "+12025550144"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "initial_hash_111"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)

    mock_me = MagicMock(spec=TelethonUser)
    mock_me.id = 5555
    mock_me.username = "testuser"
    mock_me.first_name = "Test"
    mock_client.sign_in = AsyncMock(return_value=mock_me)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client) as client_cls:
        # Step 1: Start login
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)
        assert client_cls.call_count == 1

        # Step 2: Verify code - MUST NOT call TelegramClient() again
        success, msg, acc_id = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="12345"
        )

        assert success is True
        assert acc_id is not None
        # Still exactly 1 client created
        assert client_cls.call_count == 1
        # sign_in called with same hash
        mock_client.sign_in.assert_called_once_with(
            phone=phone, code="12345", phone_code_hash="initial_hash_111"
        )
        # Pending state is cleared on success
        assert user_client_mgr.get_pending_auth(user_id) is None


@pytest.mark.asyncio
async def test_sequence_request_new_code_then_enter_new_code(user_client_mgr):
    """Sequence 2: Request New Code -> Enter ONLY new code -> Authentication successful."""
    user_id = 777
    phone = "+12025550188"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)

    send_res1 = MagicMock()
    send_res1.phone_code_hash = "old_hash_1"
    send_res2 = MagicMock()
    send_res2.phone_code_hash = "new_hash_2"
    mock_client.send_code_request = AsyncMock(side_effect=[send_res1, send_res2])

    mock_me = MagicMock(spec=TelethonUser)
    mock_me.id = 7777
    mock_me.username = "new_code_user"
    mock_me.first_name = "NewCode"
    mock_client.sign_in = AsyncMock(return_value=mock_me)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client) as client_cls:
        # Step 1: Start login
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)
        assert user_client_mgr.get_pending_auth(user_id)["phone_code_hash"] == "old_hash_1"

        # Step 2: Request New Code
        success_new, msg_new = await user_client_mgr.request_new_code(telegram_bot_user_id=user_id)
        assert success_new is True

        pending = user_client_mgr.get_pending_auth(user_id)
        assert pending["phone_code_hash"] == "new_hash_2"
        assert pending["client"] is mock_client
        assert mock_client.send_code_request.call_count == 2
        # Client not recreated
        assert client_cls.call_count == 1

        # Step 3: Enter the new code
        success_sign, msg_sign, acc_id = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="99999"
        )
        assert success_sign is True
        mock_client.sign_in.assert_called_once_with(
            phone=phone, code="99999", phone_code_hash="new_hash_2"
        )
        assert user_client_mgr.get_pending_auth(user_id) is None


@pytest.mark.asyncio
async def test_sequence_enter_incorrect_code_shows_incorrect_not_expired(user_client_mgr):
    """Sequence 3: Enter incorrect code -> returns INVALID_CODE (NOT expired)."""
    user_id = 999
    phone = "+12025550177"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "hash_persistent"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)
    mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client):
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)

        # Enter invalid code
        success, msg, acc_id = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="00000"
        )

        assert success is False
        assert msg == "INVALID_CODE"  # NOT CODE_EXPIRED
        # Client and pending state must STILL be preserved so user can retry
        pending = user_client_mgr.get_pending_auth(user_id)
        assert pending is not None
        assert pending["client"] is mock_client
        assert pending["phone_code_hash"] == "hash_persistent"


@pytest.mark.asyncio
async def test_sequence_2fa_workflow_on_same_client(user_client_mgr):
    """Sequence 4: Account with 2FA -> Code accepted -> 2FA requested -> Password accepted -> Success."""
    user_id = 444
    phone = "+12025550155"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "hash_for_2fa"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)

    mock_me = MagicMock(spec=TelethonUser)
    mock_me.id = 4444
    mock_me.username = "twofa_user"
    mock_me.first_name = "TwoFa"

    # First sign_in raises SessionPasswordNeededError, second (with password) succeeds
    mock_client.sign_in = AsyncMock(
        side_effect=[SessionPasswordNeededError(request=None), mock_me]
    )

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client) as client_cls:
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)

        # 1. Code submission triggers 2FA_REQUIRED
        success1, msg1, _ = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="12345"
        )
        assert success1 is False
        assert msg1 == "2FA_REQUIRED"
        assert user_client_mgr.get_pending_auth(user_id) is not None
        assert client_cls.call_count == 1

        # 2. Password submission on same client
        success2, msg2, acc_id2 = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="", password="supersecret2fapassword"
        )
        assert success2 is True
        assert acc_id2 is not None
        mock_client.sign_in.assert_called_with(password="supersecret2fapassword")
        assert user_client_mgr.get_pending_auth(user_id) is None
        assert client_cls.call_count == 1


@pytest.mark.asyncio
async def test_cancel_login_cleans_up_client_and_state(user_client_mgr):
    """Verify cancel_login disconnects the client and clears pending login state."""
    user_id = 333
    phone = "+12025550133"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "hash_cancel"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client):
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)
        assert user_client_mgr.get_pending_auth(user_id) is not None

        # Cancel
        res = await user_client_mgr.cancel_login(telegram_bot_user_id=user_id)
        assert res is True
        assert user_client_mgr.get_pending_auth(user_id) is None
        mock_client.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_cleanup(user_client_mgr):
    """Verify that sessions older than 10 minutes are expired and cleaned up."""
    user_id = 555
    phone = "+12025550122"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_send_res = MagicMock()
    mock_send_res.phone_code_hash = "hash_timeout"
    mock_client.send_code_request = AsyncMock(return_value=mock_send_res)

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client):
        await user_client_mgr.start_login(telegram_bot_user_id=user_id, phone_number=phone)

        # Fast forward creation time by 11 minutes
        user_client_mgr._pending_auth[user_id]["created_at"] = time.time() - 700

        # Attempt to verify code
        success, msg, _ = await user_client_mgr.verify_code(
            telegram_bot_user_id=user_id, code="12345"
        )
        assert success is False
        assert msg == "TIMEOUT"
        assert user_client_mgr.get_pending_auth(user_id) is None


@pytest.mark.asyncio
async def test_phone_invalid_and_flood_wait_errors(user_client_mgr):
    """Verify PhoneNumberInvalidError and FloodWaitError handling."""
    user_id = 666
    phone = "+99999999999999"

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.send_code_request = AsyncMock(
        side_effect=PhoneNumberInvalidError(request=None)
    )

    with patch("app.telegram.user_client.TelegramClient", return_value=mock_client):
        success, msg, _ = await user_client_mgr.start_login(
            telegram_bot_user_id=user_id, phone_number=phone
        )
        assert success is False
        assert msg == "PHONE_INVALID"
        assert user_client_mgr.get_pending_auth(user_id) is None
