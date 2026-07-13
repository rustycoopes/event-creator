"""Endpoint tests for GET/PUT /api/v1/llm-prompt and POST /api/v1/llm-prompt/reset (ported from
organize-me #48/#49 to Event Creator in Slice R9)."""

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.prompts import FACTORY_DEFAULT_PROMPT
from app.models.llm_prompt import LLMPrompt
from tests.conftest import TokenFactory, create_host_user


async def test_get_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/llm-prompt")

    assert response.status_code == 401


async def test_get_seeds_the_factory_default_for_a_user_with_no_row(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/api/v1/llm-prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert response.json()["prompt_text"] == FACTORY_DEFAULT_PROMPT
    stored = await db_session.scalar(select(LLMPrompt).where(LLMPrompt.user_id == user_id))
    assert stored is not None


async def test_put_saves_an_edited_prompt(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.put(
        "/api/v1/llm-prompt",
        cookies={"organizeme_auth": token},
        json={"prompt_text": "my custom prompt"},
    )

    assert response.status_code == 200
    assert response.json()["prompt_text"] == "my custom prompt"


async def test_put_rejects_a_blank_prompt(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.put(
        "/api/v1/llm-prompt", cookies={"organizeme_auth": token}, json={"prompt_text": "   "}
    )

    assert response.status_code == 422


async def test_put_does_not_create_a_second_row(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    await client.put(
        "/api/v1/llm-prompt", cookies={"organizeme_auth": token}, json={"prompt_text": "first"}
    )
    await client.put(
        "/api/v1/llm-prompt", cookies={"organizeme_auth": token}, json={"prompt_text": "second"}
    )

    count = await db_session.scalar(
        select(func.count()).select_from(LLMPrompt).where(LLMPrompt.user_id == user_id)
    )
    assert count == 1


async def test_reset_restores_the_factory_default(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))
    await client.put(
        "/api/v1/llm-prompt", cookies={"organizeme_auth": token}, json={"prompt_text": "edited"}
    )

    response = await client.post("/api/v1/llm-prompt/reset", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert response.json()["prompt_text"] == FACTORY_DEFAULT_PROMPT


async def test_put_requires_auth(client: AsyncClient) -> None:
    response = await client.put("/api/v1/llm-prompt", json={"prompt_text": "x"})

    assert response.status_code == 401


async def test_reset_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/llm-prompt/reset")

    assert response.status_code == 401
