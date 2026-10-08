import asyncio
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.tl.types import User as TelethonUser
from app.config import settings
from app.database import get_session
from app.models.telegram_account import TelegramAccount
from app.models.user import User

logger = logging.getLogger(__name__)


class SessionStorageError(Exception):
    """Raised when the Telethon SQLite session cannot be accessed or written due to storage/lock error."""
    pass


class TelegramAuthError(Exception):
    """Raised when Telegram authorization has genuinely expired or been revoked."""
    pass



class UserClientManager:
    """Manages Telethon MTProto client instances and authentication workflows."""

    AUTH_TIMEOUT_SECONDS = 600  # 10 minutes timeout for pending authentication

    def __init__(self) -> None:
        self._clients: Dict[int, TelegramClient] = {}
        # Persistent in-memory pending authentication state keyed by authorized bot user ID
        # pending_auth[telegram_bot_user_id] = {
        #     "client": TelethonClient,
        #     "phone": phone,
        #     "phone_code_hash": phone_code_hash,
        #     "account_id": account_id,
        #     "session_name": session_name,
        #     "created_at": timestamp
        # }
        self._pending_auth: Dict[int, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _get_session_path(self, session_name: str) -> str:
        """Get full path to Telethon .session file in the configured session directory."""
        settings.ensure_directories()
        return os.path.join(settings.SESSION_DIRECTORY, session_name)

    def _is_expired(self, state: Dict[str, Any]) -> bool:
        """Check if pending authentication state has exceeded the 10-minute timeout."""
        return (time.time() - state.get("created_at", 0)) > self.AUTH_TIMEOUT_SECONDS

    def get_pending_auth(self, telegram_bot_user_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve pending login state for an authorized Telegram bot user ID."""
        state = self._pending_auth.get(telegram_bot_user_id)
        if state and self._is_expired(state):
            return None
        return state

    async def get_active_client(
        self, account_id: int
    ) -> Optional[TelegramClient]:
        """Convenience alias for get_client_for_account."""
        return await self.get_client_for_account(account_id)

    async def get_client_for_account(
        self, account_id: int
    ) -> Optional[TelegramClient]:
        """Get or initialize the persistent Telethon client for an account ID.

        Guarantees strictly 1 client per account session, safe reconnection,
        and cleanly isolates SQLite session storage errors from real auth revocation.
        """
        async with self._lock:
            if account_id in self._clients:
                client = self._clients[account_id]
                if client.is_connected():
                    return client
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        return client
                except sqlite3.OperationalError as e:
                    logger.error("Session storage OperationalError for account %s: %s", account_id, e)
                    try:
                        if client.session and hasattr(client.session, "close"):
                            client.session.close()
                        await client.disconnect()
                    except Exception:
                        pass
                    self._clients.pop(account_id, None)
                    raise SessionStorageError(f"Session storage error: {e}")
                except Exception as e:
                    logger.warning("Failed to reconnect cached client %s: %s", account_id, e)
                    try:
                        if client.session and hasattr(client.session, "close"):
                            client.session.close()
                        await client.disconnect()
                    except Exception:
                        pass
                    self._clients.pop(account_id, None)

            # Retrieve account record from database
            async with get_session() as session:
                result = await session.execute(
                    select(TelegramAccount).where(TelegramAccount.id == account_id)
                )
                account = result.scalar_one_or_none()

            if not account or not account.is_active:
                return None

            if not settings.API_ID or not settings.API_HASH:
                raise ValueError("API_ID and API_HASH must be configured in environment.")

            if account.session_string:
                client = TelegramClient(
                    session=StringSession(account.session_string),
                    api_id=settings.API_ID,
                    api_hash=settings.API_HASH,
                )
            else:
                session_file = self._get_session_path(account.session_name)
                session_file_path = Path(
                    session_file if session_file.endswith(".session") else f"{session_file}.session"
                )

                # Check if session directory is writable
                sess_dir = Path(settings.SESSION_DIRECTORY)
                if not os.access(sess_dir, os.W_OK):
                    logger.error("Session directory %s is not writable!", sess_dir)
                    raise SessionStorageError(f"Session directory {sess_dir} is not writable.")

                # Automatic migration for legacy session files if account_X.session is absent
                if not session_file_path.exists() and account.phone_number:
                    clean_phone = account.phone_number.lstrip("+").strip()
                    legacy_file = sess_dir / f"user_{clean_phone}.session"
                    if legacy_file.exists():
                        try:
                            shutil.copy2(legacy_file, session_file_path)
                            logger.info("Migrated legacy session %s -> %s", legacy_file, session_file_path)
                        except Exception as ex:
                            logger.warning("Failed to copy legacy session: %s", ex)

                # Ensure session file has write permissions if it exists
                if session_file_path.exists():
                    if not os.access(session_file_path, os.W_OK):
                        try:
                            os.chmod(session_file_path, 0o644)
                        except Exception as ex:
                            logger.warning("Failed to set write permissions on %s: %s", session_file_path, ex)

                client = TelegramClient(
                    session=session_file,
                    api_id=settings.API_ID,
                    api_hash=settings.API_HASH,
                )

            try:
                await client.connect()
                is_authorized = await client.is_user_authorized()
                if is_authorized and not account.session_string:
                    try:
                        s_str = StringSession.save(client.session)
                        if s_str:
                            async with get_session() as s_sess:
                                acc_update = await s_sess.get(TelegramAccount, account_id)
                                if acc_update:
                                    acc_update.session_string = s_str
                    except Exception as ex:
                        logger.debug("Could not auto-populate session_string from client.session: %s", ex)
            except sqlite3.OperationalError as e:
                logger.error("Session storage OperationalError connecting account %s: %s", account_id, e)
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception:
                    pass
                raise SessionStorageError(f"Session storage error: {e}")
            except (
                AuthKeyUnregisteredError,
                UserDeactivatedError,
                UserDeactivatedBanError,
                SessionRevokedError,
                SessionExpiredError,
            ) as e:
                logger.warning("Telegram authentication revoked for account %s: %s", account_id, e)
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception:
                    pass
                async with get_session() as session:
                    res = await session.execute(
                        select(TelegramAccount).where(TelegramAccount.id == account_id)
                    )
                    acc = res.scalar_one_or_none()
                    if acc:
                        acc.is_active = False
                return None
            except Exception as e:
                logger.error("Unexpected error connecting client for account %s: %s", account_id, e)
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception:
                    pass
                return None

            if not is_authorized:
                logger.warning(
                    "Account id=%s is no longer authorized. Session may have expired.",
                    account_id,
                )
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception:
                    pass
                return None

            self._clients[account_id] = client
            return client

    async def reconnect_account(self, account_id: int) -> Tuple[bool, str]:
        """Safely disconnect, release SQLite locks, and re-establish connection for an account."""
        async with self._lock:
            old_client = self._clients.pop(account_id, None)
            if old_client:
                try:
                    if old_client.session and hasattr(old_client.session, "close"):
                        old_client.session.close()
                    await old_client.disconnect()
                except Exception:
                    pass

        try:
            client = await self.get_client_for_account(account_id)
            if client and client.is_connected() and await client.is_user_authorized():
                return True, "Account reconnected successfully!"
            return False, "Failed to reconnect account. Please check credentials or log in again."
        except SessionStorageError as e:
            return False, f"Session storage error: {e}"
        except Exception as e:
            return False, f"Reconnect failed: {e}"

    async def start_login(
        self, telegram_bot_user_id: int, phone_number: str
    ) -> Tuple[bool, str, str]:
        """Initiate MTProto login flow for authorized bot user ID.

        Creates ONE Telethon client, connects, sends code request, and stores
        state keyed by telegram_bot_user_id.
        """
        logger.info("auth started user_id=%s", telegram_bot_user_id)

        if not settings.API_ID or not settings.API_HASH:
            return (
                False,
                "API_ID and API_HASH are not configured. Please add them to your .env file.",
                "CONFIG_ERROR",
            )

        async with self._lock:
            # Clean up any existing pending auth for this user
            if telegram_bot_user_id in self._pending_auth:
                await self._cleanup_pending(telegram_bot_user_id)

            # Allocate or retrieve deterministic account_id for safe session naming
            async with get_session() as session:
                res_user = await session.execute(
                    select(User).where(User.telegram_id == telegram_bot_user_id)
                )
                user_rec = res_user.scalar_one_or_none()
                if not user_rec:
                    user_rec = User(
                        telegram_id=telegram_bot_user_id,
                        is_admin=True,
                    )
                    session.add(user_rec)
                    await session.flush()

                res_acc = await session.execute(
                    select(TelegramAccount).where(
                        TelegramAccount.user_id == user_rec.id,
                        TelegramAccount.phone_number == phone_number,
                    )
                )
                acc_rec = res_acc.scalar_one_or_none()
                if not acc_rec:
                    unique_temp = f"pending_{telegram_bot_user_id}_{int(time.time() * 1000)}"
                    acc_rec = TelegramAccount(
                        user_id=user_rec.id,
                        phone_number=phone_number,
                        session_name=unique_temp,
                        is_active=False,
                    )
                    session.add(acc_rec)
                    await session.flush()
                    acc_rec.session_name = f"account_{acc_rec.id}"
                    await session.flush()
                account_id = acc_rec.id

            # Ensure any previous active client for this account is disconnected and removed
            if account_id in self._clients:
                old_client = self._clients.pop(account_id)
                try:
                    if old_client.session and hasattr(old_client.session, "close"):
                        old_client.session.close()
                    await old_client.disconnect()
                except Exception:
                    pass

            session_name = f"account_{account_id}"
            session_file = self._get_session_path(session_name)

            client = TelegramClient(
                session=session_file,
                api_id=settings.API_ID,
                api_hash=settings.API_HASH,
            )

            try:
                await client.connect()
                sent = await client.send_code_request(phone_number)
                phone_code_hash = sent.phone_code_hash

                self._pending_auth[telegram_bot_user_id] = {
                    "client": client,
                    "phone": phone_number,
                    "phone_code_hash": phone_code_hash,
                    "account_id": account_id,
                    "session_name": session_name,
                    "created_at": time.time(),
                }

                logger.info("code requested user_id=%s", telegram_bot_user_id)
                return True, phone_code_hash, "CODE_REQUESTED"

            except PhoneNumberInvalidError:
                logger.warning("phone invalid user_id=%s", telegram_bot_user_id)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, "PHONE_INVALID", "ERROR"

            except FloodWaitError as e:
                logger.warning("flood wait user_id=%s wait=%s", telegram_bot_user_id, e.seconds)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, f"FLOOD_WAIT:{e.seconds}", "ERROR"

            except Exception as e:
                logger.error("error sending code user_id=%s: %s", telegram_bot_user_id, e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, str(e), "ERROR"

    async def request_new_code(
        self, telegram_bot_user_id: int
    ) -> Tuple[bool, str]:
        """Request new code using the SAME Telethon client instance and replace phone_code_hash."""
        logger.info("request new code started user_id=%s", telegram_bot_user_id)

        async with self._lock:
            state = self._pending_auth.get(telegram_bot_user_id)
            if not state:
                return (
                    False,
                    "No pending login session found. Please enter your phone number to start over.",
                )

            if self._is_expired(state):
                logger.info("auth timeout user_id=%s", telegram_bot_user_id)
                await self._cleanup_pending(telegram_bot_user_id)
                return (
                    False,
                    "Authentication session timed out. Please enter your phone number to restart.",
                )

            client: TelegramClient = state["client"]
            phone: str = state["phone"]

            try:
                if not client.is_connected():
                    await client.connect()

                sent = await client.send_code_request(phone)
                # Replace ONLY the stored phone_code_hash
                state["phone_code_hash"] = sent.phone_code_hash
                state["created_at"] = time.time()  # Reset timeout timer on new code

                logger.info("code requested user_id=%s", telegram_bot_user_id)
                return True, "A new login code has been sent by Telegram."

            except FloodWaitError as e:
                logger.warning("flood wait user_id=%s wait=%s", telegram_bot_user_id, e.seconds)
                return False, f"FLOOD_WAIT:{e.seconds}"

            except Exception as e:
                logger.error("error requesting new code user_id=%s: %s", telegram_bot_user_id, e)
                return False, f"Failed to request new code: {str(e)}"

    async def verify_code(
        self,
        telegram_bot_user_id: int,
        code: str,
        password: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[int]]:
        """Verify the login code or 2FA password using the SAME client instance."""
        logger.info("code verification started user_id=%s", telegram_bot_user_id)

        async with self._lock:
            state = self._pending_auth.get(telegram_bot_user_id)
            if not state:
                return (
                    False,
                    "No pending login session found. Please enter your phone number to start over.",
                    None,
                )

            if self._is_expired(state):
                logger.info("auth timeout user_id=%s", telegram_bot_user_id)
                await self._cleanup_pending(telegram_bot_user_id)
                return (
                    False,
                    "TIMEOUT",
                    None,
                )

            client: TelegramClient = state["client"]
            phone: str = state["phone"]
            phone_code_hash: str = state["phone_code_hash"]
            account_id: int = state["account_id"]
            session_name: str = state["session_name"]

            try:
                if not client.is_connected():
                    await client.connect()

                if password:
                    # 2FA password step
                    me = await client.sign_in(password=password)
                else:
                    # Login code step: authenticate with exact stored phone_code_hash
                    me = await client.sign_in(
                        phone=phone,
                        code=code,
                        phone_code_hash=phone_code_hash,
                    )

            except SessionPasswordNeededError:
                # 2FA required: do NOT treat as error, preserve state and client
                logger.info("2FA required user_id=%s", telegram_bot_user_id)
                return False, "2FA_REQUIRED", None

            except PhoneCodeInvalidError:
                # Code invalid: preserve state and client so user can retry
                logger.warning("code invalid user_id=%s", telegram_bot_user_id)
                return False, "INVALID_CODE", None

            except PhoneCodeExpiredError:
                # Code expired: preserve state and client so user can click Request New Code
                logger.warning("code expired user_id=%s", telegram_bot_user_id)
                return False, "CODE_EXPIRED", None

            except PasswordHashInvalidError:
                # Password invalid: preserve state and client so user can retry password
                logger.warning("password invalid user_id=%s", telegram_bot_user_id)
                return False, "INVALID_2FA", None

            except FloodWaitError as e:
                logger.warning("flood wait user_id=%s wait=%s", telegram_bot_user_id, e.seconds)
                return False, f"FLOOD_WAIT:{e.seconds}", None

            except Exception as e:
                logger.error("sign-in error user_id=%s: %s", telegram_bot_user_id, e)
                return False, f"Sign in failed: {str(e)}", None

            # Succeeded: ensure client remains connected
            if not client.is_connected():
                await client.connect()

            # Export StringSession so credentials persist safely across container restarts
            sess_str = None
            try:
                sess_str = StringSession.save(client.session)
            except Exception as se:
                logger.warning("Could not export session string: %s", se)

            # Store metadata and mark connected
            async with get_session() as session:
                res = await session.execute(
                    select(TelegramAccount).where(TelegramAccount.id == account_id)
                )
                acc_rec = res.scalar_one_or_none()
                if acc_rec:
                    acc_rec.session_name = session_name
                    if sess_str:
                        acc_rec.session_string = sess_str
                    acc_rec.account_user_id = getattr(me, "id", None)
                    acc_rec.username = getattr(me, "username", None)
                    acc_rec.first_name = getattr(me, "first_name", None)
                    acc_rec.is_active = True

            # Register client in active clients cache
            self._clients[account_id] = client

            # Clear pending authentication state
            self._pending_auth.pop(telegram_bot_user_id, None)

            logger.info("authentication successful user_id=%s", telegram_bot_user_id)
            return True, "Account connected successfully!", account_id

    async def cancel_login(self, telegram_bot_user_id: int) -> bool:
        """Cancel pending authentication and clean up client and temporary files."""
        async with self._lock:
            logger.info("auth cancelled user_id=%s", telegram_bot_user_id)
            return await self._cleanup_pending(telegram_bot_user_id)

    async def _cleanup_pending(self, telegram_bot_user_id: int) -> bool:
        """Internal helper to disconnect client and clean up unauthenticated session."""
        state = self._pending_auth.pop(telegram_bot_user_id, None)
        if not state:
            return False

        client = state.get("client")
        if client:
            try:
                if client.session and hasattr(client.session, "close"):
                    client.session.close()
                await client.disconnect()
            except Exception:
                pass

        session_name = state.get("session_name")
        account_id = state.get("account_id")

        # If account was never activated, remove its unused DB placeholder and session file
        if account_id:
            try:
                async with get_session() as session:
                    res = await session.execute(
                        select(TelegramAccount).where(TelegramAccount.id == account_id)
                    )
                    acc_rec = res.scalar_one_or_none()
                    if acc_rec and not acc_rec.is_active:
                        await session.delete(acc_rec)
            except Exception:
                pass

        # Only clean temporary pending sessions, preserve activated account files
        if session_name and session_name.startswith("pending_"):
            session_file = self._get_session_path(session_name)
            for ext in ["", ".session", ".session-journal"]:
                fpath = Path(session_file + ext)
                if fpath.exists():
                    try:
                        fpath.unlink()
                    except Exception:
                        pass

        return True

    async def disconnect_account(self, account_id: int) -> bool:
        """Disconnect and deactivate an account while preserving its session file."""
        async with self._lock:
            client = self._clients.pop(account_id, None)
            if client:
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception as e:
                    logger.warning("Error disconnecting client %s: %s", account_id, e)

        async with get_session() as session:
            res = await session.execute(
                select(TelegramAccount).where(TelegramAccount.id == account_id)
            )
            account = res.scalar_one_or_none()
            if not account:
                return False

            account.is_active = False

        return True

    async def list_user_accounts(self, owner_telegram_id: int) -> List[TelegramAccount]:
        """Retrieve all active connected accounts for an owner."""
        async with get_session() as session:
            stmt = (
                select(TelegramAccount)
                .join(User)
                .where(
                    User.telegram_id == owner_telegram_id,
                    TelegramAccount.is_active.is_(True),
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def close_all(self) -> None:
        """Disconnect all active Telethon clients on shutdown."""
        async with self._lock:
            for user_id in list(self._pending_auth.keys()):
                await self._cleanup_pending(user_id)

            for acc_id, client in list(self._clients.items()):
                try:
                    if client.session and hasattr(client.session, "close"):
                        client.session.close()
                    await client.disconnect()
                except Exception as e:
                    logger.warning("Error disconnecting client %s: %s", acc_id, e)
            self._clients.clear()


user_client_manager = UserClientManager()
