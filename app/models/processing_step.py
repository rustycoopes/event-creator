import uuid
from datetime import datetime
from enum import Enum

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessingStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProcessingStep(Base):
    """One of the pipeline steps within a ProcessingRun, with its own status and captured log
    lines. Adopted from the organize-me monolith's Slice R1 schema separation; the pipeline that
    creates/updates these lands in Slice R8.
    """

    __tablename__ = "processing_steps"
    __table_args__ = {"schema": "event_creator"}

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("event_creator.processing_runs.id", ondelete="cascade"),
        nullable=False,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProcessingStepStatus] = mapped_column(
        SAEnum(
            ProcessingStepStatus,
            name="processing_step_status",
            schema="event_creator",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=ProcessingStepStatus.PENDING,
    )
    log_lines: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
