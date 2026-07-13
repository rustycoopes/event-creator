"""Tests for POST /api/v1/import-pending-files (ported from organize-me's
tests/test_import_pending_files_api.py, #110, to Event Creator in Slice R8).

The endpoint's own logic - storage gating, file discovery, run creation, and handing off to the
scheduler's batch method - is tested here with the storage provider and scheduler overridden
(mirrors tests/test_upload_api.py); the underlying pipeline behaviour itself is already covered by
tests/test_pipeline_runner.py.

Auth is a Host-issued JWT cookie (see app.core.auth) rather than organize-me's register+cookie-
login flow.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.import_pending_files import get_import_storage
from app.api.v1.upload import get_pipeline_scheduler
from app.main import app
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.storage.base import RemoteFile
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.google_drive import GoogleDriveError
from app.services.user_settings import get_user_settings
from tests.conftest import TokenFactory, create_host_user


class _FailingStorageProvider(FakeStorageProvider):
    """A storage provider whose file listing blows up, mirroring a real Drive/Dropbox API
    failure (expired token, unreachable folder, etc.) so the endpoint's error handling can be
    exercised without a live provider."""

    async def list_new_files(self) -> list[RemoteFile]:
        raise GoogleDriveError(
            "Drive API GET https://www.googleapis.com/drive/v3/files failed (401)"
        )


class _RecordingBatchScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def schedule(self, **kwargs: object) -> None:  # pragma: no cover - unused here
        self.calls.append(kwargs)

    async def schedule_batch(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _override_storage(provider: FakeStorageProvider) -> None:
    app.dependency_overrides[get_import_storage] = lambda: provider


def _override_scheduler(scheduler: _RecordingBatchScheduler) -> None:
    app.dependency_overrides[get_pipeline_scheduler] = lambda: scheduler


async def test_import_pending_files_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/import-pending-files")
    assert response.status_code == 401


async def test_import_pending_files_requires_a_connected_storage_provider(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    # No storage config saved, and get_import_storage is not overridden - exercises the real
    # dependency's gating rather than a fake.
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    response = await client.post("/api/v1/import-pending-files", cookies=cookies)

    assert response.status_code == 400
    assert response.json()["detail"] == "storage_not_connected"


async def test_import_pending_files_returns_400_when_nothing_is_pending(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(FakeStorageProvider())
    _override_scheduler(_RecordingBatchScheduler())

    response = await client.post("/api/v1/import-pending-files", cookies=cookies)

    assert response.status_code == 400
    assert response.json()["detail"] == "no_pending_files"


async def test_import_pending_files_returns_502_with_detail_when_storage_listing_fails(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for #143: a Drive/Dropbox API failure while listing pending files used to
    propagate as an unhandled 500 with no ``detail`` body, so the client fell back to the generic
    "Import failed. Please try again." message with no indication of what actually went wrong.
    It should instead surface a specific, mappable detail code (and the run-creating/scheduling
    steps must never be reached, since there's no file list to act on)."""
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(_FailingStorageProvider())
    scheduler = _RecordingBatchScheduler()
    _override_scheduler(scheduler)

    response = await client.post("/api/v1/import-pending-files", cookies=cookies)

    assert response.status_code == 502
    assert response.json()["detail"] == "storage_error"
    assert scheduler.calls == []


async def test_import_pending_files_creates_one_run_per_file_and_schedules_batch(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    file_a = RemoteFile(id="a", name="chat-a.txt")
    file_b = RemoteFile(id="b", name="chat-b.txt")
    storage = FakeStorageProvider(files={file_a: b"a", file_b: b"b"})
    scheduler = _RecordingBatchScheduler()
    _override_storage(storage)
    _override_scheduler(scheduler)

    response = await client.post("/api/v1/import-pending-files", cookies=cookies)

    assert response.status_code == 202
    first_run_id = uuid.UUID(response.json()["run_id"])

    # One ProcessingRun row per pending file, both owned by this user.
    runs = list(
        (
            await db_session.scalars(
                select(ProcessingRun).where(ProcessingRun.user_id == user_id)
            )
        ).all()
    )
    assert len(runs) == 2
    filenames = {run.filename for run in runs}
    assert filenames == {"chat-a.txt", "chat-b.txt"}
    assert all(run.status == ProcessingRunStatus.PENDING for run in runs)

    # Completes onboarding the same as a manual upload does - a user who only ever imports
    # shouldn't have "Upload First File" stuck incomplete forever.
    settings = await get_user_settings(db_session, user_id)
    assert settings is not None
    assert settings.onboarding_first_upload_done is True

    # The returned run_id is the first pending file's run, matching schedule_batch's first entry.
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    scheduled_runs = call["runs"]
    assert isinstance(scheduled_runs, list)
    assert scheduled_runs[0][0] == first_run_id
    assert [f.name for _run_id, f in scheduled_runs] == ["chat-a.txt", "chat-b.txt"]
    assert call["storage"] is storage


async def test_e2e_test_mode_bypasses_the_drive_gate_for_import(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Mirrors test_upload_api.py's equivalent - under E2E_TEST_MODE the fake provider is used
    with no connected-Drive requirement."""
    from app.core.config import Settings, get_settings

    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    scheduler = _RecordingBatchScheduler()

    def e2e_settings() -> Settings:
        return get_settings().model_copy(update={"e2e_test_mode": True})

    app.dependency_overrides[get_settings] = e2e_settings
    _override_scheduler(scheduler)

    # get_import_storage itself isn't overridden here - E2E_TEST_MODE makes it build a fresh (empty)
    # FakeStorageProvider, so there are no pending files. That's enough to prove the Drive gate
    # (storage_not_connected) is bypassed rather than hit before file discovery even runs.
    response = await client.post("/api/v1/import-pending-files", cookies=cookies)

    assert response.status_code == 400
    assert response.json()["detail"] == "no_pending_files"
