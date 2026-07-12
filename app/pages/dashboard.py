"""Slice R6 tracer bullet: the Host↔Event Creator boundary's first real page.

Trusts the Host-issued JWT (signature + expiry only) with no login/session logic of its own — see
app.core.auth. A relative redirect to /login is correct (not an absolute Host URL): both services
sit behind the same shared Load Balancer origin, and /login is a Host-owned path in the URL map,
so the browser's next request for it is routed back to the Host automatically.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.auth import current_user_id_optional
from app.core.templating import templates

router = APIRouter(tags=["pages"])


@router.get("/dashboard", response_model=None)
async def dashboard(
    request: Request, user_id: uuid.UUID | None = Depends(current_user_id_optional)
) -> HTMLResponse | RedirectResponse:
    if user_id is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        # dark_mode: no per-user preference lookup here yet — Event Creator has no User model of
        # its own (no fastapi-users), and syncing the Host's stored preference across services is
        # out of scope for the R6 tracer bullet.
        request, "pages/dashboard.html", {"user_id": str(user_id), "dark_mode": False}
    )
