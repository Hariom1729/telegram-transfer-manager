"""Progress Tracking and UI Status Formatter."""

import asyncio
import time
from typing import Callable, Coroutine, Optional
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

        self.start_time: float = time.time()
        self.last_update_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def speed(self) -> float:
        """Calculate transfer speed in messages per second."""
        elapsed = time.time() - self.start_time
        if elapsed <= 0.5:
            return 0.0
        return self.processed_messages / elapsed

    def format_status_message(self, status_label: Optional[str] = None) -> str:
        """Format the Telegram progress message matching Section 20 specification."""
        progress_bar = format_progress_bar(
            self.processed_messages, self.total_messages
        )
        speed_str = f"{self.speed:.1f} msg/s" if self.speed > 0 else "-- msg/s"

        topic_section = (
            f"\nTopic:\n🧵 {self.topic_name}\n" if self.topic_name else "\n"
        )
        header = f"📦 Transfer #{self.job_id}"
        if status_label:
            header += f" ({status_label})"

        text = (
            f"{header}\n\n"
            f"Source:\n📢 {self.source_title}\n\n"
            f"Destination:\n👥 {self.destination_title}"
            f"{topic_section}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{progress_bar}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"{self.processed_messages:,} / {self.total_messages:,}\n\n"
            f"✅ Success: {self.successful_messages:,}\n"
            f"⏭ Skipped: {self.skipped_messages:,}\n"
            f"❌ Failed: {self.failed_messages:,}\n\n"
            f"Speed: {speed_str}"
        )
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
    ) -> None:
        """Send throttled progress update to Telegram bot UI."""
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

