import uuid
from datetime import datetime
from enum import Enum

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessingRunStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


class ProcessingRun(Base):
    """One end-to-end processing attempt for an uploaded/detected file.

    Parent of the per-step rows (ProcessingStep) and the extracted Events. Adopted from the
    organize-me monolith's Slice R1 schema separation; the pipeline that drives runs lands in
    Slice R8.
    """

    __tablename__ = "processing_runs"
    __table_args__ = {"schema": "event_creator"}

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("host.users.id", ondelete="cascade"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProcessingRunStatus] = mapped_column(
        SAEnum(
            ProcessingRunStatus,
            name="processing_run_status",
            schema="event_creator",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=ProcessingRunStatus.PENDING,
    )
    events_extracted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
