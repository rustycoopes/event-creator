"""Manual file upload -> processing pipeline (ported from organize-me's Slice 4.1/#52 to Event
Creator in Slice R8; dispatch switched from Celery to Cloud Tasks per
docs/adr/0001-event-creator-worker-cpu-throttling.md in organize-me).

``POST /api/v1/upload`` accepts a ``.txt`` / ``.zip`` / ``.csv`` export, writes it into the user's
connected storage watch folder (or an ephemeral in-memory fallback, issue #79), records a
``processing_runs`` row, and dispatches the 7-step pipeline as a **Cloud Tasks push task** -
targeting this same service's ``POST /internal/pipeline/run`` (``app.api.v1.internal_pipeline``).
The client then navigates to the progress page to watch the run advance via SSE.

Every external collaborator is an overridable dependency (storage provider, Gemini client,
notifier, and the scheduler that dispatches the Cloud Tasks task) so the endpoint's own logic -
gating, validation, run creation, the onboarding flip - is unit-testable while the full pipeline
behaviour is covered directly in tests against ``app.services.pipeline.runner.run_pipeline``.
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
from app.services.pipeline.cloud_tasks import enqueue_pipeline_run
from app.services.pipeline.dispatch import mark_runs_failed_to_schedule
from app.services.storage.base import RemoteFile, StorageProvider
from app.services.storage.dropbox import DropboxError
from app.services.storage.ephemeral import EphemeralStorageProvider
from app.services.storage.factory import build_storage_provider
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.google_drive import GoogleDriveError
from app.services.user_settings import mark_first_upload_onboarding_done

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
    """Which reconstruction strategy ``app.services.pipeline.dispatch`` should use for this
    provider - see that module's docstring. Ephemeral/fake providers hold their files purely
    in-memory, so they can't be reconstructed by the push endpoint (a different request from the
    one that created them) from a persisted config the way a real Drive/Dropbox/S3 provider can."""
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


def _build_dispatch_payload(
    *,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    remote_file: RemoteFile,
    mode: str,
    inline_b64: str | None,
    prompt_text: str,
    remaining_batch: list[dict[str, object]],
) -> dict[str, object]:
    """The JSON body shape ``app.api.v1.internal_pipeline.PipelineDispatchPayload`` parses."""
    return {
        "run_id": str(run_id),
        "user_id": str(user_id),
        "remote_file_id": remote_file.id,
        "remote_file_name": remote_file.name,
        "prompt_text": prompt_text,
        "storage_mode": mode,
        "inline_content_b64": inline_b64,
        "remaining_batch": remaining_batch,
    }


class CloudTasksPipelineScheduler:
    """Dispatches to ``POST /internal/pipeline/run`` (``app.api.v1.internal_pipeline``) via a
    Cloud Tasks push task (Slice R11 redesign, replacing Celery - see
    docs/adr/0001-event-creator-worker-cpu-throttling.md in organize-me).

    ``gemini``/``notifier`` are accepted only to satisfy the ``PipelineScheduler`` Protocol (kept
    symmetric with the original signature, and so a test double can still assert on what was
    passed in); the push endpoint always resolves its own collaborators fresh via
    ``get_gemini_client()``/``get_pipeline_notifier()``, since a live client/sender object can't
    cross the Cloud-Tasks-brokered process boundary.

    A batch's "sequential, not concurrent" requirement (organize-me's #110) is met by explicit
    chaining, not by the queue's own concurrency setting: Cloud Tasks documents dispatch order as
    best-effort by schedule time, not a guarantee, and a retry on an earlier item (well within
    normal operation - see ``infra/cloud_tasks/provision.sh``'s ``max-attempts``) can let a later
    item's task become eligible first even under ``max-concurrent-dispatches=1``. So only the
    *first* item of a batch is enqueued here; each item carries the rest of the batch
    (``remaining_batch``) in its own payload, and ``app.api.v1.internal_pipeline`` enqueues the
    next item itself once the current one finishes - see that module's docstring.
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
        mode, inline_b64 = await self._storage_payload_fields(storage, remote_file)
        await enqueue_pipeline_run(
            _build_dispatch_payload(
                run_id=run_id,
                user_id=user_id,
                remote_file=remote_file,
                mode=mode,
                inline_b64=inline_b64,
                prompt_text=prompt_text,
                remaining_batch=[],
            )
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
        if not runs:  # pragma: no cover - callers only invoke this with a non-empty batch
            await storage.aclose()
            return

        mode = _storage_mode(storage)
        # All items in a batch share the one storage provider passed in, so mode is constant
        # across the batch - only each item's own inline content (fake/ephemeral) differs.
        items = []
        for run_id, remote_file in runs:
            _, inline_b64 = await self._storage_payload_fields(storage, remote_file, mode=mode)
            items.append(
                _build_dispatch_payload(
                    run_id=run_id,
                    user_id=user_id,
                    remote_file=remote_file,
                    mode=mode,
                    inline_b64=inline_b64,
                    prompt_text=prompt_text,
                    remaining_batch=[],
                )
            )
        # Only the first item is enqueued directly; it carries the rest as remaining_batch, and
        # the push endpoint chains through them one at a time (see this class's docstring).
        first, *rest = items
        first["remaining_batch"] = rest
        await enqueue_pipeline_run(first)
        await storage.aclose()

    async def _storage_payload_fields(
        self, storage: StorageProvider, remote_file: RemoteFile, *, mode: str | None = None
    ) -> tuple[str, str | None]:
        mode = mode if mode is not None else _storage_mode(storage)
        if mode not in ("fake", "ephemeral"):
            return mode, None
        content = await storage.download_file(remote_file)
        return mode, base64.b64encode(content).decode()


def get_pipeline_scheduler() -> PipelineScheduler:
    """Return the production scheduler. Overridable in tests."""
    return CloudTasksPipelineScheduler()


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
    try:
        await scheduler.schedule(
            run_id=run.id,
            user_id=user_id,
            remote_file=remote_file,
            storage=storage,
            gemini=gemini,
            notifier=notifier,
            prompt_text=prompt_text,
        )
    except Exception:
        # The run row is already committed at this point - if enqueuing its Cloud Tasks task
        # itself fails (quota, a transient gRPC error, IAM misconfigured before
        # infra/cloud_tasks/provision.sh has run in a fresh environment), it would otherwise sit
        # at PENDING forever with nothing left to ever pick it up.
        logger.exception("upload: failed to schedule pipeline for run %s", run.id)
        await mark_runs_failed_to_schedule(
            [run.id], user_id, "Could not start processing. Please try again."
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="dispatch_error"
        ) from None
    return {"run_id": str(run.id)}
