from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.config import settings
from app.utils.formatting import format_progress_bar


class ProgressTracker:
    """Tracks transfer velocity, counts, and throttles Telegram UI updates."""

    def __init__(
        self,
        job_id: int,
        source_title: str,
        destination_title: str,
        topic_name: Optional[str] = None,
        total_messages: int = 0,
        update_callback: Optional[
            Callable[[str, InlineKeyboardMarkup], Coroutine]
        ] = None,
    ) -> None:
        self.job_id = job_id
        self.source_title = source_title
        self.destination_title = destination_title
        self.topic_name = topic_name
        self.total_messages = total_messages
        self.update_callback = update_callback

        self.processed_messages = 0
        self.successful_messages = 0
        self.skipped_messages = 0
        self.failed_messages = 0
        self.error_detail: Optional[str] = None

        self.start_time: float = time.time()
        self.last_update_time: float = 0.0
        self.current_download_info: Optional[Any] = None
        self._lock = asyncio.Lock()

    async def update_download_progress(self, info: Any) -> None:
        """Receive real-time chunk download throughput updates from FastMediaDownloader."""
        self.current_download_info = info
        await self.update(force=False, status_label="DOWNLOADING")

    def clear_download_progress(self) -> None:
        """Clear download progress state when media finishes or switches to uploading."""
        self.current_download_info = None

    @property
    def speed(self) -> float:
        """Calculate transfer speed in messages per second."""
        elapsed = time.time() - self.start_time
        if elapsed <= 0.5:
            return 0.0
        return self.processed_messages / elapsed

    def format_status_message(self, status_label: Optional[str] = None) -> str:
        """Format the Telegram progress message matching Section 20 specification."""
        is_local = "Local" in self.destination_title or "💾" in self.destination_title
        effective_total = max(self.total_messages, self.processed_messages) if self.total_messages > 0 else self.processed_messages

        if status_label == "COMPLETED":
            progress_bar = format_progress_bar(100, 100)
        elif self.total_messages > 0:
            progress_bar = format_progress_bar(
                self.processed_messages, self.total_messages
            )
        elif self.processed_messages > 0:
            active_step = (self.processed_messages % 15) + 1
            bar = "█" * active_step + "░" * (15 - active_step)
            progress_bar = f"{bar} Processing..."
        else:
            progress_bar = format_progress_bar(0, 0)
        speed_str = f"{self.speed:.1f} msg/s" if self.speed > 0 else "-- msg/s"

        topic_section = (
            f"\nTopic:\n🧵 {self.topic_name}\n" if (self.topic_name and not is_local) else "\n"
        )
        header_prefix = "💾 Local Download" if is_local else "📦 Transfer"
        if status_label == "COMPLETED":
            header = f"✅ {header_prefix} #{self.job_id} (COMPLETED)"
        elif status_label:
            header = f"{header_prefix} #{self.job_id} ({status_label})"
        else:
            header = f"{header_prefix} #{self.job_id}"

        dest_icon = "💾" if is_local else "👥"

        count_display = (
            f"{self.processed_messages:,} / {self.total_messages:,}"
            if self.total_messages > 0
            else f"Processed: {self.processed_messages:,}"
        )

        text = (
            f"{header}\n\n"
            f"Source:\n📢 {self.source_title}\n\n"
            f"Destination:\n{dest_icon} {self.destination_title}"
            f"{topic_section}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{progress_bar}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"{count_display}\n\n"
            f"✅ Success: {self.successful_messages:,}\n"
            f"⏭ Skipped: {self.skipped_messages:,}\n"
            f"❌ Failed: {self.failed_messages:,}\n\n"
            f"Speed: {speed_str}"
        )
        if status_label == "COMPLETED":
            if self.skipped_messages > 0 and self.successful_messages == 0:
                text += (
                    f"\n\n💡 *Note:* All {self.skipped_messages} message(s) were skipped because they were already transferred/downloaded earlier (duplicate protection) or were non-media service messages.\n"
                    f"👉 *To download new messages:* specify the next range (e.g. `6 15` or `6-20`).\n"
                    f"👉 *To re-download previous messages:* choose *🔄 Transfer again* on the duplicate handling screen."
                )
            elif self.skipped_messages > 0:
                text += f"\n\n💡 *Note:* {self.skipped_messages} message(s) skipped (already transferred or filtered)."

        if self.current_download_info:
            info = self.current_download_info
            size_mb = info.file_size / (1024 * 1024)
            size_gb = info.file_size / (1024 * 1024 * 1024)
            dl_mb = info.downloaded_bytes / (1024 * 1024)
            dl_gb = info.downloaded_bytes / (1024 * 1024 * 1024)

            if info.file_size >= 1024 * 1024 * 1024:
                size_str = f"{size_gb:.2f} GB"
                dl_str = f"{dl_gb:.2f} GB"
            else:
                size_str = f"{size_mb:.1f} MB"
                dl_str = f"{dl_mb:.1f} MB"

            cur_spd = info.current_speed / (1024 * 1024)
            avg_spd = info.average_speed / (1024 * 1024)
            eta_str = f"{int(info.eta_seconds)}s" if info.eta_seconds > 0 else "--"

            pct = min(100.0, max(0.0, info.percent))
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)

            status_header = "📤 *Uploading*" if getattr(info, "is_upload", False) else "📥 *Downloading*"
            label = "Uploaded" if getattr(info, "is_upload", False) else "Downloaded"

            text += (
                f"\n\n{status_header}\n"
                f"File: `{info.file_name}`\n"
                f"Size: {size_str}\n"
                f"{label}: {dl_str} / {size_str}\n"
                f"Progress: {pct:.1f}% [{bar}]\n"
                f"Speed: {cur_spd:.1f} MB/s (Avg: {avg_spd:.1f} MB/s)\n"
                f"ETA: {eta_str}"
            )

        if self.error_detail:
            text += f"\n\n⚠️ *Reason:* _{self.error_detail}_"
        return text

    def get_control_keyboard(self) -> InlineKeyboardMarkup:
        """Get standard control buttons for a running transfer."""
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⏸ Pause", callback_data=f"job_pause:{self.job_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"job_cancel_prompt:{self.job_id}"
                    ),
                ]
            ]
        )

    async def update(
        self,
        force: bool = False,
        status_label: Optional[str] = None,
        custom_keyboard: Optional[InlineKeyboardMarkup] = None,
        error_detail: Optional[str] = None,
    ) -> None:
        """Send throttled progress update to Telegram bot UI."""
        if error_detail:
            self.error_detail = error_detail

        if not self.update_callback:
            return

        now = time.time()
        if not force and (now - self.last_update_time) < settings.PROGRESS_UPDATE_INTERVAL:
            return

        async with self._lock:
            self.last_update_time = now
            msg_text = self.format_status_message(status_label)
            kb = custom_keyboard or self.get_control_keyboard()
            try:
                await self.update_callback(msg_text, kb)
            except Exception:
                # Silently catch MessageNotModified or transient network edit errors
                pass

