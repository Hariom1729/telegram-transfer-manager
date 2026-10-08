from dataclasses import dataclass
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, List, Optional
import uuid

from telethon import TelegramClient, utils
from telethon.errors import (
    ServerError,
    TimedOutError,
)
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

from app.transfer.downloader import fast_media_downloader
from app.transfer.retry import NonRetryableTransferError
from app.utils.paths import (
    get_temp_download_directory,
    get_unique_filepath,
    sanitize_filename,
)

logger = logging.getLogger(__name__)


@dataclass
class TransferDiagnostics:
    """Detailed performance and routing diagnostics for a message transfer."""

    method: str  # 'native' or 'download_upload'
    file_size: int = 0  # bytes
    download_time: float = 0.0  # seconds
    upload_time: float = 0.0  # seconds
    total_time: float = 0.0  # seconds
    download_speed: float = 0.0  # bytes/second
    upload_speed: float = 0.0  # bytes/second
    transfer_speed: float = 0.0  # overall bytes/second
    error: Optional[str] = None

    @property
    def file_size_mb(self) -> float:
        return round(self.file_size / (1024 * 1024), 2)

    @property
    def download_speed_mb(self) -> float:
        return round(self.download_speed / (1024 * 1024), 2)

    @property
    def upload_speed_mb(self) -> float:
        return round(self.upload_speed / (1024 * 1024), 2)

    @property
    def transfer_speed_mb(self) -> float:
        return round(self.transfer_speed / (1024 * 1024), 2)


class MessageCopier:
    """Handles content filtering, capability-based routing, and high-fidelity message copying."""

    last_diagnostics: Optional[TransferDiagnostics] = None

    @staticmethod
    def is_protected_or_restricted(
        message: Message, source_entity: Optional[Any] = None
    ) -> bool:
        """Determine if a message or its origin chat has copy protection or forward restrictions.

        When protected or restricted, Telegram-native forwarding/copying will either fail
        or stamp copyright restriction flags onto the copied message, causing client devices
        to display: 'This message couldn't be displayed on your device due to copyright infringement.'
        """
        # 1. Message-level restrictions
        if getattr(message, "noforwards", None) is True:
            return True
        if getattr(message, "restricted", None) is True:
            return True
        msg_rr = getattr(message, "restriction_reason", None)
        if isinstance(msg_rr, list) and len(msg_rr) > 0:
            return True

        # 2. Source entity restrictions (if provided)
        if source_entity is not None:
            if getattr(source_entity, "noforwards", None) is True:
                return True
            if getattr(source_entity, "restricted", None) is True:
                return True
            src_rr = getattr(source_entity, "restriction_reason", None)
            if isinstance(src_rr, list) and len(src_rr) > 0:
                return True

        # 3. Message chat restrictions (from Telethon message.chat)
        chat = getattr(message, "chat", None)
        if chat is not None:
            if getattr(chat, "noforwards", None) is True:
                return True
            if getattr(chat, "restricted", None) is True:
                return True
            chat_rr = getattr(chat, "restriction_reason", None)
            if isinstance(chat_rr, list) and len(chat_rr) > 0:
                return True

        return False

    @classmethod
    def _log_diagnostics(
        cls,
        job_id: Optional[int],
        message_id: int,
        diag: TransferDiagnostics,
    ) -> None:
        """Log internal performance diagnostics."""
        cls.last_diagnostics = diag
        if diag.method == "download_upload":
            logger.info(
                "Transfer diagnostics job=%s message=%s method=%s size=%.2fMB (%d bytes) "
                "download_time=%.2fs (%.2f MB/s) upload_time=%.2fs (%.2f MB/s) "
                "total_time=%.2fs speed=%.2f MB/s",
                job_id if job_id is not None else "-",
                message_id,
                diag.method,
                diag.file_size_mb,
                diag.file_size,
                diag.download_time,
                diag.download_speed_mb,
                diag.upload_time,
                diag.upload_speed_mb,
                diag.total_time,
                diag.transfer_speed_mb,
            )
        else:
            logger.info(
                "Transfer diagnostics job=%s message=%s method=%s size=%.2fMB (%d bytes) "
                "total_time=%.2fs speed=%.2f MB/s",
                job_id if job_id is not None else "-",
                message_id,
                diag.method,
                diag.file_size_mb,
                diag.file_size,
                diag.total_time,
                diag.transfer_speed_mb,
            )

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
        destination_entity: Any,
        message: Message,
        destination_thread_id: Optional[int] = None,
        job_id: Optional[int] = None,
        source_entity: Optional[Any] = None,
        on_diagnostics: Optional[Callable[[TransferDiagnostics], None]] = None,
        download_progress_callback: Optional[Callable[[Any], Coroutine]] = None,
    ) -> int:
        """Copy a message to destination entity using capability-based routing.

        Strategy:
        1. Text-only messages: send natively via client.send_message.
        2. Unrestricted media messages: attempt native copy/forward first.
        3. Restricted media messages OR rejected native forwards:
           Fall back to authorized media download/upload via Telethon's MTProto chunk streaming,
           using file-backed OS temporary storage (never in RAM), creating a clean new media entity.
        4. If Telegram denies media access to this account:
           Raise NonRetryableTransferError("Telegram did not make the media available to this account.").
        """
        reply_to = destination_thread_id if destination_thread_id else None

        # 1. Plain text message without media
        if not message.media:
            text_content = message.message or ""
            if not text_content:
                return 0

            t_start = time.perf_counter()
            sent = await client.send_message(
                entity=destination_entity,
                message=text_content,
                formatting_entities=message.entities,
                reply_to=reply_to,
                link_preview=bool(message.web_preview),
            )
            t_end = time.perf_counter()
            total_time = max(0.001, t_end - t_start)
            content_bytes = len(text_content.encode("utf-8"))

            diag = TransferDiagnostics(
                method="native",
                file_size=content_bytes,
                total_time=total_time,
                transfer_speed=content_bytes / total_time,
            )
            cls._log_diagnostics(job_id, message.id, diag)
            if on_diagnostics:
                on_diagnostics(diag)
            return sent.id

        # 2. Media message (Photo, Video, Audio, Document, Animation)
        caption = message.message or ""
        is_restricted = cls.is_protected_or_restricted(message, source_entity)

        # Attempt native copy/forward ONLY if message and source are not restricted/protected
        if not is_restricted:
            t_native_start = time.perf_counter()
            try:
                # First attempt: direct send_file using original media reference
                sent = await client.send_file(
                    entity=destination_entity,
                    file=message.media,
                    caption=caption,
                    formatting_entities=message.entities,
                    reply_to=reply_to,
                )
                t_native_end = time.perf_counter()
                total_time = max(0.001, t_native_end - t_native_start)
                fsize = getattr(getattr(message, "file", None), "size", 0) or 0
                diag = TransferDiagnostics(
                    method="native",
                    file_size=fsize,
                    total_time=total_time,
                    transfer_speed=fsize / total_time if fsize else 0.0,
                )
                cls._log_diagnostics(job_id, message.id, diag)
                if on_diagnostics:
                    on_diagnostics(diag)
                return sent.id
            except Exception as media_err:
                logger.debug(
                    "Direct send_file failed for message %s (%s). Attempting forward with drop_author...",
                    message.id,
                    media_err,
                )
                try:
                    forwarded = await client.forward_messages(
                        entity=destination_entity,
                        messages=message,
                        drop_author=True,
                        reply_to=reply_to,
                    )
                    t_native_end = time.perf_counter()
                    dest_id = (
                        forwarded[0].id
                        if isinstance(forwarded, list) and forwarded
                        else getattr(forwarded, "id", 0)
                    )
                    if dest_id:
                        total_time = max(0.001, t_native_end - t_native_start)
                        fsize = getattr(getattr(message, "file", None), "size", 0) or 0
                        diag = TransferDiagnostics(
                            method="native",
                            file_size=fsize,
                            total_time=total_time,
                            transfer_speed=fsize / total_time if fsize else 0.0,
                        )
                        cls._log_diagnostics(job_id, message.id, diag)
                        if on_diagnostics:
                            on_diagnostics(diag)
                        return dest_id
                except Exception as fwd_err:
                    logger.debug(
                        "Native forward failed for message %s (%s).",
                        message.id,
                        fwd_err,
                    )
            logger.info(
                "Native transfer unavailable or rejected for message %s. Falling back to authorized media download/upload path...",
                message.id,
            )
        else:
            logger.info(
                "Message %s is copy-protected/restricted. Routing directly to authorized media download/upload pipeline.",
                message.id,
            )

        # 3. Authorized media download/upload pipeline:
        # Stream download media chunks into a file-backed OS temporary location to protect RAM,
        # upload cleanly to destination, and clean up temporary storage in finally block.
        temp_dir = get_temp_download_directory()
        msg_chat_id = getattr(message, "chat_id", 0)
        temp_path = temp_dir / f"stream_{msg_chat_id}_{message.id}_{uuid.uuid4().hex[:8]}"
        downloaded_path = None

        try:
            t_dl_start = time.perf_counter()
            try:
                downloaded_path = await fast_media_downloader.download_media(
                    client=client,
                    message=message,
                    target_path=temp_path,
                    progress_callback=download_progress_callback,
                    job_id=job_id,
                )
            except (TimedOutError, ServerError, ConnectionError):
                raise
            except NonRetryableTransferError:
                raise
            except Exception as dl_err:
                logger.error(
                    "Telegram media retrieval error for message %s: %s",
                    message.id,
                    dl_err,
                )
                raise NonRetryableTransferError(
                    "Telegram did not make the media available to this account."
                ) from dl_err

            t_dl_end = time.perf_counter()

            # Verify downloaded file
            if (
                not downloaded_path
                or not os.path.exists(downloaded_path)
                or os.path.getsize(downloaded_path) == 0
            ):
                raise NonRetryableTransferError(
                    "Telegram did not make the media available to this account."
                )

            file_size = os.path.getsize(downloaded_path)
            dl_time = max(0.001, t_dl_end - t_dl_start)
            dl_speed = file_size / dl_time

            # Upload freshly downloaded media file from disk
            t_ul_start = time.perf_counter()
            attributes = None
            if hasattr(message, "document") and message.document:
                attributes = getattr(message.document, "attributes", None)

            sent = await client.send_file(
                entity=destination_entity,
                file=downloaded_path,
                caption=caption,
                formatting_entities=message.entities,
                reply_to=reply_to,
                attributes=attributes,
                supports_streaming=bool(getattr(message, "video", None)),
            )
            t_ul_end = time.perf_counter()
            ul_time = max(0.001, t_ul_end - t_ul_start)
            ul_speed = file_size / ul_time
            total_time = dl_time + ul_time
            tot_speed = file_size / total_time

            diag = TransferDiagnostics(
                method="download_upload",
                file_size=file_size,
                download_time=dl_time,
                upload_time=ul_time,
                total_time=total_time,
                download_speed=dl_speed,
                upload_speed=ul_speed,
                transfer_speed=tot_speed,
            )
            cls._log_diagnostics(job_id, message.id, diag)
            if on_diagnostics:
                on_diagnostics(diag)

            return sent.id

        finally:
            for p in (
                temp_path,
                temp_path.with_name(f"{temp_path.name}.part"),
                temp_path.with_name(f"{temp_path.name}.part.json"),
                Path(downloaded_path) if downloaded_path else None,
            ):
                if p and p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass

    @classmethod
    def get_suggested_filename(cls, message: Message) -> str:
        """Extract original filename if available, or generate a safe media filename."""
        # 1. Direct file name attribute
        if getattr(message, "file", None) and getattr(message.file, "name", None):
            return sanitize_filename(message.file.name)
        if hasattr(message, "document") and message.document:
            for attr in getattr(message.document, "attributes", []):
                if hasattr(attr, "file_name") and attr.file_name:
                    return sanitize_filename(attr.file_name)

        # 2. Derive extension from media or Telethon helpers
        ext = getattr(getattr(message, "file", None), "ext", None)
        if not ext and hasattr(message, "media"):
            ext = utils.get_extension(message.media) or ""

        date_str = (
            message.date.strftime("%Y-%m-%d_%H-%M-%S")
            if getattr(message, "date", None)
            else f"msg_{message.id}"
        )

        if getattr(message, "photo", None):
            return f"photo_{date_str}{ext or '.jpg'}"
        if getattr(message, "video", None):
            return f"video_{date_str}{ext or '.mp4'}"
        if getattr(message, "audio", None):
            return f"audio_{date_str}{ext or '.mp3'}"
        if getattr(message, "voice", None):
            return f"voice_{date_str}{ext or '.ogg'}"
        return f"media_{date_str}{ext or ''}"

    @classmethod
    async def download_to_local(
        cls,
        client: TelegramClient,
        message: Message,
        download_dir: Path,
        job_id: Optional[int] = None,
        download_progress_callback: Optional[Callable[[Any], Coroutine]] = None,
    ) -> Optional[str]:
        """Download message media or text directly to a local directory.

        Supports restricted/copy-protected channels (noforwards) and all media types.
        Preserves original filename, prevents collisions, and writes companion caption files.
        """
        download_dir.mkdir(parents=True, exist_ok=True)

        # 1. Media message (Photo, Video, Audio, Document, Voice, Animation)
        if message.media:
            candidate_name = cls.get_suggested_filename(message)
            target_file = get_unique_filepath(download_dir, candidate_name)
            t_start = time.perf_counter()
            try:
                downloaded_path = await fast_media_downloader.download_media(
                    client=client,
                    message=message,
                    target_path=target_file,
                    progress_callback=download_progress_callback,
                    job_id=job_id,
                )
            except (TimedOutError, ServerError, ConnectionError):
                raise
            except NonRetryableTransferError:
                raise
            except Exception as e:
                logger.error(
                    "Failed to download media for message %s: %s", message.id, e
                )
                raise NonRetryableTransferError(
                    "Telegram did not make the media available to this account."
                ) from e

            t_end = time.perf_counter()
            if not downloaded_path or not os.path.exists(downloaded_path):
                raise NonRetryableTransferError(
                    "Telegram did not make the media available to this account."
                )

            file_size = os.path.getsize(downloaded_path)
            dl_time = max(0.001, t_end - t_start)
            logger.info(
                "Local download completed message=%s file=%s size=%.2fMB download_time=%.2fs (%.2f MB/s)",
                message.id,
                os.path.basename(downloaded_path),
                file_size / (1024 * 1024),
                dl_time,
                (file_size / dl_time) / (1024 * 1024),
            )

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

        # 2. Text-only message
        text_content = (message.message or "").strip()
        if text_content:
            txt_path = get_unique_filepath(download_dir, f"msg_{message.id}.txt")
            txt_path.write_text(text_content, encoding="utf-8")
            return str(txt_path)

        return None


message_copier = MessageCopier()


