"""The pre-send compliance gate on a real scoring run and its treat list (Plan K M134, DEC-1405).

The run is the product's own: the uplift model `test_channel_consent_real_run` trains, scored through
`Pipeline.run_score` over a file with per-channel consent columns, with a consent ledger in the store's
`platform.db` (an all-channel grant for everyone, SMS withdrawals for some) to which WhatsApp grants are added for
every third customer. The use case's treat action is a catalogue action planned on WhatsApp, then SMS, then
email, under the India region file, with a registered DLT template, an approved message and a URL whitelist.
Nothing below writes a run artefact by hand: the treat list is built through `build_treat_list` from what the
run wrote, under a config root whose `compliance.gate.enabled` is on.

* With the gate on: `compliance_decisions.parquet` holds one row per customer the list would contact and per
  planned channel; WhatsApp goes only to the customers with a WhatsApp grant of their own; the others go on the
  next planned channel the gate allows (SMS where they are contactable on it, else email), else they are not
  treated - never `treat = 1` with no channel; the summary counts them per reason; the file's sha256 is in a
  signed `evidence.label` entry that `verify_ledger` accepts with the data folder's key.
* A catalogue edited after the run: every contact is withheld (`COMPLIANCE_CATALOGUE_CHANGED`), fail closed.
* With the gate off (the switch false, or no `configs/compliance/` at all): the treat list is byte for byte the
  list without the gate, and no compliance file is written.

On the unchanged code (no `engine.compliance`) the gate-on list sends every treated customer on WhatsApp, the
first planned channel, and writes no decision file: these tests fail there.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import hashlib
import io
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from engine.agentic.ledger import EVIDENCE_LABEL, verify_ledger
from engine.decide.treat_list import TREAT_LIST_CSV, TREAT_LIST_SUMMARY_FILENAME, build_treat_list
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.consent import ConsentLedger
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.integration.decide.test_channel_consent_real_run import (
    CHANNELS,
    CLIENT,
    PRIMARY_KEY,
    PRIVACY_SALT,
    PURPOSE,
    Scored,
    World,
    _masks,
    _score,
    world,
)

pytestmark = pytest.mark.integration

RUN_GATE = "r_20261010_0c134001"
DECISIONS = "compliance_decisions.parquet"
DECISIONS_JSON = "compliance_decisions.json"
TEMPLATE = "Dear customer, come back for Rs {#numeric#} off. Shop at {#url#} - Acme"
MESSAGE = "Dear customer, come back for Rs 200 off. Shop at https://shop.example.in/back - Acme"
CATALOGUE = f"""\
# A test catalogue: one win-back action, WhatsApp first, then SMS, then email, under India's rules.
region: IN
actions:
  - action_id: winback_multi
    label: Win-back offer
    channels: [whatsapp, sms, email]
    offer_cost: 20.0
    contact_cost: 0.15
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{TEMPLATE}"
    message_text: "{MESSAGE}"
"""
WHITELIST = "channels: [sms]\nurls: [shop.example.in]\n"
GATE_ON = "compliance:\n  gate:\n    enabled: true\n"
GATE_OFF = "compliance:\n  gate:\n    enabled: false\n"


def _root(source: Path, target: Path, *, gate: str | None, catalogue: str = CATALOGUE) -> Path:
    shutil.copytree(source, target)
    (target / "decide" / "catalogue.yaml").write_text(catalogue, encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = CHANNELS
    document.setdefault("uplift", {}).setdefault("policy", {})["treat_action_id"] = "winback_multi"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    shutil.rmtree(target / "compliance")
    if gate is not None:
        (target / "compliance").mkdir()
        (target / "compliance" / "compliance.yaml").write_text(gate, encoding="utf-8")
        (target / "compliance" / "url_whitelist.yaml").write_text(WHITELIST, encoding="utf-8")
    return target


def _plant_whatsapp(data_dir: Path, frame: pd.DataFrame) -> frozenset[str]:
    """A WhatsApp grant of its own for every third customer: their opt-in record."""
    keys = [str(key) for position, key in enumerate(frame[PRIMARY_KEY]) if position % 3 == 0]
    at = (utc_now() - timedelta(days=5)).date().isoformat()
    lines = ["principal_id,purpose,status,recorded_at,channel"]
    lines += [f"{key},{PURPOSE},granted,{at},whatsapp" for key in keys]
    ledger = ConsentLedger(sqlite_engine(data_dir / PLATFORM_DB_FILENAME), salt=PRIVACY_SALT)
    report = ledger.import_csv("\n".join(lines) + "\n", client_id=CLIENT, privacy=load_privacy_config())
    assert report.imported and not report.errors, report.errors
    return frozenset(keys)


@dataclass(frozen=True)
class Gated:
    world: World
    run: Scored
    opted_in: frozenset[str]
    snapshot: Path
    """The data folder right after the run, before any treat list: each scenario builds in its own copy."""


@pytest.fixture(scope="module")
def gated(world: World, config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Gated:
    base = tmp_path_factory.mktemp("compliance-gate")
    roots = {
        "gate_on": _root(config_root, base / "gate_on", gate=GATE_ON),
        "gate_off": _root(config_root, base / "gate_off", gate=GATE_OFF),
        "no_gate_file": _root(config_root, base / "no_gate_file", gate=None),
        "edited": _root(
            config_root, base / "edited", gate=GATE_ON, catalogue=CATALOGUE.replace("Rs 200", "Rs 250")
        ),
    }
    here = replace(world, roots={**world.roots, **roots})
    opted_in = _plant_whatsapp(here.data_dir, here.frame)
    run = _score(here, RUN_GATE, "gate_on", client=CLIENT)
    snapshot = base / "snapshot"
    shutil.copytree(here.data_dir, snapshot)
    return Gated(here, run, opted_in, snapshot)


@dataclass(frozen=True)
class Built:
    data_dir: Path
    out: pd.DataFrame
    summary: Any

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)


def _build(gated: Gated, name: str, variant: str) -> Built:
    """The treat list of the run in a copy of the data folder, under one config root, as a deployment builds it."""
    data_dir = gated.snapshot.parent / name
    shutil.copytree(gated.snapshot, data_dir)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        patch.setenv("MARKETING_AI_CONFIG_DIR", str(gated.world.roots[variant]))
        storage = LocalStorage(data_dir)
        summary = build_treat_list(storage, RUN_GATE, config_root=gated.world.roots[variant])
    text = storage.read_bytes(run_key(RUN_GATE, TREAT_LIST_CSV)).decode("utf-8")
    return Built(data_dir, pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False), summary)


@pytest.fixture(scope="module")
def on(gated: Gated) -> Built:
    return _build(gated, "built_on", "gate_on")


def _decisions(built: Built) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(built.storage.read_bytes(run_key(RUN_GATE, DECISIONS))))


def _expected(gated: Gated, built: Built) -> dict[str, np.ndarray]:
    m = _masks(gated.world, gated.run, built.out)
    whatsapp = built.out[PRIMARY_KEY].isin(gated.opted_in).to_numpy(dtype=bool)
    reachable = whatsapp | m.sms_ok | m.email_ok
    channel = np.where(whatsapp, "whatsapp", np.where(m.sms_ok, "sms", np.where(m.email_ok, "email", "")))
    return {
        "base": m.base,
        "treat": m.base & reachable,
        "channel": np.where(m.base & reachable, channel, ""),
        "withheld": m.base & ~reachable,
        "rerouted": m.base & ~whatsapp & (m.sms_ok | m.email_ok),
    }


# ---------------------------------------------------------------------------
# The gate on
# ---------------------------------------------------------------------------
def test_whatsapp_goes_only_to_the_opted_in_and_the_others_take_the_next_channel_allowed(
    gated: Gated, on: Built
) -> None:
    expected = _expected(gated, on)
    treat = (on.out["treat"] == "1").to_numpy()
    assert (treat == expected["treat"]).all()
    assert (on.out["channel"].to_numpy(dtype=object) == expected["channel"]).all()
    assert (
        expected["rerouted"].sum() > 100
        and (expected["base"] & (expected["channel"] == "whatsapp")).sum() > 50
    )
    assert expected["withheld"].sum() > 5, "some customers are reachable on no planned channel"
    assert not ((on.out["treat"] == "1") & (on.out["channel"] == "")).any(), "never treat = 1 with no channel"


def test_the_decision_file_has_a_row_per_customer_and_planned_channel(gated: Gated, on: Built) -> None:
    expected = _expected(gated, on)
    decisions = _decisions(on)
    assert list(decisions.columns) == [
        PRIMARY_KEY,
        "channel",
        "purpose",
        "action_id",
        "allowed",
        "reasons",
        "ledger_ref",
        "catalogue_ref",
        "rule_version",
    ]
    contacted = on.out.loc[expected["base"], PRIMARY_KEY]
    assert list(decisions[PRIMARY_KEY]) == [key for key in contacted for _ in range(3)]
    assert list(decisions["channel"]) == ["whatsapp", "sms", "email"] * len(contacted)
    assert set(decisions["purpose"]) == {PURPOSE} and set(decisions["action_id"]) == {"winback_multi"}
    catalogue_sha = hashlib.sha256(CATALOGUE.encode("utf-8")).hexdigest()
    assert set(decisions["catalogue_ref"]) == {f"{catalogue_sha}#winback_multi"}
    summary = json.loads(on.storage.read_text(run_key(RUN_GATE, DECISIONS_JSON)))
    assert set(decisions["rule_version"]) == {summary["rule_version"]}
    from engine.agentic.canonical import digest

    assert digest(summary["rules"]) == summary["rule_version"], "a reader can recompute the rule version"
    assert summary["rules"]["region"] == "in" and summary["rules"]["dlt"]["channels"] == ["sms"]
    assert summary["rules"]["url_whitelist"]["urls"] == ["shop.example.in"]
    # Every treated row has an allowed decision on its channel; every withheld customer has none.
    allowed = decisions[decisions["allowed"]]
    pairs = set(zip(allowed[PRIMARY_KEY], allowed["channel"], strict=True))
    sent = on.out[on.out["treat"] == "1"]
    assert all(pair in pairs for pair in zip(sent[PRIMARY_KEY], sent["channel"], strict=True))
    withheld = set(on.out.loc[expected["withheld"], PRIMARY_KEY])
    assert not withheld & set(allowed[PRIMARY_KEY])


def test_the_reasons_are_the_rules_that_closed_each_channel(gated: Gated, on: Built) -> None:
    decisions = _decisions(on).set_index([PRIMARY_KEY, "channel"])
    m = _masks(gated.world, gated.run, on.out)
    keys = on.out[PRIMARY_KEY].to_numpy(dtype=object)
    contacted = set(decisions.index.get_level_values(0))
    for position, key in enumerate(keys):
        if key not in contacted:
            continue
        whatsapp = list(decisions.loc[(key, "whatsapp"), "reasons"])
        sms = list(decisions.loc[(key, "sms"), "reasons"])
        email = list(decisions.loc[(key, "email"), "reasons"])
        assert whatsapp == ([] if key in gated.opted_in else ["COMPLIANCE_WHATSAPP_NO_OPT_IN"])
        assert sms == (
            [] if m.sms_ok[position] else ["COMPLIANCE_CONSENT"]
        ), "the message passes the DLT and URL checks"
        assert email == ([] if m.email_ok[position] else ["COMPLIANCE_CONSENT"])
    refs = decisions["ledger_ref"]
    assert refs.str.startswith("channel_contactability.parquet#sha256=").all()
    opted = decisions.xs("whatsapp", level="channel")
    seq = opted.loc[opted.index.isin(sorted(gated.opted_in)), "ledger_ref"]
    assert seq.str.contains(r";consent_record\.seq=\d+$").all(), "the opt-in names the consent record used"


def test_the_summaries_count_the_withheld_per_reason(gated: Gated, on: Built) -> None:
    expected = _expected(gated, on)
    withheld = int(expected["withheld"].sum())
    assert on.summary.compliance_withheld == {
        "COMPLIANCE_CONSENT": withheld,
        "COMPLIANCE_WHATSAPP_NO_OPT_IN": withheld,
    }
    assert on.summary.compliance_withheld_rows == withheld
    assert on.summary.compliance_rerouted_rows == int(expected["rerouted"].sum())
    assert on.summary.treat_rows == int(expected["treat"].sum())
    stored = json.loads(on.storage.read_text(run_key(RUN_GATE, DECISIONS_JSON)))
    assert stored["withheld"] == on.summary.compliance_withheld
    assert stored["customers"] == int(expected["base"].sum()) and stored["rows"] == 3 * stored["customers"]
    assert stored["rows_without_consent_evidence"] == 0
    assert on.summary.compliance_rule_version == stored["rule_version"]
    checks = {(item["action_id"], item["channel"]): item for item in stored["messages"]}
    assert checks[("winback_multi", "sms")]["template_checked"] is True
    assert checks[("winback_multi", "sms")]["template_mismatch"] is False
    assert checks[("winback_multi", "sms")]["urls_not_whitelisted"] == 0
    assert checks[("winback_multi", "whatsapp")]["template_checked"] is False


def test_the_decision_file_s_sha256_is_on_the_ledger_signed_and_verifiable(gated: Gated, on: Built) -> None:
    from engine.agentic.ledger import DecisionLedger, signer_for_data_dir

    data = on.storage.read_bytes(run_key(RUN_GATE, DECISIONS))
    sha = hashlib.sha256(data).hexdigest()
    assert on.summary.compliance_decisions_sha256 == sha
    engine = sqlite_engine(on.data_dir / PLATFORM_DB_FILENAME)
    signer = signer_for_data_dir(on.data_dir, create=False)
    report = verify_ledger(engine, signer)
    assert report.ok, report
    entries = list(
        DecisionLedger(engine, signer=signer, policy_version="unused").entries(kind=EVIDENCE_LABEL)
    )
    entry = next(item for item in entries if item.body.get("artefact_sha256") == sha)
    assert entry.signature is not None and entry.key_id == signer.key_id
    assert entry.actor == "system:compliance_gate"
    assert entry.body["evidence_id"] == on.summary.compliance_evidence_id
    assert entry.body["run_id"] == RUN_GATE
    assert entry.body["artefact_refs"] == [run_key(RUN_GATE, DECISIONS)]
    stored = json.loads(on.storage.read_text(run_key(RUN_GATE, DECISIONS_JSON)))
    assert (stored["ledger_seq"], stored["ledger_entry_hash"]) == (entry.seq, entry.entry_hash)
    assert entry.body["rule_version"] == stored["rule_version"]


def test_a_decision_file_edited_afterwards_no_longer_matches_its_signed_sha256(
    gated: Gated, on: Built
) -> None:
    data = on.storage.read_bytes(run_key(RUN_GATE, DECISIONS))
    decisions = pd.read_parquet(io.BytesIO(data))
    decisions.loc[~decisions["allowed"], "allowed"] = True  # someone "clears" the withheld contacts
    buffer = io.BytesIO()
    decisions.to_parquet(buffer, index=False)
    assert hashlib.sha256(buffer.getvalue()).hexdigest() != on.summary.compliance_decisions_sha256


# ---------------------------------------------------------------------------
# Fail closed, and nothing when off
# ---------------------------------------------------------------------------
def test_a_catalogue_edited_after_the_run_withholds_every_contact(gated: Gated) -> None:
    built = _build(gated, "built_edited", "edited")
    assert (built.out["treat"] == "0").all()
    decisions = _decisions(built)
    assert decisions["reasons"].map(lambda reasons: "COMPLIANCE_CATALOGUE_CHANGED" in list(reasons)).all()
    assert decisions["catalogue_ref"].isna().all()
    assert built.summary.compliance_withheld is not None
    assert (
        built.summary.compliance_withheld["COMPLIANCE_CATALOGUE_CHANGED"]
        == built.summary.compliance_withheld_rows
    )
    assert built.summary.catalogue_note is not None


def test_with_the_gate_off_the_treat_list_is_byte_for_byte_the_list_without_it(gated: Gated) -> None:
    off = _build(gated, "built_off", "gate_off")
    absent = _build(gated, "built_absent", "no_gate_file")
    for name in (TREAT_LIST_CSV, "treat_list.parquet"):
        key = run_key(RUN_GATE, name)
        assert off.storage.read_bytes(key) == absent.storage.read_bytes(key), name
    for built in (off, absent):
        assert not built.storage.exists(run_key(RUN_GATE, DECISIONS))
        assert not built.storage.exists(run_key(RUN_GATE, DECISIONS_JSON))
        text = built.storage.read_text(run_key(RUN_GATE, TREAT_LIST_SUMMARY_FILENAME))
        assert "compliance" not in text
    # Without the gate every treated customer goes on the first planned channel: WhatsApp.
    sent = off.out[off.out["treat"] == "1"]
    assert set(sent["channel"]) == {"whatsapp"}
    assert not (gated.snapshot.parent / "built_off" / "agentic").exists(), "no signing key is created"
