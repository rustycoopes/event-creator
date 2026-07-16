"""Integration tests for the read-only `HostUser` mapping onto `host.users` (Slice R7).

Exercises a genuine cross-schema `SELECT` against the real, shared database (inside the
rolled-back db_session fixture) - not a mock - proving `app.models.host_user.HostUser` actually
resolves against `host.users` as created by the Host repo's own migrations, using only the four
columns this service declares (see that module's docstring for why it's a deliberately narrow,
select-only mapping).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host_user import HostUser
from app.services.host_user import get_dark_mode, get_host_user
from tests.conftest import create_host_user


async def test_host_user_selects_the_row_inserted_via_raw_sql(db_session: AsyncSession) -> None:
    user_id = await create_host_user(
        db_session, email="readonly-mapping@example.com", phone_number="+15550001111", dark_mode=True
    )

    host_user = (
        await db_session.execute(select(HostUser).where(HostUser.id == user_id))
    ).scalar_one()

    assert host_user.id == user_id
    assert host_user.email == "readonly-mapping@example.com"
    assert host_user.phone_number == "+15550001111"
    assert host_user.dark_mode is True


async def test_get_host_user_returns_none_for_an_unknown_id(db_session: AsyncSession) -> None:
    assert await get_host_user(db_session, uuid.uuid4()) is None


async def test_get_host_user_returns_the_row_for_a_known_id(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session, email="known@example.com")

    host_user = await get_host_user(db_session, user_id)

    assert host_user is not None
    assert host_user.email == "known@example.com"


async def test_get_dark_mode_returns_false_for_an_unknown_id(db_session: AsyncSession) -> None:
    assert await get_dark_mode(db_session, uuid.uuid4()) is False


async def test_get_dark_mode_returns_the_hosts_stored_preference(db_session: AsyncSession) -> None:
    user_id = await create_host_user(db_session, dark_mode=True)

    assert await get_dark_mode(db_session, user_id) is True


async def test_host_user_mapping_only_declares_the_columns_it_reads(db_session: AsyncSession) -> None:
    """Regression guard for the narrow-mapping design decision: HostUser must never grow
    hashed_password/is_active/etc. columns event-creator has no business reading."""
    mapped_columns = {c.key for c in HostUser.__table__.columns}
    assert mapped_columns == {"id", "email", "phone_number", "dark_mode"}
