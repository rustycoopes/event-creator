"""Tests for Slice 7.1: branded email notifications (ported from organize-me for R8).

Adapted for event-creator's read-only ``HostUser`` mapping: rather than ``db.add()``ing a writable
``User`` row (organize-me has no equivalent restriction), tests here seed the shared `host.users`
table via ``tests.conftest.create_host_user`` and attach event-creator's own (writable)
``UserSettings`` row directly.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_settings import UserSettings
from app.services.notifications.email import FakeEmailSender
from app.services.notifications.pipeline import (
    NotificationOutcome,
    PipelineNotification,
)
from app.services.notifications.sender import RealNotificationSender
from tests.conftest import create_host_user


@pytest_asyncio.fixture
async def test_user_id(db_session: AsyncSession) -> uuid.UUID:
    """Create a test user with notifications enabled."""
    user_id = await create_host_user(db_session, email="test@example.com")
    db_session.add(
        UserSettings(user_id=user_id, notification_email=True, notification_sms=False)
    )
    await db_session.commit()
    return user_id


@pytest_asyncio.fixture
async def test_user_id_notifications_disabled(db_session: AsyncSession) -> uuid.UUID:
    """Create a test user with email notifications disabled."""
    user_id = await create_host_user(db_session, email="notified@example.com")
    db_session.add(
        UserSettings(user_id=user_id, notification_email=False, notification_sms=False)
    )
    await db_session.commit()
    return user_id


@pytest.fixture
def fake_email_sender() -> FakeEmailSender:
    return FakeEmailSender()


class TestEmailNotifications:
    async def test_success_notification_sends_email(
        self, test_user_id: uuid.UUID, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that a successful run triggers a success email."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=42,
            message="42 new events added.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_email_sender.sent) == 1
        email = fake_email_sender.sent[0]
        assert email["to"] == "test@example.com"
        assert "test.csv" in email["subject"]
        assert "successfully" in email["subject"].lower()
        assert "42" in email["html"]
        assert "dashboard" in email["html"].lower()

    async def test_zero_event_notification_sends_email(
        self, test_user_id: uuid.UUID, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that a zero-event run triggers a success email with count = 0."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="empty.csv",
            outcome=NotificationOutcome.NO_NEW_EVENTS,
            new_event_count=0,
            message="No new events found.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_email_sender.sent) == 1
        email = fake_email_sender.sent[0]
        assert email["to"] == "test@example.com"
        assert "empty.csv" in email["subject"]
        assert "0" in email["html"]

    async def test_failure_notification_sends_email(
        self, test_user_id: uuid.UUID, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that a failed run triggers a failure email with error details."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        run_id = uuid.uuid4()
        error_message = "CSV parsing failed: invalid format"
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=run_id,
            filename="corrupt.csv",
            outcome=NotificationOutcome.FAILED,
            new_event_count=0,
            message=error_message,
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_email_sender.sent) == 1
        email = fake_email_sender.sent[0]
        assert email["to"] == "test@example.com"
        assert "corrupt.csv" in email["subject"]
        assert "failed" in email["subject"].lower()
        assert error_message in email["html"]
        assert str(run_id) in email["html"]

    async def test_no_email_sent_when_notifications_disabled(
        self,
        test_user_id_notifications_disabled: uuid.UUID,
        fake_email_sender: FakeEmailSender,
        db_session: AsyncSession,
    ) -> None:
        """Test that no email is sent when user.notification_email is False."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        notification = PipelineNotification(
            user_id=test_user_id_notifications_disabled,
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=5,
            message="5 new events added.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_email_sender.sent) == 0

    async def test_no_email_sent_for_unknown_user(
        self, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that sending fails gracefully for unknown users."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        notification = PipelineNotification(
            user_id=uuid.uuid4(),  # Non-existent user
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=1,
            message="1 new event added.",
        )

        # Should not raise, just log and return
        await sender._send_with_session(db_session, notification)

        assert len(fake_email_sender.sent) == 0

    async def test_success_email_contains_dashboard_link(
        self, test_user_id: uuid.UUID, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that success emails contain a link to the dashboard."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="events.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=10,
            message="10 new events added.",
        )

        await sender._send_with_session(db_session, notification)

        email = fake_email_sender.sent[0]
        assert "/dashboard" in email["html"]

    async def test_failure_email_contains_log_page_link(
        self, test_user_id: uuid.UUID, fake_email_sender: FakeEmailSender, db_session: AsyncSession
    ) -> None:
        """Test that failure emails contain a link to the processing log page."""
        sender = RealNotificationSender(email_sender=fake_email_sender)
        run_id = uuid.uuid4()
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=run_id,
            filename="failed.csv",
            outcome=NotificationOutcome.FAILED,
            new_event_count=0,
            message="Processing error",
        )

        await sender._send_with_session(db_session, notification)

        email = fake_email_sender.sent[0]
        assert f"/runs/{run_id}" in email["html"]

    async def test_send_with_session_returns_failure_when_email_delivery_raises(
        self, test_user_id: uuid.UUID, db_session: AsyncSession
    ) -> None:
        """Regression test for #144: a live Resend delivery failure (e.g. the account's
        onboarding@resend.dev sandbox sender rejecting a recipient that isn't the account's own
        verified address - see `Settings.email_from`'s docstring) used to be swallowed by a bare
        `except Exception: logger.exception(...)`, so the Notify step still logged "Notified
        user: ..." and succeeded even though nothing was actually delivered - exactly the
        reported symptom (no error, but no email). `_send_with_session` must now return a
        description of the failure instead of swallowing it silently."""

        class _FailingEmailSender:
            async def send(self, *, to: str, subject: str, html: str) -> None:
                raise RuntimeError("Resend: recipient not verified for sandbox sender")

        sender = RealNotificationSender(email_sender=_FailingEmailSender())
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="chat.txt",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=1,
            message="1 new event added.",
        )

        failures = await sender._send_with_session(db_session, notification)

        assert len(failures) == 1
        assert "email" in failures[0].lower()
        assert "recipient not verified" in failures[0]


class TestResendEmailSender:
    async def test_raises_clear_error_when_api_key_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ResendEmailSender.send() should fail loudly and clearly if RESEND_API_KEY is unset,
        rather than surfacing a confusing error from the Resend SDK itself - mirrors
        TwilioSmsSender's equivalent guard (issue #124's deferred email-side half)."""
        import app.services.notifications.email as email_module
        from app.core.config import Settings
        from app.services.notifications.email import ResendEmailSender

        unset_settings = Settings(
            database_url="postgresql://unused",
            jwt_secret="unused",
            google_oauth_client_id="unused",
            google_oauth_client_secret="unused",
            # Explicit empty string: pydantic-settings otherwise falls back to reading
            # .env.local for any field not passed here, which would pick up this worktree's
            # real Resend key and defeat the point of this test.
            resend_api_key="",
        )
        monkeypatch.setattr(email_module, "get_settings", lambda: unset_settings)

        sender = ResendEmailSender()

        with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
            await sender.send(to="user@example.com", subject="hi", html="<p>hi</p>")
