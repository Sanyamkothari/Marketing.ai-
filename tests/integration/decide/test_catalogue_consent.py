"""Integration tests for Plan J M99: Offer and channel catalogue, channel-aware consent.

Acceptance tests:
1. A customer opted out of SMS but in for email, with both planned, is treated only by email.
2. A customer whose only planned channel is SMS and who opted out of SMS is not treated, is counted in
   channel_counts, and is not suppressed in scores.csv.
3. Existing consent records apply to all channels; the platform migration tests pass.
4. An unknown action id fails config load.
5. Every existing suppression test passes unchanged.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlmodel import Session, create_engine

from engine.config import resolve_config
from engine.decide.treat_list import (
    TREAT_LIST_CSV,
    build_treat_list,
)
from engine.privacy.consent import ConsentLedger, ConsentStatus
from engine.privacy.tables import ConsentRecordRow
from engine.stages.actions import (
    ACTION_COLUMN,
    SUPPRESSED_ACTION,
    SUPPRESSED_REASON_COLUMN,
    apply_actions,
)
from engine.stages.export import _suppression_counts
from engine.storage import LocalStorage, run_key

pytestmark = pytest.mark.integration


def test_customer_opted_out_sms_in_email_treated_only_by_email(tmp_path: Path) -> None:
    """A customer opted out of SMS but in for email, with both planned, is treated only by email."""
    # Write catalogue with multi-channel or email action
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """actions:
  - action_id: winback_multi
    label: "Win-back offer"
    channel: "sms,email"
    offer_cost: 10.0
    contact_cost: 0.15
""",
        encoding="utf-8",
    )

    storage = LocalStorage(tmp_path)
    run_id = "r_m99_test_001"

    # Frame with 2 customers:
    # row 0: opted out of SMS, in for email -> treated by email
    # row 1: in for SMS, in for email -> treated by sms (or first contactable)
    df = pd.DataFrame(
        {
            "customer_id": ["C-1", "C-2"],
            "propensity": [0.95, 0.90],
            "sms_opt_in": ["false", "true"],
            "email_opt_in": ["true", "true"],
            "phone_valid": ["true", "true"],
            "email_valid": ["true", "true"],
        }
    )

    # Use case config with channels defined
    import shutil

    from engine.config import config_root

    real_root = config_root()
    shutil.copy(real_root / "engine.yaml", tmp_path / "engine.yaml")
    uc_dir = tmp_path / "use_cases"
    uc_dir.mkdir(parents=True)
    base_uc = (real_root / "use_cases" / "win_back_campaign.yaml").read_text(encoding="utf-8")
    modified_uc = base_uc + """
actions:
  bands:
    - name: High
      min_score: 0.80
      action: "Act now"
      action_id: winback_multi
    - name: Medium
      min_score: 0.50
      action: "Monitor"
    - name: Low
      min_score: 0.00
      action: "No action"
  suppression:
    suppress_opted_out: true
    opt_out_column: null
    channels:
      sms:
        consent_column: sms_opt_in
        contactable_column: phone_valid
      email:
        consent_column: email_opt_in
        contactable_column: email_valid
"""
    (uc_dir / "win_back_campaign.yaml").write_text(modified_uc, encoding="utf-8")
    resolved = resolve_config("win_back_campaign", {}, root=tmp_path)
    config = resolved.config

    scored = apply_actions(df, config, run_id=run_id, primary_key="customer_id")
    # In scores.csv, neither row is suppressed because channel opt-out does not suppress in scores.csv
    assert scored[SUPPRESSED_REASON_COLUMN].isna().all()
    assert (scored[ACTION_COLUMN] != SUPPRESSED_ACTION).all()

    from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
    from engine.stages.export import SCORES_PARQUET
    from tests.fixtures.decide.treat_runs import _record

    record = _record(
        run_id, kind="propensity", rows=len(df), primary_key="customer_id", upload_id="u_fixture"
    )
    storage.write_model(run_key(run_id, RUN_FILENAME), record)
    storage.write_model(run_key(run_id, RUN_CONFIG_FILENAME), resolved)
    storage.write_bytes(run_key(run_id, SCORES_PARQUET), scored.to_parquet())

    # Build treat list
    summary = build_treat_list(storage, run_id, config_root=tmp_path)
    assert summary.treat_rows == 2
    assert summary.catalogue_sha256 is not None
    treat_csv = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_CSV))), dtype=str)

    # Customer C-1: opted out of SMS, in for email -> treated by email!
    c1 = treat_csv[treat_csv["customer_id"] == "C-1"].iloc[0]
    assert c1["treat"] == "1"
    assert "email" in c1["contactable_channels"]
    assert "sms" not in c1["contactable_channels"]
    assert c1["channel"] == "email"


def test_customer_only_planned_sms_opted_out_not_treated_and_in_channel_counts_not_suppressed_in_scores(
    tmp_path: Path,
) -> None:
    """A customer whose only planned channel is SMS and who opted out of SMS is not treated, is counted in
    channel_counts, and is not suppressed in scores.csv."""
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """actions:
  - action_id: winback_sms_only
    label: "Win-back SMS offer"
    channel: sms
    offer_cost: 10.0
    contact_cost: 0.15
""",
        encoding="utf-8",
    )

    storage = LocalStorage(tmp_path)
    run_id = "r_m99_test_002"

    df = pd.DataFrame(
        {
            "customer_id": ["C-SMS-OPTED-OUT"],
            "propensity": [0.95],
            "sms_opt_in": ["false"],
            "phone_valid": ["true"],
        }
    )

    import shutil

    from engine.config import config_root

    real_root = config_root()
    shutil.copy(real_root / "engine.yaml", tmp_path / "engine.yaml")
    uc_dir = tmp_path / "use_cases"
    uc_dir.mkdir(parents=True)
    base_uc = (real_root / "use_cases" / "win_back_campaign.yaml").read_text(encoding="utf-8")
    modified_uc = base_uc + """
actions:
  bands:
    - name: High
      min_score: 0.80
      action: "Act now"
      action_id: winback_sms_only
    - name: Medium
      min_score: 0.50
      action: "Monitor"
    - name: Low
      min_score: 0.00
      action: "No action"
  suppression:
    suppress_opted_out: true
    opt_out_column: null
    channels:
      sms:
        consent_column: sms_opt_in
        contactable_column: phone_valid
"""
    (uc_dir / "win_back_campaign.yaml").write_text(modified_uc, encoding="utf-8")
    resolved = resolve_config("win_back_campaign", {}, root=tmp_path)
    config = resolved.config

    scored = apply_actions(df, config, run_id=run_id, primary_key="customer_id")

    # In scores.csv: NOT suppressed!
    assert scored[SUPPRESSED_REASON_COLUMN].iloc[0] is None or pd.isna(
        scored[SUPPRESSED_REASON_COLUMN].iloc[0]
    )
    assert scored[ACTION_COLUMN].iloc[0] != SUPPRESSED_ACTION

    # In _suppression_counts: channel_counts carries {"consent_denied_sms": 1}
    counts = _suppression_counts(scored, config)
    assert any(
        c.channel_counts is not None and c.channel_counts.get("consent_denied_sms") == 1 for c in counts
    )

    # In treat_list: treat = 0!
    from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
    from engine.stages.export import SCORES_PARQUET
    from tests.fixtures.decide.treat_runs import _record

    record = _record(
        run_id, kind="propensity", rows=len(df), primary_key="customer_id", upload_id="u_fixture"
    )
    storage.write_model(run_key(run_id, RUN_FILENAME), record)
    storage.write_model(run_key(run_id, RUN_CONFIG_FILENAME), resolved)
    storage.write_bytes(run_key(run_id, SCORES_PARQUET), scored.to_parquet())

    summary = build_treat_list(storage, run_id, config_root=tmp_path)
    assert summary.treat_rows == 0

    treat_csv = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_CSV))), dtype=str)
    row = treat_csv.iloc[0]
    assert row["treat"] == "0"


def test_existing_consent_records_apply_to_all_channels(tmp_path: Path) -> None:
    """Existing consent records (channel null) apply to all channels; ConsentLedger.classify gains channel=None."""
    db_path = tmp_path / "platform.db"
    engine = create_engine(f"sqlite:///{db_path}")

    ledger = ConsentLedger(engine, salt="test-salt-12345678", create=True)
    at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    # 1. Existing record with channel=None (granted)
    ledger.record(
        client_id="client_1",
        principal_id="user_all",
        purpose="marketing_communication",
        status=ConsentStatus.GRANTED,
        source="test",
        recorded_at=at,
    )

    # Queries with channel=None, channel="sms", channel="email" should all see valid consent!
    c_none = ledger.classify("client_1", "marketing_communication", ["user_all"], at, channel=None)
    assert "user_all" in c_none.valid

    c_sms = ledger.classify("client_1", "marketing_communication", ["user_all"], at, channel="sms")
    assert "user_all" in c_sms.valid

    c_email = ledger.classify("client_1", "marketing_communication", ["user_all"], at, channel="email")
    assert "user_all" in c_email.valid

    # 2. Add channel-specific withdrawal for SMS at later timestamp
    at_later = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        row = ConsentRecordRow(
            client_id="client_1",
            principal_hash=ledger.hash("user_all"),
            purpose="marketing_communication",
            status=ConsentStatus.WITHDRAWN.value,
            source="sms_optout",
            recorded_at=at_later,
            created_at=at_later,
            channel="sms",
        )
        session.add(row)
        session.commit()

    # Now for SMS: withdrawn! For Email: still valid!
    c_sms_after = ledger.classify(
        "client_1", "marketing_communication", ["user_all"], at_later, channel="sms"
    )
    assert "user_all" in c_sms_after.withdrawn

    c_email_after = ledger.classify(
        "client_1", "marketing_communication", ["user_all"], at_later, channel="email"
    )
    assert "user_all" in c_email_after.valid
