"""Integration tests for the 7-step processing pipeline (ported from organize-me's #52 to Event
Creator in Slice R8).

Drives ``run_pipeline`` directly (awaited) with a ``FakeStorageProvider`` + fake Gemini + fake
notifier, against the rolled-back QA ``db_session`` fixture, and asserts events land in the DB and
the run/steps/file-movement/notification all reflect the outcome. This exercises the pipeline logic
itself, independent of the dispatch layer that invokes it in production (see test_dispatch.py for
that layer's own responsibilities: reconstructing collaborators from serialisable ids).
"""

import io
import json
import uuid
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.models.user_settings import UserSettings
from app.services.llm.gemini import FakeGeminiClient, GeminiError
from app.services.notifications.pipeline import FakeNotificationSender, NotificationOutcome
from app.services.pipeline.runner import run_pipeline
from app.services.storage.base import FileDestination
from app.services.storage.fake import FakeStorageProvider
from tests.conftest import create_host_user

_EXAMPLE_OUTPUT = (
    Path(__file__).resolve().parents[1] / "examples" / "example.lmmoutput.txt"
).read_text(encoding="utf-8")

# The count of distinct (description, resolved_date) pairs in the example payload - what the
# deduplicating save should land on a first, empty-DB run.
_EXPECTED_NEW_EVENTS = len(
    {(e["description"], e["resolved_date"]) for e in json.loads(_EXAMPLE_OUTPUT)}
)


async def _set_phone_number(session: AsyncSession, user_id: uuid.UUID, phone_number: str) -> None:
    """HostUser is SELECT-ONLY (see app.models.host_user's docstring) - a test that needs a
    non-default phone_number on the seeded host.users row updates it with a raw statement, the
    same way create_host_user seeds the row in the first place."""
    await session.execute(
        text("UPDATE host.users SET phone_number = :phone_number WHERE id = :id"),
        {"phone_number": phone_number, "id": user_id},
    )


async def _make_run(session: AsyncSession, user_id: uuid.UUID, filename: str) -> ProcessingRun:
    run = ProcessingRun(user_id=user_id, filename=filename, status=ProcessingRunStatus.PENDING)
    session.add(run)
    await session.flush()
    return run


async def _steps(session: AsyncSession, run_id: uuid.UUID) -> list[ProcessingStep]:
    result = await session.scalars(
        select(ProcessingStep)
        .where(ProcessingStep.run_id == run_id)
        .order_by(ProcessingStep.step_number)
    )
    return list(result.all())


async def _events(session: AsyncSession, user_id: uuid.UUID) -> list[Event]:
    result = await session.scalars(select(Event).where(Event.user_id == user_id))
    return list(result.all())


async def test_txt_upload_runs_all_steps_and_events_land_in_db(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"5/30/26, 10:00 - Russ: hi")
    notifier = FakeNotificationSender()

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=notifier,
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS
    assert len(await _events(db_session, user_id)) == _EXPECTED_NEW_EVENTS

    steps = await _steps(db_session, run.id)
    assert [s.step_number for s in steps] == [1, 2, 3, 4, 5, 6, 7]
    # Extract is skipped for a .txt; every other step succeeds.
    assert steps[1].status == ProcessingStepStatus.SKIPPED
    assert all(
        s.status == ProcessingStepStatus.SUCCESS for i, s in enumerate(steps) if i != 1
    )

    # File moved to processed/, and a single success notification fired.
    assert storage.moved[remote_file.id] == FileDestination.PROCESSED
    assert len(notifier.sent) == 1
    assert notifier.sent[0].outcome == NotificationOutcome.SUCCESS
    assert notifier.sent[0].new_event_count == _EXPECTED_NEW_EVENTS


async def test_multi_date_event_stores_earliest_date(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    # "Sunday 7 June 2026, Monday 8 June 2026" -> earliest is 2026-06-07.
    multi = await db_session.scalar(
        select(Event).where(
            Event.user_id == user_id,
            Event.resolved_date == "Sunday 7 June 2026, Monday 8 June 2026",
        )
    )
    assert multi is not None
    assert multi.resolved_date_earliest == date(2026, 6, 7)


async def test_zip_upload_is_unzipped_at_extract_step(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "export.zip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_chat.txt", "5/30/26, 10:00 - Russ: hi")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("export.zip", buffer.getvalue())

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    steps = await _steps(db_session, run.id)
    assert steps[1].status == ProcessingStepStatus.SUCCESS  # Extract ran (not skipped) for a .zip
    assert run.status == ProcessingRunStatus.SUCCESS
    assert len(await _events(db_session, user_id)) == _EXPECTED_NEW_EVENTS


async def test_csv_upload_skips_extract(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.csv")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.csv", b"5/30/26, 10:00 - Russ: hi")

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    steps = await _steps(db_session, run.id)
    assert steps[1].status == ProcessingStepStatus.SKIPPED
    assert run.status == ProcessingRunStatus.SUCCESS


async def test_duplicate_events_are_skipped(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    # Pre-insert one event that also appears in the payload, so it should be skipped as a duplicate.
    first = json.loads(_EXAMPLE_OUTPUT)[0]
    db_session.add(
        Event(
            user_id=user_id,
            run_id=run.id,
            type=first["type"],
            description=first["description"],
            resolved_date=first["resolved_date"],
            raw_date_text=first.get("raw_date_text", ""),
            agreed_by=first.get("agreed_by", []),
        )
    )
    await db_session.flush()
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS - 1
    # Total rows = the pre-seeded one + the newly saved ones, with no UNIQUE violation.
    assert len(await _events(db_session, user_id)) == _EXPECTED_NEW_EVENTS


async def test_zero_new_events_is_a_success_with_no_new_events_notice(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    payload = json.dumps(
        [
            {
                "type": "Medical",
                "description": "Dentist for Oliver.",
                "resolved_date": "Saturday 6 June 2026",
                "raw_date_text": "Saturday",
                "agreed_by": ["Russ"],
            }
        ]
    )
    # Pre-seed the only event the LLM will "return", so the run finds nothing new.
    db_session.add(
        Event(
            user_id=user_id,
            run_id=run.id,
            type="Medical",
            description="Dentist for Oliver.",
            resolved_date="Saturday 6 June 2026",
            raw_date_text="Saturday",
            agreed_by=["Russ"],
        )
    )
    await db_session.flush()
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")
    notifier = FakeNotificationSender()

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(payload),
        notifier=notifier,
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == 0
    assert storage.moved[remote_file.id] == FileDestination.PROCESSED
    assert notifier.sent[0].outcome == NotificationOutcome.NO_NEW_EVENTS


class _RaisingGemini:
    """A Gemini client that always fails, to exercise the fatal-error path."""

    async def extract(self, *, prompt: str, conversation: str) -> str:
        raise GeminiError("simulated Gemini outage")


async def test_gemini_failure_fails_run_and_moves_file_to_failed(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")
    notifier = FakeNotificationSender()

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=_RaisingGemini(),
        notifier=notifier,
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.FAILED
    assert len(await _events(db_session, user_id)) == 0
    steps = await _steps(db_session, run.id)
    gemini_step = next(s for s in steps if s.step_number == 4)
    assert gemini_step.status == ProcessingStepStatus.FAILED
    assert any("failed" in line.lower() for line in gemini_step.log_lines)
    # File went to failed/, and the user still got a failure notification (step 7).
    assert storage.moved[remote_file.id] == FileDestination.FAILED
    assert notifier.sent[-1].outcome == NotificationOutcome.FAILED


async def test_unparseable_llm_response_fails_run(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")
    notifier = FakeNotificationSender()

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient("this is not JSON at all"),
        notifier=notifier,
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.FAILED
    steps = await _steps(db_session, run.id)
    parse_step = next(s for s in steps if s.step_number == 5)
    assert parse_step.status == ProcessingStepStatus.FAILED
    assert storage.moved[remote_file.id] == FileDestination.FAILED
    assert notifier.sent[-1].outcome == NotificationOutcome.FAILED


async def test_markdown_fenced_json_is_parsed(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")
    fenced = f"```json\n{_EXAMPLE_OUTPUT}\n```"

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(fenced),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS


async def _run_notify_only(db_session: AsyncSession, user_id: uuid.UUID) -> ProcessingStep:
    """Drives a minimal zero-event run and returns its Notify (step 7) row, for asserting the
    silent-notification-mode warning independently of event-extraction behaviour."""
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient("[]"),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    steps = await _steps(db_session, run.id)
    notify_step = steps[-1]
    assert notify_step.step_number == 7
    return notify_step


async def test_notify_step_warns_when_all_channels_disabled(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(
        UserSettings(user_id=user_id, notification_email=False, notification_sms=False)
    )
    await db_session.flush()

    notify_step = await _run_notify_only(db_session, user_id)

    assert notify_step.status == ProcessingStepStatus.SUCCESS
    assert "Warning: disabled email; disabled SMS" in notify_step.log_lines


async def test_notify_step_warns_for_disabled_email_only(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(UserSettings(user_id=user_id, notification_email=False))
    await _set_phone_number(db_session, user_id, "+15551234567")
    await db_session.flush()

    notify_step = await _run_notify_only(db_session, user_id)

    assert "Warning: disabled email" in notify_step.log_lines


async def test_notify_step_warns_for_disabled_sms_only(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(UserSettings(user_id=user_id, notification_sms=False))
    await db_session.flush()

    notify_step = await _run_notify_only(db_session, user_id)

    assert "Warning: disabled SMS" in notify_step.log_lines


async def test_notify_step_warns_for_sms_enabled_without_phone_number(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    # notification_sms defaults True; phone_number defaults None - SMS is "on" but unreachable.
    await db_session.flush()

    notify_step = await _run_notify_only(db_session, user_id)

    assert "Warning: no phone number" in notify_step.log_lines


async def test_notify_step_has_no_warning_when_fully_configured(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session, phone_number="+15551234567")
    await db_session.flush()

    notify_step = await _run_notify_only(db_session, user_id)

    assert not any(line.startswith("Warning:") for line in notify_step.log_lines)
    assert notify_step.status == ProcessingStepStatus.SUCCESS


async def test_notify_step_warns_when_delivery_fails(db_session: AsyncSession) -> None:
    """Regression coverage (organize-me #144): both notification channels enabled (no "silent
    mode" warning applies) but the actual send raised - e.g. Resend's sandbox sender rejecting a
    recipient that isn't the account's own verified address. That failure must land in the Notify
    step's log_lines so it's visible via /processing-runs/{id}/logs, instead of vanishing into
    only server-side logs while the step (and the user-visible run) reports plain success."""
    user_id = await create_host_user(db_session, phone_number="+15551234567")
    await db_session.flush()
    run = await _make_run(db_session, user_id, "chat.txt")
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")
    notifier = FakeNotificationSender()
    notifier.failures = ["email delivery failed: Resend: recipient not verified"]

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient("[]"),
        notifier=notifier,
        prompt_text="extract events",
    )

    steps = await _steps(db_session, run.id)
    notify_step = steps[-1]
    assert notify_step.status == ProcessingStepStatus.SUCCESS
    assert "Warning: email delivery failed: Resend: recipient not verified" in notify_step.log_lines
