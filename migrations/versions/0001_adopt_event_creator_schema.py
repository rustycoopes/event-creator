"""Adopt the event_creator schema tables created by organize-me's R1 migration

Revision ID: 0001_adopt_event_creator_schema
Revises:
Create Date: 2026-07-12

This is the baseline of event-creator's own independent Alembic history (Slice R6). Its
upgrade()/downgrade() are deliberately no-ops: the event_creator.* tables already exist in the
shared database, created by organize-me's migration
d4e5f6a7b8c9_schema_separation_host_event_creator (which moved them out of the monolith via
ALTER TABLE ... SET SCHEMA, not a rewrite). Applying this revision only records "already caught up
to this baseline" in event-creator's own version table (see migrations/env.py's
VERSION_TABLE_SCHEMA = "event_creator") — it must never contain real DDL for these tables, since
running it against a database where they don't already exist would leave every model in
app/models/ with no backing table.

Future migrations building on top of this one are real and should use normal op.* calls.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_adopt_event_creator_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
