"""Tests for GET/PUT /api/v1/storage-config (ported, #46).

Run against the QA DB inside the rolled-back db_session fixture (see conftest), so nothing
persists. Auth is a Host-issued JWT cookie (this service has no login of its own - see
app.core.auth), unlike organize-me's register+cookie-login flow.
"""

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_config import StorageConfig, StorageProviderType
from tests.conftest import TokenFactory, create_host_user


async def test_get_returns_unset_state_when_no_config(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.get(
        "/api/v1/storage-config", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] is None
    assert body["folder_path"] is None
    assert body["is_connected"] is False


async def test_put_creates_config_and_get_returns_it(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    put = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe/exports"},
        cookies=cookies,
    )

    assert put.status_code == 200
    assert put.json() == {
        "provider": "google_drive",
        "folder_path": "/OrganizeMe/exports",
        "is_connected": False,
    }

    follow_up = await client.get("/api/v1/storage-config", cookies=cookies)
    assert follow_up.status_code == 200
    assert follow_up.json() == {
        "provider": "google_drive",
        "folder_path": "/OrganizeMe/exports",
        "is_connected": False,
    }


async def test_put_is_an_upsert_and_updates_existing_config(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/first"},
        cookies=cookies,
    )
    second = await client.put(
        "/api/v1/storage-config",
        json={"provider": "s3", "folder_path": "/second"},
        cookies=cookies,
    )

    assert second.status_code == 200
    assert second.json() == {"provider": "s3", "folder_path": "/second", "is_connected": False}

    row_count = await db_session.scalar(
        select(func.count()).select_from(StorageConfig).where(StorageConfig.user_id == user_id)
    )
    assert row_count == 1


async def test_put_rejects_empty_folder_path(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": ""},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 422


async def test_put_rejects_whitespace_only_folder_path(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "   "},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 422


async def test_put_trims_surrounding_whitespace_from_folder_path(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "  /OrganizeMe/exports  "},
        cookies=cookies,
    )

    assert response.status_code == 200
    assert response.json()["folder_path"] == "/OrganizeMe/exports"

    follow_up = await client.get("/api/v1/storage-config", cookies=cookies)
    assert follow_up.json()["folder_path"] == "/OrganizeMe/exports"


async def test_put_rejects_unknown_provider(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "onedrive", "folder_path": "/x"},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 422


async def test_read_never_leaks_stored_credentials(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    secret = "super-secret-encrypted-token-value"
    db_session.add(
        StorageConfig(
            user_id=user_id,
            provider=StorageProviderType.GOOGLE_DRIVE,
            folder_path="/OrganizeMe",
            oauth_access_token=secret,
            oauth_refresh_token=secret,
        )
    )
    await db_session.flush()

    response = await client.get(
        "/api/v1/storage-config", cookies={"organizeme_auth": make_token.valid(sub=str(user_id))}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"provider", "folder_path", "is_connected"}
    assert secret not in response.text
    assert body["is_connected"] is True


async def test_put_switching_provider_clears_stale_credentials(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test (#93 review): switching provider must clear the old provider's
    credentials."""
    user_id = await create_host_user(db_session)
    secret = "super-secret-encrypted-token-value"
    db_session.add(
        StorageConfig(
            user_id=user_id,
            provider=StorageProviderType.GOOGLE_DRIVE,
            folder_path="/OrganizeMe",
            oauth_access_token=secret,
            oauth_refresh_token=secret,
        )
    )
    await db_session.flush()

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "dropbox", "folder_path": "/OrganizeMe"},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert response.json()["is_connected"] is False

    config = (
        await db_session.scalars(select(StorageConfig).where(StorageConfig.user_id == user_id))
    ).one()
    assert config.provider == StorageProviderType.DROPBOX
    assert config.oauth_access_token is None
    assert config.oauth_refresh_token is None


async def test_put_same_provider_keeps_existing_credentials(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """The credential-clearing on provider switch must not fire when the provider is unchanged."""
    user_id = await create_host_user(db_session)
    secret = "super-secret-encrypted-token-value"
    db_session.add(
        StorageConfig(
            user_id=user_id,
            provider=StorageProviderType.GOOGLE_DRIVE,
            folder_path="/OrganizeMe",
            oauth_access_token=secret,
            oauth_refresh_token=secret,
        )
    )
    await db_session.flush()

    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe/new-path"},
        cookies={"organizeme_auth": make_token.valid(sub=str(user_id))},
    )

    assert response.status_code == 200
    assert response.json()["is_connected"] is True

    config = (
        await db_session.scalars(select(StorageConfig).where(StorageConfig.user_id == user_id))
    ).one()
    assert config.folder_path == "/OrganizeMe/new-path"
    assert config.oauth_access_token == secret


async def test_get_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/storage-config")
    assert response.status_code == 401


async def test_put_requires_authentication(client: AsyncClient) -> None:
    response = await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/x"},
    )
    assert response.status_code == 401
