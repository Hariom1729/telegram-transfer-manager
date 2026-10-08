"""Transfer Job database model."""

from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class JobStatus(str, Enum):
    """Lifecycle statuses for transfer jobs."""

    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TransferJob(Base):
    """Represents a transfer operation between Telegram chats/topics."""

    __tablename__ = "transfer_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True
    )

    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_thread_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Source Forum topic thread ID
    source_topic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


    destination_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    destination_chat_title: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    destination_thread_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Forum topic thread ID
    topic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    content_types: Mapped[str] = mapped_column(
        String(255), default="all", nullable=False
    )
    duplicate_mode: Mapped[str] = mapped_column(
        String(32), default="skip", nullable=False
    )  # "skip" or "overwrite"

    start_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.QUEUED.value, index=True, nullable=False
    )

    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_messages: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    failed_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_processed_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    telegram_account = relationship("TelegramAccount", backref="jobs")

    def __repr__(self) -> str:
        return (
            f"<TransferJob id={self.id} status={self.status} "
            f"progress={self.processed_messages}/{self.total_messages}>"
        )

