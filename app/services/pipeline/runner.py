"""The 7-step processing pipeline (ported from organize-me's Slice 4.1/#52 to Event Creator in
Slice R8).

Turns one uploaded/detected file into extracted ``events`` rows. In the monolith this ran
in-process as a plain asyncio task; here it runs as the body of a Celery task (see
``app.worker``), invoked with its own DB session - state still lives entirely in Postgres via the
``processing_runs`` / ``processing_steps`` / ``events`` rows this writes as it goes, so the SSE
progress page can watch a run advance by polling those rows regardless of which process is driving
it.

``run_pipeline`` is deliberately pure and fully injected - it takes the DB session and its
collaborators (storage, Gemini, notifier) as arguments rather than resolving them from globals - so
the integration test drives the whole thing with a ``FakeStorageProvider`` + ``FakeGeminiClient``
and asserts events land in the DB. The Celery task (app.worker) wires the real collaborators.

The 7 steps: (1) File Received, (2) Extract, (3) Filter by Date, (4) Call Gemini, (5) Parse LLM
Response, (6) Deduplicate & Save, (7) Notify. Per the Slice 4 spec the Gemini step is fatal on
error: any failure marks the run ``failed``, moves the file to ``failed/``, records the error in the
step log, and still fires a (failure) notification. A run that produces zero new events (everything
was a duplicate) is a *success*: the file moves to ``processed/`` and a "0 new events" notice fires.
"""

import io
import json
import logging
import re
import uuid
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.date_parser import parse_earliest_date
from app.core.message_filter import filter_messages_within_window
from app.models.event import Event
from app.models.host_user import HostUser
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.schemas.pipeline import ExtractedEvent
from app.services.llm.gemini import GeminiClient, GeminiError
from app.services.notifications.pipeline import (
    NotificationOutcome,
    NotificationSender,
    PipelineNotification,
)
from app.services.storage.base import FileDestination, RemoteFile, StorageProvider
from app.services.user_settings import get_or_create_user_settings

logger = logging.getLogger(__name__)

# The 7 steps, in order. Kept here as the single source of truth so the pipeline and the SSE
# progress page agree on the count, numbering, and names.
STEP_FILE_RECEIVED = (1, "File Received")
STEP_EXTRACT = (2, "Extract")
STEP_FILTER_BY_DATE = (3, "Filter by Date")
STEP_CALL_GEMINI = (4, "Call Gemini LLM")
STEP_PARSE_RESPONSE = (5, "Parse LLM Response")
STEP_DEDUPLICATE_SAVE = (6, "Deduplicate & Save")
STEP_NOTIFY = (7, "Notify")

# The 7 steps in order, as (number, name). The single source of truth the pipeline writes and the
# SSE progress page renders from, so the two never disagree on the count/numbering/names.
PIPELINE_STEPS: list[tuple[int, str]] = [
    STEP_FILE_RECEIVED,
    STEP_EXTRACT,
    STEP_FILTER_BY_DATE,
    STEP_CALL_GEMINI,
    STEP_PARSE_RESPONSE,
    STEP_DEDUPLICATE_SAVE,
    STEP_NOTIFY,
]

DEFAULT_DATE_WINDOW_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _begin_step(
    session: AsyncSession, run_id: uuid.UUID, step: tuple[int, str]
) -> ProcessingStep:
    """Create a step row in ``in_progress`` and commit it, so the SSE page sees the step
    start the moment work on it begins."""
    number, name = step
    row = ProcessingStep(
        run_id=run_id,
        step_number=number,
        step_name=name,
        status=ProcessingStepStatus.IN_PROGRESS,
        log_lines=[],
        started_at=_utcnow(),
    )
    session.add(row)
    await session.commit()
    return row


async def _finish_step(
    session: AsyncSession,
    step: ProcessingStep,
    status: ProcessingStepStatus,
    log_lines: Iterable[str],
) -> None:
    step.status = status
    # Reassign (not mutate-in-place) so SQLAlchemy detects the JSONB change.
    step.log_lines = list(log_lines)
    step.completed_at = _utcnow()
    await session.commit()


def _extract_zip(content: bytes) -> tuple[bytes, str]:
    """Return the bytes + name of the first regular file inside a zip archive. Raises if the
    archive is unreadable or contains no files."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("archive contains no files")
        first = names[0]
        return archive.read(first), first


def _parse_events(raw: str) -> list[ExtractedEvent]:
    """Validate Gemini's raw text into ``ExtractedEvent``s. Tolerates a ```json ...``` markdown
    fence (Gemini sometimes wraps JSON in one). Raises ``ValueError``/``ValidationError`` on
    anything that isn't a JSON array of valid event objects - the caller fails the run."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of events")
    return [ExtractedEvent.model_validate(item) for item in data]


async def _deduplicate_and_save(
    session: AsyncSession,
    run: ProcessingRun,
    user_id: uuid.UUID,
    events: list[ExtractedEvent],
) -> int:
    """Insert events that aren't already present for this user, returning the new-event count.

    Duplicate detection mirrors the ``UNIQUE(user_id, description, resolved_date)`` constraint: an
    event already stored (from a prior run) is skipped, and repeats *within this batch* are
    collapsed too, so the constraint is never actually hit."""
    new_count = 0
    seen_in_batch: set[tuple[str, str]] = set()
    for event in events:
        key = (event.description, event.resolved_date)
        if key in seen_in_batch:
            continue
        seen_in_batch.add(key)
        already_exists = await session.scalar(
            select(Event.id).where(
                Event.user_id == user_id,
                Event.description == event.description,
                Event.resolved_date == event.resolved_date,
            )
        )
        if already_exists is not None:
            continue
        session.add(
            Event(
                user_id=user_id,
                run_id=run.id,
                type=event.type,
                description=event.description,
                resolved_date=event.resolved_date,
                resolved_date_earliest=parse_earliest_date(event.resolved_date),
                raw_date_text=event.raw_date_text,
                agreed_by=list(event.agreed_by),
            )
        )
        new_count += 1
    await session.flush()
    return new_count


def _silent_notification_modes_warning(
    *, notification_email: bool, notification_sms: bool, phone_number: str | None
) -> str | None:
    """A single warning line naming which notification channels will silently not fire for this
    run (issue #112), or ``None`` if every configured channel is live.

    Mirrors ``RealNotificationSender``'s own gating exactly (email: ``notification_email``; SMS:
    ``notification_sms`` **and** a non-empty ``phone_number``) so the warning never claims a
    channel is silent when the sender would actually fire it, or vice versa. "no phone number" is
    only reported when SMS is otherwise enabled - if SMS itself is off, the missing phone number
    isn't why nothing was sent, so reporting both would be redundant.
    """
    disabled: list[str] = []
    if not notification_email:
        disabled.append("disabled email")
    if not notification_sms:
        disabled.append("disabled SMS")
    elif not phone_number:
        disabled.append("no phone number")
    if not disabled:
        return None
    return f"Warning: {'; '.join(disabled)}"


async def _notify(
    session: AsyncSession,
    run: ProcessingRun,
    user_id: uuid.UUID,
    notifier: NotificationSender,
    outcome: NotificationOutcome,
    new_count: int,
    message: str,
) -> None:
    """Run step 7: send the notification and record the step. Used by both the success and the
    failure paths so the user is always told how a run ended."""
    step = await _begin_step(session, run.id, STEP_NOTIFY)
    delivery_failures = await notifier.send(
        PipelineNotification(
            user_id=user_id,
            run_id=run.id,
            filename=run.filename,
            outcome=outcome,
            new_event_count=new_count,
            message=message,
        )
    )
    log_lines = [f"Notified user: {message}"]
    # HostUser is a SELECT-ONLY cross-schema mapping onto host.users (see its module docstring) -
    # never db.add()/update/delete it.
    user = await session.get(HostUser, user_id)
    if user is None:  # pragma: no cover - the run always has a real owning user
        warning = None
    else:
        settings = await get_or_create_user_settings(session, user_id)
        warning = _silent_notification_modes_warning(
            notification_email=settings.notification_email,
            notification_sms=settings.notification_sms,
            phone_number=user.phone_number,
        )
    if warning is not None:
        log_lines.append(warning)
    # A genuine delivery failure (bad/unset credentials, the provider rejecting the recipient, a
    # network error, ...) is different from an expected disabled/unconfigured channel above - it's
    # unexpected and worth the user's/support's attention, so it's called out the same way (issue
    # #144: previously only reached server-side logs, indistinguishable from a real send).
    log_lines.extend(f"Warning: {failure}" for failure in delivery_failures)
    # Neither case fails the step or the run's overall status (issue #112's precedent extended to
    # #144: a notification problem is never a reason to mark the underlying processing as failed).
    await _finish_step(session, step, ProcessingStepStatus.SUCCESS, log_lines)


async def _fail_run(
    session: AsyncSession,
    run: ProcessingRun,
    user_id: uuid.UUID,
    storage: StorageProvider,
    remote_file: RemoteFile,
    notifier: NotificationSender,
    message: str,
) -> None:
    """Terminate a run as failed: fire the failure notification (step 7), mark the run failed, and
    move the file to ``failed/`` - in that order (step 7 recorded before the terminal status; see
    the SSE note below). Any leftover new-event count is irrelevant - a failed run saves none."""
    # Record the Notify step (step 7) *before* flipping the run to a terminal status, so the SSE
    # progress stream never observes a terminal run before all 7 step rows exist (which would
    # close the stream with the Notify indicator stuck "pending"). The file move comes last.
    await _notify(session, run, user_id, notifier, NotificationOutcome.FAILED, 0, message)
    run.status = ProcessingRunStatus.FAILED
    run.completed_at = _utcnow()
    await session.commit()
    await storage.move_file(remote_file, FileDestination.FAILED)


async def run_pipeline(
    session: AsyncSession,
    *,
    run: ProcessingRun,
    user_id: uuid.UUID,
    remote_file: RemoteFile,
    storage: StorageProvider,
    gemini: GeminiClient,
    notifier: NotificationSender,
    prompt_text: str,
    window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> None:
    """Execute the full 7-step pipeline for ``run``, writing steps + events as it goes.

    Never raises for an expected failure (bad archive, Gemini error, unparseable response): those
    mark the run ``failed`` and return. Intended to be awaited directly in tests and run inside a
    Celery task in production."""
    run.status = ProcessingRunStatus.IN_PROGRESS
    run.started_at = _utcnow()
    await session.commit()

    # Log which storage provider is being used (issue #79 - diagnostics for ephemeral fallback).
    provider_type = type(storage).__name__
    logger.info(
        "pipeline: starting run %s for user %s using %s storage provider",
        run.id,
        user_id,
        provider_type,
    )

    # Step 1 - File Received (download the bytes the upload placed in the watch folder).
    step = await _begin_step(session, run.id, STEP_FILE_RECEIVED)
    try:
        content = await storage.download_file(remote_file)
    except Exception as exc:  # a storage/network error before any processing - fail cleanly.
        logger.exception("pipeline: could not download %s", run.filename)
        await _finish_step(session, step, ProcessingStepStatus.FAILED, [f"Download failed: {exc}"])
        await _fail_run(
            session, run, user_id, storage, remote_file, notifier,
            "Could not read the uploaded file.",
        )
        return
    await _finish_step(
        session, step, ProcessingStepStatus.SUCCESS,
        [f"Received {run.filename} ({len(content)} bytes)"],
    )

    # Step 2 - Extract (unzip .zip; skip for .txt/.csv).
    step = await _begin_step(session, run.id, STEP_EXTRACT)
    if run.filename.lower().endswith(".zip"):
        try:
            content, inner_name = _extract_zip(content)
        except Exception as exc:
            await _finish_step(
                session, step, ProcessingStepStatus.FAILED, [f"Could not unzip archive: {exc}"]
            )
            await _fail_run(
                session, run, user_id, storage, remote_file, notifier,
                "Could not extract the uploaded archive.",
            )
            return
        await _finish_step(
            session, step, ProcessingStepStatus.SUCCESS, [f"Extracted {inner_name}"]
        )
    else:
        await _finish_step(
            session, step, ProcessingStepStatus.SKIPPED, ["Not a .zip; extraction skipped"]
        )

    conversation = content.decode("utf-8", errors="replace")

    # Step 3 - Filter by Date (keep only the recent window before the LLM sees it).
    step = await _begin_step(session, run.id, STEP_FILTER_BY_DATE)
    filtered = filter_messages_within_window(conversation, window_days)
    await _finish_step(
        session, step, ProcessingStepStatus.SUCCESS,
        [f"Kept messages within the last {window_days} days of the conversation"],
    )

    # Step 4 - Call Gemini (fatal on error, no retry).
    step = await _begin_step(session, run.id, STEP_CALL_GEMINI)
    try:
        raw_response = await gemini.extract(prompt=prompt_text, conversation=filtered)
    except GeminiError as exc:
        await _finish_step(
            session, step, ProcessingStepStatus.FAILED, [f"Gemini call failed: {exc}"]
        )
        await _fail_run(
            session, run, user_id, storage, remote_file, notifier,
            "The AI extraction step failed. Please try again.",
        )
        return
    await _finish_step(
        session, step, ProcessingStepStatus.SUCCESS, ["Gemini returned a response"]
    )

    # Step 5 - Parse LLM Response (Pydantic validation).
    step = await _begin_step(session, run.id, STEP_PARSE_RESPONSE)
    try:
        events = _parse_events(raw_response)
    except (ValueError, ValidationError) as exc:
        await _finish_step(
            session, step, ProcessingStepStatus.FAILED, [f"Could not parse LLM response: {exc}"]
        )
        await _fail_run(
            session, run, user_id, storage, remote_file, notifier,
            "The AI response could not be understood. Please try again.",
        )
        return
    await _finish_step(
        session, step, ProcessingStepStatus.SUCCESS, [f"Parsed {len(events)} events"]
    )

    # Step 6 - Deduplicate & Save.
    step = await _begin_step(session, run.id, STEP_DEDUPLICATE_SAVE)
    new_count = await _deduplicate_and_save(session, run, user_id, events)
    run.events_extracted_count = new_count
    await _finish_step(
        session, step, ProcessingStepStatus.SUCCESS,
        [f"Saved {new_count} new events; skipped {len(events) - new_count} duplicate(s)"],
    )

    # Success (including the zero-new-events case): record the Notify step (step 7) and fire the
    # notification, then mark the run terminal, then move the file to processed/. Marking the run
    # terminal *after* step 7 is written keeps the SSE progress stream from closing before all
    # 7 step rows exist (which would leave the Notify indicator stuck "pending").
    if new_count == 0:
        await _notify(
            session, run, user_id, notifier, NotificationOutcome.NO_NEW_EVENTS, 0,
            "Processing finished: no new events found.",
        )
    else:
        await _notify(
            session, run, user_id, notifier, NotificationOutcome.SUCCESS, new_count,
            f"Processing finished: {new_count} new event(s) added.",
        )

    run.status = ProcessingRunStatus.SUCCESS
    run.completed_at = _utcnow()
    await session.commit()
    await storage.move_file(remote_file, FileDestination.PROCESSED)
