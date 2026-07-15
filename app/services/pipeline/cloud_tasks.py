"""Shared Cloud Tasks enqueue logic (Slice R11 redesign, replacing Celery - see
docs/adr/0001-event-creator-worker-cpu-throttling.md in organize-me).

Used by two call sites that both need to put a pipeline-dispatch payload onto the queue:
``app.api.v1.upload.CloudTasksPipelineScheduler`` (the initial dispatch, from the upload/import
endpoints) and ``app.api.v1.internal_pipeline`` itself (advancing a batch to its next item after
the current one finishes - see that module's docstring for why a batch is chained explicitly
rather than relying on the queue's ``max-concurrent-dispatches=1`` setting for ordering).
"""

import asyncio
import json
from functools import lru_cache
from typing import Any

from google.cloud import tasks_v2

from app.core.config import get_settings

PIPELINE_RUN_PATH = "/internal/pipeline/run"

# Cloud Tasks caps a task's dispatch deadline at 30 minutes - generous headroom versus a real
# pipeline run (Gemini call + parse + save), while still bounding the worst case.
_MAX_DISPATCH_DEADLINE_SECONDS = 1800


@lru_cache
def _get_tasks_client() -> tasks_v2.CloudTasksClient:
    """Process-wide gRPC client, built once (mirrors ``app.db.session.get_engine``'s
    ``lru_cache`` pattern) rather than per-call - channel setup isn't free, and nothing about
    this client is request-scoped."""
    return tasks_v2.CloudTasksClient()


async def enqueue_pipeline_run(payload: dict[str, Any]) -> None:
    """Enqueue ``payload`` as a Cloud Tasks push task targeting
    ``POST {PIPELINE_ENDPOINT_URL}/internal/pipeline/run`` on this same service, OIDC-signed as
    ``settings.pipeline_invoker_service_account``. ``payload`` must match
    ``app.api.v1.internal_pipeline.PipelineDispatchPayload``'s shape."""
    settings = get_settings()
    client = _get_tasks_client()
    queue_path = client.queue_path(
        settings.gcp_project_id, settings.cloud_tasks_location, settings.cloud_tasks_queue
    )
    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{settings.pipeline_endpoint_url}{PIPELINE_RUN_PATH}",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=settings.pipeline_invoker_service_account,
                audience=settings.pipeline_endpoint_url,
            ),
        ),
        dispatch_deadline={"seconds": _MAX_DISPATCH_DEADLINE_SECONDS},
    )
    # CloudTasksClient.create_task is a blocking gRPC call - offload it so it doesn't stall the
    # event loop the calling request (and every other in-flight request on this instance) is
    # running on.
    await asyncio.to_thread(client.create_task, parent=queue_path, task=task)
