"""Tests for the processing-run detail + logs JSON API in app.api.v1.processing_runs, and the HTML
run-detail page + its HTMX log partial in app.pages.processing (ported from organize-me's
tests/test_processing_run_detail.py, Slice 6.2/#84, to Event Creator in Slice R8/R9).

Auth is a Host-issued JWT cookie (see app.core.auth) rather than organize-me's register+cookie-
login flow.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host_user import HostUser
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from app.services.llm.gemini import FakeGeminiClient
from app.services.notifications.pipeline import FakeNotificationSender
from app.services.pipeline.runner import run_pipeline
from app.services.storage.fake import FakeStorageProvider
from tests.conftest import TokenFactory, create_host_user


async def test_processing_run_detail_page_requires_login(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    response = await client.get(f"/processing-runs/{run_id}", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login"


async def test_processing_run_detail_page_applies_the_hosts_dark_mode_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for issue #207: the page must read the Host's `dark_mode` preference
    rather than hardcoding light mode."""
    user_id = await create_host_user(db_session, dark_mode=True)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()

    response = await client.get(
        f"/processing-runs/{run.id}",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text


async def test_processing_run_detail_page_defaults_to_light_mode(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, dark_mode=False)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()

    response = await client.get(
        f"/processing-runs/{run.id}",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert 'data-theme="corporate"' in response.text


async def test_processing_run_detail_page_renders_the_hosts_collapsed_group_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for event-creator#18/#19 - see test_logs_page.py's equivalent test for the
    full rationale (missing nav context here would crash the shared sidebar template)."""
    user_id = await create_host_user(db_session, nav_collapsed_groups={"event-creator": True})
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()

    response = await client.get(
        f"/processing-runs/{run.id}",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    # Key:value pair, not the whole object literal - the registry now has more than one app, so
    # asserting the full "{...}" would break every time an app is added to the registry.
    assert "&#34;event-creator&#34;: true" in response.text


async def test_processing_run_detail_page_404s_for_nonexistent_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = uuid.uuid4()

    response = await client.get(
        f"/processing-runs/{run_id}",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 404


async def test_processing_run_detail_page_404s_for_another_users_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    other_user_id = await create_host_user(db_session)  # user B
    other_run = ProcessingRun(
        user_id=other_user_id, filename="secret.txt", status=ProcessingRunStatus.SUCCESS
    )
    db_session.add(other_run)
    await db_session.flush()
    user_a_id = await create_host_user(db_session)  # user A

    response = await client.get(
        f"/processing-runs/{other_run.id}",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_a_id))},
    )

    assert response.status_code == 404


async def test_processing_run_detail_page_renders_run_metadata_and_steps(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(
        user_id=user_id,
        filename="test.txt",
        status=ProcessingRunStatus.SUCCESS,
        events_extracted_count=42,
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        ProcessingStep(
            run_id=run.id,
            step_number=1,
            step_name="File Received",
            status=ProcessingStepStatus.SUCCESS,
        )
    )
    await db_session.flush()

    response = await client.get(f"/processing-runs/{run.id}", cookies=cookies)

    assert response.status_code == 200
    body = response.text
    assert "Run Detail" in body
    assert "test.txt" in body
    assert "42" in body
    assert "File Received" in body
    # All 7 step indicators should render
    assert "Deduplicate" in body or "deduplicate" in body.lower()
    # Download logs link
    assert f"/api/v1/processing-runs/{run.id}/logs/download" in body


async def test_processing_run_logs_html_endpoint_returns_html_partial(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    step = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Error: something went wrong", "Then recovered", "Finally done"],
    )
    db_session.add(step)
    await db_session.flush()

    response = await client.get(
        f"/api/html/processing-runs/{run.id}/logs?step_number=1", cookies=cookies
    )

    assert response.status_code == 200
    body = response.text
    assert "Error: something went wrong" in body
    assert "Then recovered" in body
    assert "Finally done" in body
    # Should include search form and pagination info
    assert "Filter logs" in body or "filter" in body.lower()


async def test_processing_run_logs_html_endpoint_requires_login(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    response = await client.get(f"/api/html/processing-runs/{run_id}/logs?step_number=1")

    assert response.status_code in (401, 403)


async def test_processing_run_logs_endpoint_returns_json(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    step = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Line 1", "Line 2", "Line 3"],
    )
    db_session.add(step)
    await db_session.flush()

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1", cookies=cookies
    )

    assert response.status_code == 200
    data = response.json()
    assert data["step_number"] == 1
    assert data["step_name"] == "File Received"
    assert data["log_lines"] == ["Line 1", "Line 2", "Line 3"]
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["total"] == 3


async def test_processing_run_logs_endpoint_shows_silent_notification_mode_warning(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for #112: a real pipeline run for a user with no phone number on file
    (SMS is "on" by default but unreachable) surfaces the silent-mode warning through the actual
    logs endpoint, not just as a ProcessingStep.log_lines assertion in the pipeline unit tests."""
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    user = await db_session.get(HostUser, user_id)
    assert user is not None
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.PENDING)
    db_session.add(run)
    await db_session.flush()
    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("chat.txt", b"data")

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=FakeGeminiClient("[]"),
        notifier=FakeNotificationSender(),
        prompt_text="extract events",
    )

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=7", cookies=cookies
    )

    assert response.status_code == 200
    assert "Warning: no phone number" in response.json()["log_lines"]


async def test_processing_run_logs_searches_log_lines(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    step = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Starting process", "Parsing file", "Error: invalid format", "Retrying"],
    )
    db_session.add(step)
    await db_session.flush()

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1&search=error", cookies=cookies
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["log_lines"]) == 1
    assert "Error: invalid format" in data["log_lines"]
    assert data["total"] == 1


async def test_processing_run_logs_search_matches_literal_percent_and_underscore(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Search is a plain substring match, not a SQL LIKE pattern — ``%``/``_`` are literal."""
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    step = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Progress: 50% complete", "some_var set", "unrelated line"],
    )
    db_session.add(step)
    await db_session.flush()

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1&search=50%25", cookies=cookies
    )
    data = response.json()
    assert data["log_lines"] == ["Progress: 50% complete"]

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1&search=some_var", cookies=cookies
    )
    data = response.json()
    assert data["log_lines"] == ["some_var set"]


async def test_processing_run_logs_paginates(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    # Create 75 log lines (1.5 pages)
    log_lines = [f"Line {i}" for i in range(1, 76)]
    step = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=log_lines,
    )
    db_session.add(step)
    await db_session.flush()

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1&page=1", cookies=cookies
    )
    data = response.json()
    assert len(data["log_lines"]) == 50
    assert data["page"] == 1
    assert data["total"] == 75

    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=1&page=2", cookies=cookies
    )
    data = response.json()
    assert len(data["log_lines"]) == 25
    assert data["page"] == 2


async def test_processing_run_logs_endpoint_requires_login(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    response = await client.get(f"/api/v1/processing-runs/{run_id}/logs?step_number=1")

    assert response.status_code in (401, 403)


async def test_processing_run_logs_endpoint_404s_for_another_users_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    other_user_id = await create_host_user(db_session)  # user B
    other_run = ProcessingRun(user_id=other_user_id, filename="secret.txt", status="success")
    db_session.add(other_run)
    await db_session.flush()
    user_a_id = await create_host_user(db_session)  # user A
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_a_id))}

    response = await client.get(
        f"/api/v1/processing-runs/{other_run.id}/logs?step_number=1", cookies=cookies
    )

    assert response.status_code == 404


async def test_processing_run_logs_endpoint_404s_for_nonexistent_step(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()

    # Step 7 is valid per spec but doesn't exist in DB, should 404.
    response = await client.get(
        f"/api/v1/processing-runs/{run.id}/logs?step_number=7", cookies=cookies
    )

    assert response.status_code == 404


async def test_processing_run_detail_api_endpoint_returns_run_with_steps(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(
        user_id=user_id,
        filename="test.txt",
        status=ProcessingRunStatus.SUCCESS,
        events_extracted_count=5,
    )
    db_session.add(run)
    await db_session.flush()
    step1 = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
    )
    step2 = ProcessingStep(
        run_id=run.id,
        step_number=2,
        step_name="Extract",
        status=ProcessingStepStatus.SUCCESS,
    )
    db_session.add_all([step1, step2])
    await db_session.flush()

    response = await client.get(f"/api/v1/processing-runs/{run.id}", cookies=cookies)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(run.id)
    assert data["filename"] == "test.txt"
    assert data["status"] == "success"
    assert data["events_extracted_count"] == 5
    assert len(data["steps"]) == 2
    assert data["steps"][0]["step_number"] == 1
    assert data["steps"][0]["step_name"] == "File Received"
    assert data["steps"][1]["step_number"] == 2


async def test_processing_run_detail_api_endpoint_requires_login(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    response = await client.get(f"/api/v1/processing-runs/{run_id}")

    assert response.status_code in (401, 403)


async def test_processing_run_detail_api_endpoint_404s_for_another_users_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    other_user_id = await create_host_user(db_session)  # user B
    other_run = ProcessingRun(
        user_id=other_user_id, filename="secret.txt", status=ProcessingRunStatus.SUCCESS
    )
    db_session.add(other_run)
    await db_session.flush()
    user_a_id = await create_host_user(db_session)  # user A
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_a_id))}

    response = await client.get(f"/api/v1/processing-runs/{other_run.id}", cookies=cookies)

    assert response.status_code == 404


async def test_processing_run_logs_download_returns_valid_json(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = ProcessingRun(user_id=user_id, filename="test.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
    step1 = ProcessingStep(
        run_id=run.id,
        step_number=1,
        step_name="File Received",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Line 1", "Line 2"],
    )
    step2 = ProcessingStep(
        run_id=run.id,
        step_number=2,
        step_name="Extract",
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["Extract line 1"],
    )
    db_session.add_all([step1, step2])
    await db_session.flush()

    response = await client.get(f"/api/v1/processing-runs/{run.id}/logs/download", cookies=cookies)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert str(run.id) in response.headers["content-disposition"]
    data = response.json()
    assert data["run_id"] == str(run.id)
    assert data["filename"] == "test.txt"
    assert len(data["steps"]) == 2
    assert data["steps"][0]["step_number"] == 1
    assert data["steps"][0]["log_lines"] == ["Line 1", "Line 2"]
    assert data["steps"][1]["step_number"] == 2
    assert data["steps"][1]["log_lines"] == ["Extract line 1"]


async def test_processing_run_logs_download_requires_login(client: AsyncClient) -> None:
    run_id = uuid.uuid4()
    response = await client.get(f"/api/v1/processing-runs/{run_id}/logs/download")

    assert response.status_code in (401, 403)


async def test_processing_run_logs_download_404s_for_nonexistent_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run_id = uuid.uuid4()

    response = await client.get(f"/api/v1/processing-runs/{run_id}/logs/download", cookies=cookies)

    assert response.status_code == 404


async def test_processing_run_logs_download_404s_for_another_users_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    other_user_id = await create_host_user(db_session)  # user B
    other_run = ProcessingRun(
        user_id=other_user_id, filename="secret.txt", status=ProcessingRunStatus.SUCCESS
    )
    db_session.add(other_run)
    await db_session.flush()
    user_a_id = await create_host_user(db_session)  # user A
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_a_id))}

    response = await client.get(
        f"/api/v1/processing-runs/{other_run.id}/logs/download", cookies=cookies
    )

    assert response.status_code == 404
