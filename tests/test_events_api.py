"""Endpoint tests for GET/DELETE/PATCH /api/v1/events (ported from organize-me #54/#55/#113 to
Event Creator in Slice R9)."""

import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
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
    type: str = "Medical",  # noqa: A002
    description: str = "Dentist appointment",
    resolved_date: str = "Sunday 7 June 2026",
    resolved_date_earliest: date | None = date(2026, 6, 7),
    raw_date_text: str = "Sunday",
    agreed_by: list[str] | None = None,
    reviewed: bool = False,
) -> Event:
    event = Event(
        user_id=user_id,
        run_id=run_id,
        type=type,
        description=description,
        resolved_date=resolved_date,
        resolved_date_earliest=resolved_date_earliest,
        raw_date_text=raw_date_text,
        agreed_by=agreed_by or [],
        reviewed=reviewed,
    )
    db.add(event)
    await db.flush()
    return event


async def test_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/events")

    assert response.status_code == 401


async def test_only_returns_the_requesting_users_events(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    other_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    other_run_id = await _make_run(db_session, other_id)
    await _make_event(db_session, user_id, run_id, description="Mine")
    await _make_event(db_session, other_id, other_run_id, description="Theirs")
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/events", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "Mine"


async def test_default_sort_is_newest_first_with_nulls_last(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(
        db_session, user_id, run_id, description="Older", resolved_date_earliest=date(2026, 1, 1)
    )
    await _make_event(
        db_session, user_id, run_id, description="Newer", resolved_date_earliest=date(2026, 6, 1)
    )
    await _make_event(
        db_session, user_id, run_id, description="Unresolved", resolved_date_earliest=None
    )
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/events", cookies={"organizeme_auth": token})

    descriptions = [e["description"] for e in response.json()["events"]]
    assert descriptions == ["Newer", "Older", "Unresolved"]


async def test_sort_asc_reverses_order_but_still_nulls_last(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(
        db_session, user_id, run_id, description="Older", resolved_date_earliest=date(2026, 1, 1)
    )
    await _make_event(
        db_session, user_id, run_id, description="Newer", resolved_date_earliest=date(2026, 6, 1)
    )
    await _make_event(
        db_session, user_id, run_id, description="Unresolved", resolved_date_earliest=None
    )
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"sort": "asc"}
    )

    descriptions = [e["description"] for e in response.json()["events"]]
    assert descriptions == ["Older", "Newer", "Unresolved"]


async def test_calendar_and_tasks_urls_present_when_date_resolved(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/events", cookies={"organizeme_auth": token})

    event = response.json()["events"][0]
    assert event["calendar_url"] is not None
    assert event["tasks_url"] is not None


async def test_calendar_and_tasks_urls_null_when_date_unresolved(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, resolved_date_earliest=None)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/events", cookies={"organizeme_auth": token})

    event = response.json()["events"][0]
    assert event["calendar_url"] is None
    assert event["tasks_url"] is None


async def test_filters_by_type(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, type="Medical", description="A")
    await _make_event(db_session, user_id, run_id, type="School", description="B")
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"type": "School"}
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "B"


async def test_filters_by_date_range(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(
        db_session, user_id, run_id, description="Jan", resolved_date_earliest=date(2026, 1, 1)
    )
    await _make_event(
        db_session, user_id, run_id, description="Jun", resolved_date_earliest=date(2026, 6, 1)
    )
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events",
        cookies={"organizeme_auth": token},
        params={"date_from": "2026-03-01", "date_to": "2026-12-31"},
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "Jun"


async def test_empty_string_date_params_are_tolerated(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events",
        cookies={"organizeme_auth": token},
        params={"date_from": "", "date_to": ""},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_malformed_date_param_returns_422(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"date_from": "not-a-date"}
    )

    assert response.status_code == 422


async def test_free_text_search_matches_description(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, description="Dentist appointment")
    await _make_event(db_session, user_id, run_id, description="Swim practice")
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"q": "dentist"}
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "Dentist appointment"


async def test_free_text_search_matches_event_type(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, type="Medical", description="A")
    await _make_event(db_session, user_id, run_id, type="School", description="B")
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"q": "medical"}
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "A"


async def test_free_text_search_matches_agreed_by(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(
        db_session, user_id, run_id, description="A", agreed_by=["Russ Cooper"]
    )
    await _make_event(
        db_session, user_id, run_id, description="B", agreed_by=["Christine Cooper"]
    )
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"q": "Russ"}
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "A"


async def test_free_text_search_escapes_like_metacharacters(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, description="100% done")
    await _make_event(db_session, user_id, run_id, description="Swim practice")
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"q": "100%"}
    )

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "100% done"


async def test_filters_compose_with_pagination(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    for i in range(3):
        await _make_event(
            db_session,
            user_id,
            run_id,
            type="Medical",
            description=f"Event {i}",
            resolved_date_earliest=date(2026, 1, i + 1),
        )
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events",
        cookies={"organizeme_auth": token},
        params={"type": "Medical", "page": 1},
    )

    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 50


async def test_reviewed_events_hidden_by_default(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, description="Reviewed", reviewed=True)
    await _make_event(db_session, user_id, run_id, description="Not reviewed", reviewed=False)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/events", cookies={"organizeme_auth": token})

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["description"] == "Not reviewed"


async def test_show_reviewed_true_returns_everything(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    await _make_event(db_session, user_id, run_id, description="Reviewed", reviewed=True)
    await _make_event(db_session, user_id, run_id, description="Not reviewed", reviewed=False)
    token = make_token.valid(sub=str(user_id))

    response = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"show_reviewed": "true"}
    )

    assert response.json()["total"] == 2


async def test_delete_removes_the_event(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    event = await _make_event(db_session, user_id, run_id)
    token = make_token.valid(sub=str(user_id))

    response = await client.delete(
        f"/api/v1/events/{event.id}", cookies={"organizeme_auth": token}
    )

    assert response.status_code == 204
    follow_up = await client.get(
        "/api/v1/events", cookies={"organizeme_auth": token}, params={"show_reviewed": "true"}
    )
    assert follow_up.json()["total"] == 0


async def test_delete_returns_404_for_another_users_event(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    owner_id = await create_host_user(db_session)
    attacker_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, owner_id)
    event = await _make_event(db_session, owner_id, run_id)
    token = make_token.valid(sub=str(attacker_id))

    response = await client.delete(
        f"/api/v1/events/{event.id}", cookies={"organizeme_auth": token}
    )

    assert response.status_code == 404


async def test_delete_returns_404_for_nonexistent_event(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.delete(
        f"/api/v1/events/{uuid.uuid4()}", cookies={"organizeme_auth": token}
    )

    assert response.status_code == 404


async def test_delete_requires_auth(client: AsyncClient) -> None:
    response = await client.delete(f"/api/v1/events/{uuid.uuid4()}")

    assert response.status_code == 401


async def test_patch_toggles_reviewed(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    event = await _make_event(db_session, user_id, run_id, reviewed=False)
    token = make_token.valid(sub=str(user_id))

    response = await client.patch(
        f"/api/v1/events/{event.id}",
        cookies={"organizeme_auth": token},
        json={"reviewed": True},
    )

    assert response.status_code == 200
    assert response.json()["reviewed"] is True


async def test_patch_can_unmark_reviewed(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, user_id)
    event = await _make_event(db_session, user_id, run_id, reviewed=True)
    token = make_token.valid(sub=str(user_id))

    response = await client.patch(
        f"/api/v1/events/{event.id}",
        cookies={"organizeme_auth": token},
        json={"reviewed": False},
    )

    assert response.status_code == 200
    assert response.json()["reviewed"] is False


async def test_patch_returns_404_for_another_users_event(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    owner_id = await create_host_user(db_session)
    attacker_id = await create_host_user(db_session)
    run_id = await _make_run(db_session, owner_id)
    event = await _make_event(db_session, owner_id, run_id)
    token = make_token.valid(sub=str(attacker_id))

    response = await client.patch(
        f"/api/v1/events/{event.id}",
        cookies={"organizeme_auth": token},
        json={"reviewed": True},
    )

    assert response.status_code == 404


async def test_patch_requires_auth(client: AsyncClient) -> None:
    response = await client.patch(f"/api/v1/events/{uuid.uuid4()}", json={"reviewed": True})

    assert response.status_code == 401
