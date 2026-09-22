"""The metadata schema as Phase 4a leaves it: `model_version` and `run`.

Revision ID: 0001
Revises:
Created: 2026-09-22

WHY there is a `0001` at all, rather than a `create_all` on first boot: `create_all` adds a missing
table and never a missing column, so the first column added after a deployment goes live would be
missing in production, silently, until a query failed. That is survivable for the SQLite file on a
laptop, which can be deleted, and not for a client's model and run history (DEC-341).

WHY it creates the tables as they stand rather than replaying Phase 1: there was no Postgres before
this revision. A deployment starts empty, so `0001` is a description of the destination, not a
history of how the SQLite file got there.

WHY every timestamp is `DateTime(timezone=True)`: SQLModel compiles a plain `datetime` to
`TIMESTAMP WITHOUT TIME ZONE`, and a server whose time zone is not UTC then stores its own wall
clock in place of the instant - measured at +05:30 on a server set to `Asia/Kolkata` (DEC-339).
`compare_type=True` in `env.py` is what keeps a later revision from quietly undoing this.

WHY `run` exists next to `model_version`: it indexes `runs/*/run.json` so a list page does not have
to read every run in the bucket. It is an index and not the record - `run.json` is still that - and
`scripts/reconcile_runs.py` can rebuild every row in it from storage (DEC-342).

This revision is reversible: `downgrade` drops both tables. Dropping `run` costs an index that can
be rebuilt; dropping `model_version` destroys the registry, which storage cannot rebuild, so a
downgrade against a live deployment is a decision to be taken deliberately and not a rollback step.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# SQLModel compiles a `str` field to `AutoString`, not to `sa.String`. Autogenerate writes it
# as `sqlmodel.sql.sqltypes.AutoString()`, which mypy cannot follow through the package, so it
# is imported from the module that defines it instead.
from sqlmodel.sql.sqltypes import AutoString

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STR = AutoString
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create `model_version` and `run`."""
    op.create_table(
        "model_version",
        sa.Column("model_id", _STR(), nullable=False),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("run_id", _STR(), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("status", _STR(), nullable=False),
        sa.Column("metric", _STR(), nullable=False),
        sa.Column("metric_label", _STR(), nullable=False),
        sa.Column("test_score", sa.Float(), nullable=False),
        sa.Column("validation_score", sa.Float(), nullable=True),
        sa.Column("model_display_name", _STR(), nullable=False),
        sa.Column("schema_key", _STR(), nullable=False),
        sa.Column("run_config_key", _STR(), nullable=False),
        sa.Column("predictor_key", _STR(), nullable=False),
        sa.Column("drift_baseline_key", _STR(), nullable=True),
        sa.Column("artefact_keys_json", _STR(), nullable=False),
        sa.Column("approved_by", _STR(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", _STR(), nullable=True),
        sa.Column("promotion_note", _STR(), nullable=True),
        sa.Column("previous_champion_id", _STR(), nullable=True),
        sa.Column("improvement_pct", sa.Float(), nullable=True),
        sa.Column("measured_against_champion_id", _STR(), nullable=True),
        sa.Column("engine_version", _STR(), nullable=False),
        sa.Column("autogluon_version", _STR(), nullable=False),
        sa.PrimaryKeyConstraint("model_id"),
        # One version number per use case. The champion rule reads version order, so a duplicate
        # would make "the next version" ambiguous rather than merely untidy.
        sa.UniqueConstraint("use_case_id", "version", name="uq_use_case_version"),
    )
    op.create_index(op.f("ix_model_version_run_id"), "model_version", ["run_id"], unique=False)
    op.create_index(op.f("ix_model_version_status"), "model_version", ["status"], unique=False)
    op.create_index(op.f("ix_model_version_use_case_id"), "model_version", ["use_case_id"], unique=False)
    op.create_table(
        "run",
        sa.Column("run_id", _STR(), nullable=False),
        sa.Column("use_case_id", _STR(), nullable=False),
        sa.Column("use_case_name", _STR(), nullable=False),
        sa.Column("mode", _STR(), nullable=False),
        sa.Column("state", _STR(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upload_id", _STR(), nullable=False),
        sa.Column("file_name", _STR(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("model_version_id", _STR(), nullable=True),
        sa.Column("headline_metric", _STR(), nullable=True),
        sa.Column("headline_score", sa.Float(), nullable=True),
        sa.Column("champion", sa.Boolean(), nullable=False),
        sa.Column("error_code", _STR(), nullable=True),
        sa.Column("engine_version", _STR(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("compute_backend", _STR(), nullable=True),
        sa.Column("estimated_usd", sa.Float(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    # The list page's four questions: which use case, which mode, what state, and newest first.
    op.create_index(op.f("ix_run_created_at"), "run", ["created_at"], unique=False)
    op.create_index(op.f("ix_run_mode"), "run", ["mode"], unique=False)
    op.create_index(op.f("ix_run_model_version_id"), "run", ["model_version_id"], unique=False)
    op.create_index(op.f("ix_run_state"), "run", ["state"], unique=False)
    op.create_index(op.f("ix_run_use_case_id"), "run", ["use_case_id"], unique=False)


def downgrade() -> None:
    """Drop both tables. See the module docstring: this is not a no-cost rollback."""
    op.drop_index(op.f("ix_run_use_case_id"), table_name="run")
    op.drop_index(op.f("ix_run_state"), table_name="run")
    op.drop_index(op.f("ix_run_model_version_id"), table_name="run")
    op.drop_index(op.f("ix_run_mode"), table_name="run")
    op.drop_index(op.f("ix_run_created_at"), table_name="run")
    op.drop_table("run")
    op.drop_index(op.f("ix_model_version_use_case_id"), table_name="model_version")
    op.drop_index(op.f("ix_model_version_status"), table_name="model_version")
    op.drop_index(op.f("ix_model_version_run_id"), table_name="model_version")
    op.drop_table("model_version")
