"""Chat and Channel Discovery via MTProto."""

from dataclasses import dataclass
import logging
from typing import List, Optional
from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat as TgChat, User as TgUser
from app.database import get_session
from app.models.chat import Chat

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredChat:
    """Standardized representation of an accessible Telegram chat."""

    id: int
    title: str
    chat_type: str  # channel, supergroup, group, private
    username: Optional[str] = None
    is_forum: bool = False
    can_post: bool = True
    can_manage_topics: bool = False


class ChatDiscovery:
    """Discovers accessible channels, groups, and chats for an authenticated client."""

    @staticmethod
    def _classify_entity(entity) -> DiscoveredChat:
        """Classify a Telethon entity into DiscoveredChat."""
        is_forum = False
        can_post = True
        can_manage_topics = False

        if isinstance(entity, Channel):
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", "Unnamed Channel")
            is_forum = bool(getattr(entity, "forum", False))
            chat_id = entity.id
            # Standardize supergroup/channel ID format with -100 prefix if not present
            if not str(chat_id).startswith("-100"):
                full_id = int(f"-100{chat_id}")
            else:
                full_id = chat_id

            if getattr(entity, "megagroup", False):
                chat_type = "supergroup"
            elif getattr(entity, "broadcast", False):
                chat_type = "channel"
            else:
                chat_type = "channel"

            # Check admin rights or permissions if available
            admin_rights = getattr(entity, "admin_rights", None)
            if admin_rights:
                can_manage_topics = bool(getattr(admin_rights, "manage_topics", False))
                can_post = bool(getattr(admin_rights, "post_messages", True))
            else:
                # Regular member or creator
                can_manage_topics = bool(getattr(entity, "creator", False))

            return DiscoveredChat(
                id=full_id,
                title=title,
                chat_type=chat_type,
                username=username,
                is_forum=is_forum,
                can_post=can_post,
                can_manage_topics=can_manage_topics,
            )

        elif isinstance(entity, TgChat):
            # Standard basic group
            chat_id = entity.id
            full_id = -chat_id if chat_id > 0 else chat_id
            return DiscoveredChat(
                id=full_id,
                title=getattr(entity, "title", "Unnamed Group"),
                chat_type="group",
                username=None,
                is_forum=False,
                can_post=True,
                can_manage_topics=False,
            )

        elif isinstance(entity, TgUser):
            # Private chat
            username = getattr(entity, "username", None)
            first_name = getattr(entity, "first_name", "") or ""
            last_name = getattr(entity, "last_name", "") or ""
            title = f"{first_name} {last_name}".strip() or getattr(entity, "phone", "User")
            return DiscoveredChat(
                id=entity.id,
                title=title,
                chat_type="private",
                username=username,
                is_forum=False,
                can_post=True,
                can_manage_topics=False,
            )

        # Fallback
        entity_id = getattr(entity, "id", 0)
        return DiscoveredChat(
            id=entity_id,
            title=getattr(entity, "title", str(entity_id)),
            chat_type="chat",
            is_forum=False,
        )

    @classmethod
    async def get_dialogs(
        cls,
        client: TelegramClient,
        filter_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[DiscoveredChat]:
        """Fetch accessible dialogs from Telegram and filter by type if requested."""
        results: List[DiscoveredChat] = []

        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            chat_info = cls._classify_entity(entity)

            if filter_type:
                if filter_type == "channel" and chat_info.chat_type != "channel":
                    continue
                if filter_type == "group" and chat_info.chat_type not in (
                    "group",
                    "supergroup",
                ):
                    continue
                if filter_type == "private" and chat_info.chat_type != "private":
                    continue

            results.append(chat_info)
            # Asynchronously cache in database
            await cls._cache_chat(chat_info)

        return results

    @classmethod
    async def search_dialogs(
        cls,
        client: TelegramClient,
        query: str,
        limit: int = 20,
    ) -> List[DiscoveredChat]:
        """Search dialogs matching a query string."""
        query_lower = query.lower()
        matches: List[DiscoveredChat] = []

        async for dialog in client.iter_dialogs(limit=100):
            entity = dialog.entity
            chat_info = cls._classify_entity(entity)

            title_matches = query_lower in chat_info.title.lower()
            user_matches = (
                chat_info.username is not None
                and query_lower in chat_info.username.lower()
            )
            id_matches = str(chat_info.id) == query

            if title_matches or user_matches or id_matches:
                matches.append(chat_info)
                if len(matches) >= limit:
                    break

        return matches

    @classmethod
    async def resolve_chat(
        cls, client: TelegramClient, identifier: str | int
    ) -> DiscoveredChat:
        """Resolve arbitrary chat ID, username, or invite link via MTProto."""
        entity = await client.get_entity(identifier)
        chat_info = cls._classify_entity(entity)
        await cls._cache_chat(chat_info)
        return chat_info

    @staticmethod
    async def _cache_chat(chat_info: DiscoveredChat) -> None:
        """Cache discovered chat in database."""
        try:
            async with get_session() as session:
                res = await session.execute(
                    select(Chat).where(Chat.telegram_chat_id == chat_info.id)
                )
                existing = res.scalar_one_or_none()
                if existing:
                    existing.title = chat_info.title
                    existing.chat_type = chat_info.chat_type
                    existing.username = chat_info.username
                    existing.is_forum = chat_info.is_forum
                else:
                    new_chat = Chat(
                        telegram_chat_id=chat_info.id,
                        title=chat_info.title,
                        chat_type=chat_info.chat_type,
                        username=chat_info.username,
                        is_forum=chat_info.is_forum,
                    )
                    session.add(new_chat)
        except Exception as e:
            logger.debug("Failed to cache chat %s: %s", chat_info.id, e)

