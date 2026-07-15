"""Tests for `CloudTasksPipelineScheduler` (Slice R11 redesign, replacing Celery) - the dispatch
glue in app.api.v1.upload that builds and enqueues a Cloud Tasks task. Covers `_storage_mode()`'s
choice, the base64-inline-content path for a `FakeStorageProvider` run, that the payload it builds
is exactly what `app.services.pipeline.dispatch.run_pipeline_dispatch` accepts (a name mismatch
between the two would raise a `TypeError` below instead of only surfacing at runtime in
production), the OIDC/queue-path wiring the real `CloudTasksClient.create_task` call receives, and
that a batch enqueues only its *first* item with the rest chained via `remaining_batch` (not one
task per run - see `app.api.v1.internal_pipeline`'s docstring on why strict ordering needs
explicit chaining rather than relying on the queue's own concurrency setting).

Doesn't call a real Cloud Tasks queue: `app.services.pipeline.cloud_tasks._get_tasks_client()` is
monkeypatched to a fake client that records the `Task` it was asked to create instead of
performing the (blocking, networked) gRPC call.
"""

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from google.cloud import tasks_v2
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.upload as upload_module
import app.services.pipeline.cloud_tasks as cloud_tasks_module
from app.core.config import get_settings
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.pipeline.dispatch import run_pipeline_dispatch
from app.services.storage.fake import FakeStorageProvider
from tests.conftest import create_host_user
from tests.test_dispatch import _EXAMPLE_OUTPUT, _EXPECTED_NEW_EVENTS, _bind_dispatch_to_session


class _FakeTasksClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, tasks_v2.Task]] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, *, parent: str, task: tasks_v2.Task) -> None:
        self.created.append((parent, task))


@pytest.fixture
def fake_tasks_client(monkeypatch: pytest.MonkeyPatch) -> _FakeTasksClient:
    fake = _FakeTasksClient()
    monkeypatch.setattr(cloud_tasks_module, "_get_tasks_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _cloud_tasks_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("CLOUD_TASKS_LOCATION", "test-region")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "test-queue")
    monkeypatch.setenv("PIPELINE_ENDPOINT_URL", "https://event-creator-test.a.run.app")
    monkeypatch.setenv(
        "PIPELINE_INVOKER_SERVICE_ACCOUNT", "invoker@test-project.iam.gserviceaccount.com"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _decode_payload(task: tasks_v2.Task) -> dict[str, Any]:
    body: bytes = task.http_request.body
    return dict(json.loads(body.decode()))


def _without_remaining_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """`run_pipeline_dispatch` doesn't accept `remaining_batch` - that field is consumed by
    `app.api.v1.internal_pipeline`'s chain-advance step, not the pipeline dispatch itself."""
    return {k: v for k, v in payload.items() if k != "remaining_batch"}


async def test_schedule_enqueues_one_task_with_the_expected_payload_and_oidc_target(
    db_session: AsyncSession,
    fake_tasks_client: _FakeTasksClient,
) -> None:
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()

    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"5/30/26, 10:00 - Russ: hi")

    scheduler = upload_module.CloudTasksPipelineScheduler()
    await scheduler.schedule(
        run_id=run.id,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=None,  # type: ignore[arg-type]  # unused by CloudTasksPipelineScheduler
        notifier=None,  # type: ignore[arg-type]  # unused by CloudTasksPipelineScheduler
        prompt_text="extract events",
    )

    assert len(fake_tasks_client.created) == 1
    parent, task = fake_tasks_client.created[0]
    assert parent == "projects/test-project/locations/test-region/queues/test-queue"
    assert task.http_request.url == "https://event-creator-test.a.run.app/internal/pipeline/run"
    assert task.http_request.oidc_token.service_account_email == (
        "invoker@test-project.iam.gserviceaccount.com"
    )
    assert task.http_request.oidc_token.audience == "https://event-creator-test.a.run.app"

    payload = _decode_payload(task)
    assert payload["run_id"] == str(run.id)
    assert payload["storage_mode"] == "fake"
    assert payload["remaining_batch"] == []
    inline_content_b64 = payload["inline_content_b64"]
    assert base64.b64decode(inline_content_b64) == b"5/30/26, 10:00 - Russ: hi"


async def test_dispatched_payload_drives_a_completed_run_through_run_pipeline_dispatch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    fake_tasks_client: _FakeTasksClient,
) -> None:
    """Proves the exact kwarg names the scheduler puts in the JSON payload are the ones
    `run_pipeline_dispatch` accepts - a real name mismatch would raise `TypeError` here."""
    import app.services.pipeline.dispatch as dispatch_module
    from app.services.llm.gemini import FakeGeminiClient
    from app.services.notifications.pipeline import FakeNotificationSender

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

    scheduler = upload_module.CloudTasksPipelineScheduler()
    await scheduler.schedule(
        run_id=run.id,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=None,  # type: ignore[arg-type]
        notifier=None,  # type: ignore[arg-type]
        prompt_text="extract events",
    )

    _, task = fake_tasks_client.created[0]
    payload = _without_remaining_batch(_decode_payload(task))

    await run_pipeline_dispatch(**payload)

    await db_session.refresh(run)
    assert run.status == ProcessingRunStatus.SUCCESS
    assert run.events_extracted_count == _EXPECTED_NEW_EVENTS


async def test_schedule_batch_enqueues_only_the_first_item_with_the_rest_chained(
    db_session: AsyncSession,
    fake_tasks_client: _FakeTasksClient,
) -> None:
    """A batch enqueues exactly one Cloud Tasks task (the first item); the remaining items travel
    in that task's own `remaining_batch` payload field, to be chained one at a time by
    `app.api.v1.internal_pipeline` - not enqueued directly here. Strict ordering can't be
    delegated to the queue's `max-concurrent-dispatches=1` setting alone (Cloud Tasks documents
    dispatch order as best-effort, not guaranteed, and a retry on an earlier item can let a later
    item's task become eligible first)."""
    user_id = await create_host_user(db_session)
    storage = FakeStorageProvider()
    runs = []
    for i in range(3):
        run = ProcessingRun(
            user_id=user_id, filename=f"chat{i}.txt", status=ProcessingRunStatus.PENDING
        )
        db_session.add(run)
        await db_session.flush()
        remote_file = await storage.upload_file(f"chat{i}.txt", f"file {i}".encode())
        runs.append((run.id, remote_file))

    scheduler = upload_module.CloudTasksPipelineScheduler()
    await scheduler.schedule_batch(
        runs=runs,
        user_id=user_id,
        storage=storage,
        gemini=None,  # type: ignore[arg-type]
        notifier=None,  # type: ignore[arg-type]
        prompt_text="extract events",
    )

    assert len(fake_tasks_client.created) == 1
    _, task = fake_tasks_client.created[0]
    payload = _decode_payload(task)

    assert payload["run_id"] == str(runs[0][0])
    remaining = payload["remaining_batch"]
    assert [item["run_id"] for item in remaining] == [str(run_id) for run_id, _ in runs[1:]]
