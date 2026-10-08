"""Forum Supergroup Topics Management via MTProto."""

from dataclasses import dataclass
import logging
import random
from typing import Any, List, Optional, Tuple
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.messages import (
    CreateForumTopicRequest,
    GetForumTopicsRequest,
)
from telethon.tl.types import (
    Channel,
    ForumTopic,
    ForumTopicDeleted,
    MessageActionTopicCreate,
    UpdateNewChannelMessage,
)
from app.database import get_session
from app.models.topic import Topic

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredTopic:
    """Standardized representation of a forum topic."""

    id: int  # message_thread_id
    title: str
    icon_color: Optional[int] = None
    icon_emoji_id: Optional[int] = None
    is_closed: bool = False
    is_pinned: bool = False


class TopicManager:
    """Manages discovery, searching, creation, and permission verification of forum topics."""

    @classmethod
    def check_topic_permissions(cls, entity: Any) -> Tuple[bool, str]:
        """Validate whether topic creation is permitted for the destination entity."""
        if not isinstance(entity, Channel) or not getattr(entity, "megagroup", False):
            return False, "NOT_A_SUPERGROUP"
        if not getattr(entity, "forum", False):
            return False, "NOT_A_FORUM"

        # Check creator rights
        if getattr(entity, "creator", False):
            return True, "OK"

        # Check admin rights
        admin_rights = getattr(entity, "admin_rights", None)
        if admin_rights:
            if getattr(admin_rights, "manage_topics", False) or getattr(admin_rights, "change_info", False):
                return True, "OK"

        # Check default banned rights for members
        default_banned = getattr(entity, "default_banned_rights", None)
        if default_banned and getattr(default_banned, "manage_topics", False):
            return False, "PERMISSION_DENIED"

        return True, "OK"

    @classmethod
    async def get_topics(
        cls,
        client: TelegramClient,
        chat_id: int,
        limit: int = 100,
        force_refresh: bool = False,
    ) -> List[DiscoveredTopic]:
        """Fetch all forum topics for a forum supergroup using correct Telethon signature."""
        topics: List[DiscoveredTopic] = []

        try:
            entity = await client.get_entity(chat_id)
            result = await client(
                GetForumTopicsRequest(
                    peer=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=limit,
                )
            )

            has_general = False
            for item in getattr(result, "topics", []):
                if isinstance(item, ForumTopicDeleted):
                    continue
                if isinstance(item, ForumTopic):
                    t = DiscoveredTopic(
                        id=item.id,
                        title=item.title,
                        icon_color=getattr(item, "icon_color", None),
                        icon_emoji_id=getattr(item, "icon_emoji_id", None),
                        is_closed=bool(getattr(item, "closed", False)),
                        is_pinned=bool(getattr(item, "pinned", False)),
                    )
                    if item.id == 1 or item.title.lower() == "general":
                        has_general = True
                    topics.append(t)
                    await cls._cache_topic(chat_id, t)

            if not has_general:
                general = DiscoveredTopic(id=1, title="General")
                topics.insert(0, general)
                await cls._cache_topic(chat_id, general)

        except Exception as e:
            logger.error("Failed to fetch forum topics for chat %s: %s", chat_id, e)
            fallback = DiscoveredTopic(id=1, title="General")
            topics.append(fallback)

        return topics

    @classmethod
    async def search_topics(
        cls,
        client: TelegramClient,
        chat_id: int,
        query: str,
    ) -> List[DiscoveredTopic]:
        """Search forum topics matching query."""
        all_topics = await cls.get_topics(client, chat_id)
        q = query.lower().strip()
        return [t for t in all_topics if q in t.title.lower() or str(t.id) == q]

    @classmethod
    async def create_topic(
        cls,
        client: TelegramClient,
        chat_id: int,
        title: str,
        icon_color: Optional[int] = None,
        account_id: Optional[int] = None,
    ) -> DiscoveredTopic:
        """Create a new topic in a forum supergroup using inspected Telethon signature."""
        try:
            entity = await client.get_entity(chat_id)
        except Exception as e:
            logger.error(
                "topic_create_failed account_id=%s destination_chat_id=%s error_type=%s error_message=%s",
                account_id,
                chat_id,
                type(e).__name__,
                str(e),
            )
            raise

        can_create, reason = cls.check_topic_permissions(entity)
        if not can_create:
            err_msg = (
                "Topics aren't available in this destination."
                if reason in ("NOT_A_SUPERGROUP", "NOT_A_FORUM")
                else "Your connected Telegram account cannot create topics here."
            )
            logger.warning(
                "topic_create_failed account_id=%s destination_chat_id=%s error_type=PermissionDenied reason=%s",
                account_id,
                chat_id,
                reason,
            )
            raise PermissionError(err_msg)

        random_id = random.randint(1, 2**60)
        try:
            result = await client(
                CreateForumTopicRequest(
                    peer=entity,
                    title=title,
                    random_id=random_id,
                    icon_color=icon_color,
                )
            )

            # Extract created message_thread_id from Updates
            topic_id = None
            for update in getattr(result, "updates", []):
                msg = getattr(update, "message", None)
                if msg and hasattr(msg, "id"):
                    action = getattr(msg, "action", None)
                    if isinstance(action, MessageActionTopicCreate):
                        topic_id = msg.id
                        break
                    elif topic_id is None:
                        topic_id = msg.id

            # Fallback: query topics from Telegram to get the exact created topic ID
            if topic_id is None:
                try:
                    server_topics = await cls.get_topics(client, chat_id, force_refresh=True)
                    for t in server_topics:
                        if t.title == title:
                            topic_id = t.id
                            break
                except Exception as ex:
                    logger.warning("Could not resolve topic_id from server topics: %s", ex)

            if topic_id is None:
                topic_id = 1  # Fallback to General topic only if completely unable to resolve

            new_topic = DiscoveredTopic(
                id=topic_id,
                title=title,
                icon_color=icon_color,
            )
            await cls._cache_topic(chat_id, new_topic)
            return new_topic

        except RPCError as e:
            logger.error(
                "topic_create_failed account_id=%s destination_chat_id=%s error_type=%s error_message=%s",
                account_id,
                chat_id,
                type(e).__name__,
                str(e),
            )
            raise
        except Exception as e:
            logger.error(
                "topic_create_failed account_id=%s destination_chat_id=%s error_type=%s error_message=%s",
                account_id,
                chat_id,
                type(e).__name__,
                str(e),
            )
            raise

    @classmethod
    async def create_and_refresh_topic(
        cls,
        client: TelegramClient,
        chat_id: int,
        title: str,
        icon_color: Optional[int] = None,
        account_id: Optional[int] = None,
    ) -> Tuple[DiscoveredTopic, List[DiscoveredTopic]]:
        """Create a topic, refresh the topic list from Telegram, and return both."""
        created_topic = await cls.create_topic(
            client=client,
            chat_id=chat_id,
            title=title,
            icon_color=icon_color,
            account_id=account_id,
        )

        # Refresh topic list from Telegram server
        refreshed = await cls.get_topics(client, chat_id, force_refresh=True)

        # Find the newly created topic in the refreshed list if possible
        matched = next(
            (t for t in refreshed if t.id == created_topic.id or t.title == title),
            created_topic,
        )
        if matched not in refreshed:
            refreshed.append(matched)

        return matched, refreshed

    @staticmethod
    async def _cache_topic(chat_id: int, topic: DiscoveredTopic) -> None:
        """Cache forum topic in database."""
        try:
            async with get_session() as session:
                res = await session.execute(
                    select(Topic).where(
                        Topic.chat_id == chat_id, Topic.topic_id == topic.id
                    )
                )
                existing = res.scalar_one_or_none()
                if existing:
                    existing.title = topic.title
                    existing.icon_color = topic.icon_color
                    existing.icon_emoji_id = topic.icon_emoji_id
                else:
                    new_rec = Topic(
                        chat_id=chat_id,
                        topic_id=topic.id,
                        title=topic.title,
                        icon_color=topic.icon_color,
                        icon_emoji_id=topic.icon_emoji_id,
                    )
                    session.add(new_rec)
        except Exception as e:
            logger.debug("Failed to cache topic %s in chat %s: %s", topic.id, chat_id, e)
