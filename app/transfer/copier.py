"""Message Copier and Content Filter Service."""

import logging
import os
from pathlib import Path
from typing import List, Optional
import uuid
from telethon import TelegramClient
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    MessageActionChatCreate,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageService,
)

logger = logging.getLogger(__name__)


class MessageCopier:
    """Handles content filtering and high-fidelity message copying."""

    @staticmethod
    def classify_message_content(message: Message) -> str:
        """Determine content type of a Telethon message."""
        if isinstance(message, MessageService) or message.action:
            return "service"

        if message.photo or isinstance(message.media, MessageMediaPhoto):
            return "photo"

        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            if doc and hasattr(doc, "attributes"):
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        return "video"
                    if isinstance(attr, DocumentAttributeAudio):
                        return "audio"
                    if isinstance(attr, DocumentAttributeAnimated):
                        return "animation"
            return "document"

        if message.video:
            return "video"
        if message.audio or message.voice:
            return "audio"
        if message.gif:
            return "animation"
        if message.document or message.file:
            return "document"

        if isinstance(message.media, MessageMediaPoll):
            return "poll"

        if message.text or message.message:
            return "text"

        return "unknown"

    @classmethod
    def should_transfer_message(
        cls, message: Message, allowed_types: List[str]
    ) -> bool:
        """Check if message matches configured content types filter."""
        # Always skip chat service messages (e.g. pinned message notifications, member joins)
        if isinstance(message, MessageService) or getattr(message, "action", None):
            return False

        if "all" in allowed_types or "everything" in allowed_types:
            return True

        content_type = cls.classify_message_content(message)

        if content_type in allowed_types:
            return True

        # Treat files and documents synonymously
        if "file" in allowed_types and content_type == "document":
            return True
        if "document" in allowed_types and content_type == "file":
            return True

        return False

    @classmethod
    async def copy_message(
        cls,
        client: TelegramClient,
        destination_entity,
        message: Message,
        destination_thread_id: Optional[int] = None,
    ) -> int:
        """Copy a message to destination entity and optional forum topic thread.

        Preserves text formatting, captions, and media without exposing forward header.
        Returns the destination message ID.
        """
        # Format reply target for forum topic
        reply_to = destination_thread_id if destination_thread_id else None

        # 1. Plain text message without media
        if not message.media:
            text_content = message.message or ""
            if not text_content:
                # Nothing to transfer
                return 0

            sent = await client.send_message(
                entity=destination_entity,
                message=text_content,
                formatting_entities=message.entities,
                reply_to=reply_to,
                link_preview=bool(message.web_preview),
            )
            return sent.id

        # 2. Media message (Photo, Video, Audio, Document, Animation)
        caption = message.message or ""
        try:
            # First attempt: Send file directly using original media reference
            sent = await client.send_file(
                entity=destination_entity,
                file=message.media,
                caption=caption,
                formatting_entities=message.entities,
                reply_to=reply_to,
            )
            return sent.id
        except Exception as media_err:
            logger.debug(
                "Direct send_file failed (%s). Attempting forward with drop_author...",
                media_err,
            )

        # 3. Fallback: forward_messages with drop_author=True (removes forward tag)
        try:
            forwarded = await client.forward_messages(
                entity=destination_entity,
                messages=message,
                drop_author=True,
                reply_to=reply_to,
            )
            if isinstance(forwarded, list):
                return forwarded[0].id if forwarded else 0
            return getattr(forwarded, "id", 0)
        except Exception as forward_err:
            logger.debug(
                "Forward with drop_author failed (%s). Attempting download and re-upload...",
                forward_err,
            )

        # 4. Final fallback for channels with strict restrictions:
        # Stream download media to temporary disk file to protect low RAM servers (e.g. 1GB/2GB AWS)
        # and delete immediately after upload
        temp_dir = Path("data/temp")
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"stream_{message.chat_id}_{message.id}_{uuid.uuid4().hex[:8]}"
        downloaded_path = None
        try:
            downloaded_path = await client.download_media(message, file=str(temp_path))
            if downloaded_path and os.path.exists(downloaded_path):
                sent = await client.send_file(
                    entity=destination_entity,
                    file=downloaded_path,
                    caption=caption,
                    formatting_entities=message.entities,
                    reply_to=reply_to,
                    attributes=getattr(message.document, "attributes", None),
                )
                return sent.id
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if downloaded_path and os.path.exists(downloaded_path):
                try:
                    os.remove(downloaded_path)
                except Exception:
                    pass

        raise RuntimeError(
            f"Unable to transfer message {message.id}: unsupported or inaccessible media."
        )

    @classmethod
    async def download_to_local(
        cls,
        client: TelegramClient,
        message: Message,
        download_dir: Path,
    ) -> Optional[str]:
        """Download message media or text directly to a local directory.

        Supports restricted/copy-protected channels (noforwards) and all media types.
        """
        download_dir.mkdir(parents=True, exist_ok=True)

        # 1. Media message (Photo, Video, Audio, Document, Voice, Animation)
        if message.media:
            try:
                # Telethon's download_media handles streaming from MTProto even on restricted channels
                downloaded_path = await client.download_media(
                    message, file=str(download_dir)
                )
                if downloaded_path:
                    # If there's an accompanying caption, write it to a companion .caption.txt file
                    caption = (message.message or "").strip()
                    if caption:
                        caption_file = Path(downloaded_path).with_suffix(
                            Path(downloaded_path).suffix + ".caption.txt"
                        )
                        try:
                            caption_file.write_text(caption, encoding="utf-8")
                        except Exception as ce:
                            logger.debug("Could not write caption file: %s", ce)
                    return str(downloaded_path)
            except Exception as e:
                logger.error(
                    "Failed to download media for message %s: %s", message.id, e
                )
                raise

        # 2. Text-only message
        text_content = (message.message or "").strip()
        if text_content:
            txt_path = download_dir / f"msg_{message.id}.txt"
            txt_path.write_text(text_content, encoding="utf-8")
            return str(txt_path)

        return None


message_copier = MessageCopier()


