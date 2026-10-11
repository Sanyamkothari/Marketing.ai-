"""Message variants pass the pre-send compliance gate (Plan K M145, DEC-1412).

The run is the M140 journey's (`tests.fixtures.make_run.write_run`, whose banding, suppression and control group
are the engine's own stages), stamped with the catalogue it was scored under the way the route that creates a
scoring run stamps it (`stamp_checked_catalogue`). The High and Medium bands send the catalogue action
`winback_sms` on SMS, under India's region rule, with a registered DLT template, an approved message (the control
template) and a URL whitelist; the gate is on (`compliance.gate.enabled`). Then, through the product's own API:
campaign copy is written by the language model (the deterministic fake, scripted to write four different texts),
every template is approved, each variant's words are registered in the catalogue (`copy:` reference, DLT template
id, and for three of them the DLT template text), and variant arms are drawn
(`POST /runs/{id}/campaign-copy/variant-arms`), which builds the treat list through the gate.

The four variants:

* A: its words match its registered template, and its link (`shop.example.in/back`) is on the whitelist;
* B: its words break its registered template (the registration says "when you like", the copy "whenever you are");
* C: its words match its template, but carry a link that is not whitelisted (`deals-x.in/back`, a bare host);
* D: registered with a template id but no template text, on a channel whose region rule requires one.

Checked: the control template's customers and variant A's are sent; B's, C's and D's are withheld with
`COMPLIANCE_TEMPLATE_MISMATCH`, `COMPLIANCE_URL_NOT_WHITELISTED` and `COMPLIANCE_VARIANT_UNREGISTERED`; the
decision file names each variant's own catalogue action and arm, the summaries count per reason and per arm, and
the signed ledger entry carries the file's sha256 and the codes. A campaign opened from the run sets B, C and D
aside whole (their words were stopped: no such message went out) and keeps every other drawn customer in their
arm; measured, `variant_comparison.json` compares the control template and A only and lists B, C and D as not
sent. A catalogue changed after the arms were drawn fails closed. With the gate off, or the arms set aside, the
list is the one without this milestone.

What the unchanged code (merge-base 443115c) does with this world: its gate ties every message to the run's stamp,
and registering the variants changed the catalogue after the stamp, so every customer with a catalogue action -
the control template's included - is withheld as `COMPLIANCE_CATALOGUE_CHANGED`. That fails these tests, but it
is not the failure this milestone mends. The original failure - a variant whose words break the rules handed off
under the band's approved message - is reproduced by the second world (`stamped_after`): the variants are
registered before the run is stamped, so the stamp is the catalogue that registers them, and the unchanged gate,
checking only the band's action, sends B's, C's and D's customers. There the test fails on the unchanged code and
passes here.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from engine.agentic.canonical import digest
from engine.agentic.config import AGENTIC_CONFIG_FILE
from engine.agentic.ledger import EVIDENCE_LABEL, DecisionLedger, signer_for_data_dir, verify_ledger
from engine.config import RunMode, load_use_case
from engine.decide.catalogue import clear_catalogue_cache, stamp_checked_catalogue
from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.llm import FakeLLMClient
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.storage import LocalStorage, run_key
from tests.fixtures.make_run import RunSpec, write_run
from tests.integration.measurement.support import ok
from tests.unit.compliance.switch import set_gate_switch

pytestmark = pytest.mark.integration

USE_CASE: Final[str] = "win-back-campaign"
KEY: Final[str] = "customer_id"
SCORED_AT: Final[datetime] = datetime(2026, 3, 2, tzinfo=UTC)
ROWS: Final[int] = 6000
TIMEOUT_S: Final[float] = 30.0
DECISIONS: Final[str] = "compliance_decisions.parquet"
DECISIONS_JSON: Final[str] = "compliance_decisions.json"
CONTROL_ACTION: Final[str] = "winback_sms"
CONTROL_TEMPLATE: Final[str] = "Dear customer, come back for Rs {#numeric#} off. Shop at {#url#} - Acme"
CONTROL_MESSAGE: Final[str] = (
    "Dear customer, come back for Rs 200 off. Shop at https://shop.example.in/back - Acme"
)
CATALOGUE: Final[str] = f"""\
# The client's catalogue as the run was scored under it: one win-back action on SMS, under India's rules.
region: IN
actions:
  - action_id: {CONTROL_ACTION}
    label: Win-back SMS
    channels: [sms]
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{CONTROL_TEMPLATE}"
    message_text: "{CONTROL_MESSAGE}"
"""
WHITELIST: Final[str] = "channels: [sms]\nurls: [shop.example.in]\n"
SETTINGS: Final[str] = "compliance:\n  gate:\n    opt_in_channels: [whatsapp]\n"
OPENER: Final[str] = "{{snapshot_date}}"
BODIES: Final[dict[str, str]] = {
    "A": "we would be glad to have you back. Plans at shop.example.in/back",
    "B": "your line is ready whenever you are. Reply and we will pick it up.",
    "C": "your old plan is waiting at deals-x.in/back for you.",
    "D": "a new plan is ready for you. Reply YES to hear more.",
}
"""What the scripted language model writes for each variant letter, after its greeting."""
ARM_REASON: Final[dict[str, list[str]]] = {
    "control_template": [],
    "variant_a": [],
    "variant_b": ["COMPLIANCE_TEMPLATE_MISMATCH"],
    "variant_c": ["COMPLIANCE_URL_NOT_WHITELISTED"],
    "variant_d": ["COMPLIANCE_VARIANT_UNREGISTERED"],
}
"""The reasons each arm's customers get on SMS, their only planned channel."""


def _scripted(original: Callable[[Any, str], str]) -> Callable[[Any, str], str]:
    """The fake model's copy answer with each variant's body replaced by its scripted text (greeting and
    opt-out line kept): a language model that wrote four different messages."""

    def copy(self: Any, user: str) -> str:
        answer = json.loads(original(self, user))
        for item in answer["variants"]:
            greeting = item["text"].partition(", ")[0]
            required = item["text"].partition("\n")[2]
            item["text"] = item["body"] = f"{greeting}, {BODIES[item['label']]}\n{required}"
        return json.dumps(answer)

    return copy


def _root(source: Path, target: Path) -> Path:
    """`configs/` with the gate on, the control action registered and the win-back copy as four SMS variants."""
    shutil.copytree(source, target)
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for band in document["actions"]["bands"]:
        if band["name"] in ("High", "Medium"):
            band["action_id"] = CONTROL_ACTION
    copy = document["generative"]["campaign_copy"]
    copy["channels"] = ["sms"]
    copy["allowed_fields"] = ["snapshot_date", "band"]  # columns make_run writes
    copy["variants_per_band"] = 4
    document["generative"]["variant_arms"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    shutil.rmtree(target / "compliance")
    (target / "compliance").mkdir()
    (target / "compliance" / "compliance.yaml").write_text(SETTINGS, encoding="utf-8")
    (target / "compliance" / "url_whitelist.yaml").write_text(WHITELIST, encoding="utf-8")
    set_gate_switch(target, on=True)
    return target


def _derived(registered: Path, target: Path, *, gate: bool | None, variant_arms: bool = True) -> Path:
    """A copy of the root as it is after the variants were registered (the same catalogue, byte for byte), with
    the gate on, off, or no Plan K files at all (None), and the use case's variant arms on or off."""
    shutil.copytree(registered, target)
    if gate is None:
        (target / AGENTIC_CONFIG_FILE).unlink()
        shutil.rmtree(target / "compliance")
    else:
        set_gate_switch(target, on=gate)
    if not variant_arms:
        path = target / "use_cases" / "win_back_campaign.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["generative"]["variant_arms"] = False
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _generate_and_approve(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    import time

    ok(client.post(f"/runs/{run_id}/campaign-copy", json={"overrides": {}}), 202)
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        status = client.get(f"/runs/{run_id}/artefacts/copy_status.json")
        if status.status_code == 200 and status.json()["state"] in ("done", "failed"):
            break
        assert time.monotonic() < deadline, "campaign copy did not finish"
        time.sleep(0.1)
    assert status.json()["state"] == "done", status.json()
    batch = ok(client.get(f"/runs/{run_id}/artefacts/copy_batch.json"))
    approved = []
    for template in batch["templates"]:
        assert template["status"] == "pending_review", template
        ok(
            client.post(
                f"/runs/{run_id}/campaign-copy/templates/{template['template_id']}/approve",
                json={"approved_by": "Asha Rao"},
            )
        )
        approved.append(template)
    return approved


def _reference(template: dict[str, Any]) -> str:
    """The catalogue reference of a template's words, computed independently of the engine."""
    payload = "\x00".join((template["channel"], template["subject"] or "", template["text"])).encode("utf-8")
    return "copy:" + hashlib.sha256(payload).hexdigest()[:16]


def _dlt_template(letter: str, text: str) -> str | None:
    """The DLT template the client registered for a variant's words: its greeting's placeholder and its link
    are variables; B's registration says other words than the copy; D has none."""
    template = text.replace(OPENER, "{#var#}")
    if letter == "A":
        return template.replace("shop.example.in/back", "{#var#}")
    if letter == "B":
        return template.replace("whenever you are", "when you like")
    if letter == "C":
        return template.replace("deals-x.in/back", "{#var#}")
    return None


def _register(root: Path, approved: list[dict[str, Any]]) -> dict[str, str]:
    """Add one catalogue action per variant's words to the client's catalogue; returns letter -> action id."""
    path = root / "decide" / "catalogue.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    by_letter: dict[str, dict[str, Any]] = {}
    for template in approved:
        seen = by_letter.setdefault(template["variant"], template)
        assert _reference(seen) == _reference(template), "one text per letter, whichever band"
    actions: dict[str, str] = {}
    for index, letter in enumerate(sorted(by_letter)):
        template = by_letter[letter]
        action_id = f"winback_sms_variant_{letter.lower()}"
        action: dict[str, Any] = {
            "action_id": action_id,
            "label": f"Win-back SMS, variant {letter}",
            "channels": ["sms"],
            "template_ref": _reference(template),
            "dlt_template_id": f"11070000000000001{index + 1}",
            "message_category": "promotional",
        }
        registered = _dlt_template(letter, template["text"])
        if registered is not None:
            action["dlt_template_text"] = registered
        document["actions"].append(action)
        actions[letter] = action_id
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    clear_catalogue_cache()
    return actions


@dataclass(frozen=True)
class World:
    base: Path
    run_id: str
    roots: dict[str, Path]
    registered: Path
    """The data folder after the variants were registered, before any arm was drawn."""
    armed: Path
    """The data folder right after the arms were drawn (and the list built through the gate)."""
    arms: dict[str, Any]
    actions: dict[str, str]

    def copy(self, source: Path, name: str) -> LocalStorage:
        target = self.base / name
        shutil.copytree(source, target)
        return LocalStorage(target)


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    base = tmp_path_factory.mktemp("gate-variants")
    root = _root(config_root, base / "gate_on")
    data_dir = base / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    run_id = write_run(
        storage,
        RunSpec(
            use_case_id=USE_CASE,
            mode=RunMode.SCORE,
            rows=ROWS,
            positive_rate=0.6,
            created_at=SCORED_AT,
            config_root=root,
        ),
    )
    # What `POST /runs` does for a scoring run: stamp the catalogue its use case was checked against.
    stamp = stamp_checked_catalogue(
        storage, run_id, load_use_case(USE_CASE, root), root=root, created_at=SCORED_AT
    )
    assert stamp is not None and stamp.planned_channels == {CONTROL_ACTION: ("sms",)}
    clear_catalogue_cache()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeLLMClient, "_copy", _scripted(FakeLLMClient._copy))
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            approved = _generate_and_approve(client, run_id)
            assert sorted({template["variant"] for template in approved}) == ["A", "B", "C", "D"]
            actions = _register(root, approved)
            registered = base / "registered"
            shutil.copytree(data_dir, registered)
            arms = ok(client.post(f"/runs/{run_id}/campaign-copy/variant-arms"))
    armed = base / "armed"
    shutil.copytree(data_dir, armed)
    roots = {
        "gate_on": root,
        "gate_off": _derived(root, base / "root_gate_off", gate=False),
        "no_plan_k": _derived(root, base / "root_no_plan_k", gate=None),
        "arms_off": _derived(root, base / "root_arms_off", gate=True, variant_arms=False),
    }
    yield World(base, run_id, roots, registered, armed, arms, actions)
    clear_catalogue_cache()


def _csv(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    text = storage.read_bytes(run_key(run_id, TREAT_LIST_CSV)).decode("utf-8")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _drawn(storage: LocalStorage, run_id: str) -> pd.Series:
    """Each customer's arm as M140 drew it (`variant_arms.parquet`), by key; null for the held-back."""
    table = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, "variant_arms.parquet"))))
    return pd.Series(table["variant_arm"].to_numpy(dtype=object), index=table[KEY].astype(str))


def _decisions(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, DECISIONS))))


@pytest.fixture(scope="module")
def armed(world: World) -> LocalStorage:
    return LocalStorage(world.armed)


# ---------------------------------------------------------------------------
# The gate on, the arms in force: each customer's own words are checked
# ---------------------------------------------------------------------------
def test_controls_and_variant_a_are_sent_and_b_c_d_are_withheld(world: World, armed: LocalStorage) -> None:
    assert world.arms["arms"] == ["control_template", "variant_a", "variant_b", "variant_c", "variant_d"]
    out = _csv(armed, world.run_id)
    drawn = _drawn(armed, world.run_id).dropna()
    arm = drawn.reindex(out[KEY]).to_numpy(dtype=object)
    treat = (out["treat"] == "1").to_numpy()
    sent = pd.Series(arm).isin(["control_template", "variant_a"]).to_numpy()
    stopped = pd.Series(arm).isin(["variant_b", "variant_c", "variant_d"]).to_numpy()
    assert sent.sum() > 500 and stopped.sum() > 1000
    assert treat[sent].all(), "the control template and variant A pass the gate"
    assert not treat[stopped].any(), "B, C and D are withheld"
    assert not treat[pd.isna(arm)].any(), "nobody outside the copy's audience is sent"
    # Never treat = 1 with no channel, or with a variant the customer will not receive.
    assert (out.loc[treat, "channel"] == "sms").all()
    assert (out.loc[treat, "variant_arm"].to_numpy(dtype=object) == arm[treat]).all()
    assert (out.loc[~treat, "variant_arm"] == "").all() and (out.loc[~treat, "variant_template"] == "").all()
    a_rows = out[out["variant_arm"] == "variant_a"]
    assert (a_rows["variant_template"] == a_rows["band"] + "-sms-A").all()
    assert (out.loc[out["variant_arm"] == "control_template", "variant_template"] == "").all()


def test_the_decision_file_names_each_variant_s_own_action_and_reason(
    world: World, armed: LocalStorage
) -> None:
    decisions = _decisions(armed, world.run_id)
    assert list(decisions.columns) == [
        KEY,
        "channel",
        "purpose",
        "action_id",
        "allowed",
        "reasons",
        "ledger_ref",
        "catalogue_ref",
        "rule_version",
        "variant_arm",
    ]
    drawn = _drawn(armed, world.run_id).dropna()
    assert decisions[KEY].is_unique and set(decisions[KEY]) == set(
        drawn.index
    ), "one row per customer, on SMS"
    assert (decisions["channel"] == "sms").all()
    assert (decisions["variant_arm"].to_numpy(dtype=object) == drawn.reindex(decisions[KEY]).to_numpy()).all()
    summary = json.loads(armed.read_text(run_key(world.run_id, DECISIONS_JSON)))
    sha = summary["rules"]["variant_arms"]["catalogue_sha256"]
    action_of = {
        "control_template": CONTROL_ACTION,
        **{f"variant_{letter.lower()}": action for letter, action in world.actions.items()},
    }
    for level, reasons in ARM_REASON.items():
        rows = decisions[decisions["variant_arm"] == level]
        assert len(rows) > 100, level
        assert all(list(item) == reasons for item in rows["reasons"]), level
        assert (rows["allowed"] == (not reasons)).all(), level
        assert (rows["action_id"] == action_of[level]).all(), "the variant's own registered action"
        assert (rows["catalogue_ref"] == f"{sha}#{action_of[level]}").all()


def test_the_summaries_count_per_reason_and_per_arm(world: World, armed: LocalStorage) -> None:
    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary

    summary = armed.read_model(run_key(world.run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)
    drawn = _drawn(armed, world.run_id).dropna().value_counts()
    counts = {level: int(drawn.get(level, 0)) for level in ARM_REASON}
    assert summary.compliance_withheld == {
        "COMPLIANCE_TEMPLATE_MISMATCH": counts["variant_b"],
        "COMPLIANCE_URL_NOT_WHITELISTED": counts["variant_c"],
        "COMPLIANCE_VARIANT_UNREGISTERED": counts["variant_d"],
    }
    assert summary.compliance_withheld_rows == counts["variant_b"] + counts["variant_c"] + counts["variant_d"]
    # The arm counts are the customers actually sent their arm; the withheld are counted per arm beside them.
    assert summary.variant_arm_counts == {
        "control_template": counts["control_template"],
        "variant_a": counts["variant_a"],
        "variant_b": 0,
        "variant_c": 0,
        "variant_d": 0,
    }
    assert summary.variant_arm_withheld == {
        "control_template": 0,
        "variant_a": 0,
        "variant_b": counts["variant_b"],
        "variant_c": counts["variant_c"],
        "variant_d": counts["variant_d"],
    }
    assert summary.treat_rows == counts["control_template"] + counts["variant_a"]

    stored = json.loads(armed.read_text(run_key(world.run_id, DECISIONS_JSON)))
    assert digest(stored["rules"]) == stored["rule_version"] == summary.compliance_rule_version
    rules = stored["rules"]["variant_arms"]
    assert rules["batch_id"] == world.arms["batch_id"]
    assert rules["catalogue_sha256"] == world.arms["catalogue_sha256"]
    assert {tuple(item[:3]) for item in rules["templates"]} == {
        (level, band, "sms")
        for level in ARM_REASON
        if level != "control_template"
        for band in ("High", "Medium")
    }
    assert stored["withheld_by_arm"] == summary.variant_arm_withheld
    checks = {(item["arm"], item["unit"], item["channel"]): item for item in stored["variant_messages"]}
    assert len(checks) == 8
    for band in ("High", "Medium"):
        a, b, c, d = (checks[(f"variant_{x}", band, "sms")] for x in "abcd")
        assert a["template_checked"] and a["template_mismatch"] is False and a["urls_not_whitelisted"] == 0
        assert b["template_mismatch"] is True and b["urls_not_whitelisted"] == 0
        assert c["template_mismatch"] is False and c["urls_not_whitelisted"] == 1
        assert d["template_checked"] is False and d["unregistered"] is True
        assert a["template_id"] == f"{band}-sms-A" and a["catalogue_action_id"] == world.actions["A"]
    # The control template is still checked once per action and channel, as M134 does.
    control = {(item["action_id"], item["channel"]): item for item in stored["messages"]}
    assert control[(CONTROL_ACTION, "sms")]["template_mismatch"] is False


def test_the_ledger_entry_signs_the_variant_decisions(world: World, armed: LocalStorage) -> None:
    data = armed.read_bytes(run_key(world.run_id, DECISIONS))
    sha = hashlib.sha256(data).hexdigest()
    stored = json.loads(armed.read_text(run_key(world.run_id, DECISIONS_JSON)))
    assert stored["decisions_sha256"] == sha
    engine = sqlite_engine(world.armed / PLATFORM_DB_FILENAME)
    signer = signer_for_data_dir(world.armed, create=False)
    report = verify_ledger(engine, signer)
    assert report.ok, report
    entries = list(
        DecisionLedger(engine, signer=signer, policy_version="unused").entries(kind=EVIDENCE_LABEL)
    )
    entry = next(item for item in entries if item.body.get("artefact_sha256") == sha)
    assert entry.signature is not None and entry.actor == "system:compliance_gate"
    assert entry.body["rule_version"] == stored["rule_version"]
    assert entry.body["codes"] == [
        "COMPLIANCE_TEMPLATE_MISMATCH",
        "COMPLIANCE_URL_NOT_WHITELISTED",
        "COMPLIANCE_VARIANT_UNREGISTERED",
    ]
    assert (stored["ledger_seq"], stored["ledger_entry_hash"]) == (entry.seq, entry.entry_hash)


STOPPED: Final[tuple[str, ...]] = ("variant_b", "variant_c", "variant_d")
OUTCOME: Final[str] = "reactivated_90d"
MATURE: Final[datetime] = datetime(2026, 7, 1, tzinfo=UTC)
"""Every 90-day outcome window from the run's finish has closed by then."""


def test_a_campaign_sets_aside_the_arms_the_gate_stopped_and_keeps_the_rest(world: World) -> None:
    """B, C and D were never sent: their customers are in neither campaign arm. The control template's and A's
    drawn customers stay in their arm (intention to treat, as for a customer withheld for their consent)."""
    storage = world.copy(world.armed, "campaign")
    with TestClient(create_app(config_root=world.roots["gate_on"], data_dir=storage.root)) as client:
        created = ok(client.post("/campaigns", json={"run_id": world.run_id}), 201)
    campaign = created["campaign"]
    assert set(campaign["variant_arms_set_aside"]) == set(STOPPED)
    assert all(reason.startswith("Not sent") for reason in campaign["variant_arms_set_aside"].values())
    assignment = pd.read_parquet(
        io.BytesIO(storage.read_bytes(f"campaigns/{campaign['campaign_id']}/assignment.parquet"))
    )
    keys = assignment[KEY].astype(str)
    drawn = _drawn(storage, world.run_id).dropna()
    kept = drawn[~drawn.isin(STOPPED)]
    aside = drawn[drawn.isin(STOPPED)]
    treated = assignment[assignment["arm"] == "treated"]
    assert set(treated[KEY].astype(str)) == set(kept.index)
    joined = kept.reindex(treated[KEY].astype(str)).to_numpy(dtype=object)
    assert (treated["variant_arm"].astype(str).to_numpy(dtype=object) == joined).all()
    assert treated["variant_arm"].value_counts().to_dict() == {
        level: count for level, count in world.arms["arm_rows"].items() if level not in STOPPED
    }
    stopped = assignment[keys.isin(aside.index).to_numpy()]
    assert len(stopped) == len(aside) and (stopped["arm"] == "suppressed").all()
    assert not stopped["intended"].any() and stopped["variant_arm"].isna().all()
    # The no-message group is every held-back customer of the copy's groups, as without the gate.
    held = _drawn(storage, world.run_id)
    held_back = held[held.isna()].index
    assert set(keys[assignment["arm"] == "holdout"]) == set(held_back)


def test_measured_the_stopped_arms_are_listed_not_sent_and_never_compared(world: World) -> None:
    import numpy as np

    storage = world.copy(world.armed, "measured")
    out = _csv(storage, world.run_id)
    rng = np.random.default_rng(3)
    outcomes = pd.DataFrame({KEY: out[KEY], OUTCOME: (rng.random(len(out)) < 0.2).astype(int)})
    payload = outcomes.to_csv(index=False, lineterminator="\n").encode()
    with TestClient(create_app(config_root=world.roots["gate_on"], data_dir=storage.root)) as client:
        campaign_id = ok(client.post("/campaigns", json={"run_id": world.run_id}), 201)["campaign"][
            "campaign_id"
        ]
        upload_id = ok(
            client.post(
                "/uploads",
                files={"file": ("outcomes.csv", payload, "text/csv")},
                data={"use_case": USE_CASE, "mode": "score"},
            ),
            201,
        )["upload_id"]
        ok(
            client.post(
                f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": OUTCOME}
            )
        )
        ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()}))
    report = json.loads(storage.read_bytes(f"campaigns/{campaign_id}/incrementality_report.json"))
    assert [arm["arm"] for arm in report["arms"]] == ["control_template", "variant_a"]
    comparison = json.loads(storage.read_bytes(f"campaigns/{campaign_id}/variant_comparison.json"))
    assert comparison["arms"] == ["control_template", "variant_a"]
    assert comparison["family_size"] == 1
    assert [(pair["first"], pair["second"]) for pair in comparison["pairs"]] == [
        ("control_template", "variant_a")
    ]
    named = {pair[name] for pair in comparison["pairs"] for name in ("first", "second")}
    assert not named & set(STOPPED)
    assert comparison["leader"] not in STOPPED and comparison["winner"] not in STOPPED
    assert [item["arm"] for item in comparison["not_sent"]] == list(STOPPED)
    assert all(item["difference"] is None for item in comparison["not_sent"])
    assert all("compliance gate" in item["reason"] for item in comparison["not_sent"])


def test_a_catalogue_changed_after_the_arms_were_drawn_fails_closed(world: World) -> None:
    root = _derived(world.roots["gate_on"], world.base / "root_edited", gate=True)
    path = root / "decide" / "catalogue.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "# edited after the arms were drawn\n", encoding="utf-8"
    )
    clear_catalogue_cache()
    storage = world.copy(world.armed, "edited")
    summary = build_treat_list(storage, world.run_id, config_root=root)
    clear_catalogue_cache()
    contacted = int(_drawn(storage, world.run_id).notna().sum())
    assert summary.treat_rows == 0
    assert summary.compliance_withheld == {"COMPLIANCE_CATALOGUE_CHANGED": contacted}
    stored = json.loads(storage.read_text(run_key(world.run_id, DECISIONS_JSON)))
    assert stored["variant_messages"] == [] and stored["messages"] == []


# ---------------------------------------------------------------------------
# The gate off, or the arms set aside: the list without this milestone
# ---------------------------------------------------------------------------
def test_with_the_gate_off_the_list_is_the_one_without_the_gate(world: World) -> None:
    off = world.copy(world.armed, "gate_off")
    before = world.copy(world.armed, "no_plan_k")
    summary = build_treat_list(off, world.run_id, config_root=world.roots["gate_off"])
    build_treat_list(before, world.run_id, config_root=world.roots["no_plan_k"])
    clear_catalogue_cache()
    for name in (TREAT_LIST_CSV, "treat_list.parquet"):
        assert off.read_bytes(run_key(world.run_id, name)) == before.read_bytes(run_key(world.run_id, name))
    out = _csv(off, world.run_id)
    drawn = _drawn(off, world.run_id).dropna()
    assert (out["treat"] == "1").sum() == len(drawn), "every arm is sent, as M140 sends it"
    assert summary.compliance_rule_version is None and summary.variant_arm_withheld is None
    assert summary.variant_arm_counts == world.arms["arm_rows"]


def test_with_the_arms_set_aside_the_gate_decides_as_it_did_before(world: World) -> None:
    never = world.copy(world.registered, "never_armed")
    aside = world.copy(world.armed, "arms_off")
    first = build_treat_list(never, world.run_id, config_root=world.roots["gate_on"])
    second = build_treat_list(aside, world.run_id, config_root=world.roots["arms_off"])
    clear_catalogue_cache()
    for name in (TREAT_LIST_CSV, "treat_list.parquet", DECISIONS):
        assert aside.read_bytes(run_key(world.run_id, name)) == never.read_bytes(run_key(world.run_id, name))
    assert first.compliance_rule_version == second.compliance_rule_version
    assert second.variant_arm_counts is None and second.variant_arm_withheld is None
    stored = json.loads(aside.read_text(run_key(world.run_id, DECISIONS_JSON)))
    assert "variant_arms" not in stored["rules"] and "variant_messages" not in stored
    assert "withheld_by_arm" not in stored
    assert "variant_arm" not in _decisions(aside, world.run_id).columns


# ---------------------------------------------------------------------------
# The original failure: the run stamped with the catalogue that registers the variants
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stamped_after(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    """The variants are registered first, then the run is stamped (its stamp is the catalogue that registers
    them), then the arms are drawn: the unchanged gate finds the catalogue unchanged and checks the band's
    action only, so every variant's customers would be handed off whatever their own words say."""
    base = tmp_path_factory.mktemp("gate-variants-stamped-after")
    root = _root(config_root, base / "gate_on")
    data_dir = base / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    run_id = write_run(
        storage,
        RunSpec(
            use_case_id=USE_CASE,
            mode=RunMode.SCORE,
            rows=ROWS,
            positive_rate=0.6,
            created_at=SCORED_AT,
            config_root=root,
        ),
    )
    clear_catalogue_cache()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeLLMClient, "_copy", _scripted(FakeLLMClient._copy))
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            approved = _generate_and_approve(client, run_id)
            actions = _register(root, approved)
            stamp = stamp_checked_catalogue(
                storage, run_id, load_use_case(USE_CASE, root), root=root, created_at=SCORED_AT
            )
            clear_catalogue_cache()
            assert stamp is not None and stamp.planned_channels.get(CONTROL_ACTION) == ("sms",)
            registered = base / "registered"
            shutil.copytree(data_dir, registered)
            arms = ok(client.post(f"/runs/{run_id}/campaign-copy/variant-arms"))
    assert arms["run_catalogue_sha256"] == arms["catalogue_sha256"] == stamp.catalogue_sha256
    yield World(base, run_id, {"gate_on": root}, registered, data_dir, arms, actions)
    clear_catalogue_cache()


def test_a_variant_with_bad_words_is_not_handed_off_under_the_band_s_message(stamped_after: World) -> None:
    """The fail-open the milestone mends: on the unchanged code B's, C's and D's customers are all sent here."""
    storage = LocalStorage(stamped_after.armed)
    run_id = stamped_after.run_id
    out = _csv(storage, run_id)
    drawn = _drawn(storage, run_id).dropna()
    arm = pd.Series(drawn.reindex(out[KEY]).to_numpy(dtype=object))
    treat = (out["treat"] == "1").to_numpy()
    for level in ("control_template", "variant_a"):
        rows = (arm == level).to_numpy()
        assert rows.sum() > 100 and treat[rows].all(), level
    for level in STOPPED:
        rows = (arm == level).to_numpy()
        assert rows.sum() > 100 and not treat[rows].any(), level
    decisions = _decisions(storage, run_id)
    for level, reasons in ARM_REASON.items():
        rows = decisions[decisions["variant_arm"] == level]
        assert all(list(item) == reasons for item in rows["reasons"]), level
    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary

    summary = storage.read_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)
    assert "COMPLIANCE_CATALOGUE_CHANGED" not in (summary.compliance_withheld or {})
    assert summary.variant_arm_words_withheld == summary.variant_arm_withheld
    assert {level for level, count in (summary.variant_arm_withheld or {}).items() if count} == set(STOPPED)
