"""Read/write the current user's single storage configuration (ported from organize-me, #46).

`GET`/`PUT /api/v1/storage-config` back the Settings > Storage tab fragment
(app.pages.settings_fragments). Auth is the Host-issued JWT (app.core.auth.current_user_id) rather
than fastapi-users' `current_active_user` - see that module's docstring - so a missing/invalid
cookie raises 401 the same way `current_active_user` would.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_id
from app.core.config import Settings
from app.db.session import get_db
from app.models.storage_config import StorageConfig
from app.schemas.storage_config import StorageConfigRead, StorageConfigWrite

router = APIRouter(prefix="/api/v1", tags=["storage-config"])


async def get_user_storage_config(db: AsyncSession, user_id: uuid.UUID) -> StorageConfig | None:
    """The user's single storage config row, or ``None`` if they haven't configured one.

    Shared by this router and the Settings storage fragment (app.pages.settings_fragments) so the
    "one config per user" lookup lives in exactly one place.
    """
    result = await db.execute(select(StorageConfig).where(StorageConfig.user_id == user_id))
    return result.scalar_one_or_none()


def config_is_connected(config: StorageConfig | None) -> bool:
    """Whether a fetched config row represents a usable, connected storage provider.

    Split out from any single call site so future callers that already fetched the config for
    another reason (e.g. to build a StorageProvider from it) can reuse this same definition of
    "connected" without an extra query.
    """
    return config is not None and config.oauth_access_token is not None


async def is_storage_connected(db: AsyncSession, user_id: uuid.UUID, settings: Settings) -> bool:
    """Whether the user has a usable, connected storage provider (or E2E is faking one)."""
    if settings.e2e_test_mode:
        return True
    config = await get_user_storage_config(db, user_id)
    return config_is_connected(config)


@router.get("/storage-config", response_model=StorageConfigRead)
async def read_storage_config(
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> StorageConfigRead:
    config = await get_user_storage_config(db, user_id)
    if config is None:
        # Unset state: an all-null read the settings fragment renders as an empty form.
        return StorageConfigRead()
    return StorageConfigRead(
        provider=config.provider,
        folder_path=config.folder_path,
        is_connected=config.oauth_access_token is not None,
    )


@router.put("/storage-config", response_model=StorageConfigRead)
async def upsert_storage_config(
    payload: StorageConfigWrite,
    user_id: uuid.UUID = Depends(current_user_id),
    db: AsyncSession = Depends(get_db),
) -> StorageConfigRead:
    config = await get_user_storage_config(db, user_id)
    if config is None:
        # One row per user (user_id is UNIQUE), so this is a create-or-update, never an insert of
        # a second row.
        config = StorageConfig(
            user_id=user_id,
            provider=payload.provider,
            folder_path=payload.folder_path,
        )
        db.add(config)
    else:
        if config.provider != payload.provider:
            # Switching providers leaves any previously-connected credentials meaningless for the
            # new one (a Google Drive OAuth token doesn't authenticate Dropbox calls, etc.) - clear
            # them so `is_connected`/build_storage_provider don't act on stale, wrong-provider
            # credentials.
            config.oauth_access_token = None
            config.oauth_refresh_token = None
            config.oauth_token_expires_at = None
            config.s3_access_key = None
            config.s3_secret_key = None
            config.s3_bucket_name = None
            config.s3_region = None
        config.provider = payload.provider
        config.folder_path = payload.folder_path
    # get_db doesn't auto-commit, so persist here (savepoint-safe under the test fixture's
    # rolled-back session).
    await db.commit()
    return StorageConfigRead(
        provider=payload.provider,
        folder_path=config.folder_path,
        is_connected=config.oauth_access_token is not None,
    )
