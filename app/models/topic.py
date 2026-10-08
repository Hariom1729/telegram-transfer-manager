"""Topic database model."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Topic(Base):
    """Forum supergroup topic."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    topic_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    icon_color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    icon_emoji_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "topic_id", name="uq_chat_topic"),
    )

    def __repr__(self) -> str:
        return f"<Topic chat={self.chat_id} id={self.topic_id} title='{self.title}'>"

