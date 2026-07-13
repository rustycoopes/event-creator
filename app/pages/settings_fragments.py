"""Settings tab fragment routes for the Host's Settings-shell page (Slice R7).

`GET /settings/event-creator/{storage,notifications,preferences}` each render an HTML *fragment*
(no `{% extends "chrome_authenticated_base.html" %}` - just the panel content), consumed by the
Host's Settings shell page via same-origin HTMX fetch. Ported from organize-me's
`app/pages/settings.py` (the full Settings page there), split into three independently-fetchable
fragments and adapted to this service's own Storage/Notifications data sources.

Unauthenticated requests (missing/invalid/expired Host JWT) render the small
`partials/settings_reauth_required.html` prompt with a 200 status, rather than the 302-to-/login
every other page in this service uses - see that template's docstring for why a full-page redirect
is the wrong shape for something meant to be swapped into a tab panel. Deliberately 200, not 401:
these fragments are consumed via `hx-get`/`hx-trigger="load"` from the Host's Settings-shell page
(app/templates/settings.html, organize-me repo), and htmx's default `responseHandling` config only
swaps 2xx responses into the target - a 401 would fire `htmx:responseError` instead and leave the
tab panel's loading spinner stuck forever, never showing the prompt at all. A 200 carrying the
"log back in" markup is what actually renders.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import get_user_storage_config
from app.core.auth import current_user_id_optional
from app.core.templating import templates
from app.db.session import get_db
from app.models.storage_config import StorageProviderType
from app.services.host_user import get_host_user
from app.services.user_settings import get_or_create_user_settings

router = APIRouter(prefix="/settings/event-creator", tags=["pages"])


def _reauth_required(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/settings_reauth_required.html", {})


@router.get("/storage", response_model=None)
async def storage_fragment(
    request: Request,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    if user_id is None:
        return _reauth_required(request)
    config = await get_user_storage_config(db, user_id)
    storage_data = {
        "provider": (
            config.provider.value if config is not None else StorageProviderType.GOOGLE_DRIVE.value
        ),
        "folder_path": config.folder_path if config is not None else "",
        "is_connected": config.oauth_access_token is not None if config is not None else False,
        "has_config": config is not None,
    }
    return templates.TemplateResponse(
        request, "partials/settings_storage.html", {"storage_data": storage_data}
    )


@router.get("/notifications", response_model=None)
async def notifications_fragment(
    request: Request,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    if user_id is None:
        return _reauth_required(request)
    user_settings = await get_or_create_user_settings(db, user_id)
    host_user = await get_host_user(db, user_id)
    notifications_data = {
        "notification_email": user_settings.notification_email,
        "notification_sms": user_settings.notification_sms,
        "email": (host_user.email if host_user is not None else None) or "",
        "phone_number": (host_user.phone_number if host_user is not None else None) or "",
    }
    return templates.TemplateResponse(
        request, "partials/settings_notifications.html", {"notifications_data": notifications_data}
    )


@router.get("/preferences", response_model=None)
async def preferences_fragment(
    request: Request,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
) -> HTMLResponse:
    if user_id is None:
        return _reauth_required(request)
    return templates.TemplateResponse(request, "partials/settings_preferences.html", {})
