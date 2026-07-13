"""List + delete/update the current user's extracted events (ported from organize-me's Slice
5.1/5.2, #54/#55/#113, to Event Creator in Slice R9).

``GET /api/v1/events`` backs the dashboard table: the user's events, paginated 50/page, newest
``resolved_date_earliest`` first by default, with optional type/date-range/search filters, a
sort toggle, and a reviewed filter (all composable with pagination). ``DELETE
/api/v1/events/{id}`` removes a single event and ``PATCH /api/v1/events/{id}`` toggles its
reviewed flag, both scoped to the requesting user so no one can touch (or even discover the
existence of) another user's event.
"""

import uuid
from datetime import date as date_
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_id
from app.core.calendar_url import build_google_calendar_url, build_google_tasks_url
from app.db.session import get_db
from app.models.event import Event
from app.schemas.event import EventListRead, EventRead, EventUpdate

SortOrder = Literal["asc", "desc"]

router = APIRouter(prefix="/api/v1", tags=["events"])

PAGE_SIZE = 50


def parse_date_param(value: str | None) -> date_ | None:
    """Parse an optional ``YYYY-MM-DD`` query param, treating "" as unset.

    The dashboard's filter form is a plain HTML ``<form>`` that HTMX serializes as-is: an empty
    ``<input type="date">`` submits ``date_from=`` (empty string), not an omitted param. FastAPI's
    own ``date`` query-param parsing rejects "" with a 422 before this code ever runs, so both
    routes declare these params as ``str | None`` and call this explicitly instead.

    Raises the same 422 FastAPI's own date parsing would have given a malformed (non-"", non-ISO)
    value, rather than letting ``ValueError`` propagate as an unhandled 500.
    """
    if not value:
        return None
    try:
        return date_.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid date: {value!r}, expected YYYY-MM-DD",
        ) from None


def to_event_read(event: Event) -> EventRead:
    """Build the API/page representation of an event, computing its Calendar/Tasks links.

    Shared by the JSON endpoint and the dashboard page (app.pages.dashboard) so both render the
    same links from one place."""
    return EventRead(
        id=event.id,
        type=event.type,
        description=event.description,
        resolved_date=event.resolved_date,
        resolved_date_earliest=event.resolved_date_earliest,
        raw_date_text=event.raw_date_text,
        agreed_by=event.agreed_by,
        created_at=event.created_at,
        reviewed=event.reviewed,
        calendar_url=build_google_calendar_url(
            title=event.description,
            event_date=event.resolved_date_earliest,
            raw_date_text=event.raw_date_text,
            agreed_by=event.agreed_by,
        ),
        tasks_url=build_google_tasks_url(
            title=event.description, due_date=event.resolved_date_earliest
        ),
    )


async def list_user_events(
    db: AsyncSession,
    user_id: uuid.UUID,
    page: int = 1,
    *,
    event_type: str | None = None,
    date_from: date_ | None = None,
    date_to: date_ | None = None,
    search: str | None = None,
    sort: SortOrder = "desc",
    show_reviewed: bool = False,
) -> tuple[list[Event], int]:
    """The user's events for one page, newest ``resolved_date_earliest`` first by default, plus
    the total count (for pagination). Shared by the JSON endpoint and the dashboard page's
    server-rendered table.

    ``event_type``/``date_from``/``date_to``/``search`` narrow the result set (all combine with
    AND); ``sort`` flips the default newest-first ordering to oldest-first. All compose with
    ``page``: the count and the page window are both taken over the filtered set.

    ``show_reviewed=False`` (the default) hides events the user has marked reviewed, so old,
    already-addressed entries don't clutter the table; pass ``True`` to show every event regardless
    of reviewed state.

    ``.nullslast()`` is required regardless of ``sort`` direction: Postgres treats NULL as larger
    than any value by default, so unresolved ("TBC") dates should always sort to the bottom, not
    flip to the top when ``sort="asc"``.
    """
    conditions = [Event.user_id == user_id]
    if not show_reviewed:
        conditions.append(Event.reviewed.is_(False))
    if event_type:
        conditions.append(Event.type == event_type)
    if date_from is not None:
        conditions.append(Event.resolved_date_earliest >= date_from)
    if date_to is not None:
        conditions.append(Event.resolved_date_earliest <= date_to)
    if search:
        # Escape LIKE metacharacters in the user's search text - unescaped, a literal "%" matches
        # everything (silently disabling the filter) and "_" matches any single character, both
        # producing false-positive results.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conditions.append(
            or_(
                Event.type.ilike(like, escape="\\"),
                Event.description.ilike(like, escape="\\"),
                Event.raw_date_text.ilike(like, escape="\\"),
                # agreed_by is a JSONB array; cast to Text for substring matching.
                cast(Event.agreed_by, Text).ilike(like, escape="\\"),
            )
        )

    total = await db.scalar(select(func.count()).select_from(Event).where(*conditions))
    if sort == "asc":
        order_by = (Event.resolved_date_earliest.asc().nullslast(), Event.created_at.asc())
    else:
        order_by = (Event.resolved_date_earliest.desc().nullslast(), Event.created_at.desc())
    result = await db.scalars(
        select(Event)
        .where(*conditions)
        .order_by(*order_by)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return list(result.all()), total or 0


async def get_owned_event(db: AsyncSession, event_id: uuid.UUID, user_id: uuid.UUID) -> Event | None:
    """The event if it exists and belongs to ``user_id``, else ``None`` - shared by ``DELETE`` and
    ``PATCH`` so both give the same 404 whether the id doesn't exist at all or belongs to another
    user, never confirming another user's event exists."""
    event: Event | None = await db.scalar(
        select(Event).where(Event.id == event_id, Event.user_id == user_id)
    )
    return event


async def list_user_event_types(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Distinct event types the user has, for the dashboard's type filter dropdown.

    Unaffected by any currently-applied filter, so the dropdown always offers every type the user
    could switch to - not just the ones present in the current (possibly already-filtered) page.
    """
    result = await db.scalars(
        select(Event.type).where(Event.user_id == user_id).distinct().order_by(Event.type)
    )
    return list(result.all())


@router.get("/events", response_model=EventListRead)
async def read_events(
    page: int = Query(default=1, ge=1),
    type: str | None = Query(default=None, alias="type"),  # noqa: A002 - query param name
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: SortOrder = Query(default="desc"),
    show_reviewed: bool = Query(default=False),
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> EventListRead:
    events, total = await list_user_events(
        db,
        user_id,
        page,
        event_type=type,
        date_from=parse_date_param(date_from),
        date_to=parse_date_param(date_to),
        search=q,
        sort=sort,
        show_reviewed=show_reviewed,
    )
    return EventListRead(
        events=[to_event_read(e) for e in events], page=page, page_size=PAGE_SIZE, total=total
    )


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> None:
    event = await get_owned_event(db, event_id, user_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await db.delete(event)
    await db.commit()


@router.patch("/events/{event_id}", response_model=EventRead)
async def update_event(
    event_id: uuid.UUID,
    update: EventUpdate,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> EventRead:
    """Toggle an event's reviewed flag, scoped to the requesting user like ``DELETE``."""
    event = await get_owned_event(db, event_id, user_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    event.reviewed = update.reviewed
    await db.commit()
    # No db.refresh() needed: the session is expire_on_commit=False (app/db/session.py), so `event`
    # still holds the value just assigned - refreshing would just be a wasted round-trip.
    return to_event_read(event)
