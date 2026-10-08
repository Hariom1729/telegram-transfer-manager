"""Transfer Error database model."""

from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class TransferError(Base):
    """Detailed record of an individual failed message transfer."""

    __tablename__ = "transfer_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transfer_job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transfer_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_message_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    transfer_job = relationship("TransferJob", backref="errors")

    def __repr__(self) -> str:
        return (
            f"<TransferError job={self.transfer_job_id} msg={self.source_message_id} "
            f"type='{self.error_type}'>"
        )

