"""Tests for the Upload page (ported from organize-me #52 to Event Creator in Slice R11, #166 -
missed when R8 ported the rest of Upload/Pipeline/Processing/Logs)."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_config import StorageConfig, StorageProviderType
from tests.conftest import TokenFactory, create_host_user


async def test_upload_page_redirects_anonymous_visitor_to_login(client: AsyncClient) -> None:
    response = await client.get("/upload", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_upload_page_renders_dropzone_and_file_picker(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/upload", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    body = response.text
    assert 'id="upload-dropzone"' in body
    assert 'id="file-input"' in body
    assert 'accept=".txt,.zip,.csv"' in body
    assert "/api/v1/upload" in body


async def test_upload_page_warns_when_storage_not_connected_with_ephemeral_fallback(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/upload", cookies={"organizeme_auth": token})

    body = response.text
    assert "driveConnected:false" in body.replace(" ", "")
    assert "usingEphemeral:true" in body.replace(" ", "")
    assert "No storage provider is connected" in body
    assert "/settings" in body


async def test_upload_page_marks_storage_connected_when_token_present(
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
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/upload", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "driveConnected:true" in response.text.replace(" ", "")


async def test_upload_page_import_pending_files_button_present(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/upload", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert 'id="import-pending-files-btn"' in response.text
    assert "/api/v1/import-pending-files" in response.text
