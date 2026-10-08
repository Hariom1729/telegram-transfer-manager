"""Message Deduplication Service."""

import logging
from typing import Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.message_mapping import MessageMapping

logger = logging.getLogger(__name__)


class DeduplicationService:
    """Detects previously transferred messages and records new mappings."""

    @staticmethod
    async def is_duplicate(
        session: AsyncSession,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_thread_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[int]]:
        """Check whether a source message was already transferred to the destination chat and topic.

        Returns:
            (is_duplicate, existing_destination_message_id)
        """
        stmt = select(MessageMapping).where(
            MessageMapping.source_chat_id == source_chat_id,
            MessageMapping.source_message_id == source_message_id,
            MessageMapping.destination_chat_id == destination_chat_id,
            MessageMapping.destination_thread_id == destination_thread_id,
        )
        result = await session.execute(stmt)
        mapping = result.scalar_one_or_none()
        if mapping:
            return True, mapping.destination_message_id
        return False, None

    @staticmethod
    async def record_mapping(
        session: AsyncSession,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_message_id: int,
        destination_thread_id: Optional[int] = None,
        transfer_job_id: Optional[int] = None,
    ) -> MessageMapping:
        """Persist a newly transferred message mapping."""
        mapping = MessageMapping(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            destination_chat_id=destination_chat_id,
            destination_thread_id=destination_thread_id,
            destination_message_id=destination_message_id,
            transfer_job_id=transfer_job_id,
        )
        session.add(mapping)
        await session.flush()
        return mapping

