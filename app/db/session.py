from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.db.url import to_asyncpg_url


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        to_asyncpg_url(get_settings().database_url),
        connect_args={"statement_cache_size": 0},
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session (mirrors organize-me's own get_db).

    Does not auto-commit - route handlers that write must call `db.commit()` themselves (see
    app.api.v1.storage_config for the pattern this was ported from).
    """
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with session_maker() as session:
        yield session
