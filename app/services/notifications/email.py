import asyncio
from typing import Protocol

import resend

from app.core.config import get_settings


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, html: str) -> None: ...


class ResendEmailSender:
    """Sends email via the Resend API.

    This is the first cut of the email-sending interface; Slice 7 (Notifications)
    reuses it for processing-run success/failure emails.
    """

    async def send(self, *, to: str, subject: str, html: str) -> None:
        settings = get_settings()
        if not settings.resend_api_key:
            raise RuntimeError(
                "ResendEmailSender used but RESEND_API_KEY is unset - wire that secret into "
                "this environment before email notifications can send."
            )
        resend.api_key = settings.resend_api_key
        # resend's SendClient is a blocking (requests-based) client; run it off the
        # event loop thread so a slow/hanging call to Resend can't stall the server.
        await asyncio.to_thread(
            resend.Emails.send,
            {
                "from": settings.email_from,
                "to": [to],
                "subject": subject,
                "html": html,
            },
        )


class FakeEmailSender:
    """Records sent messages instead of calling the real Resend API. Used in tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "html": html})
