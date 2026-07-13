"""The Processing progress page and run detail page (ported from organize-me's Slice 4.2/#53 and
Slice 6.2/#84 to Event Creator in Slice R8/R9).

Shows the 7 pipeline-step indicators for a processing run and streams live updates from
``/api/v1/processing-runs/{id}/sse`` via the HTMX SSE extension. The Upload flow (#52/R8) sends
the user here as ``/processing?run=<id>`` right after an upload; opening ``/processing`` with no
run (e.g. the sidebar link) falls back to the user's most recent run, or an empty state if they
have none.

The progress page paints the run's current step states server-side first, then the SSE stream
takes over — so a run that already finished before the page loaded still renders correctly, and a
running one advances live.

The detail page (``/processing-runs/{id}``) shows a historical run with final step statuses and
structured log lines for each step (paginated, searchable via HTMX).

Auth follows this repo's own convention (app.core.auth), not the monolith's fastapi-users: page
routes use ``current_user_id_optional`` and redirect to the Host's ``/login`` on ``None``; the
HTMX log-partial endpoint uses ``current_user_id`` (401), matching every other JSON/HTML-partial
endpoint fetched via ``hx-get`` from an already-authenticated page.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_id, current_user_id_optional
from app.core.templating import templates
from app.db.session import get_db
from app.models.processing_run import ProcessingRun
from app.models.processing_step import ProcessingStep
from app.services.pipeline.progress import (
    TERMINAL_RUN_STATUSES,
    build_step_views,
    load_step_statuses,
)
from app.services.processing_logs import LOG_PAGE_SIZE, filter_log_lines, paginate_log_lines

router = APIRouter(tags=["pages"])


async def _latest_run(db: AsyncSession, user_id: uuid.UUID) -> ProcessingRun | None:
    run: ProcessingRun | None = await db.scalar(
        select(ProcessingRun)
        .where(ProcessingRun.user_id == user_id)
        .order_by(ProcessingRun.created_at.desc())
        .limit(1)
    )
    return run


async def _owned_run(
    db: AsyncSession, user_id: uuid.UUID, run_id: uuid.UUID
) -> ProcessingRun | None:
    run = await db.get(ProcessingRun, run_id)
    return run if run is not None and run.user_id == user_id else None


def _parse_run_id(run: str | None) -> uuid.UUID | None:
    """Parse the ?run= query value to a UUID, tolerating a missing/malformed value (-> None).

    Kept lenient (rather than a typed UUID query param that would 422) so a stale or hand-edited
    link falls through to the latest-run fallback instead of erroring."""
    if not run:
        return None
    try:
        return uuid.UUID(run)
    except ValueError:
        return None


@router.get("/processing", response_model=None)
async def processing_page(
    request: Request,
    run: str | None = Query(default=None),
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    if user_id is None:
        return RedirectResponse("/login", status_code=302)

    # A ?run= that's malformed, isn't the user's, or doesn't exist falls back to their latest run
    # rather than leaking a 404 or a 422 — the page never exposes another user's run.
    run_id = _parse_run_id(run)
    processing_run = (
        await _owned_run(db, user_id, run_id) if run_id is not None else None
    ) or await _latest_run(db, user_id)

    steps = []
    if processing_run is not None:
        steps = build_step_views(await load_step_statuses(db, processing_run.id))

    run_status = processing_run.status.value if processing_run is not None else None
    # Only stream when there's something left to watch: a run that already finished renders its
    # final state statically (no wasted SSE connection just to receive one update and close).
    live = processing_run is not None and run_status not in TERMINAL_RUN_STATUSES

    return templates.TemplateResponse(
        request,
        "pages/processing.html",
        {
            # No per-user dark-mode preference lookup here - Event Creator has no User model of
            # its own (see app.pages.dashboard).
            "dark_mode": False,
            "run": processing_run,
            "run_id": str(processing_run.id) if processing_run is not None else None,
            "run_status": run_status,
            "filename": processing_run.filename if processing_run is not None else None,
            "steps": steps,
            "live": live,
        },
    )


@router.get("/processing-runs/{run_id}", response_model=None)
async def processing_run_detail_page(
    request: Request,
    run_id: uuid.UUID,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Detail page for a historical processing run (ported from Slice 6.2/#84).

    Displays the run's metadata, step statuses, and provides HTMX-driven log viewing for each
    step.
    """
    if user_id is None:
        return RedirectResponse("/login", status_code=302)

    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    steps = build_step_views(await load_step_statuses(db, run.id))

    return templates.TemplateResponse(
        request,
        "pages/processing_run_detail.html",
        {
            "dark_mode": False,
            "run": run,
            "run_id": str(run.id),
            "steps": steps,
        },
    )


@router.get("/api/html/processing-runs/{run_id}/logs", response_model=None)
async def processing_run_logs_partial(
    request: Request,
    run_id: uuid.UUID,
    step_number: int = Query(..., ge=1, le=7),
    page: int = Query(default=1, ge=1),
    search: str | None = Query(default=None),
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Log lines for one step as HTML partial, paginated and searchable (ported from #84).

    Used by HTMX to swap logs into the detail page. Returns HTML (not JSON) for direct insertion.
    """
    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    step = await db.scalar(
        select(ProcessingStep).where(
            and_(ProcessingStep.run_id == run_id, ProcessingStep.step_number == step_number)
        )
    )
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    log_lines = filter_log_lines(step.log_lines or [], search)
    paginated, total = paginate_log_lines(log_lines, page)

    return templates.TemplateResponse(
        request,
        "partials/processing_logs.html",
        {
            "run_id": str(run_id),
            "logs": type(
                "Logs",
                (),
                {
                    "step_number": step.step_number,
                    "step_name": step.step_name,
                    "log_lines": paginated,
                    "page": page,
                    "page_size": LOG_PAGE_SIZE,
                    "total": total,
                },
            )(),
            "search": search,
        },
    )
