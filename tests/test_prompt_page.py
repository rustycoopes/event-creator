"""Page tests for GET /prompt (ported from organize-me #49 to Event Creator in Slice R9)."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.prompts import FACTORY_DEFAULT_PROMPT
from tests.conftest import TokenFactory, create_host_user


async def test_no_cookie_redirects_to_host_login(client: AsyncClient) -> None:
    response = await client.get("/prompt", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_renders_the_seeded_default_prompt_for_a_new_user(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "Extraction prompt" in response.text
    # The seeded default prompt should be present in the rendered textarea.
    assert FACTORY_DEFAULT_PROMPT.splitlines()[0] in response.text


async def test_prompt_page_applies_the_hosts_dark_mode_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for issue #207: the page must read the Host's `dark_mode` preference
    rather than hardcoding light mode."""
    user_id = await create_host_user(db_session, dark_mode=True)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert 'data-theme="dark"' in response.text


async def test_prompt_page_defaults_to_light_mode(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session, dark_mode=False)
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert 'data-theme="corporate"' in response.text


async def test_prompt_page_renders_the_hosts_collapsed_group_preference(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    """Regression test for event-creator#18/#19 - see test_logs_page.py's equivalent test for the
    full rationale (missing nav context here would crash the shared sidebar template)."""
    user_id = await create_host_user(db_session, nav_collapsed_groups={"event-creator": True})
    token = make_token.valid(sub=str(user_id))

    response = await client.get("/prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "storedCollapsed: {&#34;event-creator&#34;: true}" in response.text


async def test_renders_a_previously_saved_edit(
    client: AsyncClient, db_session: AsyncSession, make_token: type[TokenFactory]
) -> None:
    user_id = await create_host_user(db_session)
    token = make_token.valid(sub=str(user_id))
    await client.put(
        "/api/v1/llm-prompt",
        cookies={"organizeme_auth": token},
        json={"prompt_text": "my saved custom prompt"},
    )

    response = await client.get("/prompt", cookies={"organizeme_auth": token})

    assert response.status_code == 200
    assert "my saved custom prompt" in response.text
