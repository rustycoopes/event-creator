"""Host-JWT trust boundary (Slice R6): Event Creator answers "which user is this," never "is
this a valid session" beyond the JWT's own signature/expiry. No fastapi-users, no password
handling, no network call to the Host — identity comes entirely from the cookie's JWT.
"""

import uuid

from fastapi import Request
from organizeme_chrome.jwt_verify import InvalidTokenError, verify_token

from app.core.config import get_settings

# Must match the Host's CookieTransport(cookie_name=...) in app/auth/backend.py (organize-me repo).
AUTH_COOKIE_NAME = "organizeme_auth"


def current_user_id_optional(request: Request) -> uuid.UUID | None:
    """Returns the Host-authenticated user's id, or None if the cookie is missing/invalid/expired.

    Callers redirect to the Host's `/login` on None — Event Creator owns no login page of its own.
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
