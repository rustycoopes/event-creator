"""The Celery worker that owns pipeline execution (Slice R8).

Unlike the organize-me monolith - where ``app/worker.py`` was a never-deployed stub and the 7-step
pipeline ran in-process as a plain asyncio background task (no Celery/Redis) - Event Creator runs
the pipeline as a real Celery task, dispatched by the API process (``app.api.v1.upload``,
``app.api.v1.import_pending_files``) and executed by the worker process supervisord starts
alongside the web process (``[program:worker]`` in ``supervisord.conf``).

``run_pipeline_task`` is a thin async-to-sync bridge: it takes only JSON-serialisable arguments
(ids/strings, never live objects like a ``StorageProvider`` or DB session - those don't survive a
Redis-brokered hop to a different process), reconstructs its collaborators, and calls
``app.services.pipeline.runner.run_pipeline`` - the actual pipeline logic, unchanged from the
monolith and independently unit-tested with fakes.

Two storage modes:

- ``"configured"``: the file was already written into the user's real, connected storage provider
  (Google Drive/Dropbox/S3) by the API process before dispatch. The task rebuilds the same kind of
  provider from the user's persisted ``storage_configs`` row and downloads by the file's remote id.
- ``"ephemeral"``/``"fake"``: there is no persistent, cross-process store to hand the file through
  (the graceful ephemeral fallback, issue #79, and the E2E fake provider are both purely
  in-memory). For these, the dispatching process base64-encodes the file's bytes into the task
  payload (small files only - the same 10MB cap the upload endpoint already enforces) and the task
  re-seeds a fresh in-memory provider from them.

Each Celery worker child process reuses the *same* process (and hence the same ``@lru_cache``d
``app.db.session.get_engine()``) across many task invocations, but ``run_pipeline_task`` bridges
into async code with a fresh ``asyncio.run(...)`` **per task** - a fresh event loop every time.
asyncpg connections are bound to the loop that created them, so a pooled connection opened during
one task's loop is unusable (and errors) once that loop closes. Every task therefore disposes the
engine's connection pool at the end of its own run (still inside its own loop, before it closes),
so the next task starts with a clean pool rather than reusing now-orphaned connections.
"""

import asyncio
import base64
import logging
import os
import ssl
import uuid
from datetime import datetime, timezone

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import select
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
from app.services.storage.fake import FakeStorageProvider
from app.services.storage.factory import build_storage_provider

logger = logging.getLogger(__name__)

# A plain os.environ read (not the pydantic Settings class, mirrors app.core.auth's
# COOKIE_SECURE) - constructing Celery's app happens at import time, and Settings() requires
# every other field (DATABASE_URL, JWT_SECRET, ...) to already be resolved too. Defaulting to a
# local placeholder mirrors organize-me's own worker.py stub; only actually dispatching/running a
# task requires a reachable Redis.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("event_creator", broker=_REDIS_URL, backend=_REDIS_URL)

if _REDIS_URL.startswith("rediss://"):
    # kombu's redis transport refuses to start over TLS (`rediss://`, e.g. Upstash) unless
    # ssl_cert_reqs is set explicitly - "A rediss:// URL must have parameter ssl_cert_reqs..."
    # crashes the worker at startup otherwise (celery/backends/redis.py). CERT_REQUIRED verifies
    # the server cert against the system CA bundle, same as any other TLS client would default to.
    _redis_ssl_options = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    celery_app.conf.broker_use_ssl = _redis_ssl_options
    celery_app.conf.redis_backend_use_ssl = _redis_ssl_options


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
            logger.error("pipeline task: run %s vanished before it could be marked failed", run_uuid)
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
            logger.exception("pipeline task: failure notification itself failed for run %s", run_uuid)


async def _run_pipeline_task_async(
    *,
    run_id: str,
    user_id: str,
    remote_file_id: str,
    remote_file_name: str,
    prompt_text: str,
    storage_mode: str,
    inline_content_b64: str | None,
) -> None:
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
            logger.exception("pipeline task: could not set up storage for run %s", run_id)
            await _fail_run_without_storage(
                run_uuid, user_uuid, f"Could not start processing: {exc}"
            )
            return

        async with _session_maker()() as session:
            run = await session.get(ProcessingRun, run_uuid)
            if run is None:  # pragma: no cover - the row was just committed by the API process
                logger.error("pipeline task: run %s vanished before the worker picked it up", run_id)
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
    except Exception:  # pragma: no cover - a Celery task must never crash silently
        logger.exception("pipeline task failed for run %s", run_id)
        raise
    finally:
        if storage is not None:
            await storage.aclose()
        # Every task ran its own asyncio.run() loop; a Celery worker child process reuses this
        # module's lru_cache'd engine across many such loops. Dispose the pool here, inside the
        # loop that created its connections, so the next task's fresh loop never inherits
        # connections bound to a now-closed one (see module docstring).
        await get_engine().dispose()


@celery_app.task(name="pipeline.run")  # type: ignore[untyped-decorator]
def run_pipeline_task(
    *,
    run_id: str,
    user_id: str,
    remote_file_id: str,
    remote_file_name: str,
    prompt_text: str,
    storage_mode: str,
    inline_content_b64: str | None = None,
) -> None:
    """Celery entrypoint (sync, per Celery's own contract) - bridges into the async pipeline."""
    asyncio.run(
        _run_pipeline_task_async(
            run_id=run_id,
            user_id=user_id,
            remote_file_id=remote_file_id,
            remote_file_name=remote_file_name,
            prompt_text=prompt_text,
            storage_mode=storage_mode,
            inline_content_b64=inline_content_b64,
        )
    )
