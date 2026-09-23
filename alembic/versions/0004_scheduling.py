"""Phase 4b M49: `schedule`, `schedule_firing` and `alert`.

Revision ID: 0004
Revises: 0003
Created: 2026-09-23

WHY these three tables and no others: a schedule, each attempt at it and each alert are the only
M49 state that is not an artefact. Drift-check reports and outcome reports are aggregates that belong
beside the run they describe (`runs/<id>/drift_checks/`, `runs/<id>/outcome_report.json`), and an
uploaded outcomes file is never stored at all (DEC-771).

WHY `schedule_firing` has a unique index on `(schedule_id, scheduled_for)` (DEC-763): a firing claims
its due slot by inserting that row before it does any work, so an EventBridge retry, a second replica
or a sweep racing the target cannot run one slot twice - the second insert fails and that process
does nothing. A manual or catch-up firing has no slot (null), which a unique index never collides on.

WHY no column holds a data value: `parameters_json` holds ids (a recipe, a dataset, a model version),
`result_code`/`error_code` hold short tokens, and `alert.message` is business language written by
the engine (DEC-770) - never a customer id, a feature value or a score.

Every timestamp is `DateTime(timezone=True)`, for DEC-339's reason. The models are
`engine/scheduling/schedules.py` and `engine/scheduling/alerts.py`; this revision creates exactly
what they declare. Reversible: `downgrade` drops all three, which loses the firing history and the
alert history - a decision, not a rollback step.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the three M49 tables and their indexes."""
    op.create_table(
        "schedule",
        sa.Column("schedule_id", _STR(), nullable=False),
        sa.Column("client_id", _STR(), nullable=True),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("cron", _STR(), nullable=False),
        sa.Column("timezone", _STR(), nullable=False),
        sa.Column("preset", _STR(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("managed_by", _STR(), nullable=True),
        sa.Column("created_by", _STR(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("next_due_at", _TS, nullable=True),
        sa.Column("last_fired_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("schedule_id"),
    )
    op.create_index("ix_schedule_scope", "schedule", ["client_id", "use_case_id"], unique=False)
    op.create_table(
        "schedule_firing",
        sa.Column("firing_id", _STR(), nullable=False),
        sa.Column("schedule_id", _STR(), nullable=False),
        sa.Column("client_id", _STR(), nullable=True),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("trigger", _STR(), nullable=False),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("scheduled_for", _TS, nullable=True),
        sa.Column("run_id", _STR(), nullable=True),
        sa.Column("dataset_id", _STR(), nullable=True),
        sa.Column("result_code", _STR(), nullable=True),
        sa.Column("error_code", _STR(), nullable=True),
        sa.Column("flagged_models_json", sa.Text(), nullable=False),
        sa.Column("fired_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("firing_id"),
    )
    op.create_index(op.f("ix_schedule_firing_schedule_id"), "schedule_firing", ["schedule_id"], unique=False)
    op.create_index("ix_schedule_firing_status", "schedule_firing", ["status"], unique=False)
    op.create_index(
        "ux_schedule_firing_slot", "schedule_firing", ["schedule_id", "scheduled_for"], unique=True
    )
    op.create_table(
        "alert",
        sa.Column("alert_id", _STR(), nullable=False),
        sa.Column("kind", _STR(), nullable=False),
        sa.Column("severity", _STR(), nullable=False),
        sa.Column("client_id", _STR(), nullable=True),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("schedule_id", _STR(), nullable=True),
        sa.Column("run_id", _STR(), nullable=True),
        sa.Column("model_id", _STR(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("acknowledged_at", _TS, nullable=True),
        sa.Column("acknowledged_by", _STR(), nullable=True),
        sa.PrimaryKeyConstraint("alert_id"),
    )
    op.create_index("ix_alert_scope", "alert", ["client_id", "use_case_id"], unique=False)
    op.create_index("ix_alert_created_at", "alert", ["created_at"], unique=False)


def downgrade() -> None:
    """Drop the three tables and, with them, the firing and alert history."""
    op.drop_index("ix_alert_created_at", table_name="alert")
    op.drop_index("ix_alert_scope", table_name="alert")
    op.drop_table("alert")
    op.drop_index("ux_schedule_firing_slot", table_name="schedule_firing")
    op.drop_index("ix_schedule_firing_status", table_name="schedule_firing")
    op.drop_index(op.f("ix_schedule_firing_schedule_id"), table_name="schedule_firing")
    op.drop_table("schedule_firing")
    op.drop_index("ix_schedule_scope", table_name="schedule")
    op.drop_table("schedule")
