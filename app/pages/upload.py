"""The authenticated Upload page (ported from organize-me's Slice 4.1/#52 to Event Creator in
Slice R11, #166 - this page was missed when R8 ported the rest of the Upload/Pipeline/Processing/
Logs feature area, since R8 only needed the ``POST /api/v1/upload`` API for its own tests; the R11
routing cutover surfaced the gap before it could reach QA).

Drag-and-drop + file-picker for a ``.txt``/``.zip``/``.csv`` export, which it POSTs to
``/api/v1/upload`` and then follows to the processing progress page. Anonymous visitors are
redirected to /login like the other authenticated pages (app.core.auth).

Whether storage is connected is passed to the template so the page can steer an unconnected user
to Settings first (the upload itself is gated server-side in app.api.v1.upload regardless, per
issue #79's ephemeral-fallback behaviour - never rejected outright).
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.storage_config import is_storage_connected
from app.core.auth import current_user_id_optional
from app.core.config import Settings, get_settings
from app.core.templating import templates
from app.db.session import get_db

router = APIRouter(tags=["pages"])


@router.get("/upload", response_model=None)
async def upload_page(
    request: Request,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse | RedirectResponse:
    if user_id is None:
        return RedirectResponse("/login", status_code=302)
    storage_connected = await is_storage_connected(db, user_id, settings)
    # If no storage configured, uploads will fall back to ephemeral storage (issue #79).
    using_ephemeral = not settings.e2e_test_mode and not storage_connected
    return templates.TemplateResponse(
        request,
        "pages/upload.html",
        {
            "dark_mode": False,
            "drive_connected": storage_connected,
            "using_ephemeral": using_ephemeral,
        },
    )
