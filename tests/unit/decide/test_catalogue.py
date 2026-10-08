"""Unit tests for Plan J M99: the offer and channel catalogue (DEC-1309).

1. `configs/decide/catalogue.yaml` validated by a frozen `ActionCatalogue` model in `engine/decide/catalogue.py`
   (the `privacy.yaml` pattern): action id, label, channels, offer cost, contact cost, eligibility, required fields.
2. An unknown action id referenced in a use case (Band.action_id or uplift.policy.treat_action_id) fails config load.
3. Editing the catalogue changes `catalogue_sha256` (and the cached catalogue).
4. Region rule, data-driven: `configs/regions/in.yaml` declares `sms_requires: [dlt_template_id, message_category]`;
   with region IN, an SMS action without a DLT template id is refused at config load (`ACTION_DLT_TEMPLATE_MISSING`).
5. `engine.pilot.roi.lookup_value_costs`: offer cost only from an action id; a channel gives the contact cost only.
6. No catalogue ships: with none, config loads and costs are M97's exactly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from engine.config import ConfigError, config_root, list_use_case_ids, load_use_case, load_yaml
from engine.contracts import SuppressionCount
from engine.decide.catalogue import (
    CATALOGUE_FILENAME,
    catalogue_or_none,
    catalogue_sha256,
    load_catalogue,
)
from engine.pilot.roi import VALUE_CONFIG, ValueCosts, lookup_value_costs

REGION_IN = """sms_requires:
  - dlt_template_id
  - message_category
"""


def _catalogue(root: Path, text: str, *, region: bool = False) -> Path:
    (root / "decide").mkdir(parents=True, exist_ok=True)
    path = root / CATALOGUE_FILENAME
    path.write_text(text, encoding="utf-8")
    if region:
        (root / "regions").mkdir(parents=True, exist_ok=True)
        (root / "regions" / "in.yaml").write_text(REGION_IN, encoding="utf-8")
    return path


def _use_case_root(root: Path, *, bands: list[dict[str, object]] | None = None) -> None:
    real = config_root()
    shutil.copy(real / "engine.yaml", root / "engine.yaml")
    (root / "use_cases").mkdir(parents=True, exist_ok=True)
    document = yaml.safe_load((real / "use_cases" / "win_back_campaign.yaml").read_text(encoding="utf-8"))
    if bands is not None:
        document["actions"]["bands"] = bands
    (root / "use_cases" / "win_back_campaign.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def test_catalogue_validation_and_sha256(tmp_path: Path) -> None:
    """ActionCatalogue is frozen, forbids extra fields, and catalogue_sha256 is stable and sensitive to edits."""
    path = _catalogue(
        tmp_path,
        """region: IN
actions:
  - action_id: winback_sms
    label: "Offer (SMS)"
    channels: [sms]
    offer_cost: 45.0
    contact_cost: 0.15
    dlt_template_id: "1107000000000000001"
    message_category: "promotional"
  - action_id: winback_any
    label: "Offer (email, else SMS)"
    channels: [Email, sms]
    offer_cost: 45.0
    contact_cost: 0.05
    dlt_template_id: "1107000000000000002"
    message_category: "promotional"
""",
        region=True,
    )
    catalogue = load_catalogue(root=tmp_path)
    assert catalogue.region == "IN"
    assert catalogue.action_ids == ("winback_sms", "winback_any")
    assert catalogue.actions[1].channels == ("email", "sms")
    assert catalogue.actions[0].offer_cost == 45.0 and catalogue.actions[0].contact_cost == 0.15
    assert catalogue.channel_contact_costs() == {
        "sms": 0.15,
        "email": 0.05,
    }, "the first action listing a channel"

    sha1 = catalogue_sha256(root=tmp_path)
    assert sha1 is not None and len(sha1) == 64
    path.write_text(path.read_text(encoding="utf-8").replace("45.0", "50.0"), encoding="utf-8")
    assert catalogue_sha256(root=tmp_path) != sha1
    assert load_catalogue(root=tmp_path).actions[0].offer_cost == 50.0, "an edited catalogue is read again"


def test_a_single_channel_key_is_an_alias(tmp_path: Path) -> None:
    _catalogue(tmp_path, "actions:\n  - {action_id: a, label: A, channel: SMS}\n")
    assert load_catalogue(root=tmp_path).actions[0].channels == ("sms",)


@pytest.mark.parametrize(
    "item",
    [
        "{action_id: a, label: A, channel: 'sms,email'}",
        "{action_id: a, label: A, channel: sms, channels: [email]}",
        "{action_id: a, label: A, channels: sms}",
        "{action_id: a, label: A, channels: []}",
        "{action_id: a, label: A, channels: [sms, SMS]}",
        "{action_id: a, label: A, channels: ['e mail']}",
        "{action_id: a, label: A}",
    ],
)
def test_channels_are_a_list_of_names(tmp_path: Path, item: str) -> None:
    _catalogue(tmp_path, f"actions:\n  - {item}\n")
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=tmp_path)
    assert caught.value.code == "CATALOGUE_INVALID"


def test_unknown_action_id_fails_config_load(tmp_path: Path) -> None:
    """An unknown action id in Band.action_id fails config load with CATALOGUE_ACTION_UNKNOWN."""
    _catalogue(tmp_path, "actions:\n  - {action_id: winback_sms, label: SMS offer, channels: [sms]}\n")
    _use_case_root(tmp_path)
    load_use_case("win_back_campaign", root=tmp_path)  # no action id: loads
    bands: list[dict[str, object]] = [
        {"name": "High", "min_score": 0.8, "action": "Act now", "action_id": "winback_sms"},
        {"name": "Medium", "min_score": 0.5, "action": "Monitor"},
        {"name": "Low", "min_score": 0.0, "action": "Hold"},
    ]
    _use_case_root(tmp_path, bands=bands)
    assert load_use_case("win_back_campaign", root=tmp_path).actions.bands[0].action_id == "winback_sms"
    bands[0]["action_id"] = "nonexistent_action_123"
    _use_case_root(tmp_path, bands=bands)
    with pytest.raises(ConfigError) as caught:
        load_use_case("win_back_campaign", root=tmp_path)
    assert caught.value.code == "CATALOGUE_ACTION_UNKNOWN"
    assert caught.value.path == "actions.bands[0].action_id"


def test_region_in_sms_requires_dlt_template_id(tmp_path: Path) -> None:
    """With region IN, an SMS action without a DLT template id is refused at config load."""
    _catalogue(
        tmp_path,
        """region: IN
actions:
  - {action_id: nodlt, label: SMS offer, channels: [sms], message_category: promotional}
""",
        region=True,
    )
    _use_case_root(tmp_path)
    with pytest.raises(ConfigError) as caught:
        load_use_case("win_back_campaign", root=tmp_path)
    assert caught.value.code == "ACTION_DLT_TEMPLATE_MISSING"
    assert caught.value.path == "actions[0].dlt_template_id"


def test_region_in_sms_requires_message_category(tmp_path: Path) -> None:
    _catalogue(
        tmp_path,
        """region: IN
actions:
  - {action_id: nocat, label: SMS offer, channels: [email, sms], dlt_template_id: "1107000000000000001"}
""",
        region=True,
    )
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=tmp_path)
    assert caught.value.code == "CATALOGUE_INVALID"


def test_the_shipped_example_is_refused_until_its_placeholders_are_filled(tmp_path: Path) -> None:
    """Copied unchanged, `catalogue.example.yaml` fails region validation; filled in, it loads."""
    example = (config_root() / "decide" / "catalogue.example.yaml").read_text(encoding="utf-8")
    assert "<your DLT template id>" in example
    shutil.copytree(config_root() / "regions", tmp_path / "regions")
    _catalogue(tmp_path, example)
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=tmp_path)
    assert caught.value.code == "ACTION_DLT_TEMPLATE_MISSING"
    _catalogue(tmp_path, example.replace("<your DLT template id>", "1107000000000000001"))
    assert len(load_catalogue(root=tmp_path).actions) == 2


def test_no_catalogue_ships_and_with_none_nothing_changes() -> None:
    """The repository's config root has no catalogue: no id is checked, no cost is overridden."""
    root = config_root()
    assert not (root / CATALOGUE_FILENAME).exists()
    assert catalogue_or_none() is None and catalogue_sha256() is None
    block = load_yaml(root / VALUE_CONFIG).get("value") or {}
    assert lookup_value_costs() == ValueCosts.model_validate(block), "M97's costs exactly"
    for use_case_id in list_use_case_ids():
        dumped = load_use_case(use_case_id).model_dump(mode="json")
        assert "channels" not in dumped["actions"]["suppression"], use_case_id
        assert all("action_id" not in band for band in dumped["actions"]["bands"]), use_case_id
        assert "treat_action_id" not in dumped["uplift"]["policy"], use_case_id


def test_lookup_value_costs_offer_cost_comes_only_from_an_action(tmp_path: Path) -> None:
    _catalogue(
        tmp_path,
        """actions:
  - {action_id: custom_sms, label: SMS action, channels: [sms], offer_cost: 25.0, contact_cost: 0.20}
  - {action_id: custom_email, label: Email action, channels: [email], offer_cost: 10.0, contact_cost: 0.02}
""",
    )
    plain = lookup_value_costs(root=tmp_path)
    by_sms = lookup_value_costs(channel="sms", root=tmp_path)
    assert by_sms.contact_cost == 0.20
    assert by_sms.offer_cost == plain.offer_cost, "a channel never brings an action's offer cost"
    assert lookup_value_costs(channel="email", root=tmp_path).contact_cost == 0.02
    by_action = lookup_value_costs(action_id="custom_sms", root=tmp_path)
    assert (by_action.offer_cost, by_action.contact_cost) == (25.0, 0.20)
    both = lookup_value_costs(channel="email", action_id="custom_sms", root=tmp_path)
    assert (both.offer_cost, both.contact_cost) == (25.0, 0.02)
    with pytest.raises(ConfigError) as caught:
        lookup_value_costs(action_id="nope", root=tmp_path)
    assert caught.value.code == "CATALOGUE_ACTION_UNKNOWN"


def test_suppression_count_schema_and_channel_counts() -> None:
    """SuppressionCount validates with and without channel_counts; without, the file is as before M99."""
    plain = SuppressionCount(reason="opted_out", rows=10)
    assert plain.channel_counts is None
    assert "channel_counts" not in plain.model_dump_json()
    counted = SuppressionCount(reason="opted_out", rows=10, channel_counts={"sms": 5, "email": 2})
    assert counted.channel_counts == {"sms": 5, "email": 2}
    assert SuppressionCount.model_validate_json(counted.model_dump_json()) == counted
