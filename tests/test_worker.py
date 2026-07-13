"""Tests for the Celery task layer (Slice R8) - the async-to-sync bridge and collaborator
reconstruction in app.worker, as distinct from the pipeline logic itself (see
test_pipeline_runner.py, which drives app.services.pipeline.runner.run_pipeline directly, and the
API-level tests, which assert the upload/import endpoints dispatch to it correctly).

`_build_storage_and_file`'s fake/ephemeral branches are pure (no DB, no network) and covered
directly here. `_run_pipeline_task_async`'s "run row missing" path is covered against the real
`get_engine()` (a bogus run id needs no pre-seeded data, so no shared-transaction trickery is
needed to make it visible) - like every other DB-backed test in this repo, it requires a real,
reachable DATABASE_URL (see tests/conftest.py's db_session fixture docstring).
"""

import base64
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.v1.upload as upload_module
import app.worker as worker_module
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
    storage, remote_file = await worker_module._build_storage_and_file(
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
    storage, remote_file = await worker_module._build_storage_and_file(
        storage_mode="ephemeral",
        remote_file_id="ignored",
        remote_file_name="chat.zip",
        inline_content_b64=base64.b64encode(content).decode(),
        user_id=uuid.uuid4(),
    )
    assert isinstance(storage, EphemeralStorageProvider)
    assert await storage.download_file(remote_file) == content


async def test_build_storage_and_file_defaults_to_empty_bytes_without_inline_content() -> None:
    storage, remote_file = await worker_module._build_storage_and_file(
        storage_mode="fake",
        remote_file_id="ignored",
        remote_file_name="empty.txt",
        inline_content_b64=None,
        user_id=uuid.uuid4(),
    )
    assert await storage.download_file(remote_file) == b""


async def test_build_storage_and_file_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown storage_mode"):
        await worker_module._build_storage_and_file(
            storage_mode="bogus",
            remote_file_id="id",
            remote_file_name="name",
            inline_content_b64=None,
            user_id=uuid.uuid4(),
        )


async def test_task_logs_and_returns_when_run_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run id that doesn't exist (e.g. a stale/duplicate task delivery after the row was
    somehow removed) must not raise - it's logged and the task returns cleanly. Uses a bogus,
    never-seeded run id, so this needs a reachable DB (per module docstring) but no fixture data."""
    monkeypatch.setattr(worker_module, "get_gemini_client", lambda: FakeGeminiClient("[]"))
    monkeypatch.setattr(worker_module, "get_pipeline_notifier", lambda: FakeNotificationSender())

    await worker_module._run_pipeline_task_async(
        run_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        remote_file_id="unused",
        remote_file_name="chat.txt",
        prompt_text="extract events",
        storage_mode="fake",
        inline_content_b64=base64.b64encode(b"data").decode(),
    )


async def _bind_worker_to_session(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Make every session app.worker opens (via its own `_session_maker()`) share `db_session`'s
    connection/transaction, via the documented `AsyncSession.connection()` API, rather than a
    separate engine/connection that (correctly, per normal DB isolation) could never see this
    test's uncommitted setup rows. Each session `_session_maker()()` opens is a fresh `AsyncSession`
    bound to that shared connection (not `db_session` itself, so `async with ... as session:`
    inside worker.py closing it doesn't tear down the test's own fixture session)."""
    connection = await db_session.connection()
    bound_maker = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(worker_module, "_session_maker", lambda: bound_maker)
    # worker.py's finally block still disposes the real, process-wide get_engine() after every
    # task (see its module docstring) - a separate engine from db_session's own, so this is a
    # harmless no-op for this test's purposes, not something that needs patching out.


async def test_storage_setup_failure_marks_run_failed_instead_of_orphaning_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "configured" run whose storage_configs row vanished (or fails to reconstruct) before the
    worker picked up the task must not leave the run stuck at PENDING forever - it should be
    marked FAILED and notified, the same as any other pipeline failure path."""
    await _bind_worker_to_session(monkeypatch, db_session)
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    notifier = FakeNotificationSender()
    monkeypatch.setattr(worker_module, "get_gemini_client", lambda: FakeGeminiClient("[]"))
    monkeypatch.setattr(worker_module, "get_pipeline_notifier", lambda: notifier)

    # storage_mode="configured" with no matching storage_configs row -> _build_storage_and_file
    # raises RuntimeError before any StorageProvider exists.
    await worker_module._run_pipeline_task_async(
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


async def test_celery_scheduler_dispatch_kwargs_drive_a_completed_run(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the actual dispatch glue `CeleryPipelineScheduler` builds - `_storage_mode()`'s
    choice, the base64-inline-content path for a `FakeStorageProvider` run, and that the kwargs it
    passes to `run_pipeline_task.delay()` are exactly the keyword arguments the task itself
    accepts (a name mismatch between the two would raise a `TypeError` below instead of only
    surfacing at runtime in production).

    Doesn't run the real Celery task synchronously in-process: `run_pipeline_task`'s entrypoint
    calls `asyncio.run(...)`, which cannot be nested inside pytest-asyncio's already-running event
    loop (the failure mode Celery's own `task_always_eager` would hit here too). Instead, this
    captures the exact kwargs `.delay()` was called with, then feeds them straight into
    `_run_pipeline_task_async` (the coroutine `run_pipeline_task` wraps) directly - proving both
    that the dispatch produced the right payload *and* that the task accepts it end-to-end.
    """
    await _bind_worker_to_session(monkeypatch, db_session)
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    monkeypatch.setattr(
        worker_module, "get_gemini_client", lambda: FakeGeminiClient(_EXAMPLE_OUTPUT)
    )
    monkeypatch.setattr(worker_module, "get_pipeline_notifier", lambda: FakeNotificationSender())

    captured: list[dict[str, object]] = []
    monkeypatch.setattr(worker_module.run_pipeline_task, "delay", lambda **kw: captured.append(kw))

    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"5/30/26, 10:00 - Russ: hi")

    scheduler = upload_module.CeleryPipelineScheduler()
    await scheduler.schedule(
        run_id=run.id,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient(_EXAMPLE_OUTPUT),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    assert len(captured) == 1
    dispatched = captured[0]
    assert dispatched["run_id"] == str(run.id)
    assert dispatched["storage_mode"] == "fake"
    inline_content_b64 = dispatched["inline_content_b64"]
    assert isinstance(inline_content_b64, str)
    assert base64.b64decode(inline_content_b64) == b"5/30/26, 10:00 - Russ: hi"

    # The exact kwargs the scheduler dispatched, fed straight into the task's own async core.
    await worker_module._run_pipeline_task_async(**dispatched)  # type: ignore[arg-type]

    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS
    events = (await db_session.scalars(select(Event).where(Event.user_id == user_id))).all()
    assert len(events) == _EXPECTED_NEW_EVENTS
