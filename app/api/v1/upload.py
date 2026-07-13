"""Manual file upload -> processing pipeline (ported from organize-me's Slice 4.1/#52 to Event
Creator in Slice R8).

``POST /api/v1/upload`` accepts a ``.txt`` / ``.zip`` / ``.csv`` export, writes it into the user's
connected storage watch folder (or an ephemeral in-memory fallback, issue #79), records a
``processing_runs`` row, and dispatches the 7-step pipeline as a **Celery task** (unlike the
monolith, which never turned its worker on - see ``app.worker``). The client then navigates to the
progress page to watch the run advance via SSE.

Every external collaborator is an overridable dependency (storage provider, Gemini client,
notifier, and the scheduler that dispatches the Celery task) so the endpoint's own logic - gating,
validation, run creation, the onboarding flip - is unit-testable while the full pipeline behaviour
is covered directly in tests against ``app.services.pipeline.runner.run_pipeline``.
"""

import base64
import logging
import uuid
from pathlib import PurePosixPath
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import get_user_storage_config
from app.core.auth import current_user_id
from app.core.config import Settings, get_settings
from app.core.prompts import FACTORY_DEFAULT_PROMPT
from app.core.security import get_credential_cipher
from app.db.session import get_db
from app.models.llm_prompt import LLMPrompt
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.llm.gemini import GeminiClient, get_gemini_client
from app.services.notifications.pipeline import NotificationSender, get_pipeline_notifier
from app.services.storage.base import RemoteFile, StorageProvider
from app.services.storage.dropbox import DropboxError
from app.services.storage.ephemeral import EphemeralStorageProvider
from app.services.storage.factory import build_storage_provider
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.google_drive import GoogleDriveError
from app.services.user_settings import mark_first_upload_onboarding_done
from app.worker import run_pipeline_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["upload"])

ALLOWED_EXTENSIONS = {".txt", ".zip", ".csv"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB (per organize-me #52's resolved decision)


async def get_upload_storage(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StorageProvider:
    """Resolve the storage provider for this upload.

    Under ``E2E_TEST_MODE`` the fake provider is returned unconditionally (QA has no real Drive).
    Otherwise attempts to use the user's connected storage provider. If not available, falls back
    to ephemeral (in-memory) storage so uploads can proceed without data loss (issue #79).
    Ephemeral uploads are logged with a warning so operators are aware."""
    if settings.e2e_test_mode:
        return build_storage_provider(config=None, settings=settings, cipher=None)
    config = await get_user_storage_config(db, user_id)
    if config is None or config.oauth_access_token is None:
        # Graceful fallback to ephemeral storage instead of rejecting the upload (issue #79).
        logger.warning(
            "user %s uploading without configured storage provider, using ephemeral fallback",
            user_id,
        )
        return build_storage_provider(
            config=None, settings=settings, cipher=None, fallback_to_ephemeral=True
        )
    return build_storage_provider(
        config=config, settings=settings, cipher=get_credential_cipher()
    )


def _storage_mode(storage: StorageProvider) -> str:
    """Which reconstruction strategy ``app.worker.run_pipeline_task`` should use for this
    provider - see that module's docstring. Ephemeral/fake providers hold their files purely
    in-memory, so they can't be reconstructed by the (separate-process) Celery worker from a
    persisted config the way a real Drive/Dropbox/S3 provider can."""
    if isinstance(storage, FakeStorageProvider):
        return "fake"
    if isinstance(storage, EphemeralStorageProvider):
        return "ephemeral"
    return "configured"


class PipelineScheduler(Protocol):
    async def schedule(
        self,
        *,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        remote_file: RemoteFile,
        storage: StorageProvider,
        gemini: GeminiClient,
        notifier: NotificationSender,
        prompt_text: str,
    ) -> None:
        """Dispatch the pipeline for a created run (not awaited to completion)."""
        ...

    async def schedule_batch(
        self,
        *,
        runs: list[tuple[uuid.UUID, RemoteFile]],
        user_id: uuid.UUID,
        storage: StorageProvider,
        gemini: GeminiClient,
        notifier: NotificationSender,
        prompt_text: str,
    ) -> None:
        """Dispatch a batch of runs, processed one after another (not concurrently) - used by the
        import-pending-files endpoint, where files must be processed sequentially rather than the
        fire-and-forget-per-file pattern ``schedule`` uses for a single manual upload."""
        ...


class CeleryPipelineScheduler:
    """Dispatches ``app.worker.run_pipeline_task`` to the Celery worker (Slice R8).

    ``gemini``/``notifier`` are accepted only to satisfy the ``PipelineScheduler`` Protocol (kept
    symmetric with the pre-Celery monolith signature, and so a test double can still assert on
    what was passed in); the task itself always resolves its own collaborators fresh via
    ``get_gemini_client()``/``get_pipeline_notifier()`` inside the worker process, since a live
    client/sender object can't cross the Redis-brokered process boundary.
    """

    async def schedule(
        self,
        *,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        remote_file: RemoteFile,
        storage: StorageProvider,
        gemini: GeminiClient,
        notifier: NotificationSender,
        prompt_text: str,
    ) -> None:
        await self._dispatch_one(
            run_id=run_id,
            user_id=user_id,
            remote_file=remote_file,
            storage=storage,
            prompt_text=prompt_text,
        )
        await storage.aclose()

    async def schedule_batch(
        self,
        *,
        runs: list[tuple[uuid.UUID, RemoteFile]],
        user_id: uuid.UUID,
        storage: StorageProvider,
        gemini: GeminiClient,
        notifier: NotificationSender,
        prompt_text: str,
    ) -> None:
        # A Celery `chain` runs its signatures one after another (never concurrently), preserving
        # the "sequential, not parallel" behaviour organize-me's #110 chose - whether the chain
        # lands on one worker or several.
        from celery import chain  # type: ignore[import-untyped]

        signatures = []
        for run_id, remote_file in runs:
            mode = _storage_mode(storage)
            inline_b64 = None
            if mode in ("fake", "ephemeral"):
                content = await storage.download_file(remote_file)
                inline_b64 = base64.b64encode(content).decode()
            signatures.append(
                run_pipeline_task.si(
                    run_id=str(run_id),
                    user_id=str(user_id),
                    remote_file_id=remote_file.id,
                    remote_file_name=remote_file.name,
                    prompt_text=prompt_text,
                    storage_mode=mode,
                    inline_content_b64=inline_b64,
                )
            )
        if signatures:
            chain(*signatures).apply_async()
        await storage.aclose()

    async def _dispatch_one(
        self,
        *,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        remote_file: RemoteFile,
        storage: StorageProvider,
        prompt_text: str,
    ) -> None:
        mode = _storage_mode(storage)
        inline_b64 = None
        if mode in ("fake", "ephemeral"):
            content = await storage.download_file(remote_file)
            inline_b64 = base64.b64encode(content).decode()
        run_pipeline_task.delay(
            run_id=str(run_id),
            user_id=str(user_id),
            remote_file_id=remote_file.id,
            remote_file_name=remote_file.name,
            prompt_text=prompt_text,
            storage_mode=mode,
            inline_content_b64=inline_b64,
        )


def get_pipeline_scheduler() -> PipelineScheduler:
    """Return the production scheduler. Overridable in tests."""
    return CeleryPipelineScheduler()


async def _prompt_text_for(db: AsyncSession, user_id: uuid.UUID) -> str:
    """The user's saved extraction prompt, falling back to the factory default if none is set."""
    prompt = await db.scalar(select(LLMPrompt).where(LLMPrompt.user_id == user_id))
    return prompt.prompt_text if prompt is not None else FACTORY_DEFAULT_PROMPT


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_file(
    file: UploadFile,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_upload_storage),
    gemini: GeminiClient = Depends(get_gemini_client),
    notifier: NotificationSender = Depends(get_pipeline_notifier),
    scheduler: PipelineScheduler = Depends(get_pipeline_scheduler),
) -> dict[str, str]:
    filename = file.filename or ""
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported_file_type"
        )

    # Read at most one byte past the cap so an oversized upload is rejected without pulling the
    # whole (potentially huge) file into memory first.
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="file_too_large"
        )
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty_file")

    # Write the bytes into the watch folder, then record the run so the pipeline (and the SSE
    # progress page) have a row to drive. A Drive/Dropbox API failure here (expired token,
    # unreachable watch folder) is surfaced as a distinguishable storage_error (issue #143) instead
    # of an unhandled 500, so the client can show an actionable message.
    try:
        remote_file = await storage.upload_file(filename, content)
    except (GoogleDriveError, DropboxError):
        logger.exception("upload: writing to storage failed for user %s", user_id)
        await storage.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="storage_error"
        ) from None
    run = ProcessingRun(user_id=user_id, filename=filename, status=ProcessingRunStatus.PENDING)
    db.add(run)
    # First upload completes the onboarding step; it stays true thereafter. mark_first_upload_
    # onboarding_done's own commit also persists the `run` add above (same session).
    await mark_first_upload_onboarding_done(db, user_id)
    await db.refresh(run)

    prompt_text = await _prompt_text_for(db, user_id)
    await scheduler.schedule(
        run_id=run.id,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=gemini,
        notifier=notifier,
        prompt_text=prompt_text,
    )
    return {"run_id": str(run.id)}
