"""Cascade test for the events model (Slice R10 boundary suite, #165).

Exercises the real table on the QA database, inside the rolled-back db_session fixture - so
nothing persists.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.processing_run import ProcessingRun, ProcessingRunStatus
from tests.conftest import create_host_user


async def test_deleting_host_user_cascades_to_events(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    run = ProcessingRun(user_id=user_id, filename="chat.txt", status=ProcessingRunStatus.SUCCESS)
    db_session.add(run)
    await db_session.flush()
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

    events_result = await db_session.execute(
        text("SELECT 1 FROM event_creator.events WHERE user_id = :uid").bindparams(uid=user_id)
    )
    assert events_result.first() is None

    runs_result = await db_session.execute(
        text("SELECT 1 FROM event_creator.processing_runs WHERE user_id = :uid").bindparams(
            uid=user_id
        )
    )
    assert runs_result.first() is None
