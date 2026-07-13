"""The authenticated Prompt page (ported from organize-me's #49 to Event Creator in Slice R9).

Lets a user view, edit, and reset the extraction prompt Gemini uses. Backed by
`GET`/`PUT /api/v1/llm-prompt` and `POST /api/v1/llm-prompt/reset`. Anonymous visitors are
redirected to /login, matching the other authenticated pages (app.core.auth).
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.llm_prompt import get_or_create_user_prompt
from app.core.auth import current_user_id_optional
from app.core.templating import templates
from app.db.session import get_db

router = APIRouter(tags=["pages"])


@router.get("/prompt", response_model=None)
async def prompt_page(
    request: Request,
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    if user_id is None:
        return RedirectResponse("/login", status_code=302)
    # Self-heals a user with no seeded row (mirrors the GET endpoint), so the editor always
    # renders a usable prompt and the DB always holds a real row afterwards.
    prompt = await get_or_create_user_prompt(db, user_id)
    return templates.TemplateResponse(
        request,
        "pages/prompt.html",
        {"dark_mode": False, "prompt_text": prompt.prompt_text},
    )
