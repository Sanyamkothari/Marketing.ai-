"""`keep_latest_per_id` (Plan O M238, DEC-1808 (a)(b)): stateless, point in time, counted, replayed, refused on the
ID and the outcome, and conflicting duplicates never guessed."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import engine.agent.latest as latest_module
from engine.agent.advisor import NEVER_TICKED_STEPS, ordered_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.latest import KeepLatestError, find_conflicts, keep_latest_per_id, plan_keep_latest
from engine.agent.recipe import STEP_PHASE, RecipeError, check_recipe, run_recipe
from engine.doctor.renames import recipe_columns
from engine.doctor.reshape_guard import doctor_levels
from engine.utils.time import utc_now

KEY, TIME, SNAP, TARGET = "customer_id", "seen", "as_of", "churned"
RESHAPE = (AgentLevel.CLEAN, AgentLevel.DERIVE, AgentLevel.RESHAPE)


def monthly() -> pd.DataFrame:
    """Three monthly exports of one customer list, each row described as of the export's month end."""
    return pd.DataFrame(
        {
            KEY: ["C1", "C1", "C1", "C2", "C2", "C3", "C3"],
            TIME: [
                "2024-01-05",
                "2024-03-01",
                "2024-04-15",
                "2024-02-01",
                "2024-02-01",
                "2024-01-01",
                "2024-02-20",
            ],
            SNAP: ["2024-03-31"] * 7,
            "plan": ["basic", "plus", "max", "basic", "basic", "plus", "basic"],
            TARGET: [0, 0, 1, 0, 0, 1, 1],
        }
    )


def step(order: int = 1, **params: Any) -> RecipeStep:
    base: dict[str, Any] = {"time_column": TIME, "snapshot_column": SNAP, "dayfirst": False}
    return RecipeStep(
        order=order, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column=KEY, params={**base, **params}
    )


def run(data: pd.DataFrame, steps: list[RecipeStep], **kwargs: Any) -> Any:
    return run_recipe(
        data,
        steps,
        upload_id="u",
        primary_key=KEY,
        target=TARGET,
        levels=kwargs.pop("levels", RESHAPE),
        max_failure_pct=kwargs.pop("max_failure_pct", 5.0),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# What it keeps
# ---------------------------------------------------------------------------
def test_each_id_keeps_its_latest_row_on_or_before_its_snapshot() -> None:
    out = run(monthly(), [step()]).frame
    assert out[KEY].tolist() == ["C1", "C2", "C3"]
    # C1's April row is after the 31 March snapshot: never kept, so March's row is.
    assert out.loc[out[KEY] == "C1", "plan"].item() == "plus"
    assert out.loc[out[KEY] == "C3", TIME].item() == "2024-02-20"
    assert list(out.columns) == list(monthly().columns)


def test_it_never_keeps_a_row_dated_after_the_snapshot() -> None:
    data = monthly()
    data[SNAP] = [
        "2024-03-31",
        "2024-03-31",
        "2024-03-31",
        "2024-01-31",
        "2024-01-31",
        "2024-02-29",
        "2024-02-29",
    ]
    # C2's only rows are after its snapshot (31 Jan): it has nothing to keep, and nothing later is kept instead.
    # Losing a whole ID is counted (its rows are failures), so with the default 5% limit the step stops ...
    with pytest.raises(RecipeError) as stopped:
        run(data, [step()])
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"
    assert "every row is dated after its snapshot" in stopped.value.message
    # ... and with room for it, the ID is still never kept from a later row.
    out = run(data, [step()], max_failure_pct=50.0).frame
    kept = pd.to_datetime(out[TIME])
    snapshot = pd.to_datetime(out[SNAP])
    assert (kept <= snapshot).all()
    assert "C2" not in set(out[KEY])


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 6), st.integers(0, 40), st.integers(0, 40), st.integers(0, 3)),
        min_size=1,
        max_size=40,
    )
)
def test_property_no_kept_row_is_after_its_ids_snapshot(rows: list[tuple[int, int, int, int]]) -> None:
    start = pd.Timestamp("2024-01-01")
    data = pd.DataFrame(
        {
            KEY: [f"C{k}" for k, _, _, _ in rows],
            TIME: [start + pd.Timedelta(days=t) for _, t, _, _ in rows],
            SNAP: [start + pd.Timedelta(days=s) for _, _, s, _ in rows],
            "v": [v for _, _, _, v in rows],
        }
    )
    out = keep_latest_per_id(
        data,
        key=KEY,
        params={"time_column": TIME, "snapshot_column": SNAP, "ties": "last_in_file"},
        max_failure_pct=100,
    ).frame
    snapshot_of = data.groupby(KEY)[SNAP].max()
    for _, row in out.iterrows():
        assert row[TIME] <= snapshot_of[row[KEY]]
        eligible = data[(data[KEY] == row[KEY]) & (data[TIME] <= snapshot_of[row[KEY]])]
        assert row[TIME] == eligible[TIME].max()
    assert out[KEY].is_unique


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 5), st.integers(0, 10), st.integers(0, 2)), min_size=1, max_size=30),
    st.sampled_from(["last_in_file", "first_in_file"]),
    st.randoms(use_true_random=False),
)
def test_property_stateless_an_ids_row_depends_only_on_its_own_rows(
    rows: list[tuple[int, int, int]], ties: str, rng: Any
) -> None:
    """Split the file by ID in any way, keep each part, put the parts together: the same rows as the whole file."""
    data = pd.DataFrame(
        {
            KEY: [f"C{k}" for k, _, _ in rows],
            TIME: [pd.Timestamp("2024-01-01") + pd.Timedelta(days=t) for _, t, _ in rows],
            "v": [v for _, _, v in rows],
        }
    )
    params = {"time_column": TIME, "snapshot_column": None, "ties": ties}
    whole = keep_latest_per_id(data, key=KEY, params=params, max_failure_pct=100).frame
    ids = sorted(set(data[KEY]))
    chosen = {i for i in ids if rng.random() < 0.5}
    parts = [data[data[KEY].isin(chosen)], data[~data[KEY].isin(chosen)]]
    pieces = [
        keep_latest_per_id(p, key=KEY, params=params, max_failure_pct=100).frame for p in parts if len(p)
    ]
    together = pd.concat(pieces).sort_values([KEY, TIME, "v"]).reset_index(drop=True)
    assert together.equals(whole.sort_values([KEY, TIME, "v"]).reset_index(drop=True))


def test_the_date_order_is_frozen_at_planning_and_never_guessed_at_run() -> None:
    data = monthly()
    data[TIME] = [
        "05/01/2024",
        "01/03/2024",
        "15/04/2024",
        "01/02/2024",
        "01/02/2024",
        "01/01/2024",
        "20/02/2024",
    ]
    data[SNAP] = ["31/03/2024"] * 7
    params = plan_keep_latest(data, time_column=TIME, snapshot_column=SNAP)
    assert params["dayfirst"] == {TIME: True, SNAP: True}
    out = keep_latest_per_id(data, key=KEY, params=params, max_failure_pct=5.0).frame
    assert out.loc[out[KEY] == "C1", "plan"].item() == "plus"
    # A later file with only ambiguous dates is read by the frozen order, not decided again.
    later = data[data[TIME].isin(["01/03/2024", "01/02/2024", "01/01/2024"])]
    assert keep_latest_per_id(later, key=KEY, params=params, max_failure_pct=5.0).rows_out == 3
    unproven = {**params, "dayfirst": None}
    with pytest.raises(KeepLatestError) as stopped:
        keep_latest_per_id(later, key=KEY, params=unproven, max_failure_pct=5.0)
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"


# ---------------------------------------------------------------------------
# Conflicting duplicates
# ---------------------------------------------------------------------------
def conflicting() -> pd.DataFrame:
    data = monthly()
    data.loc[4, "plan"] = "max"  # C2's two rows of 1 Feb now disagree
    return data


def test_identical_copies_are_one_row_and_counted() -> None:
    result = keep_latest_per_id(monthly(), key=KEY, params=step().params, max_failure_pct=5.0)
    assert result.identical_copies == 1 and result.conflicts == 0
    assert result.after_snapshot == 1


def test_conflicting_duplicates_stop_the_step_rather_than_being_guessed() -> None:
    with pytest.raises(RecipeError) as stopped:
        run(conflicting(), [step()])
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"
    assert "different rows on their latest date" in stopped.value.message
    counts = find_conflicts(conflicting(), key=KEY, params=step().params)
    assert (counts.ids, counts.rows) == (1, 2)


@pytest.mark.parametrize(("ties", "plan"), [("last_in_file", "max"), ("first_in_file", "basic")])
def test_an_answered_conflict_keeps_the_row_the_person_chose(ties: str, plan: str) -> None:
    out = run(conflicting(), [step(ties=ties)]).frame
    assert out.loc[out[KEY] == "C2", "plan"].item() == plan


def test_a_conflict_only_in_the_outcome_is_still_a_conflict() -> None:
    data = monthly()
    data.loc[4, TARGET] = 1
    with pytest.raises(RecipeError):
        run(data, [step()])


# ---------------------------------------------------------------------------
# Receipt, failures, replay
# ---------------------------------------------------------------------------
def test_the_receipt_counts_rows_in_out_and_failures_with_the_point_in_time_check() -> None:
    data = monthly()
    data.loc[len(data)] = ["C4", None, "2024-03-31", "basic", 0]  # no date: belongs to no time
    receipt = run(data, [step()], max_failure_pct=20.0, leak_check="full").receipt
    only = receipt.steps[0]
    assert (receipt.rows_in, receipt.rows_out) == (8, 3)
    assert (only.rows, only.changed, only.failed) == (8, 5, 1)
    assert only.leak_check is not None and only.leak_check.startswith("Full future-data check: 3 of 3 IDs")
    narrow = run(monthly(), [step()], leak_check="narrow").receipt.steps[0]
    assert narrow.leak_check is not None and narrow.leak_check.startswith("Narrow")
    assert run(monthly(), [step()]).receipt.steps[0].leak_check is None  # a preview runs no check


def test_too_many_unreadable_rows_stop_the_step() -> None:
    data = monthly()
    data[TIME] = ["not a date"] * 6 + ["2024-01-01"]
    with pytest.raises(RecipeError) as stopped:
        run(data, [step()])
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"


def test_the_probe_catches_a_rule_that_lets_a_later_row_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: an eligibility that ignores the snapshot must be caught by the point-in-time probe."""
    real = latest_module._eligibility

    def ignores_snapshot(keys: Any, has_key: Any, event: Any, snap: Any) -> Any:
        known, usable, _ = real(keys, has_key, event, snap)
        return known, usable, usable

    data = monthly().drop(index=[2])  # no row after the snapshot in the file itself
    monkeypatch.setattr(latest_module, "_eligibility", ignores_snapshot)
    with pytest.raises(RecipeError) as caught:
        run(data, [step()], leak_check="full")
    assert "FUTURE_EVENTS_LEAKED" in caught.value.message


def test_replay_on_the_same_and_next_months_file_is_identical_and_the_recipe_reads_back() -> None:
    steps = ordered_steps(
        [
            step(),
            RecipeStep(order=1, kind=RecipeStepKind.NORMALISE_TEXT, column="plan", params={"strip": True}),
        ]
    )
    first, again = run(monthly(), list(steps)), run(monthly(), list(steps))
    assert first.frame.equals(again.frame)
    assert first.receipt.recipe_hash == again.receipt.recipe_hash == recipe_hash(steps)
    recipe = DataRecipe(
        recipe_id="r1",
        use_case_id="retail-win-back",
        source_fingerprint="f",
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key=KEY,
        target=TARGET,
        levels=RESHAPE,
        created_at=utc_now(),
    )
    loaded = DataRecipe.model_validate_json(recipe.model_dump_json())
    assert loaded.prepares_like(recipe)
    next_month = monthly().assign(**{SNAP: "2024-04-30"})
    replayed = run(
        next_month.drop(columns=[TARGET]), list(loaded.steps)
    ).frame  # a scoring file has no outcome
    assert replayed.loc[replayed[KEY] == "C1", "plan"].item() == "max"


# ---------------------------------------------------------------------------
# Refusals: the ID, the outcome, the level, the order
# ---------------------------------------------------------------------------
def check(
    steps: list[RecipeStep], levels: tuple[AgentLevel, ...] = RESHAPE, primary_key: str | None = KEY
) -> None:
    check_recipe(
        steps, columns=list(monthly().columns), primary_key=primary_key, target=TARGET, levels=levels
    )


def test_it_is_keyed_on_the_id_only() -> None:
    other = step().model_copy(update={"column": "plan"})
    with pytest.raises(RecipeError, match="per ID column"):
        check([other])


@pytest.mark.parametrize("param", ["time_column", "snapshot_column"])
def test_the_outcome_never_chooses_the_rows(param: str) -> None:
    with pytest.raises(RecipeError, match="outcome column"):
        on_target: dict[str, Any] = {param: TARGET}
        check([step(**on_target)])


def test_without_an_id_it_still_never_keys_on_the_outcome() -> None:
    on_target = step().model_copy(update={"column": TARGET})
    with pytest.raises(RecipeError):
        check([on_target], primary_key=None)


def test_it_needs_the_reshape_level_which_data_doctor_turns_on_for_every_goal() -> None:
    plain = (AgentLevel.CLEAN, AgentLevel.DERIVE)
    with pytest.raises(RecipeError, match="may not use keep_latest_per_id"):
        check([step()], levels=plain)
    assert doctor_levels(plain, unified=False) == plain
    check([step()], levels=doctor_levels(plain, unified=True))


def test_rows_are_made_one_per_id_once_only() -> None:
    combine = RecipeStep(
        order=2,
        kind=RecipeStepKind.COMBINE_ROWS,
        column=KEY,
        params={"time_column": TIME, "features": [{"name": "n", "function": "count", "column": None}]},
    )
    with pytest.raises(RecipeError, match="only once"):
        check([step(), combine])
    with pytest.raises(RecipeError):
        check([combine.model_copy(update={"order": 1}), step(2)])


def test_bad_parameters_are_refused_with_a_coded_reason() -> None:
    for params in ({"ties": "random"}, {"bogus": 1}, {"time_column": KEY}, {"dayfirst": "yes"}):
        with pytest.raises(RecipeError) as refused:
            check([step(**params)])
        assert refused.value.code == "RECIPE_STEP_INVALID"
    with pytest.raises(RecipeError) as missing:
        check([step(time_column="nope")])
    assert missing.value.code == "RECIPE_COLUMN_MISSING"
    with pytest.raises(ValueError, match="creates no column"):
        RecipeStep(order=1, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column=KEY, new_column="x", params={})


def test_it_runs_where_combining_does_and_is_never_ticked() -> None:
    assert STEP_PHASE[RecipeStepKind.KEEP_LATEST_PER_ID] == STEP_PHASE[RecipeStepKind.COMBINE_ROWS]
    assert RecipeStepKind.KEEP_LATEST_PER_ID in NEVER_TICKED_STEPS
    parse = RecipeStep(order=1, kind=RecipeStepKind.PARSE_DATE, column=TIME, params={"dayfirst": False})
    assert [s.kind for s in ordered_steps([step(), parse])] == [
        RecipeStepKind.PARSE_DATE,
        RecipeStepKind.KEEP_LATEST_PER_ID,
    ]
    out = run(monthly(), list(ordered_steps([step(), parse]))).frame
    assert np.issubdtype(out[TIME].dtype, np.datetime64) and len(out) == 3


def test_a_changed_file_names_the_columns_the_step_reads() -> None:
    reads, _ = recipe_columns([step()])
    assert {KEY, TIME, SNAP} <= reads


# ---------------------------------------------------------------------------
# Review fixes: a lost ID is counted, the receipt says why rows left, the reshape seams know the step
# ---------------------------------------------------------------------------
def lost_id_file() -> pd.DataFrame:
    """a has a row on or before its snapshot (1 Feb) and one after; b's only row is after its snapshot."""
    return pd.DataFrame(
        {
            KEY: ["a", "a", "b"],
            TIME: ["2024-01-01", "2024-03-01", "2024-05-01"],
            SNAP: ["2024-02-01", None, "2024-04-01"],
            TARGET: [0, 0, 1],
        }
    )


def test_an_id_whose_every_row_is_after_its_snapshot_is_counted_as_lost() -> None:
    params = {"time_column": TIME, "snapshot_column": SNAP, "dayfirst": False}
    out = keep_latest_per_id(lost_id_file(), key=KEY, params=params, max_failure_pct=100.0)
    assert out.frame[KEY].tolist() == ["a"]
    assert (out.ids_lost, out.failed, out.after_snapshot) == (1, 1, 1)
    assert "IDs with no row on or before their snapshot, which are lost" in out.note
    conflicts = find_conflicts(lost_id_file(), key=KEY, params=params)
    assert (conflicts.ids_lost, conflicts.failed) == (1, 1)
    # Its rows are failures, so they fall under the failure limit: one of three rows is far above 5%.
    with pytest.raises(KeepLatestError) as stopped:
        keep_latest_per_id(lost_id_file(), key=KEY, params=params, max_failure_pct=5.0)
    assert stopped.value.code == "RECIPE_VALUES_UNCONVERTED"
    assert "1 IDs whose every row is dated after its snapshot" in stopped.value.message


def test_the_receipt_says_why_rows_were_left_out_and_which_ids_file_order_settled() -> None:
    data = pd.concat([monthly(), monthly().iloc[[2]]], ignore_index=True)  # one identical copy of C1's latest
    data.loc[5, TIME] = "2024-02-20"  # C3's two rows now share the latest date and differ in plan
    receipt = run(data, [step(ties="last_in_file")]).receipt.steps[0]
    assert receipt.note is not None
    assert receipt.note.startswith(
        f"{receipt.changed} of {receipt.rows} rows are left out: 1 are identical copies"
    )
    assert (
        "For 1 IDs the latest rows differ, and the row kept was chosen by its place in the file"
        in receipt.note
    )
    other = run(monthly(), [step()]).receipt.steps[0]
    assert other.note == (
        "4 of 7 rows are left out: 1 are identical copies; 1 are dated after their ID's snapshot; 2 are other "
        "rows of an ID that keeps one."
    )


def test_an_accepted_keep_latest_step_survives_re_advice() -> None:
    # Review fix: the step's column is always the ID, which `_touches_role` used to drop on every re-advice.
    from datetime import UTC, datetime

    from engine.agent.advisor import Advice
    from engine.agent.contracts import AgentConfidence, DecidedBy, Proposal, ProposalKind, ProposalState
    from engine.agent.session import _merge, start_session
    from tests.fixtures.agent_bench.make_messy import messy_frame
    from tests.unit.agent.helpers import context_for

    ctx = context_for(messy_frame())
    session = start_session(ctx, session_id="s1")
    accepted = Proposal(
        proposal_id="k1",
        kind=ProposalKind.RECIPE_STEP,
        title="Keep the latest row per customer",
        reason="Answered yes to the question.",
        step=RecipeStep(
            order=1,
            kind=RecipeStepKind.KEEP_LATEST_PER_ID,
            column="customer_id",
            params={
                "time_column": "snapshot_date",
                "snapshot_column": None,
                "dayfirst": False,
                "ties": "refuse",
            },
        ),
        evidence_ids=(session.tool_results[0].evidence_id,),
        confidence=AgentConfidence.CHECK,
        state=ProposalState.ACCEPTED,
        decided_by=DecidedBy.USER,
    )
    old = session.model_copy(update={"proposals": (*session.proposals, accepted)})
    advice = Advice(
        status=session.status,
        tool_results=session.tool_results,
        proposals=(),
        questions=(),
        assumptions=(),
        engine_hidden=(),
        stop_reason=None,
        primary_key="customer_id",
        target="converted_30d",
    )
    merged = _merge(old, advice, datetime(2026, 10, 12, tzinfo=UTC))
    kept = [p for p in merged.proposals if p.proposal_id == "k1"]
    assert len(kept) == 1 and kept[0].state is ProposalState.ACCEPTED


def test_memory_never_ticks_a_remembered_keep_latest_step() -> None:
    # Review fix: "As last time" must ask the step's own blocking question again (decision O3), as for combining.
    from datetime import UTC, datetime

    from engine.agent.session import SessionError, start_session
    from engine.doctor.memory import TreatmentMemory, use_same_treatment
    from tests.fixtures.agent_bench.make_messy import messy_frame
    from tests.unit.agent.helpers import context_for

    frame = messy_frame()
    ctx = context_for(frame)
    session = start_session(ctx, session_id="s1")
    latest = RecipeStep(
        order=1,
        kind=RecipeStepKind.KEEP_LATEST_PER_ID,
        column="customer_id",
        params={"time_column": "snapshot_date", "snapshot_column": None, "dayfirst": False, "ties": "refuse"},
    )
    when = datetime(2026, 9, 1, tzinfo=UTC)
    recipe = DataRecipe(
        recipe_id="rc-latest",
        use_case_id="targeted-advertisement",
        source_fingerprint="sha256:" + "0" * 64,
        steps=(latest,),
        recipe_hash=recipe_hash((latest,)),
        primary_key="customer_id",
        target="converted_30d",
        created_at=when,
    )
    memory = TreatmentMemory(
        use_case_id="targeted-advertisement",
        shape_hash="sha256:" + "1" * 64,
        columns=tuple(str(c) for c in frame.columns),
        recipe_id="rc-latest",
        recipe_upload_id="u-prepared",
        source_upload_id="u-source",
        steps=1,
        approved_at=when,
        remembered_at=when,
    )
    with pytest.raises(SessionError) as asked:
        use_same_treatment(session, ctx, recipe, memory)
    assert asked.value.code == "DOCTOR_MEMORY_COMBINE"
    assert "keeping the latest row" in asked.value.message


def test_the_helpers_profile_counts_the_rows_left_after_keeping_the_latest() -> None:
    # Review fix: after the step the advisor's prepared profile describes one row per ID.
    from engine.agent.advisor import _Builder, _prepared_context
    from tests.unit.agent.helpers import context_for

    data = monthly().rename(columns={TARGET: "reactivated_90d"})
    ctx = context_for(data, "retail-win-back")
    prepared = _prepared_context(_Builder(ctx), [step()], KEY, "reactivated_90d")
    assert len(prepared.frame) == 3 and prepared.profile.row_count == 3


def test_the_preview_takes_whole_ids_for_keep_latest() -> None:
    # Review fix: the first 1,000 rows could hold part of an ID's rows and show an older row as its latest.
    from api.routes.agent import PREVIEW_SAMPLE_ROWS, _preview_sample

    ids = [f"C{i:04d}" for i in range(PREVIEW_SAMPLE_ROWS + 5)]
    data = pd.DataFrame({KEY: ids + ids[:3], TIME: ["2024-01-01"] * len(ids) + ["2024-06-01"] * 3, TARGET: 0})
    recipe = DataRecipe(
        recipe_id="rc-latest",
        use_case_id="retail-win-back",
        source_fingerprint="sha256:" + "0" * 64,
        steps=(step(snapshot_column=None),),
        recipe_hash=recipe_hash((step(snapshot_column=None),)),
        primary_key=KEY,
        target=TARGET,
        created_at=utc_now(),
    )
    sample = _preview_sample(data, recipe)
    assert sample[KEY].nunique() == PREVIEW_SAMPLE_ROWS
    assert (sample[KEY] == "C0000").sum() == 2  # the later row of C0000, far down the file, is in the sample
