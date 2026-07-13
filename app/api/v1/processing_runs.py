"""SSE stream for live pipeline progress, list processing runs, and run detail + logs (ported
from organize-me's Slices 4.2/6.1/6.2/6.3 to Event Creator in Slice R8).

``GET /api/v1/processing-runs`` backs the logs page: the user's processing runs, paginated 50/page,
newest ``created_at`` first. ``GET /api/v1/processing-runs/{run_id}`` returns a single run with its
step statuses and detail. ``GET /api/v1/processing-runs/{run_id}/logs`` returns structured log lines
for one step (paginated, searchable). ``GET /api/v1/processing-runs/{run_id}/sse`` streams a run's
step-status transitions to the browser so the progress page can advance its 7 indicators live via
HTMX SSE — no manual refresh. The heavy lifting (polling, change detection, fragment rendering,
terminal close) lives in app.services.pipeline.progress; this module only resolves + ownership-gates
the run and wraps the generator in an ``EventSourceResponse``.

A run is only ever exposed to the user who owns it (404 otherwise), matching every other user-owned
resource in the app.
"""

import uuid
from collections import defaultdict
from datetime import date as date_
from datetime import datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.auth import current_user_id
from app.db.session import get_db
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.schemas.processing_run import (
    ProcessingLogLineRead,
    ProcessingRunDetailRead,
    ProcessingRunListRead,
    ProcessingRunLogsDownloadRead,
    ProcessingRunRead,
    ProcessingStepLogsRead,
    ProcessingStepRead,
)
from app.services.pipeline.progress import stream_run_progress
from app.services.processing_logs import LOG_PAGE_SIZE, filter_log_lines, paginate_log_lines

router = APIRouter(prefix="/api/v1", tags=["processing-runs"])

PAGE_SIZE = 50

SortColumn = Literal["date", "filename", "status"]
SortDir = Literal["asc", "desc"]

_SORT_COLUMNS = {
    "date": ProcessingRun.created_at,
    "filename": ProcessingRun.filename,
    "status": ProcessingRun.status,
}


def parse_date_param(value: str | None) -> date_ | None:
    """Parse an optional ``YYYY-MM-DD`` query param, treating "" as unset."""
    if not value:
        return None
    try:
        return date_.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid date: {value!r}, expected YYYY-MM-DD",
        ) from None


def build_run_detail_summaries(
    runs: list[ProcessingRun], steps: list[ProcessingStep]
) -> dict[uuid.UUID, str]:
    """The logs grid's expanded-details column: the first error line for a failed run, or a
    "N log lines" count otherwise. Computed in Python from already-fetched steps rather than in
    SQL - JSONB array introspection across a run's 7 steps isn't worth expressing as a query for
    a page of at most 50 runs. Shared by this router and the Logs page (app.pages.logs) so both
    compute the column the same way."""
    steps_by_run: dict[uuid.UUID, list[ProcessingStep]] = defaultdict(list)
    for step in steps:
        steps_by_run[step.run_id].append(step)

    summaries: dict[uuid.UUID, str] = {}
    for run in runs:
        run_steps = steps_by_run.get(run.id, [])
        if run.status == ProcessingRunStatus.FAILED:
            failed_lines = [
                line
                for step in run_steps
                if step.status == ProcessingStepStatus.FAILED
                for line in step.log_lines
            ]
            if not failed_lines:
                # A run can be FAILED without any step itself being marked failed (e.g. the
                # pipeline raised before updating a step's status) - fall back to any captured
                # log line, then to a fixed message, so the column is never silently blank.
                failed_lines = [line for step in run_steps for line in step.log_lines]
            first_error = failed_lines[0] if failed_lines else "No details available"
            summaries[run.id] = first_error[:100]
        else:
            total_lines = sum(len(step.log_lines) for step in run_steps)
            summaries[run.id] = f"{total_lines} log line{'s' if total_lines != 1 else ''}"
    return summaries


def to_processing_run_read(run: ProcessingRun, detail_summary: str = "") -> ProcessingRunRead:
    """Build the API/page representation of a processing run."""
    return ProcessingRunRead(
        id=run.id,
        filename=run.filename,
        status=run.status,
        events_extracted_count=run.events_extracted_count,
        created_at=run.created_at,
        detail_summary=detail_summary,
    )


async def list_user_processing_runs(
    db: AsyncSession,
    user_id: uuid.UUID,
    page: int = 1,
    *,
    run_status: ProcessingRunStatus | None = None,
    date_from: date_ | None = None,
    date_to: date_ | None = None,
    sort_by: SortColumn = "date",
    sort_dir: SortDir = "desc",
) -> tuple[list[ProcessingRun], int]:
    """The user's processing runs for one page, newest ``created_at`` first by default, plus the
    total count (for pagination).

    ``run_status``/``date_from``/``date_to`` narrow the result set (all combine with AND);
    ``sort_by``/``sort_dir`` pick the grid's active sort column and direction. All compose with
    ``page``: the count and the page window are both taken over the filtered set.

    Date bounds compare against ``created_at`` (a timestamptz) as UTC day boundaries rather than
    casting the column to a date, so the comparison stays sargable.
    """
    conditions = [ProcessingRun.user_id == user_id]
    if run_status is not None:
        conditions.append(ProcessingRun.status == run_status)
    if date_from is not None:
        conditions.append(
            ProcessingRun.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        )
    if date_to is not None:
        conditions.append(
            ProcessingRun.created_at
            < datetime.combine(date_to, time.min, tzinfo=timezone.utc) + timedelta(days=1)
        )

    total = await db.scalar(select(func.count()).select_from(ProcessingRun).where(*conditions))
    sort_column = _SORT_COLUMNS[sort_by]
    order_by = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
    result = await db.scalars(
        select(ProcessingRun)
        .where(*conditions)
        # created_at desc as a stable tiebreaker so filename/status sorts don't reorder on every
        # page load when values repeat.
        .order_by(order_by, ProcessingRun.created_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return list(result.all()), total or 0


@router.get("/processing-runs", response_model=ProcessingRunListRead)
async def read_processing_runs(
    page: int = Query(default=1, ge=1),
    run_status: ProcessingRunStatus | None = Query(default=None, alias="status"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sort_by: SortColumn = Query(default="date"),
    sort_dir: SortDir = Query(default="desc"),
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProcessingRunListRead:
    runs, total = await list_user_processing_runs(
        db,
        user_id,
        page,
        run_status=run_status,
        date_from=parse_date_param(date_from),
        date_to=parse_date_param(date_to),
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    steps_result = await db.scalars(
        select(ProcessingStep)
        .where(ProcessingStep.run_id.in_([r.id for r in runs]))
        .order_by(ProcessingStep.step_number)
    )
    summaries = build_run_detail_summaries(runs, list(steps_result.all()))
    return ProcessingRunListRead(
        runs=[to_processing_run_read(r, summaries.get(r.id, "")) for r in runs],
        page=page,
        page_size=PAGE_SIZE,
        total=total,
    )


@router.get("/processing-runs/{run_id}")
async def read_processing_run(
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProcessingRunDetailRead:
    """Run detail with all steps and their statuses."""
    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")

    steps_result = await db.scalars(
        select(ProcessingStep).where(ProcessingStep.run_id == run_id).order_by(ProcessingStep.step_number)
    )
    steps = [
        ProcessingStepRead(
            step_number=s.step_number,
            step_name=s.step_name,
            status=s.status,
            started_at=s.started_at,
            completed_at=s.completed_at,
        )
        for s in steps_result.all()
    ]

    return ProcessingRunDetailRead(
        id=run.id,
        filename=run.filename,
        status=run.status,
        events_extracted_count=run.events_extracted_count,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        steps=steps,
    )


@router.get("/processing-runs/{run_id}/logs")
async def read_processing_run_logs(
    run_id: uuid.UUID,
    step_number: int = Query(..., ge=1, le=7),
    page: int = Query(default=1, ge=1),
    search: str | None = Query(default=None),
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProcessingLogLineRead:
    """Structured log lines for one step, paginated and searchable."""
    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")

    step = await db.scalar(
        select(ProcessingStep).where(
            and_(ProcessingStep.run_id == run_id, ProcessingStep.step_number == step_number)
        )
    )
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="step_not_found")

    log_lines = filter_log_lines(step.log_lines or [], search)
    paginated, total = paginate_log_lines(log_lines, page)

    return ProcessingLogLineRead(
        step_number=step.step_number,
        step_name=step.step_name,
        log_lines=paginated,
        page=page,
        page_size=LOG_PAGE_SIZE,
        total=total,
    )


@router.get("/processing-runs/{run_id}/logs/download")
async def download_processing_run_logs(
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The run's full structured logs across all steps, as a downloadable JSON file."""
    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")

    steps_result = await db.scalars(
        select(ProcessingStep).where(ProcessingStep.run_id == run_id).order_by(ProcessingStep.step_number)
    )
    payload = ProcessingRunLogsDownloadRead(
        run_id=run.id,
        filename=run.filename,
        steps=[
            ProcessingStepLogsRead(
                step_number=s.step_number,
                step_name=s.step_name,
                status=s.status,
                log_lines=s.log_lines or [],
            )
            for s in steps_result.all()
        ],
    )

    return Response(
        content=payload.model_dump_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="processing-run-{run.id}-logs.json"'
        },
    )


@router.get("/processing-runs/{run_id}/sse")
async def processing_run_sse(
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> EventSourceResponse:
    run = await db.get(ProcessingRun, run_id)
    if run is None or run.user_id != user_id:
        # 404 (not 403) so the endpoint never confirms another user's run exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    return EventSourceResponse(stream_run_progress(db, run_id))
