from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.storage_config import router as storage_config_router
from app.api.v1.storage_dropbox import router as storage_dropbox_router
from app.api.v1.storage_google_drive import router as storage_google_drive_router
from app.api.v1.user_settings import router as user_settings_router
from app.pages.dashboard import router as dashboard_router
from app.pages.settings_fragments import router as settings_fragments_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Imported here, not at module level, so importing app.main (e.g. for /health tests that
    # never touch the DB) doesn't force DATABASE_URL/Settings to be resolved at import time.
    from app.db.session import get_engine

    await get_engine().dispose()


app = FastAPI(title="Event Creator", lifespan=lifespan)

app.include_router(dashboard_router)
app.include_router(settings_fragments_router)
app.include_router(storage_config_router)
app.include_router(storage_google_drive_router)
app.include_router(storage_dropbox_router)
app.include_router(user_settings_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
