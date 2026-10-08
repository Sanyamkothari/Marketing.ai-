"""Per-channel contactability's pure parts (Plan J M99, DEC-1309).

The end-to-end behaviour - a real run writes `channel_contactability.parquet`, the treat list joins it - is
`tests/integration/decide/test_channel_consent_real_run.py`. Here: the mask arithmetic, where the counts go
in `scoring_summary.json` (never on an invented entry), and the configuration's channel names.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.config import ConfigError, UseCaseConfig, load_use_case
from engine.decide.contactability import CHANNEL_COUNTS_ATTR, contactability_masks
from engine.stages.actions import _truthy, apply_actions, truthy
from engine.stages.export import _suppression_counts

USE_CASE = "win-back-campaign"


def _with_suppression(suppression: dict[str, Any]) -> UseCaseConfig:
    base = load_use_case(USE_CASE)
    document = base.model_dump(mode="json", by_alias=True)
    document["actions"]["suppression"].update(suppression)
    return UseCaseConfig.model_validate(document)


CHANNELS = {
    "sms": {"consent_column": "sms_ok"},
    "email": {"consent_column": "email_ok", "contactable_column": "email_valid"},
}


def test_truthy_is_public_and_the_old_name_is_the_same_function() -> None:
    assert _truthy is truthy
    assert truthy(pd.Series(["yes", "no", None, 1, 0])).tolist() == [True, False, False, True, False]


def test_masks_read_the_upload_by_key_and_and_the_ledger() -> None:
    config = _with_suppression({"channels": CHANNELS})
    banded = pd.DataFrame({"customer_id": ["c", "a", "b"]})  # another order than the upload
    uploaded = pd.DataFrame(
        {
            "customer_id": ["a", "b", "c"],
            "sms_ok": ["true", "false", None],
            "email_ok": ["yes", "yes", "yes"],
            "email_valid": [1, 0, 1],
        }
    )
    ledger = {"sms": np.array([True, True, True]), "email": np.array([False, True, True])}
    masks, missing = contactability_masks(
        banded, uploaded, config, row_key="customer_id", ledger_valid=ledger
    )
    assert list(masks) == ["sms", "email"]
    assert masks["sms"].tolist() == [False, True, False]  # c: null is not a consent; b: opted out
    assert masks["email"].tolist() == [False, True, False]  # c: the ledger; b: not a valid address
    assert missing == (("sms", ()), ("email", ()))


def test_a_missing_column_is_skipped_and_named() -> None:
    config = _with_suppression({"channels": CHANNELS})
    frame = pd.DataFrame({"customer_id": ["a", "b"], "email_ok": ["no", "yes"]})
    masks, missing = contactability_masks(frame, frame, config, row_key="customer_id")
    assert masks["sms"].tolist() == [True, True]
    assert masks["email"].tolist() == [False, True]
    assert missing == (("sms", ("sms_ok",)), ("email", ("email_valid",)))


def test_per_entity_a_customer_closed_at_one_snapshot_is_closed_at_all() -> None:
    config = _with_suppression({"channels": {"sms": {"consent_column": "sms_ok"}}})
    frame = pd.DataFrame(
        {"row": ["a|1", "a|2", "b|1", "b|2"], "customer": ["a", "a", "b", "b"], "sms_ok": [1, 0, 1, 1]}
    )
    masks, _ = contactability_masks(frame, frame, config, row_key="row", entity_key="customer")
    assert masks["sms"].tolist() == [False, False, True, True]


def _banded(config: UseCaseConfig, frame: pd.DataFrame) -> pd.DataFrame:
    return apply_actions(frame, config, run_id="r_20261008_0c0000aa", primary_key="customer_id")


def _frame(**columns: list[object]) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": ["a", "b", "c"], "winback_prob": [0.9, 0.6, 0.1], **columns})


def test_channel_counts_ride_on_the_opted_out_entry() -> None:
    config = _with_suppression({"channels": CHANNELS, "recently_contacted_column": None})
    banded = _banded(config, _frame(marketing_opt_in=[True, True, False]))
    banded.attrs[CHANNEL_COUNTS_ATTR] = {"sms": 2, "email": 0}
    counts = _suppression_counts(banded, config)
    assert [(item.reason, item.rows, item.channel_counts) for item in counts] == [
        ("opted_out", 1, {"sms": 2, "email": 0})
    ]


def test_channel_counts_ride_on_consent_false_when_opted_out_did_not_run() -> None:
    config = _with_suppression({"channels": CHANNELS, "opt_out_column": None}).model_copy(deep=True)
    governance = config.governance.model_copy(update={"consent_column": "consent"})
    config = config.model_copy(update={"governance": governance})
    banded = _banded(config, _frame(consent=[True, False, True], last_contacted_at=[None, None, None]))
    banded.attrs[CHANNEL_COUNTS_ATTR] = {"sms": 1, "email": 1}
    counts = {item.reason: item.channel_counts for item in _suppression_counts(banded, config)}
    assert counts == {"consent_false": {"sms": 1, "email": 1}, "recently_contacted": None}


@pytest.mark.parametrize("columns", [{}, {"last_contacted_at": [None, None, None]}])
def test_no_entry_is_invented_for_a_rule_that_did_not_run(columns: dict[str, list[object]]) -> None:
    """Neither opted_out nor consent_false ran: the counts stay out of the summary (they are in the
    contactability file), and no entry appears for a rule that did not run."""
    config = _with_suppression({"channels": CHANNELS})
    banded = _banded(config, _frame(**columns))
    banded.attrs[CHANNEL_COUNTS_ATTR] = {"sms": 3, "email": 0}
    counts = _suppression_counts(banded, config)
    assert [item.reason for item in counts] == (["recently_contacted"] if columns else [])
    assert all(item.channel_counts is None for item in counts)


def test_without_contactability_the_counts_are_as_before() -> None:
    config = load_use_case(USE_CASE)
    banded = _banded(config, _frame(marketing_opt_in=[True, False, True]))
    counts = _suppression_counts(banded, config)
    assert [(item.reason, item.rows, item.channel_counts) for item in counts] == [("opted_out", 1, None)]
    assert "channel_counts" not in counts[0].model_dump_json()


def test_channel_names_are_normalised_and_checked() -> None:
    config = _with_suppression({"channels": {" SMS ": {"consent_column": "sms_ok"}}})
    assert list(config.actions.suppression.channels) == ["sms"]
    for bad in ({"e mail": {}}, {"sms": {}, "SMS": {}}):
        with pytest.raises((ConfigError, ValueError)):
            _with_suppression({"channels": bad})
