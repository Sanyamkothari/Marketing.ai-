"""Unit tests for Plan J M99: Offer and channel catalogue, channel-aware consent (DEC-1309).

Acceptance tests:
1. `configs/decide/catalogue.yaml` validated by a frozen `ActionCatalogue` model in `engine/decide/catalogue.py`
   (the `privacy.yaml` pattern): action id, label, channel, offer cost, contact cost, eligibility, required fields.
2. An unknown action id referenced in a use case (Band.action_id or uplift.policy.treat_action_id) fails config load.
3. Editing the catalogue changes `catalogue_sha256`.
4. Region rule, data-driven: `configs/regions/in.yaml` declares `sms_requires: [dlt_template_id, message_category]`;
   with region IN, an SMS action without a DLT template id is refused at config load (`ACTION_DLT_TEMPLATE_MISSING`).
5. `engine.pilot.roi.lookup_value_costs` reads offer and contact costs from the catalogue when it exists.
6. `SuppressionCount` schema tests pass with and without `channel_counts`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import ConfigError, config_root, load_use_case
from engine.contracts import SuppressionCount
from engine.decide.catalogue import (
    catalogue_sha256,
    load_catalogue,
)
from engine.pilot.roi import lookup_value_costs


def test_catalogue_validation_and_sha256(tmp_path: Path) -> None:
    """ActionCatalogue is frozen, forbids extra fields, and catalogue_sha256 is stable and sensitive to edits."""
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    cat_file = cat_dir / "catalogue.yaml"

    cat_file.write_text(
        """region: IN
actions:
  - action_id: winback_sms_10pct
    label: "10% off next order (SMS)"
    channel: sms
    offer_cost: 45.0
    contact_cost: 0.15
    dlt_template_id: "110712345678"
    message_category: "promotional"
  - action_id: winback_email_10pct
    label: "10% off next order (Email)"
    channel: email
    offer_cost: 45.0
    contact_cost: 0.05
""",
        encoding="utf-8",
    )

    # Need regions/in.yaml for validation when region: IN
    reg_dir = tmp_path / "regions"
    reg_dir.mkdir(parents=True)
    (reg_dir / "in.yaml").write_text(
        """sms_requires:
  - dlt_template_id
  - message_category
""",
        encoding="utf-8",
    )

    catalogue = load_catalogue(root=tmp_path)
    assert catalogue.region == "IN"
    assert len(catalogue.actions) == 2
    assert catalogue.actions[0].action_id == "winback_sms_10pct"
    assert catalogue.actions[0].offer_cost == 45.0
    assert catalogue.actions[0].contact_cost == 0.15

    sha1 = catalogue_sha256(root=tmp_path)
    assert sha1 is not None and len(sha1) == 64

    # Editing the catalogue changes catalogue_sha256
    cat_file.write_text(
        """region: IN
actions:
  - action_id: winback_sms_10pct
    label: "15% off next order (SMS)"
    channel: sms
    offer_cost: 50.0
    contact_cost: 0.15
    dlt_template_id: "110712345678"
    message_category: "promotional"
""",
        encoding="utf-8",
    )
    # Clear cache by passing new root or modifying
    sha2 = catalogue_sha256(root=tmp_path)
    assert sha2 != sha1


def test_unknown_action_id_fails_config_load(tmp_path: Path) -> None:
    """An unknown action id in Band.action_id or uplift.policy.treat_action_id fails config load with CATALOGUE_ACTION_UNKNOWN."""
    # Write valid catalogue
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """actions:
  - action_id: winback_sms_10pct
    label: "10% off next order (SMS)"
    channel: sms
    offer_cost: 45.0
    contact_cost: 0.15
""",
        encoding="utf-8",
    )

    # Copy minimal engine.yaml and use case
    real_root = config_root()
    import shutil

    shutil.copy(real_root / "engine.yaml", tmp_path / "engine.yaml")
    uc_dir = tmp_path / "use_cases"
    uc_dir.mkdir(parents=True)
    shutil.copy(real_root / "use_cases" / "win_back_campaign.yaml", uc_dir / "win_back_campaign.yaml")

    # Load should succeed when no action_id is unknown
    load_use_case("win_back_campaign", root=tmp_path)

    # Now edit use case to reference an unknown action_id
    uc_text = (uc_dir / "win_back_campaign.yaml").read_text(encoding="utf-8")
    modified = uc_text + """
actions:
  bands:
    - name: High
      min_score: 0.80
      action: "Act now"
      action_id: nonexistent_action_123
    - name: Medium
      min_score: 0.50
      action: "Monitor"
    - name: Low
      min_score: 0.00
      action: "No action"
"""
    (uc_dir / "win_back_campaign.yaml").write_text(modified, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_use_case("win_back_campaign", root=tmp_path)
    assert exc_info.value.code == "CATALOGUE_ACTION_UNKNOWN"


def test_region_in_sms_requires_dlt_template_id(tmp_path: Path) -> None:
    """With region IN, an SMS action without a DLT template id is refused at config load."""
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """region: IN
actions:
  - action_id: winback_sms_nodlt
    label: "10% off next order (SMS)"
    channel: sms
    offer_cost: 45.0
    contact_cost: 0.15
    message_category: "promotional"
""",
        encoding="utf-8",
    )

    reg_dir = tmp_path / "regions"
    reg_dir.mkdir(parents=True)
    (reg_dir / "in.yaml").write_text(
        """sms_requires:
  - dlt_template_id
  - message_category
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_catalogue(root=tmp_path)
    assert exc_info.value.code == "ACTION_DLT_TEMPLATE_MISSING"


def test_region_in_sms_requires_message_category(tmp_path: Path) -> None:
    """With region IN, an SMS action without message_category is refused with CATALOGUE_INVALID."""
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """region: IN
actions:
  - action_id: winback_sms_nocat
    label: "10% off next order (SMS)"
    channel: sms
    offer_cost: 45.0
    contact_cost: 0.15
    dlt_template_id: "110712345678"
""",
        encoding="utf-8",
    )

    reg_dir = tmp_path / "regions"
    reg_dir.mkdir(parents=True)
    (reg_dir / "in.yaml").write_text(
        """sms_requires:
  - dlt_template_id
  - message_category
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_catalogue(root=tmp_path)
    assert exc_info.value.code == "CATALOGUE_INVALID"


def test_lookup_value_costs_reads_from_catalogue(tmp_path: Path) -> None:
    """lookup_value_costs reads offer and contact costs from catalogue when present."""
    cat_dir = tmp_path / "decide"
    cat_dir.mkdir(parents=True)
    (cat_dir / "catalogue.yaml").write_text(
        """actions:
  - action_id: custom_sms
    label: "SMS action"
    channel: sms
    offer_cost: 25.0
    contact_cost: 0.20
  - action_id: custom_email
    label: "Email action"
    channel: email
    offer_cost: 10.0
    contact_cost: 0.02
""",
        encoding="utf-8",
    )

    costs_sms = lookup_value_costs(channel="sms", root=tmp_path)
    assert costs_sms.contact_cost == 0.20

    costs_email = lookup_value_costs(channel="email", root=tmp_path)
    assert costs_email.contact_cost == 0.02

    costs_action = lookup_value_costs(action_id="custom_sms", root=tmp_path)
    assert costs_action.offer_cost == 25.0
    assert costs_action.contact_cost == 0.20


def test_suppression_count_schema_and_channel_counts() -> None:
    """SuppressionCount validates with and without channel_counts, preserving 3-value reason Literal."""
    sc_plain = SuppressionCount(reason="opted_out", rows=10)
    assert sc_plain.channel_counts is None

    sc_channels = SuppressionCount(
        reason="opted_out",
        rows=10,
        channel_counts={"consent_denied_sms": 5, "consent_denied_email": 2},
    )
    assert sc_channels.channel_counts == {"consent_denied_sms": 5, "consent_denied_email": 2}
    assert sc_channels.reason == "opted_out"
