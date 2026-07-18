from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1.events import router as events_router
from app.api.v1.import_pending_files import router as import_pending_files_router
from app.api.v1.internal_pipeline import router as internal_pipeline_router
from app.api.v1.llm_prompt import router as llm_prompt_router
from app.api.v1.processing_runs import router as processing_runs_router
from app.api.v1.storage_config import router as storage_config_router
from app.api.v1.storage_dropbox import router as storage_dropbox_router
from app.api.v1.storage_google_drive import router as storage_google_drive_router
from app.api.v1.upload import router as upload_router
from app.api.v1.user_settings import router as user_settings_router
from app.core.config import get_settings
from app.core.registry import (
    configure_client_registry_source,
    start_registry_refresh_task,
    stop_registry_refresh_task,
)
from app.pages.dashboard import router as dashboard_router
from app.pages.logs import router as logs_router
from app.pages.processing import router as processing_router
from app.pages.prompt import router as prompt_router
from app.pages.settings_fragments import router as settings_fragments_router
from app.pages.upload import router as upload_page_router

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Registry-decoupling (organize-me#218): serve this service's own nav/Settings/API surface
    # (SELF_APP_ENTRY) until the first successful background fetch from the Host replaces it -
    # see app/core/registry.py and docs/features/registry-decoupling/TDD.md.
    settings = get_settings()
    registry_source = configure_client_registry_source()
    refresh_task, refresh_client = start_registry_refresh_task(registry_source, settings)

    yield

    await stop_registry_refresh_task(refresh_task, refresh_client)
    # Imported here, not at module level, so importing app.main (e.g. for /health tests that
    # never touch the DB) doesn't force DATABASE_URL/Settings to be resolved at import time.
    from app.db.session import get_engine

    await get_engine().dispose()


app = FastAPI(title="Event Creator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(dashboard_router)
app.include_router(prompt_router)
app.include_router(upload_page_router)
app.include_router(events_router)
app.include_router(llm_prompt_router)
app.include_router(settings_fragments_router)
app.include_router(storage_config_router)
app.include_router(storage_google_drive_router)
app.include_router(storage_dropbox_router)
app.include_router(user_settings_router)
app.include_router(upload_router)
app.include_router(import_pending_files_router)
app.include_router(processing_runs_router)
app.include_router(processing_router)
app.include_router(logs_router)
app.include_router(internal_pipeline_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
