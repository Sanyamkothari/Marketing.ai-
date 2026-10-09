"""The pure part of the audit (Plan J M103): reading the groups, the claim the numbers can bear, the labels.

`parse_groups` reads a group column as contacted, held back or neither; `build_assignment_frame` and
`build_outcomes_frame` turn two uploaded frames into a campaign's own files; `check_randomness` tries to
tell the groups apart from the customer details (and only a failure to do so makes an assignment causal);
`decide_basis` maps what the person says and what the check found to the three labels, exactly; and
`label_report` / `audit_verdict` keep a descriptive-only campaign from ever reading "the campaign added N".
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.measurement.audit import (
    AUDIT_ARM_UNREADABLE,
    CAUSAL_LABEL,
    DECLARED_LABEL,
    DESCRIPTIVE_LABEL,
    AuditInputError,
    AuditReadout,
    RandomnessCheck,
    audit_verdict,
    build_assignment_frame,
    build_outcomes_frame,
    check_randomness,
    decide_basis,
    descriptive_summary,
    earliest_date,
    encode_features,
    label_report,
    parse_groups,
)
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population
from engine.pilot.plain import jargon_in

THRESHOLD = 0.6


def series(*values: object) -> pd.Series:
    return pd.Series(list(values), dtype="object")


# --- the group column ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("values", "treated", "held"),
    [
        ([1, 0, 1, 0], [True, False, True, False], [False, True, False, True]),
        (["1", "0", "1", "0"], [True, False, True, False], [False, True, False, True]),
        ([1.0, 0.0, 1.0, 0.0], [True, False, True, False], [False, True, False, True]),
        (["yes", "no", "Yes", "NO"], [True, False, True, False], [False, True, False, True]),
        (
            ["Treated", "Control", "treated", "control"],
            [True, False, True, False],
            [False, True, False, True],
        ),
        ([True, False, True, False], [True, False, True, False], [False, True, False, True]),
    ],
)
def test_the_usual_words_and_numbers_are_read_without_being_told(
    values: list[object], treated: list[bool], held: list[bool]
) -> None:
    split = parse_groups(pd.Series(values))
    assert (split.arm == "treated").tolist() == treated
    assert (split.arm == "holdout").tolist() == held
    assert split.offers == () and split.offer is None and split.blank == 0


def test_a_blank_group_is_in_neither_and_counted() -> None:
    split = parse_groups(series(1, None, 0, "", 1))
    assert split.arm.tolist() == ["treated", "suppressed", "holdout", "suppressed", "treated"]
    assert split.blank == 2


def test_several_offers_are_named_with_their_control_and_keep_their_given_order() -> None:
    values = series("none", "gold", "silver", "none", "silver", "gold")
    split = parse_groups(values, control_value="none", treated_values=("silver", "gold"))
    assert split.offers == ("silver", "gold") and split.control_level == "none"
    assert split.offer.dropna().tolist() == ["gold", "silver", "silver", "gold"]
    assert split.arm.tolist() == ["holdout", "treated", "treated", "holdout", "treated", "treated"]
    unordered = parse_groups(values, control_value="none")
    assert unordered.offers == ("gold", "silver"), "alphabetical when the person gives no order"


@pytest.mark.parametrize(
    "values",
    [
        series("a", "b", "a"),  # nothing says which is held back
        series(None, "", None),  # empty
        series(0, 0, 0),  # nobody contacted
    ],
)
def test_a_group_column_that_cannot_be_read_is_refused_in_plain_words(values: pd.Series) -> None:
    with pytest.raises(AuditInputError) as caught:
        parse_groups(values)
    assert caught.value.code == AUDIT_ARM_UNREADABLE and caught.value.path == "assignment.arm_column"
    assert jargon_in(str(caught.value)) == ()


def test_a_value_that_is_neither_control_nor_a_named_offer_is_refused() -> None:
    with pytest.raises(AuditInputError, match="neither a held-back value nor a contacted one"):
        parse_groups(series("none", "gold", "bronze"), control_value="none", treated_values=("gold",))


# --- the assignment and outcomes files --------------------------------------------------------------------
def assignment_file(**columns: object) -> pd.DataFrame:
    base: dict[str, object] = {"id": ["a", "b", "c", "d"], "grp": [1, 0, 1, 1]}
    return pd.DataFrame({**base, **columns})


def test_the_assignment_is_written_in_the_shape_of_a_scored_campaigns() -> None:
    built = build_assignment_frame(
        assignment_file(flag=[1, 1, 0, 1], age=[30, 40, 50, 60]),
        primary_key="id",
        arm_column="grp",
        intended_column="flag",
    )
    frame = built.assignment
    assert list(frame.columns) == ["id", "arm", "intended", "band"]
    assert frame["arm"].tolist() == ["treated", "holdout", "treated", "treated"]
    assert frame["intended"].tolist() == [True, True, False, True]
    assert list(built.features.columns) == ["age"], "the details used to test the claim, never stored"
    assert built.sent is None and built.rows_without_group == 0


def test_a_customer_with_no_group_is_never_intended() -> None:
    built = build_assignment_frame(assignment_file(grp=[1, None, 0, 1]), primary_key="id", arm_column="grp")
    assert built.assignment["arm"].tolist() == ["treated", "suppressed", "holdout", "treated"]
    assert built.assignment["intended"].tolist() == [True, False, True, True]
    assert built.rows_without_group == 1


def test_ids_are_compared_as_text_so_one_file_may_have_them_as_numbers() -> None:
    built = build_assignment_frame(
        pd.DataFrame({"id": [1.0, 2.0, 3.0], "grp": [1, 0, 1]}), primary_key="id", arm_column="grp"
    )
    outcomes, date = build_outcomes_frame(
        pd.DataFrame({"id": [1, 2, 3], "won": [1, 0, 0]}),
        primary_key="id",
        outcome_column="won",
        assignment=built,
    )
    assert built.assignment["id"].tolist() == outcomes["id"].tolist() == ["1", "2", "3"]
    assert date is None


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (assignment_file().drop(columns=["grp"]), "no column 'grp'"),
        (pd.concat([assignment_file(), assignment_file().head(1)]), "more than once"),
        (assignment_file(id=["a", "", "c", "d"]), "no customer id"),
    ],
)
def test_an_assignment_that_cannot_be_used_is_refused_with_its_reason(
    frame: pd.DataFrame, message: str
) -> None:
    with pytest.raises(AuditInputError, match=message):
        build_assignment_frame(frame, primary_key="id", arm_column="grp")


def test_the_id_column_can_be_named_differently_in_a_file() -> None:
    renamed = assignment_file().rename(columns={"id": "customer"})
    built = build_assignment_frame(renamed, primary_key="id", arm_column="grp", file_keys=("customer",))
    assert built.assignment.columns[0] == "id"
    with pytest.raises(AuditInputError, match="no column 'nobody'"):
        build_assignment_frame(renamed, primary_key="id", arm_column="grp", file_keys=("nobody",))


def test_the_date_of_contact_comes_from_one_file_and_never_both() -> None:
    built = build_assignment_frame(
        assignment_file(sent=["2026-05-01", "2026-05-02", None, "2026-05-04"]),
        primary_key="id",
        arm_column="grp",
        sent_date_column="sent",
    )
    outcomes = pd.DataFrame({"id": ["a", "b", "c", "d", "z"], "won": [1, 0, 0, 1, 0]})
    kept, date = build_outcomes_frame(outcomes, primary_key="id", outcome_column="won", assignment=built)
    assert date == "sent_date" and list(kept.columns) == ["id", "won", "sent_date"]
    assert kept["sent_date"].tolist()[:2] == ["2026-05-01", "2026-05-02"]
    assert pd.isna(kept["sent_date"].iloc[2]) and pd.isna(kept["sent_date"].iloc[4])
    with pytest.raises(AuditInputError, match="both"):
        build_outcomes_frame(
            outcomes.assign(day="2026-05-01"),
            primary_key="id",
            outcome_column="won",
            treatment_date_column="day",
            assignment=built,
        )
    own, own_date = build_outcomes_frame(
        outcomes.assign(day="2026-05-01"), primary_key="id", outcome_column="won", treatment_date_column="day"
    )
    assert own_date == "day" and list(own.columns) == ["id", "won", "day"]


def test_the_earliest_date_is_read_or_none_never_guessed() -> None:
    assert earliest_date(series("2026-05-03", "2026-05-01", None, "garbage")) == datetime(
        2026, 5, 1, tzinfo=UTC
    )
    assert earliest_date(series("nonsense", None)) is None


# --- the customer details used to test the claim ------------------------------------------------------------
def test_details_are_encoded_and_the_ones_that_can_tell_nothing_are_left_out() -> None:
    frame = pd.DataFrame(
        {
            "age": [30, 40, 50, 60, 70, 80],
            "region": list("AABBCC"),
            "constant": [1] * 6,
            "empty": [None] * 6,
            "who": list("uvwxyz"),  # a different text in every row: an id
            "member": [True, False, True, False, True, False],
            "joined": pd.to_datetime(["2020-01-01"] * 3 + ["2021-01-01"] * 3),
        }
    )
    encoded = encode_features(frame)
    assert list(encoded.columns) == ["age", "region", "member", "joined"]
    assert str(encoded["region"].dtype) == "category" and encoded["member"].tolist() == [
        1.0,
        0.0,
        1.0,
        0.0,
        1.0,
        0.0,
    ]
    assert encoded["joined"].iloc[3] - encoded["joined"].iloc[0] == 366.0


def built_from(n: int, *, seed: int, targeted: bool) -> object:
    rng = np.random.default_rng(seed)
    loyalty = rng.normal(size=n)
    chance = 1.0 / (1.0 + np.exp(-2.5 * loyalty)) if targeted else np.full(n, 0.7)
    group = (rng.random(n) < chance).astype(int)
    frame = pd.DataFrame(
        {
            "id": [f"C{i:06d}" for i in range(n)],
            "grp": group,
            "loyalty": loyalty,
            "tenure": rng.integers(1, 90, n),
        }
    )
    return build_assignment_frame(frame, primary_key="id", arm_column="grp")


def test_random_groups_pass_the_test_and_groups_chosen_by_the_details_fail_it_naming_them() -> None:
    passed = check_randomness(
        built_from(6_000, seed=1, targeted=False), primary_key="id", threshold=THRESHOLD
    )
    assert passed.status == "passed" and passed.auc is not None and passed.auc <= THRESHOLD
    assert passed.signals == () and passed.columns_used == 2
    failed = check_randomness(built_from(6_000, seed=2, targeted=True), primary_key="id", threshold=THRESHOLD)
    assert failed.status == "failed" and failed.auc is not None and failed.auc > THRESHOLD
    assert failed.signals[0] == "loyalty"
    again = check_randomness(built_from(6_000, seed=2, targeted=True), primary_key="id", threshold=THRESHOLD)
    assert again == failed, "the same file always gets the same answer"


def test_the_test_is_not_run_without_details_or_with_a_group_that_is_too_small() -> None:
    bare = build_assignment_frame(assignment_file(), primary_key="id", arm_column="grp")
    result = check_randomness(bare, primary_key="id", threshold=THRESHOLD)
    assert result.status == "not_run" and result.auc is None and "customer details" in str(result.reason)
    tiny = build_assignment_frame(assignment_file(age=[30, 40, 50, 60]), primary_key="id", arm_column="grp")
    small = check_randomness(tiny, primary_key="id", threshold=THRESHOLD)
    assert small.status == "not_run" and "at least 30" in str(small.reason)
    for check in (result, small):
        assert jargon_in(str(check.reason)) == ()


def test_the_test_is_run_inside_the_population_the_campaign_is_measured_on() -> None:
    """Customers who were never meant to be contacted must not make a random split look targeted."""
    rng = np.random.default_rng(3)
    n = 6_000
    loyalty = rng.normal(size=n)
    meant = loyalty > 0.0  # only the loyal were meant to be contacted: a targeted campaign...
    group = (rng.random(n) < 0.7).astype(int)  # ...with a random hold-out among them
    frame = pd.DataFrame(
        {"id": [f"C{i:06d}" for i in range(n)], "grp": group, "meant": meant, "loyalty": loyalty}
    )
    inside = build_assignment_frame(frame, primary_key="id", arm_column="grp", intended_column="meant")
    assert check_randomness(inside, primary_key="id", threshold=THRESHOLD).status == "passed"


def test_several_offers_are_each_tested_against_the_shared_control() -> None:
    rng = np.random.default_rng(4)
    n = 6_000
    loyalty = rng.normal(size=n)
    group = np.where(
        rng.random(n) < 0.3, "none", np.where(loyalty > 0, "gold", "silver")
    )  # gold goes to the loyal
    frame = pd.DataFrame({"id": [f"C{i:06d}" for i in range(n)], "grp": group, "loyalty": loyalty})
    built = build_assignment_frame(
        frame, primary_key="id", arm_column="grp", control_value="none", treated_values=("gold", "silver")
    )
    result = check_randomness(built, primary_key="id", threshold=THRESHOLD)
    assert result.status == "failed" and result.signals == ("loyalty",)


# --- the three labels, exactly ------------------------------------------------------------------------------
PASSED = RandomnessCheck(status="passed", auc=0.51, threshold=THRESHOLD, rows_used=5_000, columns_used=3)
FAILED = RandomnessCheck(
    status="failed", auc=0.83, threshold=THRESHOLD, rows_used=5_000, signals=("loyalty", "tenure")
)
NOT_RUN = RandomnessCheck(
    status="not_run", threshold=THRESHOLD, reason="The file has no customer details to test with."
)


@pytest.mark.parametrize(
    ("stated", "check", "basis", "causal", "label"),
    [
        ("random", PASSED, "verified_random", True, "Causal"),
        ("random", FAILED, "not_random", False, "Descriptive only"),
        ("random", NOT_RUN, "declared_random", False, "Random by your statement, not verified"),
        ("not_random", PASSED, "not_random", False, "Descriptive only"),
        ("not_random", FAILED, "not_random", False, "Descriptive only"),
        ("not_random", NOT_RUN, "not_random", False, "Descriptive only"),
    ],
)
def test_causal_only_when_the_engine_verified_the_assignment_random(
    stated: str, check: RandomnessCheck, basis: str, causal: bool, label: str
) -> None:
    got_basis, got_causal, got_label, explanation = decide_basis(stated, check)  # type: ignore[arg-type]
    assert (got_basis, got_causal, got_label) == (basis, causal, label)
    assert (CAUSAL_LABEL, DECLARED_LABEL, DESCRIPTIVE_LABEL) == (
        "Causal",
        "Random by your statement, not verified",
        "Descriptive only",
    )
    assert jargon_in(explanation) == ()
    if basis == "not_random":
        assert (
            "do not show what the campaign changed" in explanation
            or "does not show" in explanation
            or "not" in explanation
        )
    if check is FAILED and stated == "random":
        assert "loyalty and tenure" in explanation and "0.83" in explanation


# --- what is stored, and what a reader may say about it -------------------------------------------------------
def measured(effect: float = 0.05) -> tuple[object, object]:
    sim = population(6_000, 0.10, effect, seed=11, control_share=0.2)
    assignment = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "arm": np.where(sim.scores["control_group"], "holdout", "treated"),
            "intended": True,
            "band": pd.NA,
        }
    )
    report = measure_campaign(
        assignment,
        sim.outcomes,
        run_id="c_20261009_aaaaaaaa",
        primary_key="customer_id",
        outcome_column="converted",
        intended_column="intended",
        treatment_time=AS_OF,
        treatment_date_column="treatment_date",
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        as_of=AS_OF,
    )
    return sim, report


def readout(basis: str, *, causal: bool, label: str) -> AuditReadout:
    return AuditReadout(
        campaign_id="c_20261009_aaaaaaaa",
        stated_basis="random",
        causal_basis=basis,  # type: ignore[arg-type]
        causal=causal,
        label=label,
        explanation="why",
        randomness=NOT_RUN,
        assignment_upload_id="u_1",
        assignment_file_name="a.csv",
        assignment_rows=6_000,
        rows_without_group=0,
        outcome_kind="binary",
        outcome_is_good=True,
        sent_dates="outcomes",
        audited_at=AS_OF,
    )


def test_a_verified_report_is_stored_exactly_as_measured() -> None:
    _, report = measured()
    assert label_report(report, "verified_random") is report
    verdict = audit_verdict(report, readout("verified_random", causal=True, label=CAUSAL_LABEL))
    assert verdict is not None and verdict.kind.value == "added" and not verdict.detail.startswith("Random")


def test_a_report_random_by_statement_loses_its_claim_but_keeps_its_numbers_and_gets_a_labelled_verdict() -> (
    None
):
    _, report = measured()
    labelled = label_report(report, "declared_random")
    assert labelled.causal is False and labelled.summary.startswith(DECLARED_LABEL)
    assert labelled.absolute_lift == report.absolute_lift and labelled.treated_rows == report.treated_rows
    verdict = audit_verdict(labelled, readout("declared_random", causal=False, label=DECLARED_LABEL))
    assert verdict is not None and verdict.kind.value == "added"
    assert verdict.detail.startswith(DECLARED_LABEL), "the label is in front of what it says"


def test_a_descriptive_report_never_says_caused_and_has_no_verdict() -> None:
    _, report = measured()
    labelled = label_report(report, "not_random")
    assert labelled.causal is False
    assert "caused" not in labelled.summary and "does not show what the campaign changed" in labelled.summary
    assert labelled.summary == descriptive_summary(report)
    assert audit_verdict(labelled, readout("not_random", causal=False, label=DESCRIPTIVE_LABEL)) is None
    assert jargon_in(labelled.summary) == ()


def test_an_early_look_keeps_its_own_sentence_whatever_the_label() -> None:
    _, report = measured()
    early = report.model_copy(
        update={"early_look": True, "summary": "Early look, before the planned analysis date."}
    )
    for basis in ("declared_random", "not_random"):
        labelled = label_report(early, basis)  # type: ignore[arg-type]
        assert labelled.summary == early.summary and labelled.causal is False
    assert audit_verdict(early, readout("verified_random", causal=True, label=CAUSAL_LABEL)) is None


# --- a two-column key (customer and snapshot date) ---------------------------------------------------------
def test_a_two_column_key_is_read_measured_and_tested_by_customer() -> None:
    rng = np.random.default_rng(8)
    customers, snapshots = 1_500, ["2026-01-31", "2026-02-28"]
    group = (rng.random(customers) < 0.7).astype(int)  # one group per customer, at every snapshot
    frame = pd.DataFrame(
        {
            "id": np.repeat([f"C{i:05d}" for i in range(customers)], 2),
            "snap": snapshots * customers,
            "grp": np.repeat(group, 2),
            "age": np.repeat(rng.integers(18, 80, customers), 2),
        }
    )
    key = ["id", "snap"]
    built = build_assignment_frame(frame, primary_key=key, arm_column="grp")
    assert list(built.assignment.columns) == ["id", "snap", "arm", "intended", "band"]
    outcomes = frame[["id", "snap"]].assign(won=(rng.random(len(frame)) < 0.1).astype(int))
    kept, date = build_outcomes_frame(outcomes, primary_key=key, outcome_column="won", assignment=built)
    assert date is None and list(kept.columns) == ["id", "snap", "won"]
    report = measure_campaign(
        built.assignment,
        kept,
        run_id="c_x",
        primary_key=key,
        outcome_column="won",
        intended_column="intended",
        treatment_time=AS_OF,
        as_of=AS_OF,
    )
    assert report.treated_rows + report.control_rows == len(frame)
    check = check_randomness(built, primary_key=key, threshold=THRESHOLD)
    assert check.status == "passed" and check.rows_used is not None
    with pytest.raises(AuditInputError, match="more than once"):
        build_assignment_frame(pd.concat([frame, frame.head(1)]), primary_key=key, arm_column="grp")
