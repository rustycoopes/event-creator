"""Tests for the Gemini wrapper (issue #51).

Cover the injectable fake (returns the example payload, records calls) and the real
GoogleGeminiClient's success and immediate-failure behaviour. The google-genai SDK's blocking
client is monkeypatched, so no test makes a live LLM call or needs an API key.
"""

from pathlib import Path

import pytest

from app.services.llm.gemini import (
    FakeGeminiClient,
    GeminiError,
    GoogleGeminiClient,
    get_gemini_client,
)

EXAMPLE_OUTPUT = (
    Path(__file__).resolve().parents[1] / "examples" / "example.lmmoutput.txt"
).read_text(encoding="utf-8")


class _FakeResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeModels:
    """Stands in for genai client's ``.models``; records how many times generate_content ran so a
    test can assert there was no retry."""

    def __init__(self, *, response: _FakeResponse | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls = 0

    def generate_content(self, *, model: str, contents: object) -> _FakeResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, models: _FakeModels) -> None:
    class _FakeClient:
        def __init__(self, *, api_key: str) -> None:
            self.models = models

    monkeypatch.setattr("app.services.llm.gemini.genai.Client", _FakeClient)


async def test_fake_client_returns_the_seeded_payload_and_records_the_call() -> None:
    client = FakeGeminiClient(EXAMPLE_OUTPUT)

    result = await client.extract(prompt="system prompt", conversation="a chat")

    assert result == EXAMPLE_OUTPUT
    assert client.calls == [{"prompt": "system prompt", "conversation": "a chat"}]


async def test_google_client_returns_the_response_text_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _FakeModels(response=_FakeResponse('[{"type": "Medical"}]'))
    _install_fake_genai(monkeypatch, models)

    result = await GoogleGeminiClient(api_key="test-key").extract(
        prompt="p", conversation="c"
    )

    assert result == '[{"type": "Medical"}]'
    assert models.calls == 1


async def test_google_client_raises_immediately_without_retry_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _FakeModels(error=RuntimeError("boom"))
    _install_fake_genai(monkeypatch, models)

    with pytest.raises(GeminiError):
        await GoogleGeminiClient(api_key="test-key").extract(prompt="p", conversation="c")

    # Called exactly once - the wrapper must not retry.
    assert models.calls == 1


async def test_google_client_raises_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _FakeModels(response=_FakeResponse(None))
    _install_fake_genai(monkeypatch, models)

    with pytest.raises(GeminiError):
        await GoogleGeminiClient(api_key="test-key").extract(prompt="p", conversation="c")


async def test_google_client_raises_when_api_key_is_missing() -> None:
    # An explicit empty key means "unset" - the wrapper fails before any network call.
    with pytest.raises(GeminiError):
        await GoogleGeminiClient(api_key="").extract(prompt="p", conversation="c")


def test_get_gemini_client_returns_the_google_implementation() -> None:
    assert isinstance(get_gemini_client(), GoogleGeminiClient)


async def test_get_gemini_client_returns_the_fake_under_e2e_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under E2E_TEST_MODE the factory returns a fake seeded with a canned, schema-valid payload so
    # the Playwright suite (#53) can run the pipeline to success without a real GEMINI_API_KEY.
    import app.services.llm.gemini as gemini_module
    from app.core.config import get_settings

    e2e_settings = get_settings().model_copy(update={"e2e_test_mode": True})
    monkeypatch.setattr(gemini_module, "get_settings", lambda: e2e_settings)

    client = get_gemini_client()

    assert isinstance(client, FakeGeminiClient)
    payload = await client.extract(prompt="p", conversation="c")
    # The canned payload parses as the events JSON the pipeline expects.
    import json

    events = json.loads(payload)
    assert isinstance(events, list) and events
    assert {"type", "description", "resolved_date"} <= events[0].keys()
