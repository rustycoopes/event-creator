import uuid
from datetime import date, datetime

from pydantic import BaseModel


class EventRead(BaseModel):
    """A single event as returned by ``GET /api/v1/events``, including its pre-built Calendar/
    Tasks links (``None`` when the event has no resolvable date)."""

    id: uuid.UUID
    type: str
    description: str
    resolved_date: str
    resolved_date_earliest: date | None
    raw_date_text: str
    agreed_by: list[str]
    created_at: datetime
    reviewed: bool
    calendar_url: str | None
    tasks_url: str | None


class EventListRead(BaseModel):
    """One page of the current user's events, plus enough to render pagination controls."""

    events: list[EventRead]
    page: int
    page_size: int
    total: int


class EventUpdate(BaseModel):
    """Body of ``PATCH /api/v1/events/{id}`` - only the reviewed flag is user-settable."""

    reviewed: bool
