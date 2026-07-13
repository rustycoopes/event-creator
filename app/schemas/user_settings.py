"""Schemas for the narrow `PATCH /api/v1/user-settings` endpoint (Slice R7).

Deliberately does NOT mirror organize-me's `PATCH /api/v1/users/me` (app/schemas/user.py's
`UserUpdate`), which conflates core profile fields (email, name, phone_number, dark_mode) with the
two notification toggles - those core fields aren't event-creator's to own (they live on the
Host's `host.users`, read here only via the read-only `HostUser` mapping). This schema covers only
the two `UserSettings` booleans.
"""

from pydantic import BaseModel, ConfigDict


class UserSettingsUpdate(BaseModel):
    """Payload for `PATCH /api/v1/user-settings`.

    Both fields are optional (`exclude_unset=True` on the server side distinguishes "omitted"
    from "explicitly provided"), but neither accepts an explicit `null` - matching organize-me's
    `UserUpdate` behaviour for these same two fields (a `null` isn't a meaningful "turn this
    setting off" value, so pydantic's ordinary "provided" validation is the right level of
    strictness here rather than a custom reject-null validator, since `bool | None` isn't used).
    """

    notification_email: bool | None = None
    notification_sms: bool | None = None


class UserSettingsRead(BaseModel):
    """Response for `PATCH`/`GET /api/v1/user-settings`.

    `email`/`phone_number` are surfaced (read-only, sourced from the Host's `host.users` via the
    read-only `HostUser` mapping - see app.models.host_user) purely so the Notifications panel can
    render its "Set your X in Profile to enable" gating hints without a second round trip; they are
    never written through this endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    notification_email: bool
    notification_sms: bool
    email: str | None = None
    phone_number: str | None = None
