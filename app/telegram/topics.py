"""Forum Supergroup Topics Management via MTProto."""

from dataclasses import dataclass
import logging
from typing import List, Optional
from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.functions.messages import (
    CreateForumTopicRequest,
    GetForumTopicsRequest,
)
from telethon.tl.types import ForumTopic, ForumTopicDeleted
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
    """Manages discovery, searching, and creation of forum supergroup topics."""

    @classmethod
    async def get_topics(
        cls,
        client: TelegramClient,
        chat_id: int,
        limit: int = 100,
    ) -> List[DiscoveredTopic]:
        """Fetch all forum topics for a forum supergroup."""
        topics: List[DiscoveredTopic] = []

        try:
            entity = await client.get_entity(chat_id)
            result = await client(
                GetForumTopicsRequest(
                    channel=entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=limit,
                )
            )

            # Always offer "General" topic (thread_id = 1) if not explicitly in list
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
            # Fallback to General topic
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
        q = query.lower()
        return [t for t in all_topics if q in t.title.lower() or str(t.id) == q]

    @classmethod
    async def create_topic(
        cls,
        client: TelegramClient,
        chat_id: int,
        title: str,
        icon_color: Optional[int] = None,
    ) -> DiscoveredTopic:
        """Create a new topic in a forum supergroup."""
        entity = await client.get_entity(chat_id)
        result = await client(
            CreateForumTopicRequest(
                channel=entity,
                title=title,
                icon_color=icon_color,
            )
        )

        # The result is Updates containing the new topic
        # Retrieve updates to find created topic ID
        topic_id = 1
        for update in getattr(result, "updates", []):
            if hasattr(update, "message") and hasattr(update.message, "id"):
                topic_id = update.message.id
                break

        new_topic = DiscoveredTopic(
            id=topic_id,
            title=title,
            icon_color=icon_color,
        )
        await cls._cache_topic(chat_id, new_topic)
        return new_topic

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
