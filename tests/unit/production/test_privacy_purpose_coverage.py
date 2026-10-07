"""Every use case that contacts customers has a consent purpose (Plan J M91).

A contacting use case missing from `configs/privacy.yaml` `use_case_purposes` is never gated by the
consent ledger, so a customer who withdrew consent could still be contacted. The loader refuses such
a configuration with `CONSENT_PURPOSE_MISSING` instead of leaving the use case silently ungated.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from engine.config import ConfigError, list_use_case_ids, load_use_case
from engine.privacy.config import load_privacy_config

NEWLY_COVERED = {
    "retail-win-back": "marketing_communication",
    "bank-term-deposit": "marketing_communication",
    "insurance-cross-sell": "marketing_communication",
    "card-default-propensity": "account_servicing",
}


def _contacting(config_root: Path) -> list[str]:
    return [
        u for u in list_use_case_ids(config_root) if load_use_case(u, config_root).actions.contacts_customers
    ]


def _copy_root(config_root: Path, tmp_path: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(config_root, target)
    return target


def _drop_mapping(root: Path, use_case: str) -> None:
    path = root / "privacy.yaml"
    text = path.read_text(encoding="utf-8")
    stripped = re.sub(rf"^[ \t]*{re.escape(use_case)}:[^\n]*\n", "", text, flags=re.MULTILINE)
    assert stripped != text, f"{use_case} is not in privacy.yaml"
    path.write_text(stripped, encoding="utf-8")


def test_every_shipped_contacting_use_case_has_a_purpose(config_root: Path) -> None:
    privacy = load_privacy_config(config_root)
    contacting = _contacting(config_root)
    assert set(NEWLY_COVERED) <= set(
        contacting
    ), "the four use cases this fix is about no longer contact customers"
    missing = [u for u in contacting if privacy.purpose_for(u) is None]
    assert missing == [], f"contacting use cases without a consent purpose: {missing}"


@pytest.mark.parametrize(("use_case", "purpose"), sorted(NEWLY_COVERED.items()))
def test_the_four_use_cases_map_to_their_purpose(config_root: Path, use_case: str, purpose: str) -> None:
    assert load_privacy_config(config_root).purpose_for(use_case) == purpose


def test_operational_use_cases_stay_ungated(config_root: Path) -> None:
    privacy = load_privacy_config(config_root)
    for use_case in ("fault-prediction", "order-fulfillment", "ai-onboarding-assistant"):
        assert privacy.purpose_for(use_case) is None


@pytest.mark.parametrize("use_case", [*sorted(NEWLY_COVERED), "telco-churn", "payment-propensity"])
def test_removing_a_contacting_use_cases_purpose_fails_at_load(
    config_root: Path, tmp_path: Path, use_case: str
) -> None:
    root = _copy_root(config_root, tmp_path)
    _drop_mapping(root, use_case)
    with pytest.raises(ConfigError) as excinfo:
        load_privacy_config(root)
    assert excinfo.value.code == "CONSENT_PURPOSE_MISSING"
    assert use_case in excinfo.value.message


def _set_contacts_customers(root: Path, file_name: str, value: str) -> None:
    path = root / "use_cases" / file_name
    text = path.read_text(encoding="utf-8")
    assert "contacts_customers" not in text, f"{file_name} already sets contacts_customers"
    path.write_text(
        text.replace("\nactions:\n", f"\nactions:\n  contacts_customers: {value}\n", 1), encoding="utf-8"
    )


@pytest.mark.parametrize("value", ["false", '"false"', '"no"'])
def test_a_use_case_that_does_not_contact_customers_needs_no_purpose(
    config_root: Path, tmp_path: Path, value: str
) -> None:
    """The exemption itself: telco-churn made non-contacting loads without its mapping. A quoted
    `"false"` / `"no"` is read as `ActionsConfig` reads it (pydantic's lax bool), not as truthy text."""
    root = _copy_root(config_root, tmp_path)
    _set_contacts_customers(root, "telco_churn.yaml", value)
    assert load_use_case("telco-churn", root).actions.contacts_customers is False
    _drop_mapping(root, "telco-churn")
    assert load_privacy_config(root).purpose_for("telco-churn") is None


def test_a_new_use_case_with_no_actions_block_contacts_customers_by_default(
    config_root: Path, tmp_path: Path
) -> None:
    """A use case that says nothing inherits the engine default (`contacts_customers: true`), so it
    needs a purpose like any other contacting use case."""
    root = _copy_root(config_root, tmp_path)
    source = (root / "use_cases" / "telco_churn.yaml").read_text(encoding="utf-8")
    head, _, _ = source.partition("\nactions:\n")
    (root / "use_cases" / "telco_churn_copy.yaml").write_text(
        head.replace("id: telco-churn", "id: telco-churn-copy", 1) + "\n", encoding="utf-8"
    )
    assert "telco-churn-copy" in list_use_case_ids(root)
    assert load_use_case("telco-churn-copy", root).actions.contacts_customers is True
    with pytest.raises(ConfigError) as excinfo:
        load_privacy_config(root)
    assert excinfo.value.code == "CONSENT_PURPOSE_MISSING"
    assert "telco-churn-copy" in excinfo.value.message
