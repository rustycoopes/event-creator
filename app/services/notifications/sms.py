import asyncio
from typing import Protocol

from twilio.rest import Client as TwilioClient

from app.core.config import get_settings


class SmsSender(Protocol):
    async def send(self, *, to: str, body: str) -> None: ...


class TwilioSmsSender:
    """Sends SMS via the Twilio API."""

    # A fresh RealNotificationSender/TwilioSmsSender is built per upload request (see
    # get_pipeline_notifier), so caching this per-instance wouldn't help across sends.
    # Cache the client at class level instead - shared across every TwilioSmsSender, mirroring
    # RealNotificationSender's class-level Jinja env cache - so the underlying requests.Session
    # (and its connection pool) is built once per Twilio account rather than once per send.
    _client: TwilioClient | None = None

    async def send(self, *, to: str, body: str) -> None:
        settings = get_settings()
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number):
            raise RuntimeError(
                "TwilioSmsSender used but TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/"
                "TWILIO_PHONE_NUMBER is unset - wire those secrets into this environment "
                "before SMS notifications can send."
            )
        if TwilioSmsSender._client is None:
            TwilioSmsSender._client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        # twilio's REST client is a blocking (requests-based) client; run it off the event
        # loop thread so a slow/hanging call to Twilio can't stall the server.
        await asyncio.to_thread(
            TwilioSmsSender._client.messages.create,
            to=to,
            from_=settings.twilio_phone_number,
            body=body,
        )


class FakeSmsSender:
    """Records sent messages instead of calling the real Twilio API. Used in tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, body: str) -> None:
        self.sent.append({"to": to, "body": body})
