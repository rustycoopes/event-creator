"""Tests for the storage_configs model (ported, #45).

These exercise the real table on the QA database (already created - see
migrations/versions/0001_adopt_event_creator_schema.py's docstring), inside the rolled-back
db_session fixture - so nothing persists.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage_config import StorageConfig, StorageProviderType
from tests.conftest import create_host_user


async def test_storage_config_persists_and_provider_enum_round_trips(
    db_session: AsyncSession,
) -> None:
    user_id = await create_host_user(db_session)
    config = StorageConfig(
        user_id=user_id,
        provider=StorageProviderType.GOOGLE_DRIVE,
        folder_path="/OrganizeMe/exports",
    )
    db_session.add(config)
    await db_session.flush()

    # The raw stored enum label must be the value ("google_drive"), not SQLAlchemy's default
    # member name ("GOOGLE_DRIVE").
    stored_provider = await db_session.scalar(
        text("SELECT provider::text FROM event_creator.storage_configs WHERE user_id = :uid"),
        {"uid": user_id},
    )
    assert stored_provider == "google_drive"

    await db_session.refresh(config)
    assert config.provider is StorageProviderType.GOOGLE_DRIVE
    assert config.folder_path == "/OrganizeMe/exports"
    assert config.oauth_access_token is None
    assert config.created_at is not None


async def test_storage_config_is_unique_per_user(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(
        StorageConfig(user_id=user_id, provider=StorageProviderType.S3, folder_path="/first")
    )
    await db_session.flush()

    db_session.add(
        StorageConfig(user_id=user_id, provider=StorageProviderType.DROPBOX, folder_path="/second")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_host_user_cascades_to_storage_config_row(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session)
    db_session.add(
        StorageConfig(user_id=user_id, provider=StorageProviderType.GOOGLE_DRIVE, folder_path="/x")
    )
    await db_session.flush()

    await db_session.execute(text("DELETE FROM host.users WHERE id = :uid"), {"uid": user_id})
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT 1 FROM event_creator.storage_configs WHERE user_id = :uid").bindparams(
            uid=user_id
        )
    )
    assert result.first() is None


async def test_storage_provider_enum_has_exactly_the_spec_labels(db_session: AsyncSession) -> None:
    labels = (
        await db_session.scalars(
            text(
                "SELECT unnest(enum_range(NULL::event_creator.storage_provider))::text ORDER BY 1"
            )
        )
    ).all()

    assert set(labels) == {"google_drive", "dropbox", "s3"}
    assert set(labels) == {member.value for member in StorageProviderType}
