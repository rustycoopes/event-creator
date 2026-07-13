"""Gemini LLM call wrapper (issue #51).

The single seam the processing pipeline's extract step (Slice 4) goes through to turn a
conversation into the raw JSON events payload. Modelled on the email sender
(app.services.notifications.email): a ``Protocol`` interface, a real google-genai implementation,
an in-memory fake for tests, and a ``get_gemini_client`` factory overridable via FastAPI
``dependency_overrides`` - so no test ever makes a live LLM call.

Per the Slice 4 spec the call is **fatal on failure**: any error (or an empty response) raises
``GeminiError`` immediately, with no retry loop - the pipeline fails the run at this step.
"""

import asyncio
import json
from typing import Protocol

from google import genai

from app.core.config import get_settings

# Fast, low-cost model suited to structured extraction. Overridable per-client for later tuning.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Canned extraction payload used only under E2E_TEST_MODE, where the Playwright suite (#53) drives
# upload -> pipeline -> SSE without a real Gemini key. A small, self-contained, schema-valid array
# (the real examples/example.lmmoutput.txt is excluded from the deployed image by .dockerignore, so
# it can't be read at runtime). Dates use formats app.core.date_parser.parse_earliest_date handles.
E2E_FAKE_EXTRACTION_PAYLOAD = json.dumps(
    [
        {
            "type": "School",
            "description": "E2E test — pick up from school.",
            "resolved_date": "Monday 15 June 2026 at 3:45 PM",
            "raw_date_text": "Monday 345",
            "agreed_by": ["Test Parent A", "Test Parent B"],
        },
        {
            "type": "Activity",
            "description": "E2E test — swim meet.",
            "resolved_date": "Saturday 20 June 2026",
            "raw_date_text": "Saturday",
            "agreed_by": ["Test Parent A"],
        },
    ]
)


class GeminiError(RuntimeError):
    """A Gemini call failed or returned no usable text.

    Fatal by design: the pipeline fails the run at the extract step rather than retrying (Slice 4
    spec, "fail immediately on error").
    """


class GeminiClient(Protocol):
    async def extract(self, *, prompt: str, conversation: str) -> str:
        """Return Gemini's raw text response (the JSON events payload) for the given system
        ``prompt`` and ``conversation``. Raises ``GeminiError`` on any failure."""
        ...


class GoogleGeminiClient:
    """Calls Gemini via the google-genai SDK.

    The API key defaults to ``GEMINI_API_KEY`` from settings but can be passed explicitly (tests
    pass a dummy). Raises ``GeminiError`` if the key is unset, the request fails, or the response
    carries no text - never retries.
    """

    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_GEMINI_MODEL) -> None:
        self._api_key = api_key
        self._model = model

    async def extract(self, *, prompt: str, conversation: str) -> str:
        api_key = self._api_key if self._api_key is not None else get_settings().gemini_api_key
        if not api_key:
            raise GeminiError("GEMINI_API_KEY is not set")
        try:
            client = genai.Client(api_key=api_key)
            # generate_content is blocking; run it off the event loop thread (same reasoning as
            # ResendEmailSender) so a slow Gemini call can't stall the server.
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self._model,
                contents=[prompt, conversation],
            )
        except Exception as exc:
            # Any SDK/network/auth error is fatal here - fail immediately, no retry.
            raise GeminiError("Gemini request failed") from exc
        text = response.text
        if not text:
            raise GeminiError("Gemini returned an empty response")
        return text


class FakeGeminiClient:
    """Returns a canned payload instead of calling Gemini. Used in tests, seeded with the
    contents of examples/example.lmmoutput.txt so the pipeline can be exercised offline. Records
    each call so tests can assert on the prompt/conversation passed."""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[dict[str, str]] = []

    async def extract(self, *, prompt: str, conversation: str) -> str:
        self.calls.append({"prompt": prompt, "conversation": conversation})
        return self._payload


def get_gemini_client() -> GeminiClient:
    """Return the Gemini client for a run. Overridable via ``dependency_overrides`` in tests
    (like ``get_email_sender``) so request handlers never hit the live API.

    Under ``E2E_TEST_MODE`` (QA only) it returns a ``FakeGeminiClient`` with a canned payload so the
    Playwright suite (#53) can run the full upload -> pipeline -> SSE flow to a *successful* terminal
    state without a real ``GEMINI_API_KEY`` — mirroring how the storage provider resolves to the
    in-memory fake in the same mode (see app.services.storage.factory)."""
    if get_settings().e2e_test_mode:
        return FakeGeminiClient(E2E_FAKE_EXTRACTION_PAYLOAD)
    return GoogleGeminiClient()
