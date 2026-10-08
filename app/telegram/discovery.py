"""Chat and Channel Discovery via MTProto with In-Memory Caching, Refresh, and Fuzzy Search."""

from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
import unicodedata
from sqlalchemy import desc, select
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat as TgChat, User as TgUser
from app.database import get_session
from app.models.chat import Chat

logger = logging.getLogger(__name__)


def _normalize_text(s: Optional[str]) -> str:
    """Normalize string: Unicode NFKC, lowercased, and whitespace-collapsed."""
    if not s:
        return ""
    norm = unicodedata.normalize("NFKC", str(s))
    return " ".join(norm.strip().lower().split())


@dataclass
class DiscoveredChat:
    """Standardized representation of an accessible Telegram chat with entity references."""

    id: int  # Full Telegram peer ID (-100... for channels/supergroups, positive for users)
    title: str
    chat_type: str  # channel, supergroup, group, private
    username: Optional[str] = None
    is_megagroup: bool = False
    is_forum: bool = False
    can_post: bool = True
    can_manage_topics: bool = False
    db_id: Optional[int] = None
    entity: Any = None
    input_entity: Any = None

    @property
    def display_icon(self) -> str:
        """Icon representing the entity type."""
        if self.chat_type == "channel":
            return "📢"
        elif self.chat_type == "supergroup":
            return "👥"
        elif self.chat_type == "group":
            return "👥"
        elif self.chat_type == "private":
            return "💬"
        return "📁"

    @property
    def type_label(self) -> str:
        """Human-readable chat type label."""
        if self.chat_type == "channel":
            return "Channel"
        elif self.chat_type == "supergroup":
            return "Supergroup"
        elif self.chat_type == "group":
            return "Group"
        elif self.chat_type == "private":
            return "Private Chat"
        return "Chat"


class ChatDiscovery:
    """Discovers, caches, and refreshes accessible channels, groups, and chats for authenticated accounts."""

    CACHE_TTL_SECONDS: int = 600  # 10 minutes default cache TTL

    # In-memory dialog cache per telegram_account_id
    _dialog_cache: Dict[int, List[DiscoveredChat]] = {}
    _cache_timestamps: Dict[int, float] = {}

    @classmethod
    def _classify_entity(cls, entity: Any, dialog: Any = None) -> DiscoveredChat:
        """Classify a Telethon entity into DiscoveredChat and retain entity references."""
        is_megagroup = False
        is_forum = False
        can_post = True
        can_manage_topics = False

        try:
            peer_id = utils.get_peer_id(entity)
        except Exception:
            raw_id = getattr(entity, "id", 0)
            if isinstance(entity, Channel):
                peer_id = raw_id if str(raw_id).startswith("-100") else int(f"-100{raw_id}")
            elif isinstance(entity, TgChat):
                peer_id = -raw_id if raw_id > 0 else raw_id
            else:
                peer_id = raw_id

        input_ent = getattr(dialog, "input_entity", None) if dialog else None

        if isinstance(entity, Channel):
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", "Unnamed Channel")
            is_megagroup = bool(getattr(entity, "megagroup", False))
            is_forum = bool(getattr(entity, "forum", False))
            chat_type = "supergroup" if is_megagroup else "channel"

            admin_rights = getattr(entity, "admin_rights", None)
            default_banned = getattr(entity, "default_banned_rights", None)

            if getattr(entity, "creator", False):
                can_manage_topics = True
                can_post = True
            elif admin_rights:
                can_manage_topics = bool(getattr(admin_rights, "manage_topics", False) or getattr(admin_rights, "change_info", False))
                can_post = bool(getattr(admin_rights, "post_messages", True))
            elif default_banned and getattr(default_banned, "manage_topics", False):
                can_manage_topics = False
                can_post = not bool(getattr(default_banned, "send_messages", False))
            else:
                can_manage_topics = True
                can_post = True

            return DiscoveredChat(
                id=peer_id,
                title=title,
                chat_type=chat_type,
                username=username,
                is_megagroup=is_megagroup,
                is_forum=is_forum,
                can_post=can_post,
                can_manage_topics=can_manage_topics,
                entity=entity,
                input_entity=input_ent,
            )

        elif isinstance(entity, TgChat):
            return DiscoveredChat(
                id=peer_id,
                title=getattr(entity, "title", "Unnamed Group"),
                chat_type="group",
                username=None,
                is_megagroup=False,
                is_forum=False,
                can_post=True,
                can_manage_topics=False,
                entity=entity,
                input_entity=input_ent,
            )

        elif isinstance(entity, TgUser):
            username = getattr(entity, "username", None)
            first_name = getattr(entity, "first_name", "") or ""
            last_name = getattr(entity, "last_name", "") or ""
            title = f"{first_name} {last_name}".strip() or getattr(
                entity, "phone", "User"
            )
            return DiscoveredChat(
                id=peer_id,
                title=title,
                chat_type="private",
                username=username,
                is_megagroup=False,
                is_forum=False,
                can_post=True,
                can_manage_topics=False,
                entity=entity,
                input_entity=input_ent,
            )

        entity_id = getattr(entity, "id", 0)
        return DiscoveredChat(
            id=peer_id if peer_id != 0 else entity_id,
            title=getattr(entity, "title", str(entity_id)),
            chat_type="group",
            is_megagroup=False,
            is_forum=False,
            entity=entity,
            input_entity=input_ent,
        )

    @classmethod
    async def load_dialogs(
        cls,
        account_id: int,
        client: TelegramClient,
        force_refresh: bool = False,
        limit: int = 300,
    ) -> List[DiscoveredChat]:
        """Load dialogs for an account, using in-memory cache if fresh."""
        now = time.time()
        cached = cls._dialog_cache.get(account_id)
        cache_time = cls._cache_timestamps.get(account_id, 0.0)

        if not force_refresh and cached and (now - cache_time) < cls.CACHE_TTL_SECONDS:
            logger.debug("Returning %d cached dialogs for account %s", len(cached), account_id)
            return cached

        logger.info(
            "Loading dialogs from Telethon for account %s (force_refresh=%s)",
            account_id,
            force_refresh,
        )

        chats: List[DiscoveredChat] = []
        try:
            async for dialog in client.iter_dialogs(limit=limit):
                entity = dialog.entity
                if getattr(entity, "deactivated", False):
                    continue
                chat_info = cls._classify_entity(entity, dialog=dialog)
                chats.append(chat_info)

            # Store in in-memory cache
            cls._dialog_cache[account_id] = chats
            cls._cache_timestamps[account_id] = now

            # Asynchronously persist/cache in database
            await cls._sync_chats_to_db(account_id, chats)
            return chats

        except FloodWaitError as e:
            logger.warning("FloodWait %ds while loading dialogs for account %s", e.seconds, account_id)
            if cached:
                return cached
            raise
        except Exception as e:
            logger.error("Failed to load dialogs for account %s: %s", account_id, e)
            if cached:
                return cached
            raise

    @classmethod
    async def refresh_dialogs(
        cls, account_id: int, client: TelegramClient
    ) -> List[DiscoveredChat]:
        """Explicitly refresh dialogs from Telethon, replace cache, and sync to database."""
        logger.info("Explicitly refreshing dialogs for account %s", account_id)
        return await cls.load_dialogs(account_id, client, force_refresh=True)

    @classmethod
    def _rank_dialog(cls, chat: DiscoveredChat, query: str) -> Tuple[int, float, str]:
        """Rank a chat for search suggestions according to strict priority tiers:
        0. Exact numeric peer ID match
        1. Exact title match
        2. Exact username match
        3. Title starts with query
        4. Username starts with query
        5. Any word in title starts with query
        6. Title contains query
        7. Username contains query
        8. Fuzzy similarity (for queries >= 3 chars, ratio >= 0.55)
        999. No match
        """
        q = _normalize_text(query)
        if not q:
            return (0, 0.0, _normalize_text(chat.title))

        clean_q = q.lstrip("@").strip()
        title_norm = _normalize_text(chat.title)
        uname_norm = _normalize_text(chat.username).lstrip("@").strip() if chat.username else ""

        # 0. Exact numeric peer ID match
        clean_num = q.lstrip("-")
        if str(chat.id) == q or str(abs(chat.id)) == clean_num:
            return (0, 0.0, title_norm)

        # 1. Exact title match
        if title_norm == q:
            return (1, 0.0, title_norm)

        # 2. Exact username match
        if uname_norm and (uname_norm == clean_q or uname_norm == q):
            return (2, 0.0, title_norm)

        # 3. Title starts with query
        if title_norm.startswith(q):
            return (3, float(len(title_norm)), title_norm)

        # 4. Username starts with query
        if uname_norm and (uname_norm.startswith(clean_q) or uname_norm.startswith(q)):
            return (4, float(len(uname_norm)), title_norm)

        # 5. Any word in title starts with query
        words = title_norm.split()
        if any(w.startswith(q) for w in words):
            return (5, float(len(title_norm)), title_norm)

        # 6. Title contains query (partial match)
        pos_title = title_norm.find(q)
        if pos_title != -1:
            return (6, float(pos_title), title_norm)

        # 7. Username contains query (partial match)
        if clean_q and uname_norm:
            pos_uname = uname_norm.find(clean_q)
            if pos_uname != -1:
                return (7, float(pos_uname), title_norm)

        # 8. Fuzzy similarity (for queries with >= 3 characters)
        if len(q) >= 3:
            ratio_title = SequenceMatcher(None, q, title_norm).ratio()
            ratio_uname = (
                SequenceMatcher(None, clean_q, uname_norm).ratio()
                if uname_norm
                else 0.0
            )
            best_ratio = max(ratio_title, ratio_uname)
            if best_ratio >= 0.55:
                return (8, 1.0 - best_ratio, title_norm)

        return (999, 0.0, title_norm)

    @classmethod
    async def search_dialogs(
        cls,
        account_id: int,
        query: str,
        chat_type: Optional[str] = None,
        client: Optional[TelegramClient] = None,
    ) -> List[DiscoveredChat]:
        """Search dialogs from cache with ranking algorithm and optional category filter."""
        dialogs = cls._dialog_cache.get(account_id)
        if dialogs is None:
            if client:
                dialogs = await cls.load_dialogs(account_id, client)
            else:
                dialogs = []

        # Filter by category if requested
        filtered = dialogs
        if chat_type and chat_type != "all":
            if chat_type == "channel":
                filtered = [c for c in filtered if c.chat_type == "channel"]
            elif chat_type == "group":
                filtered = [c for c in filtered if c.chat_type in ("group", "supergroup")]
            elif chat_type == "private":
                filtered = [c for c in filtered if c.chat_type == "private"]

        q = query.strip()
        if not q:
            return sorted(filtered, key=lambda c: c.title.lower())

        ranked: List[Tuple[int, float, str, DiscoveredChat]] = []
        for chat in filtered:
            tier, secondary, sort_key = cls._rank_dialog(chat, q)
            if tier < 999:
                ranked.append((tier, secondary, sort_key, chat))

        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        if ranked:
            return [item[3] for item in ranked]

        # If fuzzy search in cached dialogs yields no results, attempt direct entity lookup (e.g. @channel, t.me link, ID)
        if client and q:
            try:
                target_q = int(q) if (q.startswith("-") or q.isdigit()) else q
                resolved = await client.get_entity(target_q)
                if resolved:
                    disc = cls._classify_entity(resolved)
                    # Cache it for this session
                    if account_id in cls._dialog_cache:
                        cls._dialog_cache[account_id].append(disc)
                    return [disc]
            except Exception as e:
                logger.debug("Direct get_entity fallback for '%s' returned: %s", q, e)

        return []

    @classmethod
    async def get_recent_dialogs(
        cls, account_id: int, limit: int = 8
    ) -> List[DiscoveredChat]:
        """Fetch recently selected chats from database."""
        recent_chats: List[DiscoveredChat] = []
        try:
            async with get_session() as session:
                query = (
                    select(Chat)
                    .where(Chat.last_used_at.is_not(None))
                    .order_by(desc(Chat.last_used_at))
                    .limit(limit)
                )
                if account_id:
                    acc_query = query.where(Chat.telegram_account_id == account_id)
                    res = await session.execute(acc_query)
                    rows = res.scalars().all()
                    if not rows:
                        res = await session.execute(query)
                        rows = res.scalars().all()
                else:
                    res = await session.execute(query)
                    rows = res.scalars().all()

                for row in rows:
                    recent_chats.append(
                        DiscoveredChat(
                            id=row.telegram_chat_id,
                            title=row.title,
                            chat_type=row.chat_type,
                            username=row.username,
                            is_megagroup=row.is_megagroup,
                            is_forum=row.is_forum,
                            db_id=row.id,
                        )
                    )
        except Exception as e:
            logger.error("Failed to query recent chats: %s", e)

        return recent_chats

    @classmethod
    async def record_chat_selection(
        cls, account_id: int, chat: DiscoveredChat
    ) -> None:
        """Record or update a chat's last_used_at timestamp in the database."""
        try:
            now = datetime.now(timezone.utc)
            async with get_session() as session:
                res = await session.execute(
                    select(Chat).where(Chat.telegram_chat_id == chat.id)
                )
                existing = res.scalar_one_or_none()
                if existing:
                    existing.last_used_at = now
                    existing.telegram_account_id = account_id
                    existing.title = chat.title
                    existing.chat_type = chat.chat_type
                    existing.username = chat.username
                    existing.is_megagroup = chat.is_megagroup
                    existing.is_forum = chat.is_forum
                else:
                    new_chat = Chat(
                        telegram_account_id=account_id,
                        telegram_chat_id=chat.id,
                        title=chat.title,
                        chat_type=chat.chat_type,
                        username=chat.username,
                        is_megagroup=chat.is_megagroup,
                        is_forum=chat.is_forum,
                        last_used_at=now,
                    )
                    session.add(new_chat)
        except Exception as e:
            logger.warning("Failed to record chat selection for %s: %s", chat.id, e)

    @classmethod
    async def resolve_chat(
        cls,
        client: TelegramClient,
        identifier: str | int,
        account_id: Optional[int] = None,
    ) -> DiscoveredChat:
        """Resolve arbitrary chat ID or public username via MTProto as fallback."""
        entity = await client.get_entity(identifier)
        chat_info = cls._classify_entity(entity)
        if account_id:
            await cls.record_chat_selection(account_id, chat_info)
        return chat_info

    @classmethod
    async def _sync_chats_to_db(
        cls, account_id: int, chats: List[DiscoveredChat]
    ) -> None:
        """Batch persist discovered chats into SQLite database."""
        try:
            async with get_session() as session:
                for c in chats:
                    res = await session.execute(
                        select(Chat).where(Chat.telegram_chat_id == c.id)
                    )
                    existing = res.scalar_one_or_none()
                    if existing:
                        existing.title = c.title
                        existing.chat_type = c.chat_type
                        existing.username = c.username
                        existing.is_megagroup = c.is_megagroup
                        existing.is_forum = c.is_forum
                        if not existing.telegram_account_id:
                            existing.telegram_account_id = account_id
                    else:
                        new_chat = Chat(
                            telegram_account_id=account_id,
                            telegram_chat_id=c.id,
                            title=c.title,
                            chat_type=c.chat_type,
                            username=c.username,
                            is_megagroup=c.is_megagroup,
                            is_forum=c.is_forum,
                        )
                        session.add(new_chat)
        except Exception as e:
            logger.debug("Failed to sync chats to database: %s", e)
