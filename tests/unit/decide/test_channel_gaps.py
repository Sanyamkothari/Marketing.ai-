"""Plan J M100 part B: the M99 gaps DEC-1309 left open, each fixed with a test (DEC-1310 (q) on).

* **The all-channel gate and channel-only consent.** A customer whose consent ledger records are all
  channel-specific had no all-channel record, so the scoring gate's all-channel question suppressed them
  as `consent_false` even when they had granted consent on a channel the use case sends by. When
  `actions.suppression.channels` is configured the gate now passes a customer with a valid grant on at
  least one configured channel (and no all-channel withdrawal newer than it); per-channel
  contactability then decides the channel. Without channels, nothing changes.
* **A channel consent or contactable column is never a model input.** The columns named in
  `actions.suppression.channels` are left out of the features of a training run, through the same
  `prepare.exclude_columns` path a user's own exclusions take (`engine.decide.channel_columns`), without
  an edit to a Phase 1 stage file.

The third gap (the catalogue stamped from another root than the one checked) needs an API started with
`create_app(config_root=...)`: `tests/integration/decide/test_offer_choice_run.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine

from engine.config import UseCaseConfig, load_use_case
from engine.privacy.consent import ConsentGate, ConsentLedger, apply_consent_gate
from engine.privacy.contracts import ConsentStatus

SALT = "channel-gaps-salt-0001"
CLIENT = "acme"
PURPOSE = "marketing_communication"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
CHANNELS: dict[str, Any] = {
    "sms": {"consent_column": "sms_opt_in"},
    "email": {"consent_column": "email_opt_in", "contactable_column": "email_valid"},
}


def _with_channels(config: UseCaseConfig, channels: dict[str, Any]) -> UseCaseConfig:
    suppression = config.actions.suppression.model_validate(
        {**config.actions.suppression.model_dump(), "channels": channels}
    )
    actions = config.actions.model_copy(update={"suppression": suppression})
    return config.model_copy(update={"actions": actions})


@pytest.fixture()
def gate(tmp_path: Path) -> ConsentGate:
    ledger = ConsentLedger(create_engine(f"sqlite:///{tmp_path / 'platform.db'}"), salt=SALT)

    def record(principal: str, status: ConsentStatus, days_ago: int, channel: str | None) -> None:
        ledger.record(
            client_id=CLIENT,
            principal_id=principal,
            purpose=PURPOSE,
            status=status,
            source="test",
            recorded_at=NOW - timedelta(days=days_ago),
            channel=channel,
        )

    granted, withdrawn = ConsentStatus.GRANTED, ConsentStatus.WITHDRAWN
    record("sms_only", granted, 10, "sms")
    record("email_only", granted, 10, "email")
    record("granted_then_withdrawn_all", granted, 10, "sms")
    record("granted_then_withdrawn_all", withdrawn, 5, None)
    record("withdrawn_all_then_granted", withdrawn, 10, None)
    record("withdrawn_all_then_granted", granted, 5, "sms")
    record("all_channels", granted, 10, None)
    record("other_channel_only", granted, 10, "whatsapp")
    record("sms_withdrawn_email_granted", granted, 10, "email")
    record("sms_withdrawn_email_granted", withdrawn, 5, "sms")
    return ConsentGate(ledger=ledger, client_id=CLIENT, purpose=PURPOSE)


PRINCIPALS = (
    "sms_only",
    "email_only",
    "granted_then_withdrawn_all",
    "withdrawn_all_then_granted",
    "all_channels",
    "other_channel_only",
    "sms_withdrawn_email_granted",
    "no_record",
)


def _passed(config: UseCaseConfig, gate: ConsentGate) -> tuple[dict[str, bool], Any]:
    frame = pd.DataFrame({"customer_id": list(PRINCIPALS), "x": range(len(PRINCIPALS))})
    gated, gated_config, report = apply_consent_gate(
        frame, config, gate, primary_key="customer_id", run_id="r_20261008_0e200001", at=NOW
    )
    column = gated_config.governance.consent_column
    assert column is not None
    return dict(zip(PRINCIPALS, (bool(value) for value in gated[column]), strict=True)), report


def test_with_channels_a_valid_grant_on_one_configured_channel_passes_the_gate(gate: ConsentGate) -> None:
    config = _with_channels(load_use_case("win-back-campaign"), CHANNELS)
    passed, report = _passed(config, gate)
    assert passed == {
        "sms_only": True,
        "email_only": True,
        "granted_then_withdrawn_all": False,  # the all-channel withdrawal is newer than the grant
        "withdrawn_all_then_granted": True,  # the channel grant is newer than the withdrawal
        "all_channels": True,
        "other_channel_only": False,  # whatsapp is not a channel this use case sends by
        "sms_withdrawn_email_granted": True,
        "no_record": False,
    }
    assert report.excluded_total == 3
    # The report's reasons count only the customers the gate left out.
    assert report.excluded_no_consent + report.excluded_withdrawn + report.excluded_expired == 3
    assert report.excluded_withdrawn == 1


def test_only_the_configured_channels_count(gate: ConsentGate) -> None:
    config = _with_channels(load_use_case("win-back-campaign"), {"email": CHANNELS["email"]})
    passed, _ = _passed(config, gate)
    assert passed["email_only"] and passed["sms_withdrawn_email_granted"]
    assert not passed["sms_only"] and not passed["withdrawn_all_then_granted"]


def test_without_channels_the_gate_is_what_it_was(gate: ConsentGate) -> None:
    """Behaviour unchanged: only an all-channel record answers the all-channel question."""
    passed, report = _passed(load_use_case("win-back-campaign"), gate)
    assert passed == {name: name == "all_channels" for name in PRINCIPALS}
    assert report.excluded_total == len(PRINCIPALS) - 1


# ---------------------------------------------------------------------------
# A channel consent or contactable column is never a model input
# ---------------------------------------------------------------------------
TRAIN_CHANNELS: dict[str, Any] = {
    "sms": {"consent_column": "sms_opt_in"},
    "push": {"consent_column": "push_opt_in", "contactable_column": "push_reachable"},
}
"""Channel columns whose names no personal-data rule drops on its own (an `email_*` column is)."""
RESERVED = {"sms_opt_in", "push_opt_in", "push_reachable"}


def _training_frame() -> pd.DataFrame:
    rows = 400
    return pd.DataFrame(
        {
            "customer_id": [f"C{i:05d}" for i in range(rows)],
            "months_since_churn": [i % 13 for i in range(rows)],
            "avg_monthly_spend": [100.0 + (i % 17) * 10 for i in range(rows)],
            "sms_opt_in": ["true" if i % 3 else "false" for i in range(rows)],
            "push_opt_in": ["yes" if i % 4 else "no" for i in range(rows)],
            "push_reachable": [i % 5 != 0 for i in range(rows)],
            "reactivated_90d": [int(i % 3 == 0) for i in range(rows)],
        }
    )


def test_the_configured_channel_columns_are_left_out_of_the_features() -> None:
    from engine.decide.channel_columns import reserve_channel_columns
    from engine.stages.prepare import prepare_rows

    plain = load_use_case("win-back-campaign")
    frame = _training_frame()
    _, before = prepare_rows(frame, plain, primary_key=("customer_id",), target="reactivated_90d")
    assert set(before.feature_columns) >= RESERVED, "a model input before"

    reserved = reserve_channel_columns(_with_channels(plain, TRAIN_CHANNELS))
    _, after = prepare_rows(frame, reserved, primary_key=("customer_id",), target="reactivated_90d")
    assert not RESERVED & set(after.feature_columns)
    assert {"months_since_churn", "avg_monthly_spend"} <= set(after.feature_columns)
    # A run without channels keeps its configuration as it was: the same object.
    assert reserve_channel_columns(plain) is plain


def test_a_column_the_configuration_already_uses_is_not_excluded_twice() -> None:
    """The use case's consent column (`marketing_opt_in`) is a reserved column already; naming it as a
    channel's consent column must not exclude it too, or the consent filter could not read it."""
    from engine.decide.channel_columns import reserve_channel_columns

    loaded = load_use_case("win-back-campaign")
    consent = "marketing_opt_in"
    plain = loaded.model_copy(
        update={"governance": loaded.governance.model_copy(update={"consent_column": consent})}
    )
    reserved = reserve_channel_columns(_with_channels(plain, {"sms": {"consent_column": consent}}))
    assert consent not in reserved.prepare.exclude_columns


def test_every_training_run_reads_the_reserved_configuration(tmp_path: Path) -> None:
    """The seam: `engine.pipeline` installs it on the train flow's stage table, which the propensity and
    the uplift training flows share, so both prepare (and register) with the channel columns excluded."""
    from engine.config import RunMode, resolve_config
    from engine.jobs import CancelToken, NullJobRunner
    from engine.pipeline import Pipeline, StageContext, _TrainFlow
    from engine.registry import LocalModelRegistry
    from engine.storage import LocalStorage

    assert getattr(_TrainFlow, "_channel_columns_reserved", False)
    storage = LocalStorage(tmp_path)
    registry = LocalModelRegistry(tmp_path / "registry.db")
    resolved = resolve_config("win-back-campaign")
    config = _with_channels(resolved.config, TRAIN_CHANNELS)
    ctx = StageContext(
        run_id="r_20261008_0e200002",
        mode=RunMode.TRAIN,
        config=config,
        resolved=resolved.model_copy(update={"config": config}),
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=["customer_id"],
        target="reactivated_90d",
        upload_key="uploads/u_0e2000000001/source.csv",
        model_version_id=None,
    )
    flow = _TrainFlow(Pipeline(storage, registry, NullJobRunner()), ctx)
    flow._bodies()
    assert set(flow._ctx.config.prepare.exclude_columns) >= RESERVED
