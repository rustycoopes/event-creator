import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

# Moved here (rather than app/api/v1/events.py, where the paginated GET/list query uses it) so
# BulkEventIdsRequest can enforce "at most one page's worth of ids" without an import cycle -
# app/api/v1/events.py already imports the schemas below, so a schemas -> events.py import back
# would be circular. events.py re-imports PAGE_SIZE from here instead.
PAGE_SIZE = 50


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


class BulkEventIdsRequest(BaseModel):
    """Body shared by bulk endpoints (currently just bulk-delete) - just the target ids.
    ``extra="forbid"`` so a stray unexpected field 422s loudly instead of silently no-op'ing.

    ``max_length=PAGE_SIZE`` enforces "current page only" server-side too, not just in the
    frontend - an oversized or crafted request already represents invalid application state, so
    it 422s rather than being trusted."""

    model_config = ConfigDict(extra="forbid")
    event_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=PAGE_SIZE)


class BulkActionResult(BaseModel):
    """Response for bulk endpoints. ``succeeded_ids`` is the full list (not just a count) since
    it costs nothing extra (already have it from ``RETURNING``) and lets the frontend know
    exactly which rows changed."""

    succeeded_ids: list[uuid.UUID]
    failed_ids: list[uuid.UUID]
