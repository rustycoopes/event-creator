"""Page tests for GET /dashboard's real content (ported from organize-me #54/#55/#56 to Event
Creator in Slice R9). Auth-boundary behaviour (redirect/JWT edge cases) is covered separately in
test_dashboard_auth.py."""

import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.models.storage_config import StorageConfig, StorageProviderType
from app.services.user_settings import get_or_create_user_settings
from tests.conftest import TokenFactory, create_host_user


async def _make_run(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db.add(run)
    await db.flush()
    return run.id


async def _make_event(
    db: AsyncSession,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    description: str = "Dentist appointment",
    resolved_date_earliest: date | None = date(2026, 6, 7),
    agreed_by: list[str] | None = None,
) -> Event:
    event = Event(
        user_id=user_id,
        run_id=run_id,
        type="Medical",
        description=description,
        resolved_date="Sunday 7 June 2026",
        resolved_date_earliest=resolved_date_earliest,
        raw_date_text="Sunday",
        agreed_by=agreed_by or ["Russ Cooper"],
    )
    db.add(event)
    await db.flush()
    return event


async def test_empty_state_for_a_new_user(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "No events yet" in response.text


async def test_renders_event_row_with_calendar_and_tasks_links(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "Dentist appointment" in response.text
    assert "calendar.google.com" in response.text
    assert "tasks.google.com" in response.text
    # Initials badge for "Russ Cooper".
    assert "RC" in response.text


async def test_dashboard_page_applies_the_hosts_dark_mode_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for issue #207: the page must read the Host's `dark_mode` preference
    (via `get_host_user`/`HostUser`) rather than hardcoding light mode."""
    user_id = await create_host_user(db_session, dark_mode=True)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text


async def test_dashboard_page_defaults_to_light_mode(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, dark_mode=False)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert 'data-theme="corporate"' in response.text


async def test_dashboard_renders_a_collapsed_event_creator_group_from_the_hosts_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for the sidebar-nav-groups cross-repo sync pattern (event-creator#18): the
    Dashboard page must read the real, Host-stored `nav_collapsed_groups` preference via
    `get_host_user()`/`HostUser`, not hardcode a default. The current page's own group
    (event-creator) always force-opens regardless of the stored preference (matching
    organize-me's behaviour), so this asserts on `nav_stored_collapsed_json` - the real preference
    - which stays collapsed even though the rendered `nav_collapsed_json` force-opens it."""
    user_id = await create_host_user(db_session, nav_collapsed_groups={"event-creator": True})
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "storedCollapsed: {&#34;event-creator&#34;: true}" in response.text


async def test_dashboard_defaults_to_expanded_when_no_preference_is_stored(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "storedCollapsed: {&#34;event-creator&#34;: false}" in response.text


async def test_no_date_placeholder_when_date_unresolved(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, resolved_date_earliest=None)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert "No date" in response.text


async def test_pagination_links_and_total_count(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    for i in range(55):
        await _make_event(db_session, user_id, run_id, description=f"Event {i}")
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "55 events total" in response.text
    assert "Page 1 of 2" in response.text


async def test_out_of_range_page_redirects_to_last_valid_page(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard",
        cookies={"organizeme_auth": token},
        params={"page": 5},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard?page=1"


async def test_no_redirect_when_zero_events(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard",
        cookies={"organizeme_auth": token},
        params={"page": 5},
        follow_redirects=False,
    )

    assert response.status_code == 200


async def test_htmx_request_returns_fragment_not_full_page(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Dentist appointment" in response.text


async def test_type_filter_narrows_the_table(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, description="Medical thing")
    event = Event(
        user_id=user_id,
        run_id=run_id,
        type="School",
        description="School thing",
        resolved_date="TBC",
        resolved_date_earliest=None,
        raw_date_text="TBC",
        agreed_by=[],
    )
    db_session.add(event)
    await db_session.flush()
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, params={"type": "School"}
    )

    assert "School thing" in response.text
    assert "Medical thing" not in response.text


async def test_no_match_message_when_filters_exclude_everything(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, params={"type": "Nonexistent"}
    )

    assert "No events match these filters" in response.text


async def test_onboarding_checklist_visible_until_complete(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert "Getting Started" in response.text
    assert "Connect Storage" in response.text


async def test_onboarding_checklist_hidden_once_all_steps_done(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    settings = await get_or_create_user_settings(db_session, user_id)
    settings.onboarding_storage_done = True
    settings.onboarding_notifications_done = True
    settings.onboarding_first_upload_done = True
    await db_session.commit()
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert "Getting Started" not in response.text


async def test_reviewed_checkbox_hides_row_by_default_and_shows_with_show_reviewed(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    event = await _make_event(db_session, user_id, run_id, description="Reviewed event")
    event.reviewed = True
    await db_session.commit()
    token = make_token.valid(sub=str(user_id))

    hidden = await client.get("/dashboard", cookies={"organizeme_auth": token})
    assert "Reviewed event" not in hidden.text

    shown = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, params={"show_reviewed": "true"}
    )
    assert "Reviewed event" in shown.text


async def test_sort_toggle_link_preserves_active_type_filter(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, params={"type": "Medical"}
    )

    assert "type=Medical" in response.text
    assert "sort=asc" in response.text


async def test_pagination_links_preserve_active_filters(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    for i in range(55):
        await _make_event(db_session, user_id, run_id, description=f"Medical event {i}")
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, params={"type": "Medical"}
    )

    assert "type=Medical" in response.text
    assert "page=2" in response.text


async def test_import_button_disabled_when_storage_not_connected(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert "driveConnected: false" in response.text


async def test_import_button_enabled_when_storage_connected(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(
        StorageConfig(
            user_id=user_id,
            provider=StorageProviderType.GOOGLE_DRIVE,
            folder_path="/OrganizeMe/exports",
            oauth_access_token="fake-access-token",
        )
    )
    await db_session.commit()
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    assert "driveConnected: true" in response.text


async def test_delete_button_is_gated_behind_confirm_modal(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/dashboard", cookies={"organizeme_auth": token})

    # The Delete button opens the confirm dialog rather than deleting directly - it must call
    # openConfirm(), not confirmDelete(), on click.
    assert "@click=\"openConfirm(" in response.text
    assert "confirmDelete" in response.text
