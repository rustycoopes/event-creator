import uuid
from datetime import date, datetime

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Event(Base):
    """A single agreed event extracted from a conversation by the LLM.

    Owned by event-creator since Slice R6 (adopted from the organize-me monolith's Slice R1
    schema separation — see that repo's migration d4e5f6a7b8c9_schema_separation_host_event_creator).

    ``resolved_date`` is the LLM's human-readable string (which may name several dates);
    ``resolved_date_earliest`` is the earliest date parsed from it, null when nothing parseable
    was found.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "description", "resolved_date", name="uq_events_user_description_resolved_date"
        ),
        {"schema": "event_creator"},
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("host.users.id", ondelete="cascade"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("event_creator.processing_runs.id", ondelete="cascade"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_date: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_date_earliest: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_date_text: Mapped[str] = mapped_column(Text, nullable=False)
    agreed_by: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
