"""Real-Gemini end-to-end test (ported from organize-me's #52 to Event Creator in Slice R8) -
skipped unless GEMINI_API_KEY is set.

Uploads the example WhatsApp export, runs the full pipeline through a *live* Gemini call, and
asserts real events are extracted and saved. Skipped in CI (no key, no per-run API cost); run
locally on demand with GEMINI_API_KEY in the environment / .env.local. CI correctness relies on the
stubbed integration tests in tests/test_pipeline_runner.py.

Gemini output is not bit-for-bit deterministic, so this asserts the run succeeds and extracts a
substantial, sane set of events (rather than an exact match to every line of the golden file).
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.prompts import FACTORY_DEFAULT_PROMPT
from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from app.services.llm.gemini import GoogleGeminiClient
from app.services.notifications.pipeline import FakeNotificationSender
from app.services.pipeline.runner import run_pipeline
from app.services.storage.fake import FakeStorageProvider
from tests.conftest import create_host_user

# A plain os.environ read, not get_settings() - Settings() requires DATABASE_URL/JWT_SECRET to
# already be resolved too, and this skip decision is evaluated at import/collection time, before
# conftest's autouse _env fixture (which only runs per-test) ever sets them. Reading the env var
# directly (matching the skip reason's own wording) means collecting this file never requires a
# full Settings() outside of CI, where the real env vars are already present at the job level.
pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set - live Gemini test skipped"
)

_WHATSAPP = (
    Path(__file__).resolve().parents[1] / "examples" / "example.whatsapp.txt"
).read_bytes()


async def test_real_gemini_pipeline_extracts_events(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = ProcessingRun(
        user_id=user_id, filename="example.whatsapp.txt", status=ProcessingRunStatus.PENDING
    )
    db_session.add(run)
    await db_session.flush()

    storage = FakeStorageProvider()
    remote_file = await storage.upload_file("example.whatsapp.txt", _WHATSAPP)

    await run_pipeline(
        db_session,
        run=run,
        user_id=user_id,
        remote_file=remote_file,
        storage=storage,
        gemini=GoogleGeminiClient(),
        notifier=FakeNotificationSender(),
        prompt_text=FACTORY_DEFAULT_PROMPT,
        # Wide window so the whole month-long fixture reaches Gemini (the 7-day default is exercised
        # by tests/test_message_filter.py).
        window_days=400,
    )

    assert run.status == ProcessingRunStatus.SUCCESS
    events = list((await db_session.scalars(select(Event).where(Event.user_id == user_id))).all())
    # The golden file has 22 events; allow for LLM variation but require a substantial extraction.
    assert len(events) >= 10
    # A few unambiguous agreements from the conversation should reliably surface.
    descriptions = " ".join(e.description.lower() for e in events)
    assert "swim" in descriptions
