"""Tests for GET/PATCH /api/v1/user-settings (Slice R7's narrower notifications endpoint).

Ported/adapted from the notification-toggle tests in organize-me's tests/test_users.py, scoped to
this repo's narrower endpoint (see app.api.v1.user_settings's docstring for why it doesn't mirror
`PATCH /api/v1/users/me` wholesale).
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_settings import UserSettings
from tests.conftest import TokenFactory, create_host_user


async def test_get_returns_defaults_for_a_brand_new_user(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, email="fresh@example.com")

    response = await client.get(
        "/api/v1/user-settings", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["notification_email"] is True
    assert body["notification_sms"] is True
    assert body["email"] == "fresh@example.com"
    assert body["phone_number"] is None


async def test_patch_notification_toggles_persist(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    response = await client.patch(
        "/api/v1/user-settings",
        json={"notification_email": False, "notification_sms": False},
        cookies=cookies,
    )

    assert response.status_code == 200
    assert response.json()["notification_email"] is False
    assert response.json()["notification_sms"] is False

    follow_up = await client.get("/api/v1/user-settings", cookies=cookies)
    assert follow_up.json()["notification_email"] is False
    assert follow_up.json()["notification_sms"] is False


async def test_patch_only_the_provided_field_changes(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    response = await client.patch(
        "/api/v1/user-settings", json={"notification_sms": False}, cookies=cookies
    )

    assert response.status_code == 200
    assert response.json()["notification_sms"] is False
    # notification_email untouched - still its default.
    assert response.json()["notification_email"] is True


async def test_patch_notification_field_sets_onboarding_flag_on_first_save(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.patch(
        "/api/v1/user-settings",
        json={"notification_sms": False},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    settings = (
        await db_session.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one()
    assert settings.onboarding_notifications_done is True


async def test_patch_empty_body_does_not_set_onboarding_flag(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.patch(
        "/api/v1/user-settings", json={}, cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    settings = (
        await db_session.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one()
    assert settings.onboarding_notifications_done is False


async def test_patch_notification_field_on_second_save_keeps_flag_and_updates_toggle(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, phone_number="+15551234567")
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    await client.patch("/api/v1/user-settings", json={"notification_sms": False}, cookies=cookies)
    response = await client.patch(
        "/api/v1/user-settings", json={"notification_sms": True}, cookies=cookies
    )

    assert response.status_code == 200
    assert response.json()["notification_sms"] is True
    settings = (
        await db_session.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one()
    assert settings.onboarding_notifications_done is True
    assert settings.notification_sms is True


async def test_patch_allows_enabling_sms_without_a_phone_number(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    # No server-side guard here by design (mirrors organize-me's #87): the endpoint doesn't reject
    # this combination, the notification sender is what silently skips a missing phone number.
    user_id = await create_host_user(db_session)

    response = await client.patch(
        "/api/v1/user-settings",
        json={"notification_sms": True},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert response.json()["notification_sms"] is True


async def test_response_reflects_the_read_only_host_user_email_and_phone(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Integration test (not a mock): the read-only HostUser mapping (app.models.host_user) is
    exercised against the real, shared `host.users` table via a genuine cross-schema SELECT."""
    user_id = await create_host_user(
        db_session, email="cross-schema@example.com", phone_number="+15559998888"
    )

    response = await client.get(
        "/api/v1/user-settings", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "cross-schema@example.com"
    assert body["phone_number"] == "+15559998888"


async def test_get_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/user-settings")
    assert response.status_code == 401


async def test_patch_requires_authentication(client: AsyncClient) -> None:
    response = await client.patch("/api/v1/user-settings", json={"notification_sms": True})
    assert response.status_code == 401
