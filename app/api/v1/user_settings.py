"""`GET`/`PATCH /api/v1/user-settings`: the Notifications tab's narrow settings endpoint (Slice R7).

Deliberately NOT a port of organize-me's `PATCH /api/v1/users/me` (app/api/v1/users.py), which
conflates core profile fields (email, name, phone_number, dark_mode - all Host-owned) with the two
notification toggles. Event-creator has no business writing Host profile fields, so this endpoint
is scoped to just `UserSettings.notification_email`/`notification_sms` - the onboarding-flag-flip-
on-first-save semantics are ported faithfully from that reference implementation, just narrower.

`email`/`phone_number` in the response come from the read-only `HostUser` mapping
(app.services.host_user) purely so the Notifications fragment can render its gating hints without
a second round trip - this endpoint never writes them.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_id
from app.db.session import get_db
from app.models.user_settings import UserSettings
from app.schemas.user_settings import UserSettingsRead, UserSettingsUpdate
from app.services.host_user import get_host_user
from app.services.user_settings import get_or_create_user_settings

router = APIRouter(prefix="/api/v1", tags=["user-settings"])


async def _to_read(db: AsyncSession, user_id: uuid.UUID, settings: UserSettings) -> UserSettingsRead:
    host_user = await get_host_user(db, user_id)
    return UserSettingsRead(
        notification_email=settings.notification_email,
        notification_sms=settings.notification_sms,
        email=host_user.email if host_user is not None else None,
        phone_number=host_user.phone_number if host_user is not None else None,
    )


@router.get("/user-settings", response_model=UserSettingsRead)
async def read_user_settings(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsRead:
    settings = await get_or_create_user_settings(db, user_id)
    return await _to_read(db, user_id, settings)


@router.patch("/user-settings", response_model=UserSettingsRead)
async def update_user_settings(
    update: UserSettingsUpdate,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsRead:
    fields_set = update.model_fields_set & {"notification_email", "notification_sms"}
    settings = await get_or_create_user_settings(db, user_id)

    if fields_set:
        if "notification_email" in fields_set and update.notification_email is not None:
            settings.notification_email = update.notification_email
        if "notification_sms" in fields_set and update.notification_sms is not None:
            settings.notification_sms = update.notification_sms
        # Flips on the first save and stays true thereafter - idempotent, matching organize-me's
        # PATCH /api/v1/users/me semantics for this same flag.
        if not settings.onboarding_notifications_done:
            settings.onboarding_notifications_done = True
        await db.commit()

    return await _to_read(db, user_id, settings)
