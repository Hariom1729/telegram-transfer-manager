"""Live Synchronization database model."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class LiveSync(Base):
    """Configuration for real-time live synchronization between chats."""

    __tablename__ = "live_syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True
    )

    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    destination_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    destination_chat_title: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    destination_thread_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    topic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    content_types: Mapped[str] = mapped_column(
        String(255), default="all", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    telegram_account = relationship("TelegramAccount", backref="live_syncs")

    def __repr__(self) -> str:
        return (
            f"<LiveSync id={self.id} src={self.source_chat_id} "
            f"dst={self.destination_chat_id} active={self.is_active}>"
        )

