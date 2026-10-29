"""The prescription: one saved unit, one hash, replayed at training and scoring (Plan O M243, DEC-1813).

The plan's acceptance for M243, at the engine (the API and the scheduled paths are in
`tests/integration/test_prescription_runs.py`):

* next month's training file replays every part (intake, locale, currency, recipe, contract, outcome, post-build recipe);
* scoring with a single-file recipe is byte-identical to today (a `DataRecipe` reads as a one-part prescription);
* a contract breach that breaks a step is `PRESCRIPTION_DRIFT`; one that breaks no step is a warning and a question;
  a changed column, a new value, a new currency or a different locale stops and asks - and nothing is re-learnt;
* the hash is stable across processes and changes when any replayed part changes (never with wording or who);
* PRESCRIBE writes `prescriptions/<hash>.json` with goal sections, and M246's record carries the same hash.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.agent.contracts import RecipeStepKind
from engine.agent.dataset_recipe import run_saved_recipe
from engine.config import load_use_case
from engine.doctor import prescription as prescription_module
from engine.doctor import prescription_contract
from engine.doctor.case import CaseStore
from engine.doctor.currency import money_basis
from engine.doctor.outcome_definition import OutcomeDefinition
from engine.doctor.prescription import (
    PRESCRIPTION_CODES,
    PRESCRIPTION_DRIFT,
    CasePrescriber,
    FilePart,
    IntakeDecision,
    LocalePart,
    OutcomePart,
    Prescription,
    PrescriptionError,
    Provenance,
    SettingChoice,
    load_prescription,
    one_part,
    prescription_hash,
    prescription_key,
    save_prescription,
)
from engine.doctor.prescription_contract import ColumnContract, DataContract, learn_contract
from engine.doctor.prescription_replay import ReplayFile
from engine.doctor.prescription_runs import bound
from engine.storage import LocalStorage
from tests.unit.doctor import prescription_support as ps
from tests.unit.doctor import visit_support as vs


@pytest.fixture(scope="module")
def approved() -> Prescription:
    return ps.prescription()


def _stopped(
    approved: Prescription, frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> PrescriptionError:
    """Replay `frame`, which must stop; nothing may be learnt again on the way."""

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a replay never learns a contract again")

    monkeypatch.setattr(prescription_contract, "learn_contract", never)
    monkeypatch.setattr(prescription_module, "learn_contract", never)
    before = approved.model_dump_json()
    with pytest.raises(PrescriptionError) as stopped:
        ps.replay(approved, [ps.replay_file(frame, **extra)], mode="train")
    assert approved.model_dump_json() == before, "the approved prescription is never changed by a replay"
    return stopped.value


# ---------------------------------------------------------------------------
# One hash
# ---------------------------------------------------------------------------
def test_the_hash_is_stable_across_processes(approved: Prescription, tmp_path: Path) -> None:
    path = tmp_path / "prescription.json"
    path.write_text(approved.model_dump_json(), encoding="utf-8")
    code = (
        "import sys; from pathlib import Path; "
        "from engine.doctor.prescription import Prescription, prescription_hash; "
        "p = Prescription.model_validate_json(Path(sys.argv[1]).read_text(encoding='utf-8')); "
        "print(prescription_hash(p))"
    )
    root = Path(__file__).resolve().parents[3]
    found = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True, cwd=root
    ).stdout.strip()
    assert found == approved.prescription_hash == prescription_hash(approved)
    assert len(found) == 64 and set(found) <= set("0123456789abcdef")
    assert ps.prescription().prescription_hash == approved.prescription_hash, "built twice, hashed the same"


def _changes(approved: Prescription) -> dict[str, Prescription]:
    """The approved prescription with one replayed part changed at a time."""
    part = approved.files[0]
    section = approved.goals[0]
    assert part.recipe is not None and part.intake is not None and part.contract is not None
    assert section.outcome is not None and approved.locale is not None
    assert approved.locale.profile is not None and approved.locale.money_basis is not None
    steps = tuple(
        (
            s.model_copy(update={"params": {**s.params, "rate": 84.0}})
            if s.kind is RecipeStepKind.CONVERT_CURRENCY
            else s
        )
        for s in part.recipe.steps
    )
    other_recipe = ps.recipe(steps)
    first = part.contract.columns[1]
    other_contract = DataContract(
        columns=(first.model_copy(update={"empty_share": 0.5}), *part.contract.columns[2:])
    )
    other_definition = OutcomeDefinition(
        **{
            **section.outcome.definition.model_dump(),
            "map": tuple(
                m.model_copy(update={"meaning": "yes"}) if m.value == "Suspended" else m
                for m in section.outcome.definition.map
            ),
            "exclude": (),
        }
    )
    profile = approved.locale.profile
    other_profile = profile.model_copy(
        update={"date_order": profile.date_order.model_copy(update={"value": "month_first"})}
    )
    basis = approved.locale.money_basis
    other_basis = basis.model_copy(update={"workspace_currency": "USD"})

    def with_part(**update: Any) -> Prescription:
        return ps.prescription(files=(part.model_copy(update=update),))

    def with_section(**update: Any) -> Prescription:
        return ps.prescription(goals=(section.model_copy(update=update),))

    return {
        "recipe": with_part(recipe=other_recipe),
        "recipe roles": with_part(recipe=part.recipe.model_copy(update={"target": None})),
        "intake": with_part(intake=part.intake.model_copy(update={"header_row": 3})),
        "contract": with_part(contract=other_contract),
        "slot": with_part(slot="customers_2"),
        "locale": ps.prescription(locale=LocalePart(profile=other_profile, money_basis=basis)),
        "money basis": ps.prescription(locale=LocalePart(profile=profile, money_basis=other_basis)),
        "post-build recipe": ps.prescription(post_build=part.recipe),
        "final contract": ps.prescription(contract=other_contract),
        "outcome": with_section(outcome=OutcomePart(definition=other_definition)),
        "roles": with_section(roles={"key": "customer_id", "outcome": "churned"}),
        "settings": with_section(settings=(SettingChoice(path="split.test_fraction", value=0.3),)),
        "active goal": with_section(active=False),
        "split": with_section(split={"type": "time"}),
        "acknowledgements": with_section(acknowledgements=("LEAKAGE_SUSPECTED:spend",)),
    }


def test_the_hash_changes_when_any_replayed_part_changes(approved: Prescription) -> None:
    hashes = {name: changed.prescription_hash for name, changed in _changes(approved).items()}
    assert approved.prescription_hash not in hashes.values(), [
        name for name, h in hashes.items() if h == approved.prescription_hash
    ]
    assert len(set(hashes.values())) == len(hashes), "each change hashes differently"


def test_the_hash_never_covers_reasons_wording_or_who(approved: Prescription) -> None:
    part = approved.files[0]
    section = approved.goals[0]
    assert part.recipe is not None and section.outcome is not None and section.outcome.approval is not None
    reworded = part.recipe.model_copy(
        update={
            "recipe_id": "rc-other",
            "approved_by": "u-someone-else",
            "source_fingerprint": "sha256:another-file",
            "steps": tuple(s.model_copy(update={"reason": "other words"}) for s in part.recipe.steps),
        }
    )
    same = ps.prescription(
        files=(
            part.model_copy(
                update={"recipe": reworded, "upload_ids": ("u_nov",), "file_names": ("customers_nov.csv",)}
            ),
        ),
        goals=(
            section.model_copy(
                update={
                    "brief_hash": "c" * 64,
                    "item_digests": ("d" * 64,),
                    "outcome": section.outcome.model_copy(
                        update={
                            "approval": section.outcome.approval.model_copy(
                                update={"sentence": "Other words.", "approved_by": "u-other"}
                            )
                        }
                    ),
                }
            ),
        ),
        provenance=Provenance(approved_by=("u-other",), receipts=("ledger:x",), created_at=ps.NOW),
    )
    assert same.prescription_hash == approved.prescription_hash


def test_a_prescription_whose_hash_does_not_match_its_parts_is_refused(approved: Prescription) -> None:
    raw = json.loads(approved.model_dump_json())
    raw["goals"][0]["active"] = False
    with pytest.raises(ValueError, match="prescription_hash does not match"):
        Prescription.model_validate(raw)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def test_it_is_stored_under_its_hash_once_and_read_back_checked(
    approved: Prescription, tmp_path: Path
) -> None:
    storage = LocalStorage(tmp_path)
    key = save_prescription(storage, "default", "case_0123456789abcdef", approved)
    assert key == f"cases/default/case_0123456789abcdef/prescriptions/{approved.prescription_hash}.json"
    assert save_prescription(storage, "default", "case_0123456789abcdef", approved) == key
    assert (
        load_prescription(storage, "default", "case_0123456789abcdef", approved.prescription_hash) == approved
    )
    # Altered on disk: its content no longer has that hash, and it is not read.
    raw = json.loads(storage.read_bytes(key))
    raw["files"][0]["intake"]["header_row"] = 4
    storage.write_bytes(key, json.dumps(raw).encode())
    with pytest.raises(PrescriptionError) as refused:
        load_prescription(storage, "default", "case_0123456789abcdef", approved.prescription_hash)
    assert refused.value.code == "PRESCRIPTION_NOT_FOUND"
    with pytest.raises(PrescriptionError):
        prescription_key("default", "case_0123456789abcdef", "../escape")
    assert {"PRESCRIPTION_DRIFT", "PRESCRIPTION_NOT_FOUND"} == PRESCRIPTION_CODES


# ---------------------------------------------------------------------------
# A DataRecipe reads as a one-part prescription: scoring is byte-identical
# ---------------------------------------------------------------------------
def _parquet(frame: pd.DataFrame) -> bytes:
    sink = io.BytesIO()
    frame.to_parquet(sink, index=False)
    return sink.getvalue()


def test_scoring_with_a_single_file_recipe_is_byte_identical_to_today() -> None:
    config = load_use_case(ps.GOAL)
    recipe = ps.recipe(target=None)
    october = ps.october()
    today = run_saved_recipe(october, recipe, config, upload_id="u_oct", leak_check="narrow")
    part = one_part(recipe)
    assert part.files == (FilePart(slot="file", recipe=recipe),) and part.goals == ()
    replayed = ps.replay(
        part, [ReplayFile(slot="file", frame=october, upload_id="u_oct")], mode="score", config=config
    )
    assert replayed.frame is not None
    assert _parquet(replayed.frame) == _parquet(today.frame)
    (receipt,) = replayed.recipe_receipts
    assert receipt.model_dump(exclude={"created_at"}) == today.receipt.model_dump(exclude={"created_at"})
    assert replayed.replayed == ("recipe",), "nothing else is in a one-part prescription"
    assert one_part(recipe).prescription_hash == part.prescription_hash


# ---------------------------------------------------------------------------
# Next month's training file replays every part
# ---------------------------------------------------------------------------
def test_next_months_training_file_replays_every_part(approved: Prescription) -> None:
    october = ps.october()
    replayed = ps.replay(approved, [ps.replay_file(october)], mode="train")
    assert replayed.replayed == ("recipe", "contract", "outcome")
    assert replayed.warnings == ()
    frame = replayed.frame
    assert frame is not None
    # The conversion replays with its frozen rate, date and source (never proposed again).
    dollars = october["amount"].str.replace("$", "").str.replace(",", "").astype(float)
    kept = october["status"] != "Suspended"
    assert frame["amount"].tolist() == pytest.approx((dollars[kept] * 83.1).tolist())
    # The approved outcome, by its hash: Churned is yes, Active no, Suspended left out and counted.
    assert set(frame["status"]) == {0, 1}
    outcome = replayed.outcome
    assert outcome is not None and outcome.excluded == int((~kept).sum()) and outcome.rows_out == len(frame)
    assert set(frame["active"].dropna()) <= {0.0, 1.0}


def test_scoring_replays_every_part_but_the_outcome(approved: Prescription) -> None:
    october = ps.october().drop(columns=["status"])
    replayed = ps.replay(approved, [ps.replay_file(october)], mode="score")
    assert "outcome" not in replayed.replayed
    assert replayed.replayed == ("recipe", "contract")
    assert replayed.frame is not None and len(replayed.frame) == len(october)


# ---------------------------------------------------------------------------
# A replay that sees a change stops and asks; it never re-learns
# ---------------------------------------------------------------------------
def test_a_contract_breach_that_breaks_a_step_is_prescription_drift(
    approved: Prescription, monkeypatch: pytest.MonkeyPatch
) -> None:
    october = ps.october()
    october["spend"] = ["high", "low", "medium"] * (len(october) // 3)
    stopped = _stopped(approved, october, monkeypatch)
    assert stopped.code == PRESCRIPTION_DRIFT
    (finding,) = stopped.findings
    assert (finding.detector, finding.column, finding.stops) == ("MEANING_CHANGED", "spend", True)
    assert stopped.question is not None and stopped.question.blocking
    gone = _stopped(approved, ps.october().drop(columns=["spend"]), monkeypatch)
    assert gone.code == PRESCRIPTION_DRIFT and gone.findings[0].detector == "MISSING_COLUMN"


def test_a_new_value_stops_and_asks(approved: Prescription, monkeypatch: pytest.MonkeyPatch) -> None:
    october = ps.october()
    october.loc[:39, "plan"] = "platinum"
    stopped = _stopped(approved, october, monkeypatch)
    assert stopped.code == PRESCRIPTION_DRIFT
    finding = next(f for f in stopped.findings if f.stops)
    assert (finding.detector, finding.column) == ("NEW_VALUE", "plan")
    assert finding.question.examples == ("platinum",) and "40 rows" in finding.question.text
    spelling = ps.october()
    spelling.loc[:9, "active"] = "ha"
    stopped = _stopped(approved, spelling, monkeypatch)
    assert (stopped.code, stopped.findings[0].detector, stopped.findings[0].column) == (
        PRESCRIPTION_DRIFT,
        "NEW_VALUE",
        "active",
    ), "a yes/no spelling the approved step has no meaning for is asked, not failed quietly under the limit"
    outcome = ps.october()
    outcome.loc[:19, "status"] = "Paused"
    stopped = _stopped(approved, outcome, monkeypatch)
    assert (stopped.code, stopped.findings[0].column) == (PRESCRIPTION_DRIFT, "status")


def test_a_changed_column_stops_and_asks(approved: Prescription, monkeypatch: pytest.MonkeyPatch) -> None:
    october = ps.october()
    october["plan"] = range(len(october))
    stopped = _stopped(approved, october, monkeypatch)
    finding = next(f for f in stopped.findings if f.stops)
    assert (stopped.code, finding.detector, finding.column) == (PRESCRIPTION_DRIFT, "MEANING_CHANGED", "plan")


@pytest.mark.parametrize(
    ("amounts", "code"),
    [
        (lambda n: ["€ 12.50"] * n, "CURRENCY_FOREIGN"),
        (lambda n: ["€ 12.50", "$ 7.00"] * (n // 2), "CURRENCY_MIXED"),
    ],
)
def test_a_new_currency_stops_and_asks(
    approved: Prescription, monkeypatch: pytest.MonkeyPatch, amounts: Callable[[int], list[str]], code: str
) -> None:
    october = ps.october()
    october["amount"] = amounts(len(october))
    stopped = _stopped(approved, october, monkeypatch)
    assert stopped.code == code and stopped.part == "currency"
    assert stopped.findings[0].column == "amount" and "never added" in stopped.findings[0].question.text


def test_a_different_locale_stops_and_asks(approved: Prescription, monkeypatch: pytest.MonkeyPatch) -> None:
    october = ps.october()
    october["joined"] = [f"{1 + i % 12:02d}/{13 + i % 16:02d}/2026" for i in range(len(october))]
    stopped = _stopped(approved, october, monkeypatch)
    assert stopped.code == PRESCRIPTION_DRIFT and stopped.part == "locale"
    assert stopped.findings[0].detector == "LOCALE_CHANGED" and "date order" in stopped.message


def test_a_file_read_another_way_stops_and_asks(
    approved: Prescription, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = _stopped(approved, ps.october(), monkeypatch, intake=ps.intake_report(header_row=3))
    assert stopped.code == PRESCRIPTION_DRIFT and stopped.findings[0].detector == "INTAKE_CHANGED"


def test_a_breach_that_breaks_no_step_is_a_warning_and_a_question(approved: Prescription) -> None:
    october = ps.october()
    october["tenure_months"] = range(len(october))
    october.loc[: len(october) // 4, "spend"] = None
    october.loc[:59, "city"] = "City 99"
    replayed = ps.replay(approved, [ps.replay_file(october)], mode="train")
    found = {(w.detector, w.column) for w in replayed.warnings}
    assert {("NEW_COLUMN", "tenure_months"), ("VALUE_DRIFT", "spend"), ("NEW_CATEGORY", "city")} <= found
    assert all(not w.stops and not w.question.blocking for w in replayed.warnings)
    assert replayed.questions and all(len(q.options) == 2 for q in replayed.questions)
    assert replayed.frame is not None and "tenure_months" in replayed.frame.columns


def test_the_contract_lists_no_rare_value_and_no_id(approved: Prescription) -> None:
    frame = ps.customers()
    frame.loc[0, "plan"] = "a value one person holds"
    contract = learn_contract(frame, primary_key="customer_id", target="status")
    plan = contract.column("plan")
    ident = contract.column("customer_id")
    assert plan is not None and ident is not None
    assert "a value one person holds" not in {v.value for v in plan.top} and not plan.closed
    assert ident.role == "id" and ident.top == () and ident.p50 is None
    assert isinstance(contract.columns[0], ColumnContract)


# ---------------------------------------------------------------------------
# PRESCRIBE: the case's approved items, written once, with goal sections; the record carries the hash
# ---------------------------------------------------------------------------
def test_prescribe_writes_the_prescription_with_goal_sections_and_the_record_carries_its_hash(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    bodies: list[dict[str, Any]] = []

    def ledger(kind: str, actor: str, body: Any) -> str:
        bodies.append(dict(body))
        return f"entry-{len(bodies)}"

    prescriber = CasePrescriber(storage, load_files=w.load)
    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        case = act(vs.deps_for(storage, w, prescribe=prescriber, ledger=ledger), case)
        if name == "approve":
            break
    case = CaseStore(storage).get(vs.CLIENT, case.case_id)
    assert case.prescriptions, "PRESCRIBE ran after the approval"
    written = case.prescriptions[-1].prescription_hash
    stored = load_prescription(storage, vs.CLIENT, case.case_id, written)
    approval_body = next(b for b in bodies if b.get("proposal_kind") == "prescription")
    assert (
        approval_body["prescription_hash"] == written
    ), "M246's record names the prescription PRESCRIBE wrote"
    section = stored.section()
    assert section is not None and section.goal_id == vs.GOAL_A and section.active
    assert section.roles.get("outcome") == "converted" and section.brief_hash
    assert section.split is not None and section.time_settings is not None
    assert {g.goal_id for g in stored.goals} == {g.goal_id for g in case.goals}
    (part,) = stored.files
    assert part.upload_ids == (vs.FIRST,) and part.contract is not None
    assert part.contract.column("converted") is not None
    ref = bound(storage, upload_id=vs.FIRST)
    assert ref is not None and ref.prescription_hash == written and ref.case_id == case.case_id
    assert prescriber.digest(case, None) == prescriber.digest(case, None), "the same case hashes the same"


def test_next_months_file_added_to_a_prescribed_case_is_bound_to_its_prescription(tmp_path: Path) -> None:
    """Review fix (DEC-1813 (s)): a file added after PRESCRIBE replays the approved preparation at training."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    prescriber = CasePrescriber(storage, load_files=w.load)
    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        case = act(vs.deps_for(storage, w, prescribe=prescriber), case)
        if name == "next month":
            break
    case = CaseStore(storage).get(vs.CLIENT, case.case_id)
    assert vs.SECOND in case.upload_ids()
    ref = bound(storage, upload_id=vs.SECOND)
    assert ref is not None and ref.prescription_hash == case.prescriptions[-1].prescription_hash
    assert bound(storage, upload_id=vs.FIRST) is not None


def test_a_file_added_to_a_case_with_no_stored_prescription_is_bound_to_nothing(tmp_path: Path) -> None:
    """With M235's default digest no prescription is stored, so nothing is bound (switch-off visits included)."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        case = act(vs.deps_for(storage, w), case)
        if name == "next month":
            break
    assert bound(storage, upload_id=vs.SECOND) is None
    assert not any(key.endswith("prescription_ref.json") for key in storage.list_keys(""))


def test_without_a_prescriber_the_visit_keeps_its_own_digest(tmp_path: Path) -> None:
    """Unchanged: with no PRESCRIBE port the record carries M235's default digest, as before."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    bodies: list[dict[str, Any]] = []

    def ledger(kind: str, actor: str, body: Any) -> str:
        bodies.append(dict(body))
        return f"entry-{len(bodies)}"

    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        case = act(vs.deps_for(storage, w, ledger=ledger), case)
        if name == "approve":
            break
    body = next(b for b in bodies if b.get("proposal_kind") == "prescription")
    case = CaseStore(storage).get(vs.CLIENT, case.case_id)
    assert body["prescription_hash"] == case.prescriptions[-1].prescription_hash
    assert not storage.list_keys(f"cases/{vs.CLIENT}/{case.case_id}/prescriptions/")


def test_a_cases_money_columns_keep_their_unit_and_rate(approved: Prescription) -> None:
    assert approved.locale is not None and approved.locale.money_basis is not None
    basis = approved.locale.money_basis
    (column,) = basis.columns
    assert (column.column, column.written_in, column.reported_in) == ("amount", ("USD",), "INR")
    assert column.rates[0].step_params()["rate"] == 83.1
    part = approved.files[0]
    assert part.recipe is not None
    again = money_basis(part.recipe.steps, workspace_currency="INR", rates=[ps.rate()])
    assert again.basis_hash == approved.locale.basis_hash
    assert approved.locale.profile_hash == approved.locale.profile.profile_hash  # type: ignore[union-attr]


def test_the_intake_decision_is_what_a_different_read_would_break() -> None:
    report = ps.intake_report(sheet="Data", header_row=2)
    decision = IntakeDecision.from_report(report)
    assert decision.decisions() == {
        "source_format": "csv",
        "sheet": "Data",
        "header_row": 2,
        "header_rows": 1,
        "turned_off": False,
    }


def test_guided_setups_record_names_its_recipe_as_a_one_part_prescription() -> None:
    from api.routes.agent import _one_part_hash

    recipe = ps.recipe()
    assert _one_part_hash(recipe) == one_part(recipe).prescription_hash
    assert _one_part_hash(None) is None
