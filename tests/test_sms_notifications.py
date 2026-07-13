"""Tests for Slice 7.2: SMS notifications via Twilio (ported from organize-me for R8).

See test_email_notifications.py's module docstring for why fixtures here seed `host.users` via
``tests.conftest.create_host_user`` rather than adding a writable ``User`` row directly.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_settings import UserSettings
from app.services.notifications.pipeline import (
    NotificationOutcome,
    PipelineNotification,
)
from app.services.notifications.sender import RealNotificationSender
from app.services.notifications.sms import FakeSmsSender, TwilioSmsSender
from tests.conftest import create_host_user


@pytest_asyncio.fixture
async def test_user_id(db_session: AsyncSession) -> uuid.UUID:
    """Create a test user with SMS notifications enabled and a phone number on file."""
    user_id = await create_host_user(
        db_session, email="test@example.com", phone_number="+15551234567"
    )
    db_session.add(
        UserSettings(user_id=user_id, notification_email=False, notification_sms=True)
    )
    await db_session.commit()
    return user_id


@pytest_asyncio.fixture
async def test_user_id_sms_disabled(db_session: AsyncSession) -> uuid.UUID:
    """Create a test user with SMS notifications disabled."""
    user_id = await create_host_user(
        db_session, email="notified@example.com", phone_number="+15551234567"
    )
    db_session.add(
        UserSettings(user_id=user_id, notification_email=False, notification_sms=False)
    )
    await db_session.commit()
    return user_id


@pytest_asyncio.fixture
async def test_user_id_no_phone(db_session: AsyncSession) -> uuid.UUID:
    """Create a test user with SMS enabled but no phone number on file."""
    user_id = await create_host_user(
        db_session, email="nophone@example.com", phone_number=None
    )
    db_session.add(
        UserSettings(user_id=user_id, notification_email=False, notification_sms=True)
    )
    await db_session.commit()
    return user_id


@pytest.fixture
def fake_sms_sender() -> FakeSmsSender:
    return FakeSmsSender()


class TestSmsNotifications:
    async def test_success_notification_sends_sms(
        self, test_user_id: uuid.UUID, fake_sms_sender: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Test that a successful run triggers a success SMS with count + dashboard link."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=42,
            message="42 new events added.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_sms_sender.sent) == 1
        sms = fake_sms_sender.sent[0]
        assert sms["to"] == "+15551234567"
        assert "42" in sms["body"]
        assert "/dashboard" in sms["body"]

    async def test_zero_event_notification_sends_sms(
        self, test_user_id: uuid.UUID, fake_sms_sender: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Test that a zero-event run triggers the success SMS with count = 0."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
        notification = PipelineNotification(
            user_id=test_user_id,
            run_id=uuid.uuid4(),
            filename="empty.csv",
            outcome=NotificationOutcome.NO_NEW_EVENTS,
            new_event_count=0,
            message="No new events found.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_sms_sender.sent) == 1
        sms = fake_sms_sender.sent[0]
        assert "0" in sms["body"]

    async def test_failure_notification_sends_sms(
        self, test_user_id: uuid.UUID, fake_sms_sender: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Test that a failed run triggers a failure SMS with error summary + log page link."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
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

        assert len(fake_sms_sender.sent) == 1
        sms = fake_sms_sender.sent[0]
        assert error_message in sms["body"]
        assert f"/runs/{run_id}" in sms["body"]

    async def test_no_sms_sent_when_notification_sms_disabled(
        self,
        test_user_id_sms_disabled: uuid.UUID,
        fake_sms_sender: FakeSmsSender,
        db_session: AsyncSession,
    ) -> None:
        """Test that no SMS is sent when user.notification_sms is False."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
        notification = PipelineNotification(
            user_id=test_user_id_sms_disabled,
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=5,
            message="5 new events added.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_sms_sender.sent) == 0

    async def test_no_sms_sent_when_phone_number_missing(
        self,
        test_user_id_no_phone: uuid.UUID,
        fake_sms_sender: FakeSmsSender,
        db_session: AsyncSession,
    ) -> None:
        """Test that no SMS is sent (and no error raised) when notification_sms is True but
        phone_number is empty/None."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
        notification = PipelineNotification(
            user_id=test_user_id_no_phone,
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=5,
            message="5 new events added.",
        )

        # Should not raise, just log and skip.
        await sender._send_with_session(db_session, notification)

        assert len(fake_sms_sender.sent) == 0

    async def test_no_sms_sent_for_unknown_user(
        self, fake_sms_sender: FakeSmsSender, db_session: AsyncSession
    ) -> None:
        """Test that sending fails gracefully for unknown users."""
        sender = RealNotificationSender(sms_sender=fake_sms_sender)
        notification = PipelineNotification(
            user_id=uuid.uuid4(),  # Non-existent user
            run_id=uuid.uuid4(),
            filename="test.csv",
            outcome=NotificationOutcome.SUCCESS,
            new_event_count=1,
            message="1 new event added.",
        )

        await sender._send_with_session(db_session, notification)

        assert len(fake_sms_sender.sent) == 0

    async def test_send_with_session_returns_failure_when_sms_delivery_raises(
        self, test_user_id: uuid.UUID, db_session: AsyncSession
    ) -> None:
        """Regression test for #144, SMS side of the same fix as the email delivery-failure
        test: a raised send must be returned as a failure description, not silently absorbed by
        the bare except that used to leave the pipeline's Notify step none the wiser."""

        class _FailingSmsSender:
            async def send(self, *, to: str, body: str) -> None:
                raise RuntimeError("Twilio: unverified destination number")

        sender = RealNotificationSender(sms_sender=_FailingSmsSender())
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
        assert "sms" in failures[0].lower()
        assert "unverified destination number" in failures[0]


class TestTwilioSmsSender:
    async def test_raises_clear_error_when_credentials_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TwilioSmsSender.send() should fail loudly and clearly if Twilio credentials are
        unset, rather than surfacing a confusing error from the Twilio SDK itself.

        Patches app.services.notifications.sms.get_settings directly (rather than relying on
        real env state) so this never risks hitting the live Twilio API even in an environment
        where TWILIO_* secrets happen to be configured for real.
        """
        import app.services.notifications.sms as sms_module
        from app.core.config import Settings

        unset_settings = Settings(
            database_url="postgresql://unused",
            jwt_secret="unused",
            google_oauth_client_id="unused",
            google_oauth_client_secret="unused",
            # Explicit empty strings: pydantic-settings otherwise falls back to reading
            # .env.local for any field not passed here, which would pick up this worktree's
            # real Twilio credentials and defeat the point of this test.
            twilio_account_sid="",
            twilio_auth_token="",
            twilio_phone_number="",
        )
        monkeypatch.setattr(sms_module, "get_settings", lambda: unset_settings)

        sender = TwilioSmsSender()

        with pytest.raises(RuntimeError, match="TWILIO_ACCOUNT_SID"):
            await sender.send(to="+15551234567", body="test")
