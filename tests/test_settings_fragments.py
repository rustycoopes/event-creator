"""Tests for the Settings tab fragment routes (Slice R7), ported/adapted from organize-me's
tests/test_settings_page.py - split across the three now-independent fragment routes.
"""

from html.parser import HTMLParser

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_config import StorageConfig, StorageProviderType
from tests.conftest import TokenFactory, create_host_user


class _XDataCollector(HTMLParser):
    """Collects every `x-data` attribute value, honouring HTML attribute-quote termination - so a
    stray quote that truncates the attribute (the register.html bug from organize-me issue #23) is
    caught here too."""

    def __init__(self) -> None:
        super().__init__()
        self.x_data_values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "x-data" and value is not None:
                self.x_data_values.append(value)


# ---------------------------------------------------------------------------------------------
# GET /settings/event-creator/storage
# ---------------------------------------------------------------------------------------------


async def test_storage_fragment_prompts_reauth_for_anonymous_visitor(client: AsyncClient) -> None:
    response = await client.get("/settings/event-creator/storage")

    assert response.status_code == 200
    assert "Log in" in response.text


async def test_storage_fragment_renders_provider_options(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    body = response.text
    assert 'id="provider"' in body
    assert 'value="google_drive"' in body
    assert 'value="dropbox"' in body
    assert 'value="s3"' in body
    assert 'id="folder_path"' in body


async def test_storage_fragment_hides_s3_by_default(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    body = response.text
    assert "x-show=\"provider === 's3'\"" in body
    assert "x-show=\"provider === 'google_drive'\"" in body
    assert "x-show=\"provider === 'dropbox'\"" in body


async def test_storage_fragment_shows_connect_controls_for_disconnected_drive(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    body = response.text
    assert 'id="connect-drive"' in body
    assert "Connect Google Drive" in body
    assert "Save your folder path first" in body
    assert 'x-show="!is_connected"' in body


async def test_storage_fragment_shows_disconnect_control_when_drive_connected(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(
        StorageConfig(
            user_id=user_id,
            provider=StorageProviderType.GOOGLE_DRIVE,
            folder_path="/OrganizeMe",
            oauth_access_token="ciphertext-token",
        )
    )
    await db_session.flush()

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    body = response.text
    assert 'id="disconnect-drive"' in body
    assert "Disconnect Google Drive" in body
    # The chrome-v0.5.4 tojson filter HTML-entity-escapes quotes (organize-me#212's fix for the
    # sidebar-nav-groups feature's x-data attribute embedding) - applies to every `| tojson` use
    # in the shared package, not just the sidebar, so this fragment's own JSON literal is now
    # &#34;-escaped too. Still correct: the browser decodes entities before Alpine parses the
    # attribute value as JSON.
    assert "&#34;is_connected&#34;:true" in body.replace(" ", "")


async def test_storage_fragment_shows_dropbox_connect_controls(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Slice R7 resolved decision: unlike the monolith, the Dropbox panel is actually wired up."""
    user_id = await create_host_user(db_session)
    db_session.add(
        StorageConfig(
            user_id=user_id, provider=StorageProviderType.DROPBOX, folder_path="/OrganizeMe"
        )
    )
    await db_session.flush()

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    body = response.text
    assert 'id="connect-dropbox"' in body
    assert "Connect Dropbox" in body


async def test_storage_fragment_prefills_saved_folder_path(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe/reports"},
        cookies=cookies,
    )

    response = await client.get("/settings/event-creator/storage", cookies=cookies)

    assert response.status_code == 200
    assert 'value="/OrganizeMe/reports"' in response.text


async def test_storage_fragment_x_data_is_not_truncated_by_a_stray_quote(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/storage",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    collector = _XDataCollector()
    collector.feed(response.text)

    storage_x_data = [v for v in collector.x_data_values if "async save()" in v]
    assert storage_x_data, "storage fragment has no x-data component with a save() method"
    assert "/api/v1/storage-config" in storage_x_data[0]


# ---------------------------------------------------------------------------------------------
# GET /settings/event-creator/notifications
# ---------------------------------------------------------------------------------------------


async def test_notifications_fragment_prompts_reauth_for_anonymous_visitor(
    client: AsyncClient,
) -> None:
    response = await client.get("/settings/event-creator/notifications")

    assert response.status_code == 200
    assert "Log in" in response.text


async def test_notifications_fragment_renders_toggles(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, email="fresh@example.com")

    response = await client.get(
        "/settings/event-creator/notifications",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    body = response.text
    assert 'id="notification_email"' in body
    assert 'id="notification_sms"' in body

    def _input_tag(input_id: str) -> str:
        start = body.index(f'id="{input_id}"')
        return body[max(0, start - 200) : start + 200]

    # Has an email but no phone number: email toggle enabled, SMS toggle disabled with a hint.
    assert "disabled" not in _input_tag("notification_email").split("/>")[0]
    assert "disabled" in _input_tag("notification_sms").split("/>")[0]
    assert "Set your phone number in Profile to enable." in body


async def test_notifications_fragment_reflects_saved_prefs(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, phone_number="+15551234567")
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    await client.patch(
        "/api/v1/user-settings",
        json={"notification_email": False, "notification_sms": True},
        cookies=cookies,
    )

    response = await client.get("/settings/event-creator/notifications", cookies=cookies)

    assert response.status_code == 200
    body = response.text.replace(" ", "")
    # See test_storage_fragment_shows_disconnect_control_when_drive_connected's comment above for
    # why these are now HTML-entity-escaped rather than raw quotes.
    assert "&#34;notification_email&#34;:false" in body
    assert "&#34;notification_sms&#34;:true" in body
    assert "&#34;phone_number&#34;:&#34;+15551234567&#34;" in body


async def test_notifications_fragment_x_data_is_not_truncated_by_a_stray_quote(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/notifications",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    collector = _XDataCollector()
    collector.feed(response.text)

    notifications_x_data = [
        v for v in collector.x_data_values if "notification_email" in v and "async save()" in v
    ]
    assert notifications_x_data, "notifications fragment has no notifications x-data component"
    assert "/api/v1/user-settings" in notifications_x_data[0]


# ---------------------------------------------------------------------------------------------
# GET /settings/event-creator/preferences
# ---------------------------------------------------------------------------------------------


async def test_preferences_fragment_prompts_reauth_for_anonymous_visitor(
    client: AsyncClient,
) -> None:
    response = await client.get("/settings/event-creator/preferences")

    assert response.status_code == 200
    assert "Log in" in response.text


async def test_preferences_fragment_renders_a_stub(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/settings/event-creator/preferences",
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert "Preferences coming soon" in response.text
