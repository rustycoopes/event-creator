"""Read/update/reset the current user's single extraction prompt (ported from organize-me's
Slice 3.0/3.1, #48/#49, to Event Creator in Slice R9).

`GET`/`PUT /api/v1/llm-prompt` and `POST /api/v1/llm-prompt/reset` back the Prompt page.
`FACTORY_DEFAULT_PROMPT` (app.core.prompts) is the shared source of truth for both the lazily
seeded default and the Reset button here.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_id
from app.core.prompts import FACTORY_DEFAULT_PROMPT
from app.db.session import get_db
from app.models.llm_prompt import LLMPrompt
from app.schemas.llm_prompt import LLMPromptRead, LLMPromptWrite

router = APIRouter(prefix="/api/v1", tags=["llm-prompt"])


async def get_user_prompt(db: AsyncSession, user_id: uuid.UUID) -> LLMPrompt | None:
    """The user's single prompt row, or ``None`` if they have no row yet.

    Event Creator has no eager per-user seed at registration (unlike organize-me's own
    on_after_register), so this is commonly ``None`` until first read/write. Shared by this
    router and the Prompt page so the "one prompt per user" lookup lives in one place.
    """
    result = await db.execute(select(LLMPrompt).where(LLMPrompt.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create_user_prompt(db: AsyncSession, user_id: uuid.UUID) -> LLMPrompt:
    """The user's prompt row, creating one seeded with the factory default if none exists.

    Rather than returning the factory default read-only every time, this self-heals a user with
    no row on first read so the DB always holds a real row afterwards. Shared by the GET endpoint
    and the Prompt page so a user is healed whichever they hit first.
    """
    prompt = await get_user_prompt(db, user_id)
    if prompt is not None:
        return prompt
    prompt = LLMPrompt(user_id=user_id, prompt_text=FACTORY_DEFAULT_PROMPT)
    db.add(prompt)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request for the same user created the row first (user_id is UNIQUE); roll
        # back our losing INSERT and use theirs. Mirrors get_or_create_user_settings's own race.
        await db.rollback()
        existing = await get_user_prompt(db, user_id)
        if existing is None:
            raise
        return existing
    return prompt


async def set_user_prompt(db: AsyncSession, user_id: uuid.UUID, prompt_text: str) -> LLMPrompt:
    """Create-or-update the user's single prompt row to ``prompt_text`` and commit.

    ``user_id`` is UNIQUE, so this is never an insert of a second row. Both the edit (PUT) and the
    reset paths funnel through here, so "persist a prompt for this user" is defined once. Reset is
    just this called with ``FACTORY_DEFAULT_PROMPT``.
    """
    prompt = await get_user_prompt(db, user_id)
    if prompt is None:
        prompt = LLMPrompt(user_id=user_id, prompt_text=prompt_text)
        db.add(prompt)
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent PUT/reset for the same user with no row yet (e.g. a double-click, or
            # two tabs) created it first; roll back our losing INSERT and update theirs instead,
            # same race get_or_create_user_prompt guards against.
            await db.rollback()
            existing = await get_user_prompt(db, user_id)
            if existing is None:
                raise
            existing.prompt_text = prompt_text
            await db.commit()
            return existing
        return prompt
    prompt.prompt_text = prompt_text
    await db.commit()
    return prompt


@router.get("/llm-prompt", response_model=LLMPromptRead)
async def read_prompt(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> LLMPromptRead:
    prompt = await get_or_create_user_prompt(db, user_id)
    return LLMPromptRead(prompt_text=prompt.prompt_text)


@router.put("/llm-prompt", response_model=LLMPromptRead)
async def update_prompt(
    payload: LLMPromptWrite,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> LLMPromptRead:
    prompt = await set_user_prompt(db, user_id, payload.prompt_text)
    return LLMPromptRead(prompt_text=prompt.prompt_text)


@router.post("/llm-prompt/reset", response_model=LLMPromptRead)
async def reset_prompt(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> LLMPromptRead:
    prompt = await set_user_prompt(db, user_id, FACTORY_DEFAULT_PROMPT)
    return LLMPromptRead(prompt_text=prompt.prompt_text)
