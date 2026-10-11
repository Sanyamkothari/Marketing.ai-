"""Plan M M184 (DEC-1614 (c)): `compliance.gate.strict` - the gate withholds what it could not check.

The pre-send gate (M134) fails open where it has nothing to check against: with no catalogue no message is
checked, an action with no `message_text` (or no `dlt_template_text` on a DLT channel) is "not checked" but
allowed, and with no URL whitelist links are not checked. `strict` (default false: today's behaviour, byte for
byte) withholds those contacts with `COMPLIANCE_MESSAGE_NOT_CHECKED`. A check the configuration deliberately
does not apply - a channel the region does not require a DLT template on, a channel the whitelist does not cover -
is not "missing", and stays allowed.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
from engine.compliance.config import load_compliance_config
from engine.compliance.handoff import gate_treat_list
from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache
from engine.platform_db import sqlite_engine
from engine.storage import LocalStorage, run_key
from tests.unit.compliance.test_gate_rules import CATALOGUE, WHITELIST

RUN = "r_20261011_0c184003"
KEY = "customer_id"
NOT_CHECKED = "COMPLIANCE_MESSAGE_NOT_CHECKED"
ROWS: dict[str, tuple[str | None, bool]] = {
    # key: (catalogue action, contactable on SMS)
    "C1": ("offer_ok", True),  # template and links checked on SMS
    "C2": ("offer_unregistered", True),  # no dlt_template_text and no message_text: nothing checked
    "C3": (None, True),  # no catalogue action: no known words
    "C4": ("offer_ok", False),  # not on SMS; email needs no DLT template and the whitelist covers SMS only
}


def _root(
    tmp_path: Path, config_root: Path, *, strict: bool | None, catalogue: bool = True, whitelist: bool = True
) -> Path:
    target = tmp_path / "configs"
    (target / "decide").mkdir(parents=True)
    (target / "regions").mkdir()
    (target / "compliance").mkdir()
    shutil.copy(config_root / "regions" / "in.yaml", target / "regions" / "in.yaml")
    if catalogue:
        (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    if whitelist:
        (target / "compliance" / "url_whitelist.yaml").write_text(WHITELIST, encoding="utf-8")
    if strict is not None:
        (target / "compliance" / "compliance.yaml").write_text(
            f"compliance:\n  gate:\n    strict: {str(strict).lower()}\n", encoding="utf-8"
        )
    clear_catalogue_cache()
    return target


def _gate(root: Path, tmp_path: Path) -> tuple[Any, dict[tuple[str, str], list[str]]]:
    storage = LocalStorage(tmp_path / "data")
    keys = list(ROWS)
    flags = pd.DataFrame(
        {KEY: keys, "contactable_sms": [ROWS[k][1] for k in keys], "contactable_email": True}
    )
    buffer = io.BytesIO()
    flags.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "channel_contactability.parquet"), buffer.getvalue())
    outcome = gate_treat_list(
        storage,
        run_id=RUN,
        use_case_id="win-back-campaign",
        client_id=None,
        key_frame=pd.DataFrame({KEY: keys}).astype("object"),
        treat=np.ones(len(keys), dtype=bool),
        channel=np.full(len(keys), "sms", dtype=object),
        actions=np.asarray([ROWS[k][0] for k in keys], dtype=object),
        planned_by_action={"offer_ok": ("sms", "email"), "offer_unregistered": ("sms",)},
        configured=("sms",),
        stamp_sha256=catalogue_sha256(root),
        config_root=root,
        ledger=DecisionLedger(
            sqlite_engine(tmp_path / "ledger.db"),
            signer=signer_for_data_dir(tmp_path / "keys"),
            policy_version="p" * 64,
        ),
    )
    decisions = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(RUN, "compliance_decisions.parquet"))))
    reasons = {(row[KEY], row["channel"]): list(row["reasons"]) for row in decisions.to_dict("records")}
    return outcome, reasons


def test_strict_is_off_by_default_and_shipped_off(config_root: Path, tmp_path: Path) -> None:
    assert load_compliance_config(config_root).gate.strict is False
    assert load_compliance_config(tmp_path).gate.strict is False


@pytest.mark.parametrize("strict", [None, False])
def test_off_the_gate_lets_what_it_could_not_check_through_as_before(
    tmp_path: Path, config_root: Path, strict: bool | None
) -> None:
    outcome, reasons = _gate(_root(tmp_path, config_root, strict=strict), tmp_path)
    assert list(outcome.treat) == [True, True, True, True]
    assert list(outcome.channel) == ["sms", "sms", "sms", "email"]
    assert not any(NOT_CHECKED in found for found in reasons.values())
    assert "strict" not in outcome.summary.rules, "the rules, and so rule_version, are what they were"


def test_strict_withholds_words_that_were_not_checked_and_keeps_what_was_or_need_not_be(
    tmp_path: Path, config_root: Path
) -> None:
    outcome, reasons = _gate(_root(tmp_path, config_root, strict=True), tmp_path)
    assert list(outcome.treat) == [True, False, False, True]
    assert list(outcome.channel) == ["sms", None, None, "email"]
    assert reasons[("C1", "sms")] == []
    assert reasons[("C2", "sms")] == [NOT_CHECKED]
    assert reasons[("C3", "sms")] == [NOT_CHECKED]
    # C4: refused on SMS for consent; email needs no DLT template and the whitelist does not cover it on purpose.
    assert reasons[("C4", "sms")] == ["COMPLIANCE_CONSENT"]
    assert reasons[("C4", "email")] == []
    assert outcome.summary.rules["strict"] is True
    assert outcome.summary.withheld == {NOT_CHECKED: 2}
    assert outcome.summary.withheld_rows == 2


def test_strict_with_no_url_whitelist_withholds_every_message_whose_links_were_not_checked(
    tmp_path: Path, config_root: Path
) -> None:
    lenient, _ = _gate(_root(tmp_path / "off", config_root, strict=False, whitelist=False), tmp_path / "off")
    assert list(lenient.treat) == [True, True, True, True]
    strict, reasons = _gate(
        _root(tmp_path / "on", config_root, strict=True, whitelist=False), tmp_path / "on"
    )
    assert strict.summary.rules["url_whitelist"] is None
    assert not strict.treat.any()
    assert reasons[("C1", "sms")] == [NOT_CHECKED]
    assert reasons[("C4", "email")] == [NOT_CHECKED]


def test_strict_with_no_catalogue_withholds_every_contact(tmp_path: Path, config_root: Path) -> None:
    lenient, _ = _gate(_root(tmp_path / "off", config_root, strict=False, catalogue=False), tmp_path / "off")
    assert lenient.treat.all(), "no catalogue: nothing is checked and everyone goes, as before"
    strict, reasons = _gate(
        _root(tmp_path / "on", config_root, strict=True, catalogue=False), tmp_path / "on"
    )
    assert not strict.treat.any()
    assert all(NOT_CHECKED in found for found in reasons.values()), "every planned channel, every customer"
