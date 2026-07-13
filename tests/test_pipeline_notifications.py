"""Unit tests for the processing-run notification boundary (#52, ported from organize-me)."""

import uuid

from app.services.notifications.pipeline import (
    FakeNotificationSender,
    LoggingNotificationSender,
    NotificationOutcome,
    PipelineNotification,
    get_pipeline_notifier,
)


def _notification() -> PipelineNotification:
    return PipelineNotification(
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        filename="chat.txt",
        outcome=NotificationOutcome.SUCCESS,
        new_event_count=3,
        message="3 new events added.",
    )


async def test_fake_notifier_records_payloads() -> None:
    sender = FakeNotificationSender()
    notification = _notification()

    await sender.send(notification)

    assert sender.sent == [notification]


async def test_logging_notifier_sends_without_error() -> None:
    # The Slice 4.1 production notifier only logs; it must accept a payload without raising.
    await LoggingNotificationSender().send(_notification())


def test_factory_returns_the_real_sender() -> None:
    from app.services.notifications.sender import RealNotificationSender

    assert isinstance(get_pipeline_notifier(), RealNotificationSender)
