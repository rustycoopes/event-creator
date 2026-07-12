from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.pages.dashboard import router as dashboard_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Imported here, not at module level, so importing app.main (e.g. for /health tests that
    # never touch the DB) doesn't force DATABASE_URL/Settings to be resolved at import time.
    from app.db.session import get_engine

    await get_engine().dispose()


app = FastAPI(title="Event Creator", lifespan=lifespan)

app.include_router(dashboard_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
