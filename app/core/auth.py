"""Host-JWT trust boundary (Slice R6): Event Creator answers "which user is this," never "is
this a valid session" beyond the JWT's own signature/expiry. No fastapi-users, no password
handling, no network call to the Host — identity comes entirely from the cookie's JWT.
"""

import os
import uuid

from fastapi import HTTPException, Request, status
from organizeme_chrome.jwt_verify import InvalidTokenError, verify_token

from app.core.config import get_settings

# Must match the Host's CookieTransport(cookie_name=...) in app/auth/backend.py (organize-me repo).
AUTH_COOKIE_NAME = "organizeme_auth"

# Whether the storage OAuth CSRF-state cookies (app.api.v1.storage_google_drive/storage_dropbox)
# are set with Secure - mirrors organize-me's app/auth/backend.py::COOKIE_SECURE identically (same
# reasoning: Cloud Run terminates TLS at the edge in prod, browsers treat localhost as a secure
# context, and only non-browser HTTP test clients hitting a plain http:// origin need this false).
# A plain os.environ read (not the pydantic Settings class) so importing this module never
# requires DATABASE_URL/JWT_SECRET to be resolved.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"


def current_user_id_optional(request: Request) -> uuid.UUID | None:
    """Returns the Host-authenticated user's id, or None if the cookie is missing/invalid/expired.

    Page routes redirect to the Host's `/login` on None — Event Creator owns no login page of its
    own. API routes that require auth should depend on `current_user_id` instead, which raises a
    401 (the organize-me/fastapi-users equivalent of `current_active_user`) rather than redirect.
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token is None:
        return None
    try:
        subject = verify_token(token, get_settings().jwt_secret)
        return uuid.UUID(subject)
    except (InvalidTokenError, ValueError):
        # ValueError: a signature/expiry/audience-valid token whose `sub` claim isn't a UUID
        # string. Not reachable without the shared signing secret today, but treat it the same
        # as any other untrusted token rather than letting it 500.
        return None


def current_user_id(request: Request) -> uuid.UUID:
    """Like `current_user_id_optional`, but raises 401 instead of returning None.

    The API-route equivalent of organize-me's `current_active_user` (fastapi-users) - used by the
    JSON endpoints (storage config, storage OAuth connect/disconnect, notification settings) that
    must reject an unauthenticated request outright rather than redirect a browser navigation.
    """
    user_id = current_user_id_optional(request)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user_id
