"""Chat database model."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Chat(Base):
    """Cached Telegram dialog/chat metadata."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("telegram_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # channel, supergroup, group, private
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_megagroup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Chat id={self.id} tg_id={self.telegram_chat_id} title='{self.title}'>"

