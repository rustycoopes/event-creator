"""Cascade tests for the events/processing_runs models (Slice R10 boundary suite, #165).

Exercises the real tables on the QA database, inside the rolled-back db_session fixture - so
nothing persists. Both tables have their own direct `ON DELETE CASCADE` FK to host.users (not one
cascading through the other), so each gets its own test, matching the one-test-per-table
convention in test_user_settings_model.py/test_storage_config_model.py/test_llm_prompt_model.py.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from tests.conftest import create_host_user


async def _make_run(db: AsyncSession, user_id: uuid.UUID) -> ProcessingRun:
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db.add(run)
    await db.flush()
    return run


async def test_deleting_host_user_cascades_to_processing_runs_row(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    await _make_run(db_session, user_id)

    await db_session.execute(text("DELETE FROM host.users WHERE id = :uid"), {"uid": user_id})
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT 1 FROM event_creator.processing_runs WHERE user_id = :uid").bindparams(
            uid=user_id
        )
    )
    assert result.first() is None


async def test_deleting_host_user_cascades_to_events_row(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = await _make_run(db_session, user_id)
    db_session.add(
        Event(
            user_id=user_id,
            run_id=run.id,
            type="Medical",
            description="Dentist appointment",
            resolved_date="Sunday",
            resolved_date_earliest=None,
            raw_date_text="Sunday",
            agreed_by=[],
        )
    )
    await db_session.flush()

    await db_session.execute(text("DELETE FROM host.users WHERE id = :uid"), {"uid": user_id})
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT 1 FROM event_creator.events WHERE user_id = :uid").bindparams(uid=user_id)
    )
    assert result.first() is None
