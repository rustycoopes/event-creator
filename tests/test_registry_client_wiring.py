"""Registry-decoupling (organize-me#218): tests for this service's own registry-client wiring
(app/core/registry.py) - the refresh task starting/stopping cleanly, and the cache updating on a
successful fetch / staying put on a failed one. No real sleep/network: the refresh interval is set
tiny and `fetch_registry_once` is faked, matching the TDD's "inject a fake interval or drive the
loop directly" guidance.
"""

import asyncio

import pytest

import app.core.registry as registry_module
from app.core.config import Settings
from app.core.registry import (
    SELF_APP_ENTRY,
    configure_client_registry_source,
    start_registry_refresh_task,
    stop_registry_refresh_task,
)
from organizeme_chrome.registry import AppEntry, list_apps

# Registry-decoupling Slice 3 (organize-me#220): tests/conftest.py's `_configure_registry_source`
# autouse fixture already calls configure_client_registry_source() before every test in this suite
# and reset_registry_source() after - this module's tests re-call configure_client_registry_source()
# themselves to get a fresh FetchedRegistrySource per test, and the conftest fixture's teardown
# covers restoring global state afterward, so no module-local fixture is needed here anymore.


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql://user:pass@localhost/testdb",
        jwt_secret="test-secret",
        registry_host_url="https://host.example",
        registry_refresh_interval_seconds=0.01,
        registry_fetch_timeout_seconds=1,
        **overrides,  # type: ignore[arg-type]
    )


def test_configure_client_registry_source_starts_on_the_self_only_default() -> None:
    source = configure_client_registry_source()

    assert source.get_apps() == [SELF_APP_ENTRY]
    assert list_apps() == [SELF_APP_ENTRY]


async def test_refresh_task_updates_the_cache_on_a_successful_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_app = AppEntry(service_name="organizeme", nav=[], settings_tabs=[])

    async def fake_fetch_registry_once(client: object, host_url: str, token_provider: object) -> list[AppEntry]:
        return [other_app, SELF_APP_ENTRY]

    monkeypatch.setattr(registry_module, "fetch_registry_once", fake_fetch_registry_once)

    source = configure_client_registry_source()
    task, client = start_registry_refresh_task(source, _settings())
    try:
        await asyncio.sleep(0.05)
        assert source.get_apps() == [other_app, SELF_APP_ENTRY]
    finally:
        await stop_registry_refresh_task(task, client)


async def test_refresh_task_keeps_last_known_good_on_a_failed_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_app = AppEntry(service_name="organizeme", nav=[], settings_tabs=[])
    calls = {"n": 0}

    async def fake_fetch_registry_once(client: object, host_url: str, token_provider: object) -> list[AppEntry]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [other_app, SELF_APP_ENTRY]
        raise RuntimeError("simulated Host outage")

    monkeypatch.setattr(registry_module, "fetch_registry_once", fake_fetch_registry_once)

    source = configure_client_registry_source()
    task, client = start_registry_refresh_task(source, _settings())
    try:
        await asyncio.sleep(0.15)
        # At least one failure happened after the first success, and the cache still reflects
        # that first successful fetch - a Host outage never degrades what's already cached.
        assert calls["n"] > 1
        assert source.get_apps() == [other_app, SELF_APP_ENTRY]
    finally:
        await stop_registry_refresh_task(task, client)


async def test_stop_registry_refresh_task_cancels_the_loop_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_registry_once(client: object, host_url: str, token_provider: object) -> list[AppEntry]:
        return [SELF_APP_ENTRY]

    monkeypatch.setattr(registry_module, "fetch_registry_once", fake_fetch_registry_once)

    source = configure_client_registry_source()
    task, client = start_registry_refresh_task(source, _settings())

    await stop_registry_refresh_task(task, client)

    assert task.done()
    assert client.is_closed
