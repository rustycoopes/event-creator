"""Slice R6 acceptance criteria: /dashboard trusts the Host JWT (signature + expiry only) with no
login/session logic of its own, and rejects/redirects everything else.
"""

from httpx import AsyncClient

from tests.conftest import TokenFactory


async def test_valid_host_jwt_renders_the_dashboard_for_that_user(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    token = make_token.valid(sub="22222222-2222-2222-2222-222222222222")

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 200
    assert "Dashboard" in response.text


async def test_no_cookie_redirects_to_host_login(client: AsyncClient) -> None:
    response = await client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_expired_token_redirects_to_host_login(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    token = make_token.expired()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_tampered_signature_redirects_to_host_login(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    token = make_token.tampered()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_garbage_cookie_value_redirects_to_host_login(client: AsyncClient) -> None:
    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": "not-a-jwt"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_wrong_audience_redirects_to_host_login(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    token = make_token.wrong_audience()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_missing_sub_claim_redirects_to_host_login(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    token = make_token.missing_sub()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_non_uuid_sub_claim_redirects_to_host_login_instead_of_500(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    # Regression test for the fix in app/core/auth.py: a signature/expiry/audience-valid token
    # whose sub isn't a UUID string must redirect like any other untrusted token, not 500.
    token = make_token.non_uuid_sub()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_alg_none_token_redirects_to_host_login(
    client: AsyncClient, make_token: type[TokenFactory]
) -> None:
    # Regression test locking in verify_token()'s explicit algorithms=["HS256"] pin against the
    # classic alg=none JWT bypass.
    token = make_token.alg_none()

    response = await client.get(
        "/dashboard", cookies={"organizeme_auth": token}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
