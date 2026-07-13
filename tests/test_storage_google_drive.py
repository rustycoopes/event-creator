"""Tests for the Google Drive OAuth connect/disconnect flow (ported, #47).

The token exchange is faked via a dependency override (mirrors organize-me's own test module), so
no live Google credentials are touched. The credential cipher is also overridden with a throwaway
key, so the tests don't depend on a configured ENCRYPTION_KEY and can assert the tokens are stored
as ciphertext, not plaintext. Auth is a Host-issued JWT cookie (this service has no login of its
own), unlike organize-me's register+cookie-login flow.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_google_drive import get_cipher_factory, get_token_revoker
from app.auth.oauth import get_google_oauth_client
from app.core.config import get_settings
from app.core.security import CredentialCipher
from app.models.storage_config import StorageConfig
from app.services.user_settings import get_user_settings
from tests.conftest import TokenFactory, create_host_user

# A fixed key for the whole module so tests can decrypt what the callback stored.
_CIPHER_KEY = Fernet.generate_key()
_CIPHER = CredentialCipher(_CIPHER_KEY)


class FakeTokenRevoker:
    """Records tokens passed to it instead of calling Google's revoke endpoint."""

    def __init__(self, *, raise_on_revoke: bool = False) -> None:
        self.revoked: list[str] = []
        self.raise_on_revoke = raise_on_revoke

    async def __call__(self, token: str) -> None:
        self.revoked.append(token)
        if self.raise_on_revoke:
            raise RuntimeError("simulated revoke failure")


class FakeDriveOAuth2:
    """Stands in for GoogleOAuth2 in the Drive flow - never calls Google. Records the params
    get_authorization_url was built with, and returns canned tokens from get_access_token."""

    def __init__(
        self,
        *,
        refresh_token: str | None = "fake-refresh-token",
        raise_on_access_token: bool = False,
    ) -> None:
        self.refresh_token = refresh_token
        self.raise_on_access_token = raise_on_access_token
        self.authorization_args: dict[str, object] = {}

    async def get_authorization_url(
        self,
        redirect_uri: str,
        state: str | None = None,
        scope: list[str] | None = None,
        extras_params: dict[str, str] | None = None,
        **_: object,
    ) -> str:
        self.authorization_args = {
            "redirect_uri": redirect_uri,
            "scope": scope,
            "extras_params": extras_params,
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def get_access_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> dict[str, object]:
        if self.raise_on_access_token:
            from httpx_oauth.oauth2 import GetAccessTokenError

            raise GetAccessTokenError("simulated Google token-exchange failure")
        token: dict[str, object] = {
            "access_token": f"fake-access-token-for-{code}",
            "token_type": "Bearer",
            "expires_at": 1_900_000_000,
        }
        if self.refresh_token is not None:
            token["refresh_token"] = self.refresh_token
        return token


async def _user_with_config(
    db_session: AsyncSession, make_token: type[TokenFactory]
) -> tuple[uuid.UUID, dict[str, str]]:
    """A host user with a saved storage config (Connect needs one), plus their auth cookie."""
    user_id = await create_host_user(db_session)
    cookies = {"organizeme_auth": make_token.valid(sub=str(user_id))}
    return user_id, cookies


@pytest.fixture
def fake_drive_client() -> FakeDriveOAuth2:
    return FakeDriveOAuth2()


def _override(
    client_app_fake: FakeDriveOAuth2, revoker: "FakeTokenRevoker | None" = None
) -> None:
    from app.main import app

    app.dependency_overrides[get_google_oauth_client] = lambda: client_app_fake
    # get_cipher_factory returns the cipher *getter*; the override yields one bound to the test key.
    app.dependency_overrides[get_cipher_factory] = lambda: (lambda: _CIPHER)
    if revoker is not None:
        app.dependency_overrides[get_token_revoker] = lambda: revoker


def _clear_overrides() -> None:
    from app.main import app

    app.dependency_overrides.pop(get_google_oauth_client, None)
    app.dependency_overrides.pop(get_cipher_factory, None)
    app.dependency_overrides.pop(get_token_revoker, None)


async def _drive_connect(
    client: AsyncClient, cookies: dict[str, str], fake: FakeDriveOAuth2, *, code: str = "fake-code"
) -> None:
    """Run the full POST /auth -> GET /callback happy path against the fake client."""
    _override(fake)
    try:
        auth = await client.post("/api/v1/storage-config/google-drive/auth", cookies=cookies)
        state = parse_qs(urlparse(auth.json()["authorization_url"]).query)["state"][0]
        await client.get(
            "/api/v1/storage-config/google-drive/callback",
            params={"code": code, "state": state},
            cookies=cookies,
            follow_redirects=False,
        )
    finally:
        _clear_overrides()


# ---------------------------------------------------------------------------------------------
# POST /auth
# ---------------------------------------------------------------------------------------------


async def test_auth_returns_google_consent_url_with_correct_params(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    from app.main import app

    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )

    app.dependency_overrides[get_cipher_factory] = lambda: (lambda: _CIPHER)
    try:
        response = await client.post(
            "/api/v1/storage-config/google-drive/auth", cookies=cookies
        )
    finally:
        app.dependency_overrides.pop(get_cipher_factory, None)

    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/")
    query = parse_qs(urlparse(url).query)
    settings = get_settings()
    assert query["client_id"][0] == settings.google_oauth_client_id
    assert query["redirect_uri"][0].endswith("/api/v1/storage-config/google-drive/callback")
    assert query["scope"][0] == "https://www.googleapis.com/auth/drive"
    assert query["access_type"][0] == "offline"
    assert query["prompt"][0] == "consent"
    assert any(
        c.lower().startswith("organizeme_drive_oauth_csrf=")
        for c in response.headers.get_list("set-cookie")
    )


async def test_auth_requires_a_saved_config_first(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    _, cookies = await _user_with_config(db_session, make_token)

    response = await client.post("/api/v1/storage-config/google-drive/auth", cookies=cookies)

    assert response.status_code == 409
    assert response.json()["detail"] == "save_folder_first"


async def test_auth_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/storage-config/google-drive/auth")
    assert response.status_code == 401


async def test_auth_fails_fast_when_encryption_key_missing(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test: a missing ENCRYPTION_KEY should be caught in /auth, before sending the
    user through the whole Google consent flow."""
    from app.main import app

    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )

    def _raise_missing_key() -> CredentialCipher:
        raise RuntimeError("ENCRYPTION_KEY is not set")

    app.dependency_overrides[get_cipher_factory] = lambda: _raise_missing_key
    try:
        response = await client.post(
            "/api/v1/storage-config/google-drive/auth", cookies=cookies
        )
    finally:
        app.dependency_overrides.pop(get_cipher_factory, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "storage_not_configured"


# ---------------------------------------------------------------------------------------------
# GET /callback
# ---------------------------------------------------------------------------------------------


async def test_callback_stores_encrypted_tokens_and_sets_onboarding_flag(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    user_id, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )

    _override(fake_drive_client)
    try:
        auth = await client.post(
            "/api/v1/storage-config/google-drive/auth", cookies=cookies
        )
        state = parse_qs(urlparse(auth.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/api/v1/storage-config/google-drive/callback",
            params={"code": "fake-code", "state": state},
            cookies=cookies,
            follow_redirects=False,
        )
    finally:
        _clear_overrides()

    assert callback.status_code == 302
    assert callback.headers["location"] == "/settings?connected=1"

    config = (
        await db_session.scalars(select(StorageConfig).where(StorageConfig.user_id == user_id))
    ).one()
    assert config.oauth_access_token is not None
    assert config.oauth_access_token != "fake-access-token-for-fake-code"
    assert config.oauth_refresh_token is not None
    assert config.oauth_refresh_token != "fake-refresh-token"
    assert _CIPHER.decrypt(config.oauth_access_token) == "fake-access-token-for-fake-code"
    assert _CIPHER.decrypt(config.oauth_refresh_token) == "fake-refresh-token"
    assert config.oauth_token_expires_at is not None
    assert int(config.oauth_token_expires_at.timestamp()) == 1_900_000_000

    settings = await get_user_settings(db_session, user_id)
    assert settings is not None
    assert settings.onboarding_storage_done is True


async def test_callback_marks_config_connected_for_subsequent_reads(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )

    await _drive_connect(client, cookies, fake_drive_client)

    read = await client.get("/api/v1/storage-config", cookies=cookies)
    assert read.status_code == 200
    assert read.json()["is_connected"] is True


async def test_onboarding_flag_stays_true_after_disconnect(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    user_id, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    await _drive_connect(client, cookies, fake_drive_client)

    _override(fake_drive_client, FakeTokenRevoker())
    try:
        disconnect = await client.post(
            "/api/v1/storage-config/google-drive/disconnect", cookies=cookies
        )
    finally:
        _clear_overrides()
    assert disconnect.status_code == 200
    assert disconnect.json()["is_connected"] is False

    settings = await get_user_settings(db_session, user_id)
    assert settings is not None
    assert settings.onboarding_storage_done is True


async def test_callback_rejects_tampered_state(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    _override(fake_drive_client)
    try:
        await client.post("/api/v1/storage-config/google-drive/auth", cookies=cookies)
        response = await client.get(
            "/api/v1/storage-config/google-drive/callback",
            params={"code": "fake-code", "state": "not-a-valid-state"},
            cookies=cookies,
            follow_redirects=False,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 302
    assert response.headers["location"] == "/settings?error=google_drive_auth_failed"


async def test_callback_handles_token_exchange_failure(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    failing = FakeDriveOAuth2(raise_on_access_token=True)
    _override(failing)
    try:
        auth = await client.post(
            "/api/v1/storage-config/google-drive/auth", cookies=cookies
        )
        state = parse_qs(urlparse(auth.json()["authorization_url"]).query)["state"][0]
        response = await client.get(
            "/api/v1/storage-config/google-drive/callback",
            params={"code": "fake-code", "state": state},
            cookies=cookies,
            follow_redirects=False,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 302
    assert response.headers["location"] == "/settings?error=google_drive_auth_failed"


async def test_callback_handles_missing_encryption_key(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    """Regression test: an unconfigured ENCRYPTION_KEY must redirect with a clear banner, not
    bubble up as an unhandled 500."""
    from app.main import app

    _, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    _override(fake_drive_client)

    def _raise_missing_key() -> CredentialCipher:
        raise RuntimeError("ENCRYPTION_KEY is not set")

    try:
        # /auth itself needs a working cipher (the fail-fast check), so start the flow before
        # swapping in the raising override - only the callback should see it missing.
        auth = await client.post(
            "/api/v1/storage-config/google-drive/auth", cookies=cookies
        )
        state = parse_qs(urlparse(auth.json()["authorization_url"]).query)["state"][0]
        app.dependency_overrides[get_cipher_factory] = lambda: _raise_missing_key
        response = await client.get(
            "/api/v1/storage-config/google-drive/callback",
            params={"code": "fake-code", "state": state},
            cookies=cookies,
            follow_redirects=False,
        )
    finally:
        _clear_overrides()

    assert response.status_code == 302
    assert response.headers["location"] == "/settings?error=storage_not_configured"


async def test_callback_without_auth_cookie_redirects_to_login(client: AsyncClient) -> None:
    # No logged-in user: a full-page navigation shouldn't 401, it should send them to log in.
    response = await client.get(
        "/api/v1/storage-config/google-drive/callback",
        params={"code": "fake-code", "state": "whatever"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------------------------
# POST /disconnect
# ---------------------------------------------------------------------------------------------


async def test_disconnect_clears_stored_tokens_and_revokes_at_google(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    user_id, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    await _drive_connect(client, cookies, fake_drive_client)

    revoker = FakeTokenRevoker()
    _override(fake_drive_client, revoker)
    try:
        response = await client.post(
            "/api/v1/storage-config/google-drive/disconnect", cookies=cookies
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["is_connected"] is False
    assert revoker.revoked == ["fake-refresh-token"]

    config = (
        await db_session.scalars(select(StorageConfig).where(StorageConfig.user_id == user_id))
    ).one()
    assert config.oauth_access_token is None
    assert config.oauth_refresh_token is None
    assert config.oauth_token_expires_at is None
    assert config.folder_path == "/OrganizeMe"


async def test_disconnect_still_clears_locally_when_revoke_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    make_token: type[TokenFactory],
    fake_drive_client: FakeDriveOAuth2,
) -> None:
    user_id, cookies = await _user_with_config(db_session, make_token)
    await client.put(
        "/api/v1/storage-config",
        json={"provider": "google_drive", "folder_path": "/OrganizeMe"},
        cookies=cookies,
    )
    await _drive_connect(client, cookies, fake_drive_client)

    _override(fake_drive_client, FakeTokenRevoker(raise_on_revoke=True))
    try:
        response = await client.post(
            "/api/v1/storage-config/google-drive/disconnect", cookies=cookies
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["is_connected"] is False

    config = (
        await db_session.scalars(select(StorageConfig).where(StorageConfig.user_id == user_id))
    ).one()
    assert config.oauth_access_token is None
    assert config.oauth_refresh_token is None


async def test_disconnect_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/storage-config/google-drive/disconnect")
    assert response.status_code == 401
