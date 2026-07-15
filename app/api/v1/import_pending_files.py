"""Import pending files -> processing pipeline (ported from organize-me's Slice 7/#110 to Event
Creator in Slice R8).

``POST /api/v1/import-pending-files`` scans the user's connected storage watch folder for files
not yet processed (``StorageProvider.list_new_files`` already excludes ``processed/``/``failed/``
by contract - no separate dedup bookkeeping needed), creates one ``processing_runs`` row per file,
and dispatches them as a batch of Cloud Tasks tasks (``PipelineScheduler.schedule_batch`` in
``app.api.v1.upload``) so they run **sequentially** - one file's pipeline run finishing before the
next starts, enforced by the queue's ``max-concurrent-dispatches=1`` setting (see
``infra/cloud_tasks/provision.sh``) - unlike the manual-upload path, which dispatches one
independent task per upload.

The endpoint returns only the first run's id, so the client follows it to ``/processing`` exactly
like a manual upload; any further files in the batch keep processing in the background and are
visible afterward via the ``/logs`` history page rather than a second live SSE stream (#110's
chosen v1 UX - a fully "watch every file complete live" UI was considered and deferred).

Requires a connected storage provider - no ephemeral fallback like ``app.api.v1.upload``'s
``get_upload_storage``, since there's no watch folder to scan without one.

A Drive/Dropbox API failure while listing pending files (expired token, unreachable watch folder,
etc.) is surfaced as a distinguishable ``storage_error`` detail (issue #143) rather than an
unhandled 500, so the client can show an actionable message instead of a generic failure.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import config_is_connected, get_user_storage_config
from app.api.v1.upload import PipelineScheduler, _prompt_text_for, get_pipeline_scheduler
from app.core.auth import current_user_id
from app.core.config import Settings, get_settings
from app.core.security import get_credential_cipher
from app.db.session import get_db
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.llm.gemini import GeminiClient, get_gemini_client
from app.services.notifications.pipeline import NotificationSender, get_pipeline_notifier
from app.services.pipeline.dispatch import mark_runs_failed_to_schedule
from app.services.storage.base import StorageProvider
from app.services.storage.dropbox import DropboxError
from app.services.storage.factory import build_storage_provider
from app.services.storage.google_drive import GoogleDriveError
from app.services.user_settings import mark_first_upload_onboarding_done

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["import-pending-files"])


async def get_import_storage(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StorageProvider:
    """Resolve the storage provider for a scan.

    Under ``E2E_TEST_MODE`` the fake provider is returned unconditionally, same as
    ``get_upload_storage``. Otherwise requires a connected storage provider - a 400 if not, since
    there's no ephemeral "watch folder" to scan (unlike an upload, which can proceed without one)."""
    if settings.e2e_test_mode:
        return build_storage_provider(config=None, settings=settings, cipher=None)
    config = await get_user_storage_config(db, user_id)
    if not config_is_connected(config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="storage_not_connected"
        )
    return build_storage_provider(config=config, settings=settings, cipher=get_credential_cipher())


@router.post("/import-pending-files", status_code=status.HTTP_202_ACCEPTED)
async def import_pending_files(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    storage: StorageProvider = Depends(get_import_storage),
    gemini: GeminiClient = Depends(get_gemini_client),
    notifier: NotificationSender = Depends(get_pipeline_notifier),
    scheduler: PipelineScheduler = Depends(get_pipeline_scheduler),
) -> dict[str, str]:
    try:
        pending_files = await storage.list_new_files()
    except (GoogleDriveError, DropboxError):
        logger.exception("import-pending-files: listing failed for user %s", user_id)
        await storage.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="storage_error"
        ) from None
    if not pending_files:
        # No batch to hand off to the scheduler, so this endpoint owns closing the provider itself
        # (schedule_batch's background task closes it for us on every other path).
        await storage.aclose()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no_pending_files")

    runs = [
        ProcessingRun(user_id=user_id, filename=file.name, status=ProcessingRunStatus.PENDING)
        for file in pending_files
    ]
    db.add_all(runs)
    # Completes "Upload First File" the same as a manual upload does - a user who only ever uses
    # Import (never the Upload page directly) shouldn't have that onboarding step stuck forever.
    # get_db's sessionmaker uses expire_on_commit=False, so each run's Python-side-default id
    # (ProcessingRun.id, populated at flush) is still readable after commit - no refresh needed.
    # mark_first_upload_onboarding_done's own commit also persists the `runs` add above (same
    # session).
    await mark_first_upload_onboarding_done(db, user_id)

    prompt_text = await _prompt_text_for(db, user_id)
    try:
        await scheduler.schedule_batch(
            runs=list(zip((run.id for run in runs), pending_files, strict=True)),
            user_id=user_id,
            storage=storage,
            gemini=gemini,
            notifier=notifier,
            prompt_text=prompt_text,
        )
    except Exception:
        # The run rows are already committed at this point - if enqueuing the batch's Cloud Tasks
        # task itself fails (quota, a transient gRPC error, IAM misconfigured before
        # infra/cloud_tasks/provision.sh has run in a fresh environment), every one of them would
        # otherwise sit at PENDING forever with nothing left to ever pick them up.
        logger.exception("import-pending-files: failed to schedule batch for user %s", user_id)
        await mark_runs_failed_to_schedule(
            [run.id for run in runs], user_id, "Could not start processing. Please try again."
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="dispatch_error"
        ) from None
    return {"run_id": str(runs[0].id)}
