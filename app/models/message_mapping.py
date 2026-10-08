"""Message Mapping database model."""

from datetime import datetime, timezone
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class MessageMapping(Base):
    """Maps original source message IDs to transferred destination message IDs."""

    __tablename__ = "message_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_message_id: Mapped[int] = mapped_column(
        Integer, index=True, nullable=False
    )
    destination_chat_id: Mapped[int] = mapped_column(
        BigInteger, index=True, nullable=False
    )
    destination_thread_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    destination_message_id: Mapped[int] = mapped_column(
        Integer, index=True, nullable=False
    )
    transfer_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transfer_jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            "destination_chat_id",
            "destination_thread_id",
            name="uq_message_source_dest",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MessageMapping src={self.source_chat_id}:{self.source_message_id} "
            f"dst={self.destination_chat_id}:{self.destination_message_id}>"
        )

