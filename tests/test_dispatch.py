"""Tests for the pipeline dispatch layer (Slice R11 redesign) - the async collaborator
reconstruction in app.services.pipeline.dispatch, as distinct from the pipeline logic itself (see
test_pipeline_runner.py, which drives app.services.pipeline.runner.run_pipeline directly, and the
API-level tests, which assert the upload/import endpoints dispatch to it correctly).

`_build_storage_and_file`'s fake/ephemeral branches are pure (no DB, no network) and covered
directly here. `_run_pipeline_task_async`'s "run row missing" path and `run_already_terminal` are
covered against the real `get_engine()` (no pre-seeded data needed for a bogus run id) - like
every other DB-backed test in this repo, this requires a real, reachable DATABASE_URL (see
tests/conftest.py's db_session fixture docstring).
"""

import base64
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.pipeline.dispatch as dispatch_module
from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.llm.gemini import FakeGeminiClient
from app.services.notifications.pipeline import FakeNotificationSender, NotificationOutcome
from app.services.storage.ephemeral import EphemeralStorageProvider
from app.services.storage.fake import FakeStorageProvider
from tests.conftest import create_host_user

_EXAMPLE_OUTPUT = (
    Path(__file__).resolve().parents[1] / "examples" / "example.lmmoutput.txt"
).read_text(encoding="utf-8")
_EXPECTED_NEW_EVENTS = len(
    {(e["description"], e["resolved_date"]) for e in json.loads(_EXAMPLE_OUTPUT)}
)


async def test_build_storage_and_file_fake_mode_seeds_provider_from_inline_content() -> None:
    content = b"hello world"
    storage, remote_file = await dispatch_module._build_storage_and_file(
        storage_mode="fake",
        remote_file_id="ignored",
        remote_file_name="chat.txt",
        inline_content_b64=base64.b64encode(content).decode(),
        user_id=uuid.uuid4(),
    )
    assert isinstance(storage, FakeStorageProvider)
    assert await storage.download_file(remote_file) == content
    assert remote_file.name == "chat.txt"


async def test_build_storage_and_file_ephemeral_mode_seeds_provider_from_inline_content() -> None:
    content = b"hello ephemeral"
    storage, remote_file = await dispatch_module._build_storage_and_file(
        storage_mode="ephemeral",
        remote_file_id="ignored",
        remote_file_name="chat.zip",
        inline_content_b64=base64.b64encode(content).decode(),
        user_id=uuid.uuid4(),
    )
    assert isinstance(storage, EphemeralStorageProvider)
    assert await storage.download_file(remote_file) == content


async def test_build_storage_and_file_defaults_to_empty_bytes_without_inline_content() -> None:
    storage, remote_file = await dispatch_module._build_storage_and_file(
        storage_mode="fake",
        remote_file_id="ignored",
        remote_file_name="empty.txt",
        inline_content_b64=None,
        user_id=uuid.uuid4(),
    )
    assert await storage.download_file(remote_file) == b""


async def test_build_storage_and_file_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown storage_mode"):
        await dispatch_module._build_storage_and_file(
            storage_mode="bogus",
            remote_file_id="id",
            remote_file_name="name",
            inline_content_b64=None,
            user_id=uuid.uuid4(),
        )


async def test_dispatch_logs_and_returns_when_run_row_missing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run id that doesn't exist (e.g. a stale/duplicate Cloud Tasks delivery after the row was
    somehow removed) must not raise - it's logged and dispatch returns cleanly. Uses a bogus,
    never-seeded run id, so this needs a reachable DB (per module docstring) but no fixture data.

    Bound to db_session's own connection (see test_run_already_terminal_returns_false_for_unknown_
    run's comment) rather than letting _claim_run fall through to the process-wide shared engine -
    that's what intermittently raises "Future attached to a different loop" under pytest-asyncio's
    per-test event loops."""
    await _bind_dispatch_to_session(monkeypatch, db_session)
    monkeypatch.setattr(dispatch_module, "get_gemini_client", lambda: FakeGeminiClient("[]"))
    monkeypatch.setattr(dispatch_module, "get_pipeline_notifier", lambda: FakeNotificationSender())

    await dispatch_module.run_pipeline_dispatch(
        run_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        remote_file_id="unused",
        remote_file_name="chat.txt",
        prompt_text="extract events",
        storage_mode="fake",
        inline_content_b64=base64.b64encode(b"data").decode(),
    )


async def test_run_already_terminal_returns_false_for_unknown_run(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bound to db_session's own connection (like every other DB-touching test in this file) -
    # run_already_terminal's own _session_maker() otherwise resolves to the process-wide, lru_
    # cache'd engine (app.db.session.get_engine), whose pooled connections are bound to whichever
    # event loop first created them. pytest-asyncio gives each test function its own loop by
    # default, so reusing that shared engine from a different test's loop intermittently raises
    # "Future attached to a different loop" - exactly the failure mode db_session's own docstring
    # warns about.
    await _bind_dispatch_to_session(monkeypatch, db_session)
    assert await dispatch_module.run_already_terminal(uuid.uuid4()) is False


async def _bind_dispatch_to_session(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Make every session app.services.pipeline.dispatch opens (via its own `_session_maker()`)
    share `db_session`'s connection/transaction, via the documented `AsyncSession.connection()`
    API, rather than a separate engine/connection that (correctly, per normal DB isolation) could
    never see this test's uncommitted setup rows. Each session `_session_maker()()` opens is a
    fresh `AsyncSession` bound to that shared connection (not `db_session` itself, so
    `async with ... as session:` inside dispatch.py closing it doesn't tear down the test's own
    fixture session)."""
    connection = await db_session.connection()
    bound_maker = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(dispatch_module, "_session_maker", lambda: bound_maker)


async def test_storage_setup_failure_marks_run_failed_instead_of_orphaning_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "configured" run whose storage_configs row vanished (or fails to reconstruct) before
    dispatch picked up the task must not leave the run stuck at PENDING forever - it should be
    marked FAILED and notified, the same as any other pipeline failure path."""
    await _bind_dispatch_to_session(monkeypatch, db_session)
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    notifier = FakeNotificationSender()
    monkeypatch.setattr(dispatch_module, "get_gemini_client", lambda: FakeGeminiClient("[]"))
    monkeypatch.setattr(dispatch_module, "get_pipeline_notifier", lambda: notifier)

    # storage_mode="configured" with no matching storage_configs row -> _build_storage_and_file
    # raises RuntimeError before any StorageProvider exists.
    await dispatch_module.run_pipeline_dispatch(
        run_id=str(run.id),
        user_id=str(user_id),
        remote_file_id="drive-file-id",
        remote_file_name="chat.txt",
        prompt_text="extract events",
        storage_mode="configured",
        inline_content_b64=None,
    )

    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.FAILED
    assert run.completed_at is not None
    assert len(notifier.sent) == 1
    assert notifier.sent[0].outcome == NotificationOutcome.FAILED


async def test_dispatch_runs_the_pipeline_to_completion(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the dispatch entrypoint (the same coroutine the push endpoint awaits),
    proving the fake-storage/fake-Gemini path saves events and marks the run SUCCESS."""
    await _bind_dispatch_to_session(monkeypatch, db_session)
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    monkeypatch.setattr(
        dispatch_module, "get_gemini_client", lambda: FakeGeminiClient(_EXAMPLE_OUTPUT)
    )
    monkeypatch.setattr(dispatch_module, "get_pipeline_notifier", lambda: FakeNotificationSender())

    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"5/30/26, 10:00 - Russ: hi")
    content = await storage.download_file(remote_file)

    await dispatch_module.run_pipeline_dispatch(
        run_id=str(run.id),
        user_id=str(user_id),
        remote_file_id=remote_file.id,
        remote_file_name=remote_file.name,
        prompt_text="extract events",
        storage_mode="fake",
        inline_content_b64=base64.b64encode(content).decode(),
    )

    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS
    events = (await db_session.scalars(select(Event).where(Event.user_id == user_id))).all()
    assert len(events) == _EXPECTED_NEW_EVENTS

    assert await dispatch_module.run_already_terminal(run.id) is True


async def test_claim_run_only_claims_a_pending_run_once(db_session: AsyncSession) -> None:
    """`_claim_run`'s atomic `UPDATE ... WHERE status = 'pending'` is what actually protects
    against double-processing a Cloud-Tasks-redelivered run - not `run_already_terminal`'s plain
    `SELECT` (see both functions' docstrings). A second claim attempt against the same run, once
    the first has already flipped it to IN_PROGRESS, must return None rather than a row."""
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    first = await dispatch_module._claim_run(db_session, run.id)
    assert first is not None
    assert first.status == ProcessingRunStatus.IN_PROGRESS

    second = await dispatch_module._claim_run(db_session, run.id)
    assert second is None


async def test_dispatch_does_not_reprocess_a_run_it_already_claimed_and_finished(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `run_pipeline_dispatch` call for a run that's already reached a terminal status
    (simulating a Cloud Tasks redelivery arriving after the first attempt fully completed) must
    not run the pipeline again - no second Notify send, no duplicate `processing_steps` rows."""
    await _bind_dispatch_to_session(monkeypatch, db_session)
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    notifier = FakeNotificationSender()
    monkeypatch.setattr(
        dispatch_module, "get_gemini_client", lambda: FakeGeminiClient(_EXAMPLE_OUTPUT)
    )
    monkeypatch.setattr(dispatch_module, "get_pipeline_notifier", lambda: notifier)

    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"5/30/26, 10:00 - Russ: hi")
    content = await storage.download_file(remote_file)
    dispatch_kwargs = {
        "run_id": str(run.id),
        "user_id": str(user_id),
        "remote_file_id": remote_file.id,
        "remote_file_name": remote_file.name,
        "prompt_text": "extract events",
        "storage_mode": "fake",
        "inline_content_b64": base64.b64encode(content).decode(),
    }

    await dispatch_module.run_pipeline_dispatch(**dispatch_kwargs)
    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.SUCCESS
    assert len(notifier.sent) == 1

    # Redelivery: same run_id, already terminal.
    await dispatch_module.run_pipeline_dispatch(**dispatch_kwargs)

    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.SUCCESS
    assert len(notifier.sent) == 1  # not 2 - the second call never reached run_pipeline
