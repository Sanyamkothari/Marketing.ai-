"""Phase 4b M46/M47: `platform_user`, `auth_session` and the append-only `audit_events`.

Revision ID: 0002
Revises: 0001
Created: 2026-09-23

WHY these tables live in the registry's database on a deployment: DEC-706. Locally they are in
`platform.db`, created by the stores themselves through `engine.platform_db.create_tables`; on
Postgres the schema is Alembic's, exactly as DEC-341 decided for Phase 4a's tables, so the first
column added after go-live arrives by migration and not by luck.

WHY every timestamp is `DateTime(timezone=True)`: DEC-339, for the same reason as `0001` - and here
it matters twice, because a session's expiry compared against the wrong wall clock either signs
people out five and a half hours early or keeps them signed in five and a half hours late.

WHY `audit_events` has a trigger (DEC-714). The application never updates or deletes an audit row -
the protocol has no method that could - but an audit trail whose immutability rests on the
application is only as good as every future line of the application. On Postgres a `BEFORE UPDATE
OR DELETE` row trigger and a `BEFORE TRUNCATE` statement trigger raise, so changing history needs
somebody with DDL rights to drop the trigger first: a deliberate act that itself leaves a trace in
the database's own logs, rather than an `UPDATE` anyone with the application's credentials could
run. SQLite gets the equivalent `RAISE(ABORT)` triggers (also created by `SqlAuditLog` for a
`platform.db` that never saw Alembic). A trigger is not a substitute for the S3 Object Lock export
(DEC-715): the owner of the database can still drop it, and the export is what survives that.

This revision is reversible: `downgrade` drops the triggers, the function and the three tables.
Dropping `audit_events` destroys the audit trail, so a downgrade against a live deployment is a
decision to take only after an export, never a rollback step.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)

_PG_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (DEC-714)';
END;
$$ LANGUAGE plpgsql
"""

_PG_GUARDS: tuple[str, ...] = (
    "CREATE TRIGGER audit_events_no_update_delete BEFORE UPDATE OR DELETE ON audit_events "
    "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()",
    "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
    "FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()",
)

_SQLITE_GUARDS: tuple[str, ...] = (
    "CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events "
    "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
)


def upgrade() -> None:
    """Create the three tables and the append-only guard on `audit_events`."""
    op.create_table(
        "platform_user",
        sa.Column("user_id", _STR(), nullable=False),
        sa.Column("username", _STR(), nullable=False),
        sa.Column("username_key", _STR(), nullable=False),
        sa.Column("display_name", _STR(), nullable=False),
        sa.Column("roles", _STR(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("password_hash", _STR(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("created_by", _STR(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    # Case-insensitive uniqueness: the key is the case-folded username (engine/access/users.py).
    op.create_index(op.f("ix_platform_user_username_key"), "platform_user", ["username_key"], unique=True)
    op.create_table(
        "auth_session",
        sa.Column("token_hash", _STR(), nullable=False),
        sa.Column("user_id", _STR(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"], unique=False)
    op.create_table(
        "audit_events",
        sa.Column("event_id", _STR(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", _STR(), nullable=False),
        sa.Column("actor_kind", _STR(), nullable=False),
        sa.Column("action", _STR(), nullable=False),
        sa.Column("object_type", _STR(), nullable=True),
        sa.Column("object_id", _STR(), nullable=True),
        sa.Column("before_hash", _STR(), nullable=True),
        sa.Column("after_hash", _STR(), nullable=True),
        sa.Column("request_id", _STR(), nullable=True),
        sa.Column("outcome", _STR(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    # The audit viewer's filters: when, who, what, on which object, and one request's events.
    op.create_index(op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"], unique=False)
    op.create_index(op.f("ix_audit_events_actor_id"), "audit_events", ["actor_id"], unique=False)
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(op.f("ix_audit_events_object_id"), "audit_events", ["object_id"], unique=False)
    op.create_index(op.f("ix_audit_events_request_id"), "audit_events", ["request_id"], unique=False)
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(_PG_GUARD_FUNCTION)
        for statement in _PG_GUARDS:
            op.execute(statement)
    elif dialect == "sqlite":
        for statement in _SQLITE_GUARDS:
            op.execute(statement)


def downgrade() -> None:
    """Drop the guard and the three tables. See the module docstring: export the trail first."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    op.drop_index(op.f("ix_audit_events_request_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_object_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_occurred_at"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_index(op.f("ix_platform_user_username_key"), table_name="platform_user")
    op.drop_table("platform_user")
