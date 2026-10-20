"""Alembic environment. rlp_platform.db passes the connection in; there is no alembic.ini."""

from __future__ import annotations

from alembic import context

from rlp_platform.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
