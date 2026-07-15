"""The pipeline dispatch target (Cloud Tasks replaces Celery/Redis - see
docs/adr/0001-event-creator-worker-cpu-throttling.md in organize-me).

Slice R8 ran the 7-step pipeline as a Celery task, executed by a separate worker process
`supervisord` started alongside the web process. R11 discovered that worker crash-loops on Cloud
Run's request-based billing: a background process with no HTTP request of its own gets its CPU
throttled to near-zero the instant no request is in flight on that instance, no matter what
`app.worker`'s docstring assumed.

This module is the replacement: `run_pipeline_dispatch` is called directly (awaited, no bridge
needed) by `app.api.v1.internal_pipeline`'s `POST /internal/pipeline/run` handler - the endpoint
Cloud Tasks pushes each run to. Because that push is a genuine inbound HTTP request, Cloud Run
allocates real CPU for exactly its duration; no separate process to babysit, no CPU-always-
allocated requirement, no Redis broker.

`run_pipeline_dispatch` takes only JSON-serialisable arguments (ids/strings, never live objects
like a `StorageProvider` or DB session - those don't survive the Cloud-Tasks-brokered hop from the
dispatching request to this one), reconstructs its collaborators, and calls
`app.services.pipeline.runner.run_pipeline` - the actual pipeline logic, unchanged from the
monolith and independently unit-tested with fakes.

Two storage modes:

- ``"configured"``: the file was already written into the user's real, connected storage provider
  (Google Drive/Dropbox/S3) by the API process before dispatch. This rebuilds the same kind of
  provider from the user's persisted ``storage_configs`` row and downloads by the file's remote id.
- ``"ephemeral"``/``"fake"``: there is no persistent, cross-process store to hand the file through
  (the graceful ephemeral fallback, issue #79, and the E2E fake provider are both purely
  in-memory). For these, the dispatching process base64-encodes the file's bytes into the task
  payload (small files only - the same 10MB cap the upload endpoint already enforces) and this
  re-seeds a fresh in-memory provider from them.
"""

import base64
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import get_credential_cipher
from app.db.session import get_engine
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.models.storage_config import StorageConfig
from app.services.llm.gemini import get_gemini_client
from app.services.notifications.pipeline import (
    NotificationOutcome,
    PipelineNotification,
    get_pipeline_notifier,
)
from app.services.pipeline.runner import run_pipeline
from app.services.storage.base import RemoteFile, StorageProvider
from app.services.storage.ephemeral import EphemeralStorageProvider
from app.services.storage.factory import build_storage_provider
from app.services.storage.fake import FakeStorageProvider

logger = logging.getLogger(__name__)


def _session_maker() -> async_sessionmaker[AsyncSession]:
    """A fresh sessionmaker bound to the process-wide (lru_cache'd) engine - shared by every
    session this module opens, so there's exactly one place constructing one."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def _build_storage_and_file(
    *,
    storage_mode: str,
    remote_file_id: str,
    remote_file_name: str,
    inline_content_b64: str | None,
    user_id: uuid.UUID,
) -> tuple[StorageProvider, RemoteFile]:
    """Reconstruct the storage provider + the file to process for one dispatched run."""
    if storage_mode in ("ephemeral", "fake"):
        content = base64.b64decode(inline_content_b64) if inline_content_b64 else b""
        provider: StorageProvider = (
            FakeStorageProvider() if storage_mode == "fake" else EphemeralStorageProvider()
        )
        remote_file = await provider.upload_file(remote_file_name, content)
        return provider, remote_file
    if storage_mode != "configured":  # pragma: no cover - defensive, all callers use the 3 modes
        raise ValueError(f"unknown storage_mode: {storage_mode}")

    async with _session_maker()() as session:
        config = await session.scalar(
            select(StorageConfig).where(StorageConfig.user_id == user_id)
        )
    if config is None:
        raise RuntimeError(f"storage config vanished for user {user_id} before task ran")
    provider = build_storage_provider(
        config=config, settings=get_settings(), cipher=get_credential_cipher()
    )
    return provider, RemoteFile(id=remote_file_id, name=remote_file_name)


async def _fail_run_without_storage(
    run_uuid: uuid.UUID, user_uuid: uuid.UUID, message: str
) -> None:
    """Mark a run failed when it never got far enough to have a storage provider to hand
    ``run_pipeline``/its own ``_fail_run`` (e.g. the user's storage config vanished, or a
    misconfigured S3 row raises while decrypting credentials).

    Without this, a setup-phase failure would leave the ``processing_runs`` row stuck at
    ``PENDING`` forever - no terminal status, no Notify step, no user-facing error, and the SSE
    progress page polling it with nothing to ever observe finishing.
    """
    async with _session_maker()() as session:
        run = await session.get(ProcessingRun, run_uuid)
        if run is None:  # pragma: no cover - the row was just committed by the API process
            logger.error("pipeline dispatch: run %s vanished before it could be marked failed", run_uuid)
            return
        session.add(
            ProcessingStep(
                run_id=run.id,
                step_number=1,
                step_name="File Received",
                status=ProcessingStepStatus.FAILED,
                log_lines=[message],
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
        )
        run.status = ProcessingRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        await session.commit()

        try:
            await get_pipeline_notifier().send(
                PipelineNotification(
                    user_id=user_uuid,
                    run_id=run.id,
                    filename=run.filename,
                    outcome=NotificationOutcome.FAILED,
                    new_event_count=0,
                    message=message,
                )
            )
        except Exception:  # pragma: no cover - best-effort; the run is already marked failed
            logger.exception("pipeline dispatch: failure notification itself failed for run %s", run_uuid)


async def run_already_terminal(run_id: uuid.UUID) -> bool:
    """True if ``run_id``'s status is already ``SUCCESS``/``FAILED``.

    A cheap fast-path check the push endpoint uses to skip a doomed-to-no-op storage
    reconstruction for an already-finished run - **not** itself the authoritative guard against
    double-processing. Cloud Tasks' delivery is at-least-once, so two concurrent pushes for the
    same run can both observe a non-terminal status here and both pass; the actual race is closed
    by ``_claim_run``'s atomic compare-and-swap inside ``run_pipeline_dispatch`` below."""
    async with _session_maker()() as session:
        run = await session.get(ProcessingRun, run_id)
        if run is None:  # pragma: no cover - the row was just committed by the API process
            return False
        return run.status in (ProcessingRunStatus.SUCCESS, ProcessingRunStatus.FAILED)


async def _claim_run(session: AsyncSession, run_id: uuid.UUID) -> ProcessingRun | None:
    """Atomically transition ``run_id`` from ``PENDING`` to ``IN_PROGRESS``, returning the
    claimed row - or ``None`` if it wasn't ``PENDING`` (already claimed by a concurrent delivery,
    already terminal, or the row doesn't exist).

    This - not ``run_already_terminal``'s plain ``SELECT`` - is what actually closes the
    check-then-act race: Cloud Tasks documents at-least-once delivery, so two pushes for the same
    ``run_id`` can arrive close enough together that a non-atomic check-then-proceed lets both
    call ``run_pipeline``. That would mean two concurrent executions writing duplicate
    ``processing_steps`` rows (no unique constraint on ``(run_id, step_number)``), sending the
    step-7 Notify email/SMS twice, and racing on the ``events`` table's
    ``UNIQUE(user_id, description, resolved_date)`` constraint - whichever execution loses that
    race gets an uncaught `IntegrityError` and never reaches a terminal status. The
    `UPDATE ... WHERE status = 'pending'` here is a single atomic statement: only one concurrent
    caller can ever see it affect a row, so only one ever proceeds into ``run_pipeline``."""
    result = await session.execute(
        update(ProcessingRun)
        .where(ProcessingRun.id == run_id, ProcessingRun.status == ProcessingRunStatus.PENDING)
        .values(status=ProcessingRunStatus.IN_PROGRESS, started_at=datetime.now(timezone.utc))
        .returning(ProcessingRun.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await session.commit()
    return await session.get(ProcessingRun, run_id)


async def mark_runs_failed_to_schedule(
    run_ids: list[uuid.UUID], user_id: uuid.UUID, message: str
) -> None:
    """Mark one or more freshly-created runs FAILED when enqueuing their Cloud Tasks task itself
    raised (quota, transient gRPC error, IAM misconfiguration) - called by the upload/import
    endpoints, which otherwise have no way to recover a ``PENDING`` row that was committed before
    the (failed) dispatch attempt. Reuses ``_fail_run_without_storage``'s shape since there's
    never a ``StorageProvider`` in play at this point either."""
    for run_id in run_ids:
        await _fail_run_without_storage(run_id, user_id, message)


async def run_pipeline_dispatch(
    *,
    run_id: str,
    user_id: str,
    remote_file_id: str,
    remote_file_name: str,
    prompt_text: str,
    storage_mode: str,
    inline_content_b64: str | None = None,
) -> None:
    """The push endpoint's entrypoint - bridges a Cloud Tasks-delivered payload into the async
    pipeline. Unlike the old Celery task, no ``asyncio.run(...)`` bridge is needed: the FastAPI
    handler calling this is already async, running on the request's own event loop."""
    run_uuid = uuid.UUID(run_id)
    user_uuid = uuid.UUID(user_id)
    storage: StorageProvider | None = None

    try:
        try:
            storage, remote_file = await _build_storage_and_file(
                storage_mode=storage_mode,
                remote_file_id=remote_file_id,
                remote_file_name=remote_file_name,
                inline_content_b64=inline_content_b64,
                user_id=user_uuid,
            )
        except Exception as exc:
            logger.exception("pipeline dispatch: could not set up storage for run %s", run_id)
            await _fail_run_without_storage(
                run_uuid, user_uuid, f"Could not start processing: {exc}"
            )
            return

        async with _session_maker()() as session:
            run = await _claim_run(session, run_uuid)
            if run is None:
                logger.info(
                    "pipeline dispatch: run %s already claimed or terminal, skipping", run_id
                )
                return
            await run_pipeline(
                session,
                run=run,
                user_id=user_uuid,
                remote_file=remote_file,
                storage=storage,
                gemini=get_gemini_client(),
                notifier=get_pipeline_notifier(),
                prompt_text=prompt_text,
            )
    except Exception:
        # Re-raised (not swallowed) so the push endpoint returns 5xx and Cloud Tasks retries per
        # the queue's retry policy - this is the intended safety net for infra-level failures
        # (DB unreachable, unexpected crash), distinct from the expected-failure paths above and
        # in run_pipeline itself, which already mark the run FAILED and return normally.
        logger.exception("pipeline dispatch: unhandled failure for run %s", run_id)
        raise
    finally:
        if storage is not None:
            await storage.aclose()
