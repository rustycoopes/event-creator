"""Tests for the live-progress SSE stream (ported from organize-me's Slice 4.2/#53 to Event
Creator in Slice R13, #168 — flagged in issue #198 as having no exact-name counterpart here).

Covers the ``stream_run_progress`` generator directly (terminal run -> step events + done) and the
``GET /api/v1/processing-runs/{id}/sse`` endpoint's auth + ownership gating. Adapted to this repo's
Host-JWT-cookie auth (``create_host_user`` + ``make_token``) instead of organize-me's own
register/login endpoints, which don't exist here.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.services.pipeline.progress import stream_run_progress
from tests.conftest import TokenFactory, create_host_user


async def _seed_run(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: ProcessingRunStatus,
    step_statuses: dict[int, ProcessingStepStatus],
) -> ProcessingRun:
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=status)
    db.add(run)
    await db.flush()
    for number, step_status in step_statuses.items():
        db.add(
            ProcessingStep(
                run_id=run.id,
                step_number=number,
                step_name=f"Step {number}",
                status=step_status,
            )
        )
    await db.flush()
    return run


async def test_stream_emits_step_events_and_closes_for_a_terminal_run(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    run = await _seed_run(
        db_session,
        user_id,
        status=ProcessingRunStatus.SUCCESS,
        step_statuses={n: ProcessingStepStatus.SUCCESS for n in range(1, 8)},
    )

    events = [
        event
        async for event in stream_run_progress(
            db_session, run.id, poll_interval=0.01, max_seconds=1.0
        )
    ]

    kinds = [e["event"] for e in events]
    assert kinds.count("done") == 1
    assert kinds[-1] == "done"
    for n in range(1, 8):
        assert f"step-{n}" in kinds
    assert "run-status" in kinds
    step_event = next(e for e in events if e["event"] == "step-1")
    assert 'data-status="success"' in step_event["data"]


async def test_stream_reports_a_failed_run_as_terminal(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _seed_run(
        db_session,
        user_id,
        status=ProcessingRunStatus.FAILED,
        step_statuses={1: ProcessingStepStatus.SUCCESS, 4: ProcessingStepStatus.FAILED},
    )

    events = [
        event
        async for event in stream_run_progress(
            db_session, run.id, poll_interval=0.01, max_seconds=1.0
        )
    ]

    status_event = next(e for e in events if e["event"] == "run-status")
    assert 'data-run-status="failed"' in status_event["data"]
    assert events[-1]["event"] == "done"
    step4 = next(e for e in events if e["event"] == "step-4")
    assert 'data-status="failed"' in step4["data"]


async def test_sse_endpoint_requires_authentication(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/processing-runs/{uuid.uuid4()}/sse")
    assert response.status_code == 401


async def test_sse_endpoint_hides_another_users_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    other_user_id = await create_host_user(db_session)  # user B
    run = await _seed_run(
        db_session,
        other_user_id,
        status=ProcessingRunStatus.SUCCESS,
        step_statuses={1: ProcessingStepStatus.SUCCESS},
    )
    user_a_id = await create_host_user(db_session)  # user A

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/sse",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_a_id))},
    )

    assert response.status_code == 404


async def test_sse_endpoint_streams_owned_terminal_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run = await _seed_run(
        db_session,
        user_id,
        status=ProcessingRunStatus.SUCCESS,
        step_statuses={n: ProcessingStepStatus.SUCCESS for n in range(1, 8)},
    )

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/sse",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert "event: done" in body
    assert 'data-status="success"' in body
