"""Dropbox OAuth connect/disconnect for the Storage tab (ported from organize-me, #93).

Mirrors app.api.v1.storage_google_drive's flow (see that module's docstring for the shared
reasoning behind the fetch-then-navigate /auth call, the Lax-cookie CSRF double-submit, and the
top-level GET /callback) - the two flows are kept in separate modules/routers because their OAuth
clients have provider-specific quirks: Dropbox's revoke endpoint authenticates via the token being
revoked (Bearer header), not a token passed in the request body like Google's.

Flow:
- `POST /auth` returns Dropbox's consent URL as JSON and sets a CSRF cookie.
- `GET /callback` exchanges the code and stores the encrypted tokens, flipping
  `onboarding_storage_done` on the user's Event-Creator settings row on the first successful
  connection.
- `POST /disconnect` revokes the token at Dropbox (best-effort) then clears it locally.

Unlike the organize-me monolith (which built this backend but never wired its Settings-page UI),
event-creator's Storage tab fragment (app/templates/partials/settings_storage.html) DOES wire up
Dropbox Connect/Disconnect controls - a Slice R7 resolved decision - mirroring the Google Drive
panel's Alpine JS pattern exactly.
"""

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi_users.jwt import decode_jwt, generate_jwt
from httpx_oauth.oauth2 import BaseOAuth2, GetAccessTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import get_user_storage_config
from app.auth.oauth import get_dropbox_oauth_client
from app.core.auth import COOKIE_SECURE, current_user_id, current_user_id_optional
from app.core.config import get_settings
from app.core.security import CredentialCipher, get_credential_cipher
from app.db.session import get_db
from app.schemas.storage_config import StorageConfigRead
from app.services.user_settings import mark_storage_onboarding_done

logger = logging.getLogger(__name__)

# Dropbox's token revocation endpoint (developers.dropbox.com/documentation/http/documentation
# #auth-token-revoke). It has no request body - it revokes whichever token authenticates the call.
DROPBOX_TOKEN_REVOKE_URL = "https://api.dropboxapi.com/2/auth/token/revoke"

router = APIRouter(prefix="/api/v1/storage-config/dropbox", tags=["storage-config"])

# A "scoped app" Dropbox client (the current app type Dropbox issues) only grants the permissions
# explicitly requested here, unlike Google's fixed-scope OAuth client - omitting this would silently
# leave the connection unable to list/download/upload/move files.
DROPBOX_SCOPES = ["files.content.write", "files.content.read"]

# Anti-CSRF double-submit for the Dropbox flow, separate from Google Drive's cookie so the two
# can't be cross-used.
DROPBOX_OAUTH_STATE_AUDIENCE = "organizeme:dropbox-oauth-state"
DROPBOX_OAUTH_STATE_COOKIE_NAME = "organizeme_dropbox_oauth_csrf"
DROPBOX_OAUTH_STATE_LIFETIME_SECONDS = 600

DROPBOX_CALLBACK_PATH = "/api/v1/storage-config/dropbox/callback"
SETTINGS_PATH = "/settings"


def get_cipher_factory() -> Callable[[], CredentialCipher]:
    """Return the cipher *getter*, not the cipher itself - see storage_google_drive.py's identical
    helper for why (lets the CSRF-reject/expired-session paths skip ENCRYPTION_KEY entirely)."""
    return get_credential_cipher


async def revoke_dropbox_token(token: str) -> None:
    """Ask Dropbox to revoke the token authenticating this call. Raises on network/HTTP error -
    the caller treats revocation as best-effort and still clears the local copy."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            DROPBOX_TOKEN_REVOKE_URL, headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()


def get_token_revoker() -> Callable[[str], Awaitable[None]]:
    """Indirection over revoke_dropbox_token so tests inject a fake that never calls Dropbox."""
    return revoke_dropbox_token


def _dropbox_redirect_uri(request: Request) -> str:
    """The absolute callback URL, built from the incoming request's host (same approach as the
    Google Drive callback). Must exactly match a redirect URI registered on the Dropbox app."""
    return str(request.base_url).rstrip("/") + DROPBOX_CALLBACK_PATH


@router.post("/auth")
async def dropbox_authorize(
    request: Request,
    response: Response,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    oauth_client: BaseOAuth2[dict[str, str]] = Depends(get_dropbox_oauth_client),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
) -> dict[str, str]:
    """Start the Dropbox OAuth flow: return Dropbox's consent URL (the tab navigates to it) and set
    the CSRF cookie. Requires a saved storage config first, since the tokens attach to that row."""
    config = await get_user_storage_config(db, user_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="save_folder_first")

    # Fail fast if ENCRYPTION_KEY isn't configured (mirrors the Drive flow), rather than sending
    # the user through the whole Dropbox consent flow only to hit it in the callback.
    try:
        cipher_factory()
    except RuntimeError as exc:
        logger.exception("Cannot start Dropbox connect: credential cipher unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage_not_configured"
        ) from exc

    settings = get_settings()
    csrf_token = secrets.token_urlsafe(32)
    state = generate_jwt(
        {"csrf": csrf_token, "aud": DROPBOX_OAUTH_STATE_AUDIENCE},
        settings.jwt_secret,
        DROPBOX_OAUTH_STATE_LIFETIME_SECONDS,
    )
    authorization_url = await oauth_client.get_authorization_url(
        _dropbox_redirect_uri(request),
        state=state,
        scope=DROPBOX_SCOPES,
        # token_access_type=offline is what makes Dropbox return a refresh_token (and re-issue one
        # on every reconnect), so the pipeline can keep accessing Dropbox after the short-lived
        # access token expires - Dropbox's equivalent of Google's access_type=offline.
        extras_params={"token_access_type": "offline"},
    )
    response.set_cookie(
        DROPBOX_OAUTH_STATE_COOKIE_NAME,
        csrf_token,
        max_age=DROPBOX_OAUTH_STATE_LIFETIME_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return {"authorization_url": authorization_url}


@router.get("/callback")
async def dropbox_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    dropbox_csrf_cookie: str | None = Cookie(default=None, alias=DROPBOX_OAUTH_STATE_COOKIE_NAME),
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
    oauth_client: BaseOAuth2[dict[str, str]] = Depends(get_dropbox_oauth_client),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
) -> RedirectResponse:
    """Dropbox's top-level redirect back. Identify the user via their auth cookie, validate CSRF,
    exchange the code, and store the encrypted tokens on their config."""

    def failure_redirect(reason: str = "dropbox_auth_failed") -> RedirectResponse:
        redirect = RedirectResponse(
            f"{SETTINGS_PATH}?error={reason}", status_code=status.HTTP_302_FOUND
        )
        redirect.delete_cookie(DROPBOX_OAUTH_STATE_COOKIE_NAME)
        return redirect

    # The auth cookie can lapse mid-flow (the consent screen has no time limit); send them to log
    # back in rather than 401 a full-page navigation.
    if user_id is None:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    state_data: dict[str, object] = {}
    if state is not None:
        try:
            state_data = decode_jwt(
                state, get_settings().jwt_secret, audience=[DROPBOX_OAUTH_STATE_AUDIENCE]
            )
        except pyjwt.PyJWTError:
            state_data = {}

    if error is not None or code is None or not state_data:
        return failure_redirect()

    csrf_from_state = str(state_data.get("csrf", ""))
    # Reject non-ASCII cookie values up front: secrets.compare_digest raises TypeError on them, and
    # the cookie is attacker-controllable on a direct request to this endpoint.
    if (
        not dropbox_csrf_cookie
        or not dropbox_csrf_cookie.isascii()
        or not secrets.compare_digest(dropbox_csrf_cookie, csrf_from_state)
    ):
        return failure_redirect()

    config = await get_user_storage_config(db, user_id)
    if config is None:
        return failure_redirect("save_folder_first")

    try:
        token = await oauth_client.get_access_token(code, _dropbox_redirect_uri(request))
    except GetAccessTokenError:
        # A replayed/expired code (e.g. a reloaded callback URL) or a transient Dropbox-side error.
        return failure_redirect()

    # Construct the cipher only now that everything has validated - the first point that actually
    # needs ENCRYPTION_KEY, so a misconfigured deployment redirects with a clear banner instead of
    # an unhandled 500.
    try:
        cipher = cipher_factory()
    except RuntimeError:
        logger.exception("Cannot construct credential cipher for Dropbox callback")
        return failure_redirect("storage_not_configured")
    config.oauth_access_token = cipher.encrypt(token["access_token"])
    refresh_token = token.get("refresh_token")
    if refresh_token:
        # Dropbox only returns a refresh_token with token_access_type=offline (set on the auth
        # request); keep any previously stored one if a reconnect somehow omits it.
        config.oauth_refresh_token = cipher.encrypt(refresh_token)
    expires_at = token.get("expires_at")
    config.oauth_token_expires_at = (
        datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None
    )
    # First successful connection completes the storage onboarding step; it stays true thereafter
    # (a later disconnect doesn't reset it).
    await mark_storage_onboarding_done(db, user_id)

    redirect = RedirectResponse(f"{SETTINGS_PATH}?connected=1", status_code=status.HTTP_302_FOUND)
    redirect.delete_cookie(DROPBOX_OAUTH_STATE_COOKIE_NAME)
    return redirect


@router.post("/disconnect", response_model=StorageConfigRead)
async def dropbox_disconnect(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
    revoke_token: Callable[[str], Awaitable[None]] = Depends(get_token_revoker),
) -> StorageConfigRead:
    """Revoke the Dropbox token (best-effort) then clear it locally, returning the tab to the
    disconnected state. Provider, folder path, and the onboarding flag are left intact."""
    config = await get_user_storage_config(db, user_id)
    if config is None:
        return StorageConfigRead()

    # Unlike Google's revoke (a token passed as a body param, where either the access or refresh
    # token works), Dropbox's /2/auth/token/revoke authenticates via the token *being revoked* as
    # the Bearer credential - a refresh token can't be used as a bearer access token there, so this
    # must always be the access token. Revoking it invalidates the whole grant (access + refresh)
    # at Dropbox. Best-effort: a decrypt or network failure must not block the local clear.
    encrypted = config.oauth_access_token
    if encrypted is not None:
        try:
            await revoke_token(cipher_factory().decrypt(encrypted))
        except Exception:
            logger.exception("Failed to revoke Dropbox token for user %s", user_id)

    config.oauth_access_token = None
    config.oauth_refresh_token = None
    config.oauth_token_expires_at = None
    await db.commit()
    return StorageConfigRead(
        provider=config.provider, folder_path=config.folder_path, is_connected=False
    )
