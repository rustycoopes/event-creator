"""Google Drive OAuth connect/disconnect for the Storage tab (ported from organize-me, #47).

Layers the live Google Drive authorization flow onto the storage config from storage_config.py.
Stores the resulting access/refresh tokens **encrypted at rest** (via `app.core.security`) on the
user's `storage_configs` row.

Flow:
- `POST /auth` (same-origin fetch from the Storage tab fragment) returns Google's consent URL as
  JSON and sets a CSRF cookie. The tab then navigates the browser there. A fetch (not a top-level
  form POST) is used deliberately: the `organizeme_auth` cookie is SameSite=Lax, which a top-level
  POST navigation would *not* send - a same-origin fetch always does.
- `GET /callback` is the top-level redirect back from Google (Lax cookies flow on a top-level GET),
  so the user is identified by their existing auth cookie; it exchanges the code and stores the
  encrypted tokens, flipping `onboarding_storage_done` on the first successful connection.
- `POST /disconnect` revokes the token at Google (best-effort) then clears it locally (leaving
  provider/folder and the onboarding flag).

The token exchange is injected via `get_google_oauth_client`, so tests use a fake and never touch
live Google credentials.

Adapted from the monolith's fastapi-users-based flow: event-creator has no `current_active_user`/
`current_active_user_optional` (no User model - see app.core.auth's docstring). `current_user_id`
raises 401 for the JSON endpoints (/auth, /disconnect); `current_user_id_optional` returns None for
the top-level GET /callback, which redirects to `/login` (a relative path - see app.pages.dashboard
for why that's correct here) instead of 401ing a full-page browser navigation whenever the auth
cookie lapsed mid-consent (the Google consent screen has no time limit).
"""

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from fastapi_users.jwt import decode_jwt, generate_jwt
from httpx_oauth.clients.google import GoogleOAuth2
from httpx_oauth.oauth2 import GetAccessTokenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import get_user_storage_config
from app.auth.oauth import get_google_oauth_client
from app.core.auth import COOKIE_SECURE, current_user_id, current_user_id_optional
from app.core.config import get_settings
from app.core.security import CredentialCipher, get_credential_cipher
from app.db.session import get_db
from app.schemas.storage_config import StorageConfigRead
from app.services.user_settings import mark_storage_onboarding_done

logger = logging.getLogger(__name__)

# Google's OAuth token revocation endpoint (RFC 7009). Revoking a refresh token invalidates the
# whole grant (and its access tokens).
GOOGLE_TOKEN_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

router = APIRouter(prefix="/api/v1/storage-config/google-drive", tags=["storage-config"])

# Full Drive access: the processing pipeline has to list, download, and move files in an
# arbitrary user-chosen folder, which the narrower drive.file scope (app-created files only) can't
# do. This is a Google "sensitive/restricted" scope.
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Anti-CSRF double-submit for the Drive flow, separate from any other flow's cookie/audience so the
# two can't be cross-used.
DRIVE_OAUTH_STATE_AUDIENCE = "organizeme:drive-oauth-state"
DRIVE_OAUTH_STATE_COOKIE_NAME = "organizeme_drive_oauth_csrf"
DRIVE_OAUTH_STATE_LIFETIME_SECONDS = 600

DRIVE_CALLBACK_PATH = "/api/v1/storage-config/google-drive/callback"
# Where callback outcomes send the browser. The Settings page (Host-owned) reads these query flags
# to show a banner; the Host is responsible for forwarding them into whichever tab is active.
SETTINGS_PATH = "/settings"


def get_cipher_factory() -> Callable[[], CredentialCipher]:
    """Return the cipher *getter*, not the cipher itself, so the callback constructs the cipher
    (and enforces ENCRYPTION_KEY) only after validation passes - the CSRF-reject and expired-session
    paths never touch it, and don't 500 when the key isn't set. Stays overridable in tests via
    dependency_overrides."""
    return get_credential_cipher


async def revoke_google_token(token: str) -> None:
    """Ask Google to revoke an OAuth token. Raises on network/HTTP error - the caller treats
    revocation as best-effort and still clears the local copy."""
    async with httpx.AsyncClient() as client:
        response = await client.post(GOOGLE_TOKEN_REVOKE_URL, data={"token": token})
        response.raise_for_status()


def get_token_revoker() -> Callable[[str], Awaitable[None]]:
    """Indirection over revoke_google_token so tests inject a fake that never calls Google."""
    return revoke_google_token


def _drive_redirect_uri() -> str:
    """The absolute callback URL Google redirects back to. Fixed per environment via
    GOOGLE_DRIVE_REDIRECT_URI (issue #200) rather than derived from the incoming request's Host
    header - the latter silently changed on every load-balancer/service-boundary shuffle and only
    matches Google's registered redirect URI by coincidence. Must exactly match a redirect URI
    registered on the Google OAuth client."""
    return get_settings().google_drive_redirect_uri


@router.post("/auth")
async def google_drive_authorize(
    response: Response,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    oauth_client: GoogleOAuth2 = Depends(get_google_oauth_client),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
) -> dict[str, str]:
    """Start the Drive OAuth flow: return Google's consent URL (the tab navigates to it) and set
    the CSRF cookie. Requires a saved storage config first, since the tokens attach to that row."""
    config = await get_user_storage_config(db, user_id)
    if config is None:
        # No row to hang the tokens on yet - the tab surfaces this as "save a folder path first".
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="save_folder_first")

    # Fail fast if ENCRYPTION_KEY isn't configured, rather than sending the user through the whole
    # Google consent flow only to hit the same error in the callback.
    try:
        cipher_factory()
    except RuntimeError as exc:
        logger.exception("Cannot start Google Drive connect: credential cipher unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage_not_configured"
        ) from exc

    settings = get_settings()
    # Same fail-fast reasoning as the cipher check above (issue #200): a misconfigured deployment
    # must not send the user through Google's consent screen only to have Google reject the
    # request. The endswith check also catches a redirect_uri pointed at the wrong path (e.g. a
    # copy-paste from the login flow's callback) - either way Google would reject it.
    if not settings.google_drive_redirect_uri.endswith(DRIVE_CALLBACK_PATH):
        logger.error(
            "Cannot start Google Drive connect: GOOGLE_DRIVE_REDIRECT_URI is not set to a URL "
            "ending in %s",
            DRIVE_CALLBACK_PATH,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage_not_configured"
        )
    csrf_token = secrets.token_urlsafe(32)
    state = generate_jwt(
        {"csrf": csrf_token, "aud": DRIVE_OAUTH_STATE_AUDIENCE},
        settings.jwt_secret,
        DRIVE_OAUTH_STATE_LIFETIME_SECONDS,
    )
    authorization_url = await oauth_client.get_authorization_url(
        _drive_redirect_uri(),
        state=state,
        scope=GOOGLE_DRIVE_SCOPES,
        # access_type=offline + prompt=consent are what make Google return a refresh_token (and
        # re-issue one on every reconnect), so the pipeline can keep accessing Drive after the
        # short-lived access token expires.
        extras_params={"access_type": "offline", "prompt": "consent"},
    )
    response.set_cookie(
        DRIVE_OAUTH_STATE_COOKIE_NAME,
        csrf_token,
        max_age=DRIVE_OAUTH_STATE_LIFETIME_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return {"authorization_url": authorization_url}


@router.get("/callback")
async def google_drive_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    drive_csrf_cookie: str | None = Cookie(default=None, alias=DRIVE_OAUTH_STATE_COOKIE_NAME),
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
    oauth_client: GoogleOAuth2 = Depends(get_google_oauth_client),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
) -> RedirectResponse:
    """Google's top-level redirect back. Identify the user via their auth cookie, validate CSRF,
    exchange the code, and store the encrypted tokens on their config."""

    def failure_redirect(reason: str = "google_drive_auth_failed") -> RedirectResponse:
        redirect = RedirectResponse(
            f"{SETTINGS_PATH}?error={reason}", status_code=status.HTTP_302_FOUND
        )
        redirect.delete_cookie(DRIVE_OAUTH_STATE_COOKIE_NAME)
        return redirect

    # The auth cookie can lapse mid-flow (the consent screen has no time limit); send them to log
    # back in rather than 401 a full-page navigation. Relative "/login" (not an absolute Host URL):
    # both services sit behind the same shared origin - see app.pages.dashboard's docstring.
    if user_id is None:
        return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    # Issue #203: the generic google_drive_auth_failed banner covers several distinct failure
    # modes below that previously logged nothing, making a real connect failure indistinguishable
    # from a stale/replayed callback URL without a production debugging round-trip. Each branch
    # now logs which check actually failed - none of these log the state/cookie/code values
    # themselves (attacker- or Google-controlled input, and the whole point of the CSRF check is
    # that they shouldn't be trusted).
    if error is not None:
        logger.warning("Google Drive callback: Google returned error=%s", error)
        return failure_redirect()
    if code is None:
        logger.warning("Google Drive callback: no ?code in the callback request")
        return failure_redirect()

    state_data: dict[str, object] = {}
    if state is not None:
        try:
            state_data = decode_jwt(
                state, get_settings().jwt_secret, audience=[DRIVE_OAUTH_STATE_AUDIENCE]
            )
        except pyjwt.PyJWTError as exc:
            logger.warning("Google Drive callback: state JWT failed to decode: %s", exc)
            return failure_redirect()
    if not state_data:
        logger.warning("Google Drive callback: no ?state in the callback request")
        return failure_redirect()

    csrf_from_state = str(state_data.get("csrf", ""))
    # Reject non-ASCII cookie values up front: secrets.compare_digest raises TypeError on them, and
    # the cookie is attacker-controllable on a direct request to this endpoint.
    if (
        not drive_csrf_cookie
        or not drive_csrf_cookie.isascii()
        or not secrets.compare_digest(drive_csrf_cookie, csrf_from_state)
    ):
        logger.warning(
            "Google Drive callback: CSRF cookie missing or did not match signed state "
            "(cookie_present=%s)",
            bool(drive_csrf_cookie),
        )
        return failure_redirect()

    config = await get_user_storage_config(db, user_id)
    if config is None:
        return failure_redirect("save_folder_first")

    try:
        token = await oauth_client.get_access_token(code, _drive_redirect_uri())
    except GetAccessTokenError as exc:
        # A replayed/expired code (e.g. a reloaded callback URL) or a transient Google-side error.
        # exc's message/response come from Google, not this app - safe to log (no secrets of ours).
        logger.warning(
            "Google Drive callback: token exchange rejected by Google: %s (status=%s)",
            exc,
            exc.response.status_code if exc.response is not None else "n/a",
        )
        return failure_redirect()

    # Construct the cipher only now that everything has validated - this is the first point that
    # actually needs ENCRYPTION_KEY. A misconfigured deployment must not surface as an unhandled
    # 500 - redirect to Settings with a clear banner instead.
    try:
        cipher = cipher_factory()
    except RuntimeError:
        logger.exception("Cannot construct credential cipher for Google Drive callback")
        return failure_redirect("storage_not_configured")
    config.oauth_access_token = cipher.encrypt(token["access_token"])
    refresh_token = token.get("refresh_token")
    if refresh_token:
        # Google only returns a refresh_token with access_type=offline + prompt=consent (set on the
        # auth request); keep any previously stored one if a reconnect somehow omits it.
        config.oauth_refresh_token = cipher.encrypt(refresh_token)
    # httpx_oauth's OAuth2Token computes `expires_at` (epoch seconds) from Google's expires_in; store
    # it so the pipeline can refresh the short-lived access token proactively.
    expires_at = token.get("expires_at")
    config.oauth_token_expires_at = (
        datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None
    )
    # First successful connection completes the storage onboarding step; it stays true thereafter
    # (a later disconnect doesn't reset it).
    await mark_storage_onboarding_done(db, user_id)

    redirect = RedirectResponse(f"{SETTINGS_PATH}?connected=1", status_code=status.HTTP_302_FOUND)
    redirect.delete_cookie(DRIVE_OAUTH_STATE_COOKIE_NAME)
    return redirect


@router.post("/disconnect", response_model=StorageConfigRead)
async def google_drive_disconnect(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
    cipher_factory: Callable[[], CredentialCipher] = Depends(get_cipher_factory),
    revoke_token: Callable[[str], Awaitable[None]] = Depends(get_token_revoker),
) -> StorageConfigRead:
    """Revoke the Drive token at Google (best-effort) then clear it locally, returning the tab to
    the disconnected state. Provider, folder path, and the onboarding flag are left intact."""
    config = await get_user_storage_config(db, user_id)
    if config is None:
        return StorageConfigRead()

    # Revoking the refresh token invalidates the whole grant; fall back to the access token if
    # that's all we have. Best-effort: a decrypt or network failure must not block the local clear
    # (otherwise a user could never disconnect a partially-broken connection).
    encrypted = config.oauth_refresh_token or config.oauth_access_token
    if encrypted is not None:
        try:
            await revoke_token(cipher_factory().decrypt(encrypted))
        except Exception:
            logger.exception("Failed to revoke Google Drive token for user %s", user_id)

    config.oauth_access_token = None
    config.oauth_refresh_token = None
    config.oauth_token_expires_at = None
    await db.commit()
    return StorageConfigRead(
        provider=config.provider, folder_path=config.folder_path, is_connected=False
    )
