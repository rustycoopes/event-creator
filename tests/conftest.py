import os

# Must run before any app module (in particular app.core.auth) is imported by a test module, so
# the Drive/Dropbox CSRF cookies are set with Secure=False. httpx isn't a browser and doesn't get
# the localhost-is-a-secure-context exception, so a Secure cookie set by /callback's /auth step
# would never be resent by the test client on the next request (mirrors organize-me's own
# tests/conftest.py, which this was ported from but missed this line - see #47).
os.environ.setdefault("COOKIE_SECURE", "false")

import time
import uuid
from collections.abc import AsyncIterator

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from organizeme_chrome.jwt_verify import ALGORITHM, TOKEN_AUDIENCE
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

JWT_SECRET = "test-jwt-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    # Real value only if the environment already provides one (CI sets this to the Supabase QA
    # URL - see .github/workflows/ci.yml); tests that never touch the DB (test_health.py,
    # test_dashboard_auth.py) work fine with this placeholder, but anything using the db_session
    # fixture below needs a real, reachable Postgres.
    monkeypatch.setenv(
        "DATABASE_URL",
        __import__("os").environ.get("DATABASE_URL", "postgresql://user:pass@localhost/testdb"),
    )


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


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A DB session whose writes are rolled back at teardown (mirrors organize-me's own fixture).

    Requires a real, reachable DATABASE_URL (the Supabase QA database in CI - see
    .github/workflows/ci.yml) - there is no local Docker Postgres in this project's dev
    convention. Builds its own dedicated engine per test (rather than reusing
    app.db.session's process-wide singleton) because asyncpg connections are bound to the event
    loop that created them, and pytest-asyncio gives each test function its own loop by default.
    """
    from app.core.config import get_settings
    from app.db.url import to_asyncpg_url

    engine = create_async_engine(
        to_asyncpg_url(get_settings().database_url),
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session_factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with session_factory() as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()


async def create_host_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    phone_number: str | None = None,
    dark_mode: bool = False,
) -> uuid.UUID:
    """Insert a row directly into the Host's `host.users` table for a test to attach a Host JWT
    (via `make_token`) and Event-Creator-owned rows (StorageConfig, UserSettings) to.

    Event-creator has no `User`/fastapi-users model of its own (see app.core.auth's docstring), so
    unlike organize-me's tests - which register a real account through `/api/v1/auth/register` -
    there's no ORM class to `db.add()` here. A raw INSERT is the only way to seed a `host.users`
    row from this repo, and is exactly the shape of write app.models.host_user.HostUser itself must
    never perform (see that module's docstring) - this helper deliberately lives in test code, not
    application code.
    """
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO host.users "
            "(id, email, hashed_password, is_active, is_superuser, is_verified, "
            "phone_number, dark_mode) "
            "VALUES "
            "(:id, :email, :hashed_password, true, false, true, :phone_number, :dark_mode)"
        ),
        {
            "id": user_id,
            "email": email or f"event-creator-r7-{user_id.hex}@example.com",
            "hashed_password": "not-a-real-hash",
            "phone_number": phone_number,
            "dark_mode": dark_mode,
        },
    )
    await session.flush()
    return user_id


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """httpx client for endpoint tests, wired to the same rolled-back db_session.

    Overrides the app's get_db dependency rather than letting request handlers build sessions off
    app.db.session's process-wide singleton engine: that engine binds asyncpg connections to the
    event loop that first created it, which breaks across pytest-asyncio's per-test event loops
    (see db_session's own docstring for the same underlying issue).
    """
    from app.db.session import get_db
    from app.main import app

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
