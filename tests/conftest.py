import time
from collections.abc import AsyncIterator

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from organizeme_chrome.jwt_verify import ALGORITHM, TOKEN_AUDIENCE

JWT_SECRET = "test-jwt-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")


@pytest.fixture
def make_token() -> "type[TokenFactory]":
    return TokenFactory


class TokenFactory:
    @staticmethod
    def valid(sub: str = "11111111-1111-1111-1111-111111111111") -> str:
        payload = {"sub": sub, "aud": TOKEN_AUDIENCE, "exp": int(time.time()) + 3600}
        return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

    @staticmethod
    def expired(sub: str = "11111111-1111-1111-1111-111111111111") -> str:
        payload = {"sub": sub, "aud": TOKEN_AUDIENCE, "exp": int(time.time()) - 3600}
        return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

    @staticmethod
    def tampered(sub: str = "11111111-1111-1111-1111-111111111111") -> str:
        payload = {"sub": sub, "aud": TOKEN_AUDIENCE, "exp": int(time.time()) + 3600}
        return jwt.encode(payload, "wrong-secret", algorithm=ALGORITHM)

    @staticmethod
    def wrong_audience(sub: str = "11111111-1111-1111-1111-111111111111") -> str:
        payload = {"sub": sub, "aud": "some-other-audience", "exp": int(time.time()) + 3600}
        return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

    @staticmethod
    def missing_sub() -> str:
        payload = {"aud": TOKEN_AUDIENCE, "exp": int(time.time()) + 3600}
        return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

    @staticmethod
    def non_uuid_sub() -> str:
        payload = {"sub": "not-a-uuid", "aud": TOKEN_AUDIENCE, "exp": int(time.time()) + 3600}
        return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

    @staticmethod
    def alg_none() -> str:
        # Classic JWT bypass attempt: an unsigned token claiming alg=none. verify_token() pins
        # algorithms=["HS256"] explicitly, so PyJWT must reject this outright.
        payload = {
            "sub": "11111111-1111-1111-1111-111111111111",
            "aud": TOKEN_AUDIENCE,
            "exp": int(time.time()) + 3600,
        }
        return jwt.encode(payload, "", algorithm="none")


@pytest.fixture
async def client(_env: None) -> AsyncIterator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
