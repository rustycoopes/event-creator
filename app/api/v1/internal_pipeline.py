"""The Cloud Tasks push target for pipeline execution (replaces the Celery worker - Slice R8's
`app.worker` - per docs/adr/0001-event-creator-worker-cpu-throttling.md in organize-me).

``POST /internal/pipeline/run`` is never called by a browser or by `app.api.v1.upload`/
`import_pending_files` directly - it's the HTTP target `app.api.v1.upload.CloudTasksPipelineScheduler`
enqueues a Cloud Tasks task against. Cloud Tasks calls back into this same Cloud Run service with a
Google-signed OIDC token identifying the dispatching service account; verifying that token here
(rather than relying on Cloud Run's own IAM gate) is what keeps this endpoint safe while the
service as a whole stays `--allow-unauthenticated` for its normal user-facing routes.

Not registered under the versioned `/api/v1` prefix (deliberately - this isn't a public API
surface for any client, versioned or not) and never behind `current_user_id` (there's no browser
session driving this request - it carries no Host-issued cookie at all).

**Batch chaining.** A multi-file import (`app.api.v1.import_pending_files`) needs its files
processed strictly one after another (organize-me's #110) - the queue's own
``max-concurrent-dispatches=1`` setting guarantees non-*concurrency* but Cloud Tasks documents
dispatch order as best-effort, not guaranteed, and a retry on an earlier item can let a later
item's task become eligible first. So a batch is chained explicitly instead: each payload carries
the rest of the batch as ``remaining_batch``, and once this handler finishes the current item
(whether freshly processed or skipped as already-terminal - a retried request still carries the
same ``remaining_batch``, so the chain can resume even from a retry), it enqueues the *next* item
itself before returning, with the tail of the list as that item's own ``remaining_batch``.
"""

import logging
import uuid

import cachecontrol
import requests
from fastapi import APIRouter, Depends, HTTPException, Request, status
from google.auth.transport import requests as google_auth_requests

# Explicit self-referential re-export (`as id_token`, not a plain `import`) so mypy's strict
# no-implicit-reexport check allows tests to monkeypatch
# `internal_pipeline_module.id_token.verify_oauth2_token` rather than reaching into google.oauth2
# directly.
from google.oauth2 import id_token as id_token
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.services.pipeline.cloud_tasks import enqueue_pipeline_run
from app.services.pipeline.dispatch import run_already_terminal, run_pipeline_dispatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/pipeline", tags=["internal"])

# google-auth's own docs note verify_oauth2_token re-fetches Google's public certs on every call
# by default - wrapping the session in CacheControl means repeated pushes (one per pipeline run)
# reuse the cached certs per their own Cache-Control headers instead of an extra network round
# trip on every single dispatch.
_google_auth_request = google_auth_requests.Request(
    session=cachecontrol.CacheControl(requests.Session())
)


class BatchItem(BaseModel):
    """One remaining file in a chained batch import - see this module's docstring."""

    run_id: str
    remote_file_id: str
    remote_file_name: str
    storage_mode: str
    inline_content_b64: str | None = None


class PipelineDispatchPayload(BaseModel):
    run_id: str
    user_id: str
    remote_file_id: str
    remote_file_name: str
    prompt_text: str
    storage_mode: str
    inline_content_b64: str | None = None
    remaining_batch: list[BatchItem] = Field(default_factory=list)


def _verify_push_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Reject anything but a Google-signed OIDC token for exactly the service account Cloud Tasks
    is configured to invoke this endpoint as - see `CloudTasksPipelineScheduler`'s `oidc_token`.

    Checks both the token's signature/expiry (verified against Google's public certs by
    `id_token.verify_oauth2_token`) and that its `aud`/`email` claims match what this deployment
    expects; a token that's validly signed but minted for a *different* audience or service
    account must not be accepted here."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_token")
    token = auth_header.removeprefix("Bearer ")

    if not settings.pipeline_endpoint_url or not settings.pipeline_invoker_service_account:
        # Not wired yet in this environment (e.g. local dev without Cloud Tasks configured) -
        # fail closed rather than silently accepting unauthenticated pushes.
        logger.error("pipeline push endpoint: OIDC verification not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_configured")

    try:
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, _google_auth_request, audience=settings.pipeline_endpoint_url
        )
    except Exception:
        logger.exception("pipeline push endpoint: OIDC token verification failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token") from None

    if claims.get("email") != settings.pipeline_invoker_service_account:
        logger.error(
            "pipeline push endpoint: token email %r does not match expected invoker",
            claims.get("email"),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="wrong_identity")


async def _advance_batch_chain(payload: PipelineDispatchPayload) -> None:
    """Enqueue the next item in ``payload.remaining_batch``, if any - see this module's docstring
    on batch chaining. Raising here (e.g. Cloud Tasks quota, a transient gRPC error) intentionally
    fails this whole request so Cloud Tasks retries it; a retry re-enters this same function with
    the same ``remaining_batch``, since the failing dispatch already happened (or didn't) before
    this call - `_claim_run`'s atomic pickup means re-running the *current* item's own processing
    on that retry is a safe no-op either way."""
    if not payload.remaining_batch:
        return
    next_item, *rest = payload.remaining_batch
    await enqueue_pipeline_run(
        {
            "run_id": next_item.run_id,
            "user_id": payload.user_id,
            "remote_file_id": next_item.remote_file_id,
            "remote_file_name": next_item.remote_file_name,
            "prompt_text": payload.prompt_text,
            "storage_mode": next_item.storage_mode,
            "inline_content_b64": next_item.inline_content_b64,
            "remaining_batch": [item.model_dump() for item in rest],
        }
    )


@router.post("/run", status_code=status.HTTP_200_OK, dependencies=[Depends(_verify_push_token)])
async def run_pipeline_push(payload: PipelineDispatchPayload) -> dict[str, str]:
    run_id = uuid.UUID(payload.run_id)
    if await run_already_terminal(run_id):
        # Fast-path skip: a Cloud Tasks retry delivered after an earlier attempt already finished
        # (e.g. the response was lost in transit) doesn't need to reprocess a SUCCESS/FAILED run.
        # The real double-processing guard is `_claim_run`'s atomic pickup inside
        # run_pipeline_dispatch, not this check - see that function's docstring.
        logger.info("pipeline push endpoint: run %s already terminal, skipping", run_id)
        await _advance_batch_chain(payload)
        return {"status": "already_terminal"}

    await run_pipeline_dispatch(
        run_id=payload.run_id,
        user_id=payload.user_id,
        remote_file_id=payload.remote_file_id,
        remote_file_name=payload.remote_file_name,
        prompt_text=payload.prompt_text,
        storage_mode=payload.storage_mode,
        inline_content_b64=payload.inline_content_b64,
    )
    await _advance_batch_chain(payload)
    return {"status": "done"}
