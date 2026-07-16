"""Unit tests for the pure progress-view logic (Slice 4.2, #53).

``build_step_views`` is the seam the SSE stream and the page both render from, so it's tested
directly: the canonical 7 steps always come out in order, and a step with no row yet is ``pending``.
"""

import uuid
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_run import ProcessingRunStatus
from app.models.processing_step import ProcessingStepStatus
from app.services.pipeline.progress import build_step_views, stream_run_progress
from app.services.pipeline.runner import PIPELINE_STEPS


def test_build_step_views_returns_all_seven_steps_in_order() -> None:
    views = build_step_views({})

    assert [v.number for v in views] == [1, 2, 3, 4, 5, 6, 7]
    assert [v.name for v in views] == [name for _, name in PIPELINE_STEPS]
    # Nothing recorded yet -> every step is pending.
    assert {v.status for v in views} == {"pending"}


def test_build_step_views_reflects_recorded_statuses() -> None:
    views = build_step_views(
        {
            1: ProcessingStepStatus.SUCCESS,
            2: ProcessingStepStatus.SKIPPED,
            3: ProcessingStepStatus.IN_PROGRESS,
        }
    )
    by_number = {v.number: v.status for v in views}

    assert by_number[1] == "success"
    assert by_number[2] == "skipped"
    assert by_number[3] == "in_progress"
    # Steps without a row default to pending.
    assert by_number[4] == "pending"
    assert by_number[7] == "pending"


def test_build_step_views_accepts_raw_string_statuses() -> None:
    # The DB may hand back the enum's value as a plain string; it should normalise the same way.
    views = build_step_views({1: "failed"})

    assert views[0].status == "failed"


class _FakeExecuteResult:
    def __init__(self, rows: list[tuple[int, ProcessingStepStatus]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[int, ProcessingStepStatus]]:
        return self._rows


class _ScriptedSession:
    """Feeds stream_run_progress a scripted sequence of (steps, run_status) snapshots, one per
    poll, so the multi-poll change-detection, transitions, and loop can be tested without a DB.

    ``snapshots`` is a list of (step_status_by_number, run_status); the last snapshot is reused for
    any further polls (used by the timeout test, which never reaches a terminal state)."""

    def __init__(
        self,
        snapshots: list[tuple[dict[int, ProcessingStepStatus], ProcessingRunStatus]],
    ) -> None:
        self._snapshots = snapshots
        self._poll = -1
        self.rollbacks = 0

    def _current(self) -> tuple[dict[int, ProcessingStepStatus], ProcessingRunStatus]:
        return self._snapshots[min(self._poll, len(self._snapshots) - 1)]

    async def execute(self, _stmt: object) -> _FakeExecuteResult:
        # execute() is the first DB call of each poll iteration -> advance the scripted clock here.
        self._poll += 1
        steps, _run = self._current()
        return _FakeExecuteResult(list(steps.items()))

    async def scalar(self, _stmt: object) -> ProcessingRunStatus:
        _steps, run = self._current()
        return run

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_stream_emits_only_changed_steps_across_polls() -> None:
    session = _ScriptedSession(
        [
            ({1: ProcessingStepStatus.IN_PROGRESS}, ProcessingRunStatus.IN_PROGRESS),
            (
                {1: ProcessingStepStatus.SUCCESS, 2: ProcessingStepStatus.IN_PROGRESS},
                ProcessingRunStatus.IN_PROGRESS,
            ),
            (
                {n: ProcessingStepStatus.SUCCESS for n in range(1, 8)},
                ProcessingRunStatus.SUCCESS,
            ),
        ]
    )

    events = [
        e
        async for e in stream_run_progress(
            cast(AsyncSession, session), uuid.uuid4(), poll_interval=0.001, max_seconds=5.0
        )
    ]
    kinds = [e["event"] for e in events]

    # Step 1 is emitted exactly twice (in_progress on poll 0, success on poll 1) and NOT a third
    # time on poll 2 when it's unchanged -> change-detection suppresses the no-op re-emit.
    assert kinds.count("step-1") == 2
    # Step 3 goes pending (poll 0, no row yet) -> success (poll 2): two distinct states, two emits,
    # and no emit on poll 1 where it stayed pending.
    assert kinds.count("step-3") == 2
    # Terminal run closes the stream.
    assert kinds[-1] == "done"
    assert kinds.count("done") == 1
    # The read transaction is released between polls (once per non-terminal poll).
    assert session.rollbacks >= 2


async def test_stream_times_out_on_a_run_that_never_finishes() -> None:
    session = _ScriptedSession(
        [({1: ProcessingStepStatus.IN_PROGRESS}, ProcessingRunStatus.IN_PROGRESS)]
    )

    events = [
        e
        async for e in stream_run_progress(
            cast(AsyncSession, session), uuid.uuid4(), poll_interval=0.001, max_seconds=0.003
        )
    ]

    # The safety valve fires: the generator stops (bounding the connection) but emits NO `done`
    # event, so the browser's EventSource reconnects and keeps watching instead of the page freezing.
    kinds = [e["event"] for e in events]
    assert "done" not in kinds
    # It still emitted the observed progress before giving up (at least the first step + run-status).
    assert any(k.startswith("step-") for k in kinds)
