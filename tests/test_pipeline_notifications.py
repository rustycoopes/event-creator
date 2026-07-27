"""Unit tests for the processing-run notification boundary (#52, ported from organize-me)."""

import uuid

import pytest

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


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch, *, e2e_test_mode: bool = False, mock_integrations: bool = False
) -> None:
    import app.services.notifications.pipeline as pipeline_module
    from app.core.config import get_settings

    patched = get_settings().model_copy(
        update={"e2e_test_mode": e2e_test_mode, "mock_integrations": mock_integrations}
    )
    monkeypatch.setattr(pipeline_module, "get_settings", lambda: patched)


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


def test_factory_uses_real_channel_senders_when_both_flags_unset() -> None:
    """No regression to default/QA/prod behavior when neither flag is set."""
    from app.services.notifications.email import ResendEmailSender
    from app.services.notifications.sender import RealNotificationSender
    from app.services.notifications.sms import TwilioSmsSender

    notifier = get_pipeline_notifier()

    assert isinstance(notifier, RealNotificationSender)
    assert isinstance(notifier.email_sender, ResendEmailSender)
    assert isinstance(notifier.sms_sender, TwilioSmsSender)


def test_factory_uses_fake_channel_senders_under_mock_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.notifications.email import FakeEmailSender
    from app.services.notifications.sender import RealNotificationSender
    from app.services.notifications.sms import FakeSmsSender

    _patch_settings(monkeypatch, mock_integrations=True)

    notifier = get_pipeline_notifier()

    assert isinstance(notifier, RealNotificationSender)
    assert isinstance(notifier.email_sender, FakeEmailSender)
    assert isinstance(notifier.sms_sender, FakeSmsSender)


def test_factory_uses_fake_channel_senders_under_e2e_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.notifications.email import FakeEmailSender
    from app.services.notifications.sender import RealNotificationSender
    from app.services.notifications.sms import FakeSmsSender

    _patch_settings(monkeypatch, e2e_test_mode=True)

    notifier = get_pipeline_notifier()

    assert isinstance(notifier, RealNotificationSender)
    assert isinstance(notifier.email_sender, FakeEmailSender)
    assert isinstance(notifier.sms_sender, FakeSmsSender)


def test_factory_uses_fake_channel_senders_when_both_flags_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two flags are OR-able - setting both must not be blocked."""
    from app.services.notifications.email import FakeEmailSender
    from app.services.notifications.sender import RealNotificationSender
    from app.services.notifications.sms import FakeSmsSender

    _patch_settings(monkeypatch, mock_integrations=True, e2e_test_mode=True)

    notifier = get_pipeline_notifier()

    assert isinstance(notifier, RealNotificationSender)
    assert isinstance(notifier.email_sender, FakeEmailSender)
    assert isinstance(notifier.sms_sender, FakeSmsSender)
