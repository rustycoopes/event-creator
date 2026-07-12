from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.db.url import to_asyncpg_url


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        to_asyncpg_url(get_settings().database_url),
        connect_args={"statement_cache_size": 0},
    )
