"""The processing-run notification boundary (Slice 4.1, #52, ported from organize-me for R8).

Step 7 of the pipeline notifies the user that their upload finished (success, zero-new-events, or
failure). Real SMS (Twilio) + email (Resend) delivery is Slice 7; this slice implements only the
*seam* so the pipeline is complete and testable now:

- ``NotificationSender`` — the Protocol the pipeline depends on.
- ``LoggingNotificationSender`` — the production stub for this slice: logs the payload (no send).
- ``FakeNotificationSender`` — records payloads so tests can assert what would be sent.
- ``get_pipeline_notifier`` — factory, overridable via FastAPI ``dependency_overrides`` (mirrors
  ``app.services.notifications.email.get_email_sender`` / ``app.services.llm.gemini``).

Slice 7 swaps ``LoggingNotificationSender`` for a real sender behind the same Protocol without
touching the pipeline.
"""

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


class NotificationOutcome(str, Enum):
    """Which of the three terminal states the run reached, so the notification wording (and later
    Slice 7's template selection) can branch without re-deriving it from counts + status."""

    SUCCESS = "success"
    NO_NEW_EVENTS = "no_new_events"
    FAILED = "failed"


@dataclass(frozen=True)
class PipelineNotification:
    """The payload for a single processing-run notification.

    Deliberately self-contained (ids + filename + human ``message``) so a real Slice 7 sender can
    build an SMS/email from it without loading anything else, and so tests can assert on it
    directly."""

    user_id: uuid.UUID
    run_id: uuid.UUID
    filename: str
    outcome: NotificationOutcome
    new_event_count: int
    message: str


class NotificationSender(Protocol):
    async def send(self, notification: PipelineNotification) -> list[str]:
        """Deliver (or, this slice, log) a processing-run notification.

        Returns a description of each enabled channel that failed to actually deliver (issue
        #144) - empty if every channel the user has enabled either sent successfully or was
        itself not applicable (e.g. no phone number on file, already covered by the separate
        "silent mode" warning in ``app.services.pipeline.runner``). The caller (the pipeline's
        Notify step) surfaces these as log lines so a real delivery failure - as opposed to
        expected user configuration - is visible instead of only reaching server-side logs.
        """
        ...


class LoggingNotificationSender:
    """Production notifier for Slice 4.1: logs the notification instead of sending it.

    A real SMS+email sender replaces this in Slice 7 behind the same Protocol; the pipeline is
    unaffected."""

    async def send(self, notification: PipelineNotification) -> list[str]:
        logger.info(
            "processing-run notification: run=%s user=%s outcome=%s new_events=%d file=%s :: %s",
            notification.run_id,
            notification.user_id,
            notification.outcome.value,
            notification.new_event_count,
            notification.filename,
            notification.message,
        )
        return []


class FakeNotificationSender:
    """Records notifications instead of sending them. Used in tests to assert the step-7 payload.

    ``failures`` can be set by a test before calling ``send()`` to simulate a delivery failure
    surfacing through the pipeline (issue #144) without needing a real failing channel sender."""

    def __init__(self) -> None:
        self.sent: list[PipelineNotification] = []
        self.failures: list[str] = []

    async def send(self, notification: PipelineNotification) -> list[str]:
        self.sent.append(notification)
        return list(self.failures)


def get_pipeline_notifier() -> NotificationSender:
    """Return the production notifier. Overridable via ``dependency_overrides`` in tests."""
    from app.services.notifications.sender import RealNotificationSender

    return RealNotificationSender()
