"""Real implementation of NotificationSender for Slice 7 (email + SMS notifications).

Ported from organize-me for R8, rewired onto event-creator's own read-only ``HostUser`` mapping
(``app.models.host_user``, see that module's docstring for why it's SELECT-only) instead of the
monolith's writable ``User`` model, and event-creator's own ``UserSettings``/
``get_or_create_user_settings`` (ported in Slice R7) rather than organize-me's copy. Both source
and destination ``UserSettings`` share the same ``notification_email``/``notification_sms`` field
names, so no field-name adjustment was needed.
"""

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.host_user import HostUser
from app.services.notifications.email import EmailSender, ResendEmailSender
from app.services.notifications.pipeline import NotificationOutcome, PipelineNotification
from app.services.notifications.sms import SmsSender, TwilioSmsSender
from app.services.user_settings import get_or_create_user_settings

logger = logging.getLogger(__name__)


class RealNotificationSender:
    """Sends branded HTML email + SMS notifications after processing runs.

    Implements the NotificationSender protocol to deliver success/zero-event/failure
    notifications via email and SMS, respecting each user's notification_email /
    notification_sms preferences independently. Uses Jinja2 templates for rendering
    branded HTML emails with inline CSS.
    """

    _jinja_env: Environment | None = None

    def __init__(
        self,
        email_sender: EmailSender | None = None,
        sms_sender: SmsSender | None = None,
    ) -> None:
        self.email_sender = email_sender or ResendEmailSender()
        self.sms_sender = sms_sender or TwilioSmsSender()
        # Lazy-initialize the Jinja2 environment once per class (cached)
        if RealNotificationSender._jinja_env is None:
            template_dir = Path(__file__).parent.parent.parent / "templates" / "emails"
            RealNotificationSender._jinja_env = Environment(
                loader=FileSystemLoader(str(template_dir)),
                autoescape=True,
            )
        self.jinja_env = RealNotificationSender._jinja_env

    async def send(self, notification: PipelineNotification) -> list[str]:
        """Send email + SMS notifications for a processing run.

        Fetches the user's contact info and notification preferences, renders the
        appropriate content, and sends via email_sender/sms_sender for each channel
        the user has enabled.
        """
        # Get a database session to fetch the user
        async for session in get_db():
            return await self._send_with_session(session, notification)
        return []  # pragma: no cover - get_db always yields at least one session

    async def _send_with_session(
        self, session: AsyncSession, notification: PipelineNotification
    ) -> list[str]:
        """Internal method that accepts a session (useful for testing).

        Returns a description of each enabled channel that raised while actually attempting
        delivery (issue #144) - a real failure (bad/unset credentials, the provider rejecting the
        recipient, a network error, ...) was previously only ever logged server-side via
        ``logger.exception``, indistinguishable from a genuine send by the pipeline's Notify step
        or the user."""
        user = await session.get(HostUser, notification.user_id)
        if user is None:
            logger.warning("User not found for notification: %s", notification.user_id)
            return []

        settings = await get_or_create_user_settings(session, notification.user_id)

        failures: list[str] = []

        if settings.notification_email:
            if not user.email:
                logger.info(
                    "Skipping email notification: user %s has notification_email=True but no "
                    "email on file",
                    notification.user_id,
                )
            else:
                try:
                    if notification.outcome == NotificationOutcome.FAILED:
                        await self._send_failure_email(user.email, notification)
                    else:
                        # SUCCESS and NO_NEW_EVENTS both use the success template
                        await self._send_success_email(user.email, notification)
                except Exception as exc:
                    logger.exception(
                        "Failed to send notification email to user %s", notification.user_id
                    )
                    failures.append(f"email delivery failed: {exc}")
        else:
            logger.debug(
                "Skipping email notification: user %s has notification_email=False",
                notification.user_id,
            )

        if settings.notification_sms:
            if not user.phone_number:
                logger.info(
                    "Skipping SMS notification: user %s has notification_sms=True but no "
                    "phone_number on file",
                    notification.user_id,
                )
            else:
                try:
                    if notification.outcome == NotificationOutcome.FAILED:
                        await self._send_failure_sms(user.phone_number, notification)
                    else:
                        await self._send_success_sms(user.phone_number, notification)
                except Exception as exc:
                    logger.exception(
                        "Failed to send notification SMS to user %s", notification.user_id
                    )
                    failures.append(f"SMS delivery failed: {exc}")
        else:
            logger.debug(
                "Skipping SMS notification: user %s has notification_sms=False",
                notification.user_id,
            )

        return failures

    async def _send_success_email(
        self, to_email: str, notification: PipelineNotification
    ) -> None:
        """Render and send a success or zero-event notification email."""
        template = self.jinja_env.get_template("success.html.j2")
        settings = get_settings()

        html = template.render(
            filename=notification.filename,
            new_event_count=notification.new_event_count,
            dashboard_url=f"{settings.base_url}/dashboard",
        )

        await self.email_sender.send(
            to=to_email,
            subject=f"OrganizeMe: {notification.filename} processed successfully",
            html=html,
        )
        logger.info(
            "Sent success email to %s for run %s", to_email, notification.run_id
        )

    async def _send_failure_email(
        self, to_email: str, notification: PipelineNotification
    ) -> None:
        """Render and send a failure notification email."""
        template = self.jinja_env.get_template("failure.html.j2")
        settings = get_settings()

        html = template.render(
            filename=notification.filename,
            error_message=notification.message,
            log_url=f"{settings.base_url}/runs/{notification.run_id}",
        )

        await self.email_sender.send(
            to=to_email,
            subject=f"OrganizeMe: {notification.filename} processing failed",
            html=html,
        )
        logger.info(
            "Sent failure email to %s for run %s", to_email, notification.run_id
        )

    async def _send_success_sms(
        self, to_phone: str, notification: PipelineNotification
    ) -> None:
        """Render and send a success or zero-event notification SMS."""
        settings = get_settings()
        body = (
            f"OrganizeMe: {notification.filename} processed, "
            f"{notification.new_event_count} new event(s). "
            f"{settings.base_url}/dashboard"
        )

        await self.sms_sender.send(to=to_phone, body=body)
        logger.info("Sent success SMS to %s for run %s", to_phone, notification.run_id)

    async def _send_failure_sms(
        self, to_phone: str, notification: PipelineNotification
    ) -> None:
        """Render and send a failure notification SMS."""
        settings = get_settings()
        body = (
            f"OrganizeMe: {notification.filename} processing failed - "
            f"{notification.message} {settings.base_url}/runs/{notification.run_id}"
        )

        await self.sms_sender.send(to=to_phone, body=body)
        logger.info("Sent failure SMS to %s for run %s", to_phone, notification.run_id)
