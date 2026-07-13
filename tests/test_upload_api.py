"""Tests for POST /api/v1/upload (ported from organize-me's tests/test_upload_api.py, #52, to
Event Creator in Slice R8).

The endpoint's own logic - Drive gating, file validation, run creation, the onboarding flip, and
handing off to the pipeline scheduler - is tested here with the storage provider and scheduler
overridden; the full pipeline behaviour is covered in tests/test_pipeline_runner.py.

Auth is a Host-issued JWT cookie (see app.core.auth) rather than organize-me's register+cookie-
login flow - every test seeds a `host.users` row via `create_host_user` and attaches the token via
`make_token`.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.upload import get_pipeline_scheduler, get_upload_storage
from app.core.prompts import FACTORY_DEFAULT_PROMPT
from app.main import app
from app.models.processing_run import ProcessingRun
from app.services.storage.base import RemoteFile, StorageProvider
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.google_drive import GoogleDriveError
from app.services.user_settings import get_user_settings
from tests.conftest import TokenFactory, create_host_user


class _RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def schedule(self, **kwargs: object) -> None:
        self.calls.append(kwargs)

    async def schedule_batch(self, **kwargs: object) -> None:  # pragma: no cover - unused here
        self.calls.append(kwargs)


def _override_storage(provider: StorageProvider) -> None:
    app.dependency_overrides[get_upload_storage] = lambda: provider


def _override_scheduler(scheduler: _RecordingScheduler) -> None:
    app.dependency_overrides[get_pipeline_scheduler] = lambda: scheduler


async def test_upload_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/upload", files={"file": ("chat.txt", b"hi", "text/plain")}
    )
    assert response.status_code == 401


async def test_upload_succeeds_with_ephemeral_fallback_when_drive_not_connected(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    # No storage override: the real gating dependency runs against a user with no Drive connection.
    # Uploads fall back to ephemeral storage instead of rejecting (issue #79).
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    scheduler = _RecordingScheduler()
    _override_scheduler(scheduler)

    response = await client.post(
        "/api/v1/upload", files={"file": ("chat.txt", b"hi", "text/plain")}, cookies=cookies
    )

    # Upload succeeds with ephemeral storage provider as fallback.
    assert response.status_code == 202
    run_id = uuid.UUID(response.json()["run_id"])

    # Run was created and scheduled.
    run = await db_session.get(ProcessingRun, run_id)
    assert run is not None
    assert run.filename == "chat.txt"
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["run_id"] == run_id
    # Storage provider is ephemeral since Drive is not connected.
    from app.services.storage.ephemeral import EphemeralStorageProvider

    assert isinstance(call["storage"], EphemeralStorageProvider)


async def test_upload_rejects_unsupported_extension(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(FakeStorageProvider())
    _override_scheduler(_RecordingScheduler())

    response = await client.post(
        "/api/v1/upload",
        files={"file": ("notes.pdf", b"hi", "application/pdf")},
        cookies=cookies,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported_file_type"


async def test_upload_rejects_file_over_size_cap(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(FakeStorageProvider())
    _override_scheduler(_RecordingScheduler())

    oversized = b"x" * (10 * 1024 * 1024 + 1)
    response = await client.post(
        "/api/v1/upload",
        files={"file": ("chat.txt", oversized, "text/plain")},
        cookies=cookies,
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "file_too_large"


async def test_upload_rejects_empty_file(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(FakeStorageProvider())
    _override_scheduler(_RecordingScheduler())

    response = await client.post(
        "/api/v1/upload", files={"file": ("chat.txt", b"", "text/plain")}, cookies=cookies
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "empty_file"


async def test_upload_returns_502_with_detail_when_storage_write_fails(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for #143 (same fix applied to the manual-upload path that shares the
    unhandled-storage-exception gap): a Drive/Dropbox write failure while writing the uploaded
    file into the watch folder must surface as a mappable ``storage_error`` detail rather than a
    bare 500, and must not create/schedule a run."""

    class _FailingStorageProvider(FakeStorageProvider):
        async def upload_file(self, name: str, content: bytes) -> RemoteFile:
            raise GoogleDriveError(
                "Drive API POST https://www.googleapis.com/drive/v3/files failed (401)"
            )

    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    _override_storage(_FailingStorageProvider())
    scheduler = _RecordingScheduler()
    _override_scheduler(scheduler)

    response = await client.post(
        "/api/v1/upload", files={"file": ("chat.txt", b"hi", "text/plain")}, cookies=cookies
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "storage_error"
    assert scheduler.calls == []


async def test_successful_upload_creates_run_flips_onboarding_and_schedules(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    storage = FakeStorageProvider()
    scheduler = _RecordingScheduler()
    _override_storage(storage)
    _override_scheduler(scheduler)

    response = await client.post(
        "/api/v1/upload",
        files={"file": ("chat.txt", b"5/30/26, 10:00 - Russ: hi", "text/plain")},
        cookies=cookies,
    )

    assert response.status_code == 202
    run_id = uuid.UUID(response.json()["run_id"])

    # The bytes were written to the watch folder and a run row was created.
    assert storage.uploaded == ["chat.txt"]
    run = await db_session.get(ProcessingRun, run_id)
    assert run is not None
    assert run.filename == "chat.txt"

    # First upload flips the onboarding flag.
    settings = await get_user_settings(db_session, user_id)
    assert settings is not None
    assert settings.onboarding_first_upload_done is True

    # The pipeline was handed off exactly once with the created run + resolved prompt.
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    assert call["run_id"] == run_id
    assert call["storage"] is storage
    assert isinstance(call["remote_file"], RemoteFile)
    assert call["prompt_text"] == FACTORY_DEFAULT_PROMPT


async def test_e2e_test_mode_bypasses_the_drive_gate(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Under E2E_TEST_MODE the fake provider is used with no connected-Drive requirement, so QA's
    Playwright suite can upload without real OAuth. Runs on the rolled-back db_session via the
    ``client`` fixture; only get_settings + the scheduler are additionally overridden."""
    from app.core.config import Settings, get_settings

    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    scheduler = _RecordingScheduler()

    def e2e_settings() -> Settings:
        # Same config as normal, but with E2E_TEST_MODE flipped on for the endpoint's Depends.
        return get_settings().model_copy(update={"e2e_test_mode": True})

    app.dependency_overrides[get_settings] = e2e_settings
    _override_scheduler(scheduler)

    # No Drive connection saved, but E2E mode lets the upload through.
    response = await client.post(
        "/api/v1/upload", files={"file": ("chat.txt", b"hi", "text/plain")}, cookies=cookies
    )

    assert response.status_code == 202
    assert len(scheduler.calls) == 1
