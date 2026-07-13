"""A separate declarative base for read-only mappings onto tables event-creator does NOT own.

`app.models.host_user.HostUser` maps onto `host.users` (the Host repo's fastapi-users table, same
Postgres database, cross-schema query - no network call). It must never be attached to
`app.db.base.Base`: that `Base.metadata` is what `migrations/env.py` hands Alembic as
`target_metadata`, and Alembic's autogenerate compares *every* table reachable from it against the
live schema. A `host.users` table living in event-creator's own metadata would make autogenerate
propose ALTER/DROP statements against a table this repo has no business migrating - not just
non-functional noise, but actively dangerous if anyone ever ran `alembic revision --autogenerate`
without first reviewing the diff by hand.

Keeping `HostUser` on this separate, Alembic-invisible base is what makes it structurally
impossible for this repo's own migration history to ever touch `host.users` while still letting
ordinary SQLAlchemy `select()` queries load it in the same async session as every other model.
"""

from sqlalchemy.orm import DeclarativeBase


class ReadOnlyBase(DeclarativeBase):
    pass
