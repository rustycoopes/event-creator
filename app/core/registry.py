"""Registry-decoupling (organize-me#218): this service's own registry client wiring.

`app/main.py`'s `lifespan` calls `configure_client_registry_source()` on startup (constructing a
`FetchedRegistrySource` seeded with this service's own cold-start default) and
`start_registry_refresh_task()`/`stop_registry_refresh_task()` to spawn/cancel the background
refresh loop - see docs/features/registry-decoupling/TDD.md "Background refresh loop (per
consumer)".

`SELF_APP_ENTRY` is this service's own copy of the app-registry entry that would otherwise be
authored in organize-me's `app/core/registry.py` - each consumer repo maintains its own copy
(rather than importing the Host's) precisely so it can vouch for its own nav/Settings/API surface
even when the Host is unreachable, per the PRD's "Cold-start fallback" decision.
"""

import asyncio
import contextlib
import logging

import httpx
from organizeme_chrome.registry import AppEntry, AppNavItem, SettingsTab
from organizeme_chrome.registry_client import (
    FetchedRegistrySource,
    build_default_token_provider,
    fetch_registry_once,
)

from app.core.config import Settings

logger = logging.getLogger(__name__)

SELF_APP_ENTRY = AppEntry(
    service_name="event-creator",
    nav=[
        AppNavItem("/dashboard", "Dashboard"),
        AppNavItem("/upload", "Upload"),
        AppNavItem("/processing", "Processing"),
        AppNavItem("/logs", "Logs"),
        AppNavItem("/prompt", "Prompt"),
    ],
    settings_tabs=[
        SettingsTab("storage", "Storage"),
        SettingsTab("notifications", "Notifications"),
        SettingsTab("preferences", "Preferences"),
    ],
    api_prefixes=[
        "/api/v1/storage-config",
        "/api/v1/user-settings",
        "/settings/event-creator",
        "/api/v1/events",
        "/api/v1/llm-prompt",
        "/api/v1/upload",
        "/api/v1/import-pending-files",
        "/api/v1/processing-runs",
        "/processing-runs",
        "/api/html/processing-runs",
    ],
)


async def _refresh_loop(
    source: FetchedRegistrySource,
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    token_provider = build_default_token_provider(settings.registry_host_url)
    fresh_since: str | None = None
    while True:
        try:
            apps = await fetch_registry_once(client, settings.registry_host_url, token_provider)
        except Exception:
            state = f"stale-since-{fresh_since}" if fresh_since else "still-on-cold-start-default"
            logger.warning("registry refresh: fetch failed, serving %s", state, exc_info=True)
        else:
            source.update(apps)
            fresh_since = "now"
            logger.info("registry refresh: freshly-refreshed (%d apps)", len(apps))
        # Fetches immediately on startup, then waits between subsequent attempts - a fresh
        # instance (e.g. after a Cloud Run scale-to-zero cold start) shouldn't serve only its
        # self-only default for a full registry_refresh_interval_seconds before ever trying the
        # Host, per the PRD's "Cold-start fallback" intent (organize-me#218 review feedback).
        await asyncio.sleep(settings.registry_refresh_interval_seconds)


def configure_client_registry_source() -> FetchedRegistrySource:
    from organizeme_chrome.registry import configure_registry_source

    source = FetchedRegistrySource(self_only_default=SELF_APP_ENTRY)
    configure_registry_source(source)
    return source


def start_registry_refresh_task(
    source: FetchedRegistrySource, settings: Settings
) -> tuple[asyncio.Task[None], httpx.AsyncClient]:
    client = httpx.AsyncClient(timeout=settings.registry_fetch_timeout_seconds)
    task = asyncio.create_task(_refresh_loop(source, client, settings))
    return task, client


async def stop_registry_refresh_task(task: asyncio.Task[None], client: httpx.AsyncClient) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await client.aclose()
