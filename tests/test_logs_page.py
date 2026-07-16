"""Tests for the Processing History logs page and its JSON API endpoint (ported from
organize-me's tests/test_logs_page.py, Slice 6.1/#83, redesigned as a filterable/sortable grid in
#111, to Event Creator in Slice R8).

Auth is a Host-issued JWT cookie (see app.core.auth) rather than organize-me's register+cookie-
login flow. ``app/pages/logs.py`` landed after this port was started (Slice R8, in-flight
alongside this test file) - see the report for that timing note.
"""

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.processing_step import ProcessingStep, ProcessingStepStatus
from tests.conftest import TokenFactory, create_host_user


async def _make_run(
    db: AsyncSession,
    user_id: uuid.UUID,
    filename: str = "chat.txt",
    status: ProcessingRunStatus = ProcessingRunStatus.SUCCESS,
    events_count: int = 0,
    created_at: datetime | None = None,
) -> ProcessingRun:
    run = ProcessingRun(
        user_id=user_id,
        filename=filename,
        status=status,
        events_extracted_count=events_count,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    return run


async def _make_step(
    db: AsyncSession,
    run_id: uuid.UUID,
    step_number: int = 1,
    status: ProcessingStepStatus = ProcessingStepStatus.SUCCESS,
    log_lines: list[str] | None = None,
) -> ProcessingStep:
    step = ProcessingStep(
        run_id=run_id,
        step_number=step_number,
        step_name=f"step_{step_number}",
        status=status,
        log_lines=log_lines or [],
    )
    db.add(step)
    await db.flush()
    return step


async def test_logs_redirects_anonymous_visitor_to_login(client: AsyncClient) -> None:
    response = await client.get("/logs")

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login"


async def test_logs_page_applies_the_hosts_dark_mode_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for issue #207: the page must read the Host's `dark_mode` preference
    rather than hardcoding light mode."""
    user_id = await create_host_user(db_session, dark_mode=True)

    response = await client.get(
        "/logs", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text


async def test_logs_page_defaults_to_light_mode(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, dark_mode=False)

    response = await client.get(
        "/logs", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    assert 'data-theme="corporate"' in response.text


async def test_logs_page_renders_the_hosts_collapsed_group_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for event-creator#18/#19: every page extending
    chrome_authenticated_base.html must supply nav_groups/flat_nav_items/nav_collapsed_json/
    nav_stored_collapsed_json, or the template's `{{ ... | tojson }}` calls raise a TypeError on
    Jinja's Undefined sentinel. This also confirms the real Host-stored preference is read, not
    hardcoded."""
    user_id = await create_host_user(db_session, nav_collapsed_groups={"event-creator": True})

    response = await client.get(
        "/logs", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    assert "storedCollapsed: {&#34;event-creator&#34;: true}" in response.text


async def test_logs_shows_empty_state_with_no_runs(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    response = await client.get("/logs", cookies=cookies)

    assert response.status_code == 200
    assert "No processing runs yet" in response.text


async def test_logs_renders_run_row_with_all_columns(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(
        db_session,
        user_id,
        filename="test_file.txt",
        status=ProcessingRunStatus.SUCCESS,
        events_count=5,
    )

    response = await client.get("/logs", cookies=cookies)

    assert response.status_code == 200
    body = response.text
    assert "test_file.txt" in body
    assert "success" in body
    assert "5" in body
    assert f"/processing-runs/{run.id}" in body


async def test_logs_paginates_across_multiple_pages(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for i in range(55):
        await _make_run(db_session, user_id, filename=f"file_{i}.txt")

    first_page = await client.get("/logs", cookies=cookies)
    second_page = await client.get("/logs", params={"page": 2}, cookies=cookies)

    assert "Page 1 of 2" in first_page.text
    assert "Next" in first_page.text
    assert "Page 2 of 2" in second_page.text
    assert "Previous" in second_page.text
    # First/Last jump links (carried over from the pre-grid table) let a user reach either end
    # of a large result set without repeatedly clicking Next.
    assert "Last" in first_page.text
    assert "First" in second_page.text


async def test_logs_shows_total_run_count(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for _ in range(3):
        await _make_run(db_session, user_id)

    response = await client.get("/logs", cookies=cookies)

    assert "3 total processing runs" in response.text


async def test_logs_redirects_out_of_range_page_to_last_valid_page(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for _ in range(3):
        await _make_run(db_session, user_id)

    # Only 1 page exists (3 runs, 50/page); page=5 is out of range.
    response = await client.get("/logs", params={"page": 5}, cookies=cookies)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/logs?page=1"


async def test_logs_does_not_redirect_when_there_are_zero_runs(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    # No runs at all: an out-of-range page must not redirect.
    response = await client.get("/logs", params={"page": 5}, cookies=cookies)

    assert response.status_code == 200
    assert "No processing runs yet" in response.text


async def test_api_processing_runs_returns_authenticated_user_runs_only(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user1_id = await create_host_user(db_session)
    await _make_run(db_session, user1_id, filename="user1_file.txt")

    # Check that user1 sees their own run.
    response = await client.get(
        "/api/v1/processing-runs",
        cookies={"organizeme_auth": make_token.valid(sub=str(user1_id))},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert len(body["runs"]) == 1
    assert body["runs"][0]["filename"] == "user1_file.txt"

    # Create a second user with a run.
    user2_id = await create_host_user(db_session)
    await _make_run(db_session, user2_id, filename="user2_file.txt")

    # User2 should only see their own run (not user1's).
    response = await client.get(
        "/api/v1/processing-runs",
        cookies={"organizeme_auth": make_token.valid(sub=str(user2_id))},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert len(body["runs"]) == 1
    assert body["runs"][0]["filename"] == "user2_file.txt"


async def test_api_processing_runs_paginates_at_50_per_page(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for i in range(55):
        await _make_run(db_session, user_id, filename=f"file_{i}.txt")

    first_page = await client.get(
        "/api/v1/processing-runs", params={"page": 1}, cookies=cookies
    )
    second_page = await client.get(
        "/api/v1/processing-runs", params={"page": 2}, cookies=cookies
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200

    first_body = first_page.json()
    second_body = second_page.json()

    assert len(first_body["runs"]) == 50
    assert len(second_body["runs"]) == 5
    assert first_body["total"] == 55
    assert first_body["page_size"] == 50


async def test_api_processing_runs_ordered_newest_first(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for i in range(3):
        await _make_run(db_session, user_id, filename=f"file_{i}.txt")

    response = await client.get("/api/v1/processing-runs", cookies=cookies)
    body = response.json()

    assert response.status_code == 200
    # Runs should be newest first (reverse of creation order).
    assert body["runs"][0]["filename"] == "file_2.txt"
    assert body["runs"][1]["filename"] == "file_1.txt"
    assert body["runs"][2]["filename"] == "file_0.txt"


async def test_api_processing_runs_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/processing-runs")

    assert response.status_code in (401, 403)


async def test_logs_shows_run_status_badge_with_correct_color(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, status=ProcessingRunStatus.SUCCESS)
    await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)
    await _make_run(db_session, user_id, status=ProcessingRunStatus.IN_PROGRESS)

    response = await client.get("/logs", cookies=cookies)

    body = response.text
    assert "badge-success" in body
    assert "badge-error" in body
    assert "badge-warning" in body


# --- Grid redesign (#111): status/date filters, column sort, expanded-details column ---


async def test_api_processing_runs_filters_by_status(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, filename="ok.txt", status=ProcessingRunStatus.SUCCESS)
    await _make_run(db_session, user_id, filename="bad.txt", status=ProcessingRunStatus.FAILED)

    response = await client.get(
        "/api/v1/processing-runs", params={"status": "failed"}, cookies=cookies
    )

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert body["runs"][0]["filename"] == "bad.txt"


async def test_api_processing_runs_filters_by_date_range(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    today = datetime.now(timezone.utc)
    await _make_run(db_session, user_id, filename="old.txt", created_at=today - timedelta(days=10))
    await _make_run(db_session, user_id, filename="recent.txt", created_at=today)

    response = await client.get(
        "/api/v1/processing-runs",
        params={"date_from": (today - timedelta(days=1)).date().isoformat()},
        cookies=cookies,
    )

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert body["runs"][0]["filename"] == "recent.txt"


async def test_api_processing_runs_sorts_by_filename_ascending(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, filename="charlie.txt")
    await _make_run(db_session, user_id, filename="alpha.txt")
    await _make_run(db_session, user_id, filename="bravo.txt")

    response = await client.get(
        "/api/v1/processing-runs",
        params={"sort_by": "filename", "sort_dir": "asc"},
        cookies=cookies,
    )

    filenames = [r["filename"] for r in response.json()["runs"]]
    assert filenames == ["alpha.txt", "bravo.txt", "charlie.txt"]


async def test_api_processing_runs_sorts_by_status(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, filename="a.txt", status=ProcessingRunStatus.SUCCESS)
    await _make_run(db_session, user_id, filename="b.txt", status=ProcessingRunStatus.FAILED)

    asc_response = await client.get(
        "/api/v1/processing-runs",
        params={"sort_by": "status", "sort_dir": "asc"},
        cookies=cookies,
    )
    desc_response = await client.get(
        "/api/v1/processing-runs",
        params={"sort_by": "status", "sort_dir": "desc"},
        cookies=cookies,
    )

    # Postgres sorts a native enum column by its declared member order, not alphabetically, so
    # just assert that reversing sort_dir reverses the order rather than hard-coding that order.
    asc_statuses = [r["status"] for r in asc_response.json()["runs"]]
    desc_statuses = [r["status"] for r in desc_response.json()["runs"]]
    assert asc_statuses == list(reversed(desc_statuses))
    assert asc_statuses != desc_statuses


async def test_api_processing_runs_filters_and_sort_compose_with_pagination(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    for i in range(3):
        await _make_run(
            db_session, user_id, filename=f"ok_{i}.txt", status=ProcessingRunStatus.SUCCESS
        )
    await _make_run(db_session, user_id, filename="bad.txt", status=ProcessingRunStatus.FAILED)

    response = await client.get(
        "/api/v1/processing-runs",
        params={"status": "success", "sort_by": "filename", "sort_dir": "asc", "page": 1},
        cookies=cookies,
    )

    body = response.json()
    assert body["total"] == 3
    assert [r["filename"] for r in body["runs"]] == ["ok_0.txt", "ok_1.txt", "ok_2.txt"]


async def test_api_processing_runs_detail_summary_shows_first_error_for_failed_run(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)
    await _make_step(
        db_session,
        run.id,
        step_number=1,
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["started ok"],
    )
    await _make_step(
        db_session,
        run.id,
        step_number=2,
        status=ProcessingStepStatus.FAILED,
        log_lines=["boom: could not parse file"],
    )

    response = await client.get("/api/v1/processing-runs", cookies=cookies)

    body = response.json()
    assert body["runs"][0]["detail_summary"] == "boom: could not parse file"


async def test_api_processing_runs_detail_summary_picks_earliest_failed_step_deterministically(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    # With two FAILED steps, the "first error" must be the earliest by step_number regardless of
    # DB row-return order - the steps query must ORDER BY step_number, not rely on incidental
    # physical/insertion order (a run's steps are created in step_number order today, but nothing
    # guarantees Postgres returns them that way without an explicit ORDER BY).
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)
    # Insert step 5 before step 2 so insertion order and step_number order disagree.
    await _make_step(
        db_session,
        run.id,
        step_number=5,
        status=ProcessingStepStatus.FAILED,
        log_lines=["later failure"],
    )
    await _make_step(
        db_session,
        run.id,
        step_number=2,
        status=ProcessingStepStatus.FAILED,
        log_lines=["earlier failure"],
    )

    response = await client.get("/api/v1/processing-runs", cookies=cookies)

    body = response.json()
    assert body["runs"][0]["detail_summary"] == "earlier failure"


async def test_api_processing_runs_detail_summary_falls_back_when_no_step_marked_failed(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    # A run can be FAILED without any of its steps being marked failed (e.g. the pipeline raised
    # before updating a step's status) - the column must still surface something useful.
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)
    await _make_step(
        db_session,
        run.id,
        step_number=1,
        status=ProcessingStepStatus.SUCCESS,
        log_lines=["last line before the crash"],
    )

    response = await client.get("/api/v1/processing-runs", cookies=cookies)

    body = response.json()
    assert body["runs"][0]["detail_summary"] == "last line before the crash"


async def test_api_processing_runs_detail_summary_falls_back_to_placeholder_with_no_log_lines(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)

    response = await client.get("/api/v1/processing-runs", cookies=cookies)

    body = response.json()
    assert body["runs"][0]["detail_summary"] == "No details available"


async def test_api_processing_runs_detail_summary_shows_log_line_count_for_success(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(db_session, user_id, status=ProcessingRunStatus.SUCCESS)
    await _make_step(db_session, run.id, step_number=1, log_lines=["line 1", "line 2", "line 3"])

    response = await client.get("/api/v1/processing-runs", cookies=cookies)

    body = response.json()
    assert body["runs"][0]["detail_summary"] == "3 log lines"


async def test_logs_page_status_filter_narrows_grid(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, filename="ok.txt", status=ProcessingRunStatus.SUCCESS)
    await _make_run(db_session, user_id, filename="bad.txt", status=ProcessingRunStatus.FAILED)

    response = await client.get("/logs", params={"status": "failed"}, cookies=cookies)

    body = response.text
    assert "bad.txt" in body
    assert "ok.txt" not in body


async def test_logs_page_date_range_filter_narrows_grid(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    today = datetime.now(timezone.utc)
    await _make_run(db_session, user_id, filename="old.txt", created_at=today - timedelta(days=10))
    await _make_run(db_session, user_id, filename="recent.txt", created_at=today)

    response = await client.get(
        "/logs",
        params={"date_from": (today - timedelta(days=1)).date().isoformat()},
        cookies=cookies,
    )

    body = response.text
    assert "recent.txt" in body
    assert "old.txt" not in body


async def test_logs_page_column_headers_are_sortable_links(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id)

    response = await client.get("/logs", cookies=cookies)

    body = response.text
    assert "sort_by=filename" in body
    assert "sort_by=status" in body


async def test_logs_page_htmx_request_returns_partial_not_full_page(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, filename="chat.txt")

    response = await client.get("/logs", headers={"HX-Request": "true"}, cookies=cookies)

    body = response.text
    assert "chat.txt" in body
    # The partial doesn't re-render the page shell (sidebar/nav), only the swap unit.
    assert "<html" not in body.lower()


async def test_logs_page_expanded_details_column_shows_first_error(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    run = await _make_run(db_session, user_id, status=ProcessingRunStatus.FAILED)
    await _make_step(
        db_session,
        run.id,
        status=ProcessingStepStatus.FAILED,
        log_lines=["parse error: unexpected token"],
    )

    response = await client.get("/logs", cookies=cookies)

    assert "parse error: unexpected token" in response.text


async def test_logs_page_out_of_range_page_redirect_preserves_filters(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await _make_run(db_session, user_id, status=ProcessingRunStatus.SUCCESS)

    response = await client.get(
        "/logs", params={"status": "success", "page": 5}, cookies=cookies
    )

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/logs?status=success&page=1"
