"""Cascade test for the llm_prompts model (Slice R10 boundary suite, #165).

Exercises the real table on the QA database, inside the rolled-back db_session fixture - so
nothing persists.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_prompt import LLMPrompt
from tests.conftest import create_host_user


async def test_deleting_host_user_cascades_to_llm_prompt_row(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(LLMPrompt(user_id=user_id, prompt_text="a custom prompt"))
    await db_session.flush()

    await db_session.execute(text("DELETE FROM host.users WHERE id = :uid"), {"uid": user_id})
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT 1 FROM event_creator.llm_prompts WHERE user_id = :uid").bindparams(
            uid=user_id
        )
    )
    assert result.first() is None
