"""Read-only mapping onto the Host's `host.users` table (Slice R7).

Event Creator owns no `User`/fastapi-users model of its own (see app.core.auth's docstring) - the
Host-issued JWT tells us *which* user id is making a request, nothing more. The Notifications
settings panel needs to know whether that user has an email/phone number on file (to gate the two
toggles, matching organize-me's Settings > Notifications tab) and whether they prefer dark mode,
none of which lives in event-creator's own schema.

`HostUser` is mapped onto `host.users` - the same Postgres database, a cross-schema query, no
network call - but is **select-only by convention and by construction**:

- It's mapped on `ReadOnlyBase` (see app.db.readonly_base), a declarative base kept entirely
  separate from `app.db.base.Base` / Alembic's `target_metadata`, so this repo's own migration
  history can never emit DDL against a table it doesn't own.
- Only the columns this service actually reads are declared (`id`, `email`, `phone_number`,
  `dark_mode`) - deliberately omitting `hashed_password`, `is_active`, `is_superuser`,
  `is_verified`, etc., which live on the Host's real `User` model (see
  `C:\\dev\\organize-me\\app\\models\\user.py`) and are none of Event Creator's concern.
- Nothing in this codebase ever `db.add()`s, updates, or deletes a `HostUser` - callers must only
  ever `select()` it. There is no ORM-level mechanism preventing a write (SQLAlchemy has no
  built-in "read-only mapped class"), so this is enforced by code review / convention, the same way
  organize-me's own cross-cutting invariants are - see the module docstring warning below repeated
  at every call site that imports this model.
"""

import uuid

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.readonly_base import ReadOnlyBase


class HostUser(ReadOnlyBase):
    """SELECT-ONLY. Never insert/update/delete through this class - see module docstring."""

    __tablename__ = "users"
    __table_args__ = {"schema": "host"}

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(length=320), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(nullable=True)
    dark_mode: Mapped[bool] = mapped_column(Boolean, default=False)
