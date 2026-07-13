"""Lazy get-or-create for a user's Event-Creator settings row (#158 / Slice R2).

Notification preferences and onboarding progress used to live directly on `host.users`; they now
live in `event_creator.user_settings`, one row per user, created lazily on first read/write rather
than eagerly at registration (mirrors `app.api.v1.llm_prompt.get_or_create_user_prompt`'s pattern
so `on_after_register` never writes Event-Creator data). Shared by every reader/writer of these
fields (the users API, the settings/dashboard pages, the notification sender, the pipeline
runner, and the onboarding-flag writers) so "one settings row per user" lives in one place.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_settings import UserSettings


async def get_user_settings(db: AsyncSession, user_id: uuid.UUID) -> UserSettings | None:
    """The user's settings row, or ``None`` if they have no row yet (any account - there's no
    eager seed at registration for this table)."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create_user_settings(db: AsyncSession, user_id: uuid.UUID) -> UserSettings:
    """The user's settings row, creating one with the column defaults if none exists yet.

    Every account - new or pre-existing - reaches this the first time anything reads or writes
    their notification prefs or onboarding flags, so the DB always ends up with a real row.
    """
    settings = await get_user_settings(db, user_id)
    if settings is not None:
        return settings
    settings = UserSettings(user_id=user_id)
    db.add(settings)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request for the same user created the row first (user_id is UNIQUE); roll
        # back our losing INSERT and use theirs. Mirrors get_or_create_user_prompt's own race.
        await db.rollback()
        existing = await get_user_settings(db, user_id)
        if existing is None:
            raise
        return existing
    return settings


async def mark_storage_onboarding_done(db: AsyncSession, user_id: uuid.UUID) -> UserSettings:
    """Flip ``onboarding_storage_done`` on first successful storage connection and commit.

    Shared by both storage OAuth callbacks (Dropbox, Google Drive) so "flip this flag and
    persist it" lives in one place, rather than each repeating get-or-create + setattr + commit.
    """
    settings = await get_or_create_user_settings(db, user_id)
    settings.onboarding_storage_done = True
    await db.commit()
    return settings


async def mark_first_upload_onboarding_done(db: AsyncSession, user_id: uuid.UUID) -> UserSettings:
    """Flip ``onboarding_first_upload_done`` on first successful upload and commit.

    Shared by both the manual upload endpoint and the "import pending files" batch endpoint.
    """
    settings = await get_or_create_user_settings(db, user_id)
    settings.onboarding_first_upload_done = True
    await db.commit()
    return settings
