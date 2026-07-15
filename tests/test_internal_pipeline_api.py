"""Tests for POST /internal/pipeline/run (Slice R11 redesign) - the Cloud Tasks push target that
replaced the Celery worker. Covers the OIDC push-token gate (`_verify_push_token`) and the
idempotency guard, with `run_already_terminal`/`run_pipeline_dispatch` themselves faked out - full
pipeline behaviour through this entrypoint is covered in test_dispatch.py and
test_cloud_tasks_scheduler.py.
"""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient, Response

import app.api.v1.internal_pipeline as internal_pipeline_module
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _configure_oidc_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PIPELINE_ENDPOINT_URL", "https://event-creator-test.a.run.app")
    monkeypatch.setenv(
        "PIPELINE_INVOKER_SERVICE_ACCOUNT", "invoker@test-project.iam.gserviceaccount.com"
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    calls: dict[str, object] = {"dispatched": [], "already_terminal": False}

    async def fake_run_already_terminal(run_id: uuid.UUID) -> bool:
        return bool(calls["already_terminal"])

    async def fake_run_pipeline_dispatch(**kwargs: object) -> None:
        calls["dispatched"].append(kwargs)  # type: ignore[attr-defined]

    monkeypatch.setattr(internal_pipeline_module, "run_already_terminal", fake_run_already_terminal)
    monkeypatch.setattr(internal_pipeline_module, "run_pipeline_dispatch", fake_run_pipeline_dispatch)
    return calls


def _valid_payload(remaining_batch: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "run_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "remote_file_id": "file-1",
        "remote_file_name": "chat.txt",
        "prompt_text": "extract events",
        "storage_mode": "fake",
        "inline_content_b64": None,
        "remaining_batch": remaining_batch or [],
    }


def _batch_item(run_id: str | None = None) -> dict[str, object]:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "remote_file_id": "file-2",
        "remote_file_name": "chat2.txt",
        "storage_mode": "fake",
        "inline_content_b64": None,
    }


async def _post(
    client: AsyncClient, *, token: str | None, payload: dict[str, object] | None = None
) -> Response:
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    return await client.post(
        "/internal/pipeline/run", json=payload or _valid_payload(), headers=headers
    )


@pytest.fixture
def _fake_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    enqueued: list[dict[str, object]] = []

    async def fake_enqueue_pipeline_run(payload: dict[str, object]) -> None:
        enqueued.append(payload)

    monkeypatch.setattr(internal_pipeline_module, "enqueue_pipeline_run", fake_enqueue_pipeline_run)
    return enqueued


def _accept_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        internal_pipeline_module.id_token,
        "verify_oauth2_token",
        lambda *a, **kw: {"email": "invoker@test-project.iam.gserviceaccount.com"},
    )


async def test_rejects_missing_token(client: AsyncClient, _fake_dispatch: dict[str, object]) -> None:
    response = await _post(client, token=None)
    assert response.status_code == 401
    assert response.json()["detail"] == "missing_token"


async def test_rejects_token_that_fails_verification(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _fake_dispatch: dict[str, object]
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise ValueError("bad token")

    monkeypatch.setattr(internal_pipeline_module.id_token, "verify_oauth2_token", _raise)

    response = await _post(client, token="not-a-real-token")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"


async def test_rejects_token_for_the_wrong_service_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _fake_dispatch: dict[str, object]
) -> None:
    monkeypatch.setattr(
        internal_pipeline_module.id_token,
        "verify_oauth2_token",
        lambda *a, **kw: {"email": "someone-else@evil.example.com"},
    )

    response = await _post(client, token="valid-but-wrong-identity")
    assert response.status_code == 403
    assert response.json()["detail"] == "wrong_identity"


async def test_returns_503_when_oidc_settings_are_unconfigured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _fake_dispatch: dict[str, object]
) -> None:
    monkeypatch.setenv("PIPELINE_ENDPOINT_URL", "")
    monkeypatch.setenv("PIPELINE_INVOKER_SERVICE_ACCOUNT", "")
    get_settings.cache_clear()

    response = await _post(client, token="whatever")
    assert response.status_code == 503
    assert response.json()["detail"] == "not_configured"

    get_settings.cache_clear()


async def test_accepts_a_valid_token_and_dispatches_the_run(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _fake_dispatch: dict[str, object]
) -> None:
    monkeypatch.setattr(
        internal_pipeline_module.id_token,
        "verify_oauth2_token",
        lambda *a, **kw: {"email": "invoker@test-project.iam.gserviceaccount.com"},
    )

    response = await _post(client, token="valid-token")
    assert response.status_code == 200
    assert response.json() == {"status": "done"}
    assert len(_fake_dispatch["dispatched"]) == 1  # type: ignore[arg-type]


async def test_skips_dispatch_for_an_already_terminal_run(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, _fake_dispatch: dict[str, object]
) -> None:
    """Idempotency guard: a Cloud Tasks retry delivered after an earlier attempt already finished
    must not reprocess the run."""
    monkeypatch.setattr(
        internal_pipeline_module.id_token,
        "verify_oauth2_token",
        lambda *a, **kw: {"email": "invoker@test-project.iam.gserviceaccount.com"},
    )
    _fake_dispatch["already_terminal"] = True

    response = await _post(client, token="valid-token")
    assert response.status_code == 200
    assert response.json() == {"status": "already_terminal"}
    assert _fake_dispatch["dispatched"] == []


async def test_advances_batch_chain_after_processing_the_current_item(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_dispatch: dict[str, object],
    _fake_enqueue: list[dict[str, object]],
) -> None:
    """After the current item finishes, the next item in `remaining_batch` is enqueued with the
    tail of the list as *its* own `remaining_batch` - the chaining mechanism a batch import relies
    on for strict ordering (see this module's docstring on why the queue's own
    max-concurrent-dispatches=1 setting isn't sufficient by itself)."""
    _accept_valid_token(monkeypatch)
    second = _batch_item()
    third = _batch_item()
    payload = _valid_payload(remaining_batch=[second, third])

    response = await _post(client, token="valid-token", payload=payload)

    assert response.status_code == 200
    assert len(_fake_enqueue) == 1
    next_payload = _fake_enqueue[0]
    assert next_payload["run_id"] == second["run_id"]
    assert next_payload["remote_file_id"] == second["remote_file_id"]
    assert next_payload["user_id"] == payload["user_id"]
    assert next_payload["prompt_text"] == payload["prompt_text"]
    assert next_payload["remaining_batch"] == [third]


async def test_advances_batch_chain_even_when_current_run_already_terminal(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_dispatch: dict[str, object],
    _fake_enqueue: list[dict[str, object]],
) -> None:
    """A retried request for an already-finished current item must still advance the chain - the
    original (non-retried) delivery may never have gotten the chance to (e.g. it crashed after
    marking the run terminal but before enqueuing the next item)."""
    _accept_valid_token(monkeypatch)
    _fake_dispatch["already_terminal"] = True
    second = _batch_item()
    payload = _valid_payload(remaining_batch=[second])

    response = await _post(client, token="valid-token", payload=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "already_terminal"}
    assert len(_fake_enqueue) == 1
    assert _fake_enqueue[0]["run_id"] == second["run_id"]


async def test_does_not_advance_chain_when_remaining_batch_is_empty(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_dispatch: dict[str, object],
    _fake_enqueue: list[dict[str, object]],
) -> None:
    _accept_valid_token(monkeypatch)

    response = await _post(client, token="valid-token")

    assert response.status_code == 200
    assert _fake_enqueue == []
