"""Live progress for a processing run — the read side of the SSE page (ported from organize-me's
Slice 4.2/#53 to Event Creator in Slice R8).

The pipeline (app.services.pipeline.runner) writes a ``processing_steps`` row per step as it
advances (and flips the ``processing_runs`` status at the end) - whether it's driven by a Cloud
Tasks push handler (this repo, Slice R11 redesign), a Celery worker (this repo's original Slice
R8 design), or (historically, the monolith) an in-process asyncio task. This module turns those
rows into the stream of updates the browser's HTMX SSE extension consumes:

- :func:`build_step_views` maps the (possibly partial) set of step rows onto the canonical 7
  steps, defaulting a not-yet-started step to ``pending`` — a pure function, unit-tested.
- :func:`render_step_fragment` / :func:`render_run_status_fragment` render the exact HTML the page
  first paints *and* the SSE stream later swaps in, so both come from one template (no drift).
- :func:`stream_run_progress` polls the run's rows on a short interval and yields an SSE event only
  when a step's status (or the run's status) actually changes, closing the stream with a ``done``
  event once the run reaches a terminal state (``success``/``failed``). No pub/sub for progress
  itself - Postgres rows remain the source of truth, so this keeps working unchanged regardless of
  which mechanism dispatched the pipeline.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import templates
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.services.pipeline.runner import PIPELINE_STEPS

# Poll cadence + a hard ceiling so a wedged run can never hold the SSE connection (and its DB
# connection) open forever. ~0.75s feels live without hammering the DB. The ceiling is generous
# versus a real run but bounds the worst case.
DEFAULT_POLL_INTERVAL_SECONDS = 0.75
DEFAULT_MAX_STREAM_SECONDS = 300.0

# The run statuses that end a stream (and mean the progress page has nothing left to watch). Shared
# with app.pages.processing so the page's `live` flag and this generator's stop condition agree.
TERMINAL_RUN_STATUSES = frozenset(
    {ProcessingRunStatus.SUCCESS.value, ProcessingRunStatus.FAILED.value}
)


class StepView(NamedTuple):
    """One pipeline step as shown on the progress page: its number, name, and current status
    value (``pending``/``in_progress``/``success``/``failed``/``skipped``)."""

    number: int
    name: str
    status: str


def _status_value(status: object) -> str:
    """Normalise a step/run status (enum member or raw string) to its string value."""
    if isinstance(status, (ProcessingStepStatus, ProcessingRunStatus)):
        return str(status.value)
    return str(status)


def build_step_views(status_by_number: dict[int, object]) -> list[StepView]:
    """Project the run's recorded step statuses onto the canonical 7 steps.

    ``status_by_number`` maps a step number to whatever status the pipeline has written for it so
    far; any step without a row yet is reported ``pending``. The result is always all 7 steps in
    order, so the page renders a complete list before the run has touched every step."""
    views: list[StepView] = []
    for number, name in PIPELINE_STEPS:
        raw = status_by_number.get(number)
        status = _status_value(raw) if raw is not None else ProcessingStepStatus.PENDING.value
        views.append(StepView(number=number, name=name, status=status))
    return views


def render_step_fragment(step: StepView) -> str:
    """Render the inner HTML for one step's indicator (the SSE-swapped fragment for ``step-N``)."""
    return templates.get_template("partials/processing_step.html").render(step=step)


def render_run_status_fragment(run_status: str) -> str:
    """Render the inner HTML for the overall run-status banner (the ``run-status`` fragment)."""
    return templates.get_template("partials/processing_status.html").render(
        run_status=run_status
    )


async def load_step_statuses(session: AsyncSession, run_id: uuid.UUID) -> dict[int, object]:
    """Read the recorded status of each of the run's steps, keyed by step number.

    Column-only select (not ORM-identity reads) so repeated calls always see the latest committed
    rows. Shared by the SSE stream and the progress page's first paint."""
    rows = (
        await session.execute(
            select(ProcessingStep.step_number, ProcessingStep.status).where(
                ProcessingStep.run_id == run_id
            )
        )
    ).all()
    return {number: status for number, status in rows}


async def stream_run_progress(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_seconds: float = DEFAULT_MAX_STREAM_SECONDS,
) -> AsyncIterator[dict[str, str]]:
    """Yield SSE events (``{"event": ..., "data": ...}``) tracking ``run_id`` to a terminal state.

    Emits a ``step-N`` event whenever step N's status changes and a ``run-status`` event whenever
    the run's own status changes — each carrying the freshly rendered fragment. Once the run is
    ``success``/``failed`` it emits a final ``run-status`` (if not already) plus a ``done`` event
    and returns, which tells the HTMX SSE extension (``sse-close="done"``) to close the connection.
    A per-statement fresh read (Postgres READ COMMITTED) means the worker's commits are visible
    poll to poll without ending this read-only transaction.
    """
    last_step_status: dict[int, str] = {}
    last_run_status: str | None = None
    elapsed = 0.0

    while True:
        status_by_number = await load_step_statuses(session, run_id)
        for view in build_step_views(status_by_number):
            if last_step_status.get(view.number) != view.status:
                last_step_status[view.number] = view.status
                yield {"event": f"step-{view.number}", "data": render_step_fragment(view)}

        run_status_raw = await session.scalar(
            select(ProcessingRun.status).where(ProcessingRun.id == run_id)
        )
        run_status = _status_value(run_status_raw) if run_status_raw is not None else "pending"
        if run_status != last_run_status:
            last_run_status = run_status
            yield {"event": "run-status", "data": render_run_status_fragment(run_status)}

        if run_status in TERMINAL_RUN_STATUSES:
            yield {"event": "done", "data": run_status}
            return

        if elapsed >= max_seconds:
            # Safety valve: bound how long one connection watches a still-running run, so a wedged
            # run can't pin its DB connection forever. Return *without* a `done` event: the browser's
            # EventSource then auto-reconnects (only `done` triggers sse-close) and a fresh stream
            # resumes from the current state — so a run that legitimately runs past the cap keeps
            # advancing live across reconnects instead of freezing the page mid-progress.
            return

        # End this read-only transaction before sleeping so the pooled DB connection is released
        # back to the (transaction-mode) pooler between polls, rather than sitting idle-in-
        # transaction for the whole run. The next iteration re-acquires and re-reads.
        await session.rollback()
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
