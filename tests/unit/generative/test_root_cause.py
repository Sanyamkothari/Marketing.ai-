"""Tests for `engine.generative.root_cause`.

Four properties carry the weight, and each is proved directly rather than inferred from a passing
end-to-end run:

**A claim that points nowhere is not stored.** `grounded_causes` is proved on hand-built replies
first, so the filter itself is pinned; `generate_segment_summary` is then proved against
`FakeLLMMode.UNGROUNDED`, which cites an id no pack ever contains, to show the whole retry loop
really does exhaust its attempts and raise rather than settle for the first reply that parses.

**A customer's words are redacted before a pack exists, not before a screen renders it.** The
redaction test reads `SummarisedSegment.evidence_pack` - the exact object a prompt was rendered from -
and finds markers where the planted e-mails and phone numbers used to be.

**Both complaint paths work.** A use case that names a text column gets samples; a use case that does
not (or that names a column the data does not have) gets an honest empty pack or a coded failure,
never a silent one.

**Arithmetic that never touches a model is checked as arithmetic.** `segment_stats` and
`aggregate_reasons` are pure functions over hand-built numbers, so their tests need no run, no
storage and no fake at all.

Runs are fabricated by `tests.fixtures.make_run`, not trained: two hundred and forty rows is enough
for `rca`'s three bands to each hold rows, built once per module and reused, because fabricating it
twice would prove nothing a shared run does not already prove.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engine.config import BudgetConfig, LlmConfig, RunMode, SegmentBy, load_use_case
from engine.contracts import Direction, Reason, RowExplanation, RunRecord, RunState
from engine.generative.budget import Meter
from engine.generative.contracts import (
    ROOT_CAUSE_SUMMARY_FILENAME,
    EvidencePack,
    EvidenceReason,
    GuardrailOutcome,
    RedactedComplaint,
    RootCauseSummary,
    SegmentStats,
)
from engine.generative.errors import (
    BUDGET_EXCEEDED,
    COMPLAINT_COLUMN_MISSING,
    MODEL_OUTPUT_MALFORMED,
    RUN_NOT_FINISHED,
    RUN_WITHOUT_EXPLANATIONS,
    RUN_WITHOUT_SCORES,
    UNGROUNDED_CLAIM,
    GenerativeError,
)
from engine.generative.guardrails import Guardrails, load_policy
from engine.generative.root_cause import (
    ROOT_CAUSE_PROMPT,
    _match_reasons,
    aggregate_reasons,
    build_root_cause_summary,
    generate_segment_summary,
    grounded_causes,
    segment_stats,
)
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.storage import LocalStorage, run_key
from tests.fixtures.make_run import RunSpec, write_run

if TYPE_CHECKING:
    from engine.config import UseCaseConfig

USE_CASE_ID = "rca"
ROWS = 240
POSITIVE_RATE = 0.25
"""Enough rows, at a high enough positive rate, that all three of `rca`'s bands hold rows (measured:
Low 184, Medium 32, High 24), so a run built once exercises every segment every test needs."""


def _meter(mode: FakeLLMMode = FakeLLMMode.GROUNDED) -> Meter:
    return Meter(
        FakeLLMClient(mode=mode), job_id="rc_test", llm=LlmConfig(), budget=BudgetConfig(cache=False)
    )


def _guardrails(meter: Meter, config_root: Path) -> Guardrails:
    return Guardrails(load_policy(config_root), meter=meter, prompts_root=config_root)


def _pack(
    reason_ids: tuple[str, ...] = ("r1", "r2"), complaint_ids: tuple[str, ...] = ("c1",)
) -> EvidencePack:
    """A small, self-contained pack: enough ids to ground a claim against, nothing read from a run."""
    return EvidencePack(
        segment="High",
        stats=SegmentStats(rows=10, share_pct=10.0, mean_score=0.62, min_score=0.5, max_score=0.9),
        reasons=tuple(
            EvidenceReason(
                id=reason_id,
                feature=f"feature_{reason_id}",
                direction=Direction.UP,
                mean_abs_contribution=0.3,
                share_pct=50.0,
                rows=5,
            )
            for reason_id in reason_ids
        ),
        complaints=tuple(
            RedactedComplaint(id=complaint_id, text="a redacted complaint", matched_reason_ids=())
            for complaint_id in complaint_ids
        ),
    )


def _explanation(
    primary_key: str, *, score: float, reasons: tuple[tuple[str, float, Direction], ...]
) -> RowExplanation:
    return RowExplanation(
        primary_key=primary_key,
        score=score,
        reasons=tuple(
            Reason(feature=feature, value="x", contribution=contribution, direction=direction, text="because")
            for feature, contribution, direction in reasons
        ),
    )


@pytest.fixture(scope="module")
def run(
    tmp_path_factory: pytest.TempPathFactory, config_root: Path
) -> tuple[LocalStorage, str, UseCaseConfig]:
    """One finished `rca` scoring run, with complaint text planted, built once for the module."""
    storage = LocalStorage(tmp_path_factory.mktemp("root_cause_run"))
    use_case = load_use_case(USE_CASE_ID, config_root)
    spec = RunSpec(
        use_case_id=USE_CASE_ID,
        rows=ROWS,
        positive_rate=POSITIVE_RATE,
        with_text=True,
        config_root=config_root,
    )
    run_id = write_run(storage, spec)
    return storage, run_id, use_case


def _without_complaint_column(use_case: UseCaseConfig) -> UseCaseConfig:
    return use_case.model_copy(
        update={
            "generative": use_case.generative.model_copy(
                update={
                    "root_cause": use_case.generative.root_cause.model_copy(
                        update={"complaint_text_column": None}
                    )
                }
            )
        }
    )


def _with_complaint_column(use_case: UseCaseConfig, column: str) -> UseCaseConfig:
    return use_case.model_copy(
        update={
            "generative": use_case.generative.model_copy(
                update={
                    "root_cause": use_case.generative.root_cause.model_copy(
                        update={"complaint_text_column": column}
                    )
                }
            )
        }
    )


def _with_segment_by(use_case: UseCaseConfig, segment_by: SegmentBy) -> UseCaseConfig:
    return use_case.model_copy(
        update={
            "generative": use_case.generative.model_copy(
                update={
                    "root_cause": use_case.generative.root_cause.model_copy(update={"segment_by": segment_by})
                }
            )
        }
    )


# ---------------------------------------------------------------------------
# Pure arithmetic: segment_stats, aggregate_reasons
# ---------------------------------------------------------------------------
def test_segment_stats_computes_rows_share_and_the_score_range() -> None:
    stats = segment_stats([0.2, 0.4, 0.6, 0.8], total_rows=20)
    assert stats.rows == 4
    assert stats.share_pct == 20.0
    assert stats.mean_score == pytest.approx(0.5)
    assert stats.min_score == 0.2
    assert stats.max_score == 0.8
    assert stats.positive_rate is None


def test_segment_stats_of_an_empty_segment_is_zero_not_a_division_error() -> None:
    stats = segment_stats([], total_rows=20)
    assert stats.rows == 0
    assert stats.share_pct == 0.0
    assert stats.mean_score == 0.0


def test_aggregate_reasons_averages_the_absolute_contribution_and_keeps_the_majority_direction() -> None:
    explanations = (
        _explanation(
            "k1",
            score=0.9,
            reasons=(("outages_90d", 0.6, Direction.UP), ("billing_disputes_90d", -0.1, Direction.DOWN)),
        ),
        _explanation("k2", score=0.8, reasons=(("outages_90d", 0.4, Direction.UP),)),
        _explanation(
            "k3",
            score=0.85,
            reasons=(("outages_90d", -0.2, Direction.DOWN), ("billing_disputes_90d", 0.3, Direction.UP)),
        ),
    )
    reasons = aggregate_reasons(explanations, limit=5)
    by_feature = {reason.feature: reason for reason in reasons}

    assert by_feature["outages_90d"].rows == 3
    assert by_feature["outages_90d"].mean_abs_contribution == pytest.approx(0.4)
    assert by_feature["outages_90d"].direction is Direction.UP  # two rows up against one down

    assert by_feature["billing_disputes_90d"].rows == 2
    assert by_feature["billing_disputes_90d"].mean_abs_contribution == pytest.approx(0.2)
    assert by_feature["billing_disputes_90d"].direction is Direction.UP  # a 1-1 tie goes up

    assert by_feature["outages_90d"].share_pct == pytest.approx(66.7)
    assert by_feature["billing_disputes_90d"].share_pct == pytest.approx(33.3)

    assert reasons[0].feature == "outages_90d"  # strongest mean absolute contribution first
    assert reasons[0].id == "r1"
    assert reasons[1].id == "r2"


def test_aggregate_reasons_caps_at_the_configured_limit() -> None:
    explanations = (
        _explanation(
            "k1",
            score=0.5,
            reasons=(
                ("a", 0.9, Direction.UP),
                ("b", 0.5, Direction.UP),
                ("c", 0.1, Direction.DOWN),
            ),
        ),
    )
    assert [reason.feature for reason in aggregate_reasons(explanations, limit=2)] == ["a", "b"]


def test_aggregate_reasons_breaks_an_exact_tie_with_the_runs_own_feature_ranking() -> None:
    explanations = (
        _explanation(
            "k1", score=0.9, reasons=(("feature_b", 0.5, Direction.UP), ("feature_a", 0.5, Direction.UP))
        ),
    )
    ranked = aggregate_reasons(explanations, limit=5, importance_rank={"feature_a": 1, "feature_b": 2})
    assert [reason.feature for reason in ranked] == ["feature_a", "feature_b"]


def test_aggregate_reasons_of_no_explanations_is_empty() -> None:
    assert aggregate_reasons((), limit=5) == ()


# ---------------------------------------------------------------------------
# Grounding: the check the whole module exists for
# ---------------------------------------------------------------------------
def test_grounded_causes_drops_a_claim_that_points_nowhere_and_keeps_the_one_that_does_not() -> None:
    pack = _pack()
    raw = [
        {"cause": "grounded in the pack", "evidence_refs": ["r1", "c1"], "confidence": "high"},
        {"cause": "invented", "evidence_refs": ["not_in_the_pack"], "confidence": "medium"},
    ]
    kept = grounded_causes(raw, pack)
    assert [cause.cause for cause in kept] == ["grounded in the pack"]
    assert set(kept[0].evidence_refs) == {"r1", "c1"}


def test_grounded_causes_returns_nothing_when_every_claim_points_nowhere() -> None:
    pack = _pack()
    raw = [{"cause": "invented", "evidence_refs": ["ghost"], "confidence": "low"}]
    assert grounded_causes(raw, pack) == ()


def test_grounded_causes_drops_a_reply_with_no_evidence_refs_at_all() -> None:
    pack = _pack()
    raw = [{"cause": "no evidence given", "evidence_refs": [], "confidence": "low"}]
    assert grounded_causes(raw, pack) == ()


def test_grounded_causes_ignores_an_entry_that_is_not_an_object() -> None:
    pack = _pack()
    assert grounded_causes(["not a mapping", 42], pack) == ()


def test_ungrounded_claims_are_rejected_and_the_segment_raises_after_every_retry(config_root: Path) -> None:
    """The single most important behaviour in this module: an id nobody supplied is never stored.

    `FakeLLMMode.UNGROUNDED` always cites an id no pack ever contains (DEC-214), so every one of
    `guardrails.retries + 1` attempts must fail to ground, and the segment must raise rather than
    settle for a reply with nothing left in it once the bad claim is dropped.
    """
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter(FakeLLMMode.UNGROUNDED)
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        generate_segment_summary(
            "High", _pack(), use_case=use_case, meter=meter, guardrails=guardrails, config_root=config_root
        )

    assert excinfo.value.code == UNGROUNDED_CLAIM
    assert meter.calls == guardrails.retries + 1  # every attempt was tried, and no more than that


def test_a_job_whose_model_never_grounds_stores_no_summary_for_any_segment(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    """What an exhausted segment looks like once a whole job has run over it: recorded, not thrown away."""
    storage, run_id, use_case = run
    meter = _meter(FakeLLMMode.UNGROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary = build_root_cause_summary(
        run_id,
        use_case=use_case,
        storage=storage,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    assert summary.segments
    for segment in summary.segments:
        assert segment.summary is None
        assert segment.blocked_reason
        assert (
            segment.evidence_pack.segment == segment.segment
        )  # the pack survives even when the prose does not


def test_a_reply_that_is_not_json_raises_model_output_malformed_after_every_retry(config_root: Path) -> None:
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter(FakeLLMMode.MALFORMED)
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        generate_segment_summary(
            "High", _pack(), use_case=use_case, meter=meter, guardrails=guardrails, config_root=config_root
        )

    assert excinfo.value.code == MODEL_OUTPUT_MALFORMED
    assert meter.calls == guardrails.retries + 1


def test_a_grounded_reply_is_stored_with_the_guardrail_checks_that_passed_it(config_root: Path) -> None:
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter(FakeLLMMode.GROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary, checks, attempts = generate_segment_summary(
        "High", _pack(), use_case=use_case, meter=meter, guardrails=guardrails, config_root=config_root
    )

    assert attempts == 1
    assert summary.headline
    assert summary.root_causes
    assert checks
    assert all(check.outcome is GuardrailOutcome.PASSED for check in checks)
    for cause in summary.root_causes:
        assert set(cause.evidence_refs) <= _pack().reference_ids


# ---------------------------------------------------------------------------
# Complaint text: redacted before the pack exists, and both configured paths work
# ---------------------------------------------------------------------------
def test_complaint_samples_in_the_pack_are_redacted_before_they_could_reach_a_prompt(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    """Read straight off `SummarisedSegment.evidence_pack` - the exact object the prompt was built from."""
    storage, run_id, use_case = run
    meter = _meter(FakeLLMMode.GROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary = build_root_cause_summary(
        run_id,
        use_case=use_case,
        storage=storage,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    complaints = [complaint for segment in summary.segments for complaint in segment.evidence_pack.complaints]
    assert complaints, "the run plants e-mails and phone numbers in complaint_text; some sample must exist"
    assert any("[REDACTED:" in complaint.text for complaint in complaints)
    for complaint in complaints:
        assert "@example.invalid" not in complaint.text
        assert "90000" not in complaint.text
    assert summary.complaint_source == "column:complaint_text"


def test_a_use_case_with_no_complaint_column_configured_gets_packs_with_no_complaints(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    """The summary still has to come from reasons and statistics alone, and still has to come."""
    storage, run_id, use_case = run
    no_text_use_case = _without_complaint_column(use_case)
    meter = _meter(FakeLLMMode.GROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary = build_root_cause_summary(
        run_id,
        use_case=no_text_use_case,
        storage=storage,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    assert summary.complaint_source is None
    assert summary.segments
    for segment in summary.segments:
        assert segment.evidence_pack.complaints == ()
        assert segment.evidence_pack.reasons  # the pack is not empty, only the complaint sample is
        assert segment.summary is not None


def test_a_complaint_column_that_is_not_in_the_data_raises_complaint_column_missing(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    storage, run_id, use_case = run
    bad_use_case = _with_complaint_column(use_case, "does_not_exist")
    meter = _meter()
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        build_root_cause_summary(
            run_id,
            use_case=bad_use_case,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )

    assert excinfo.value.code == COMPLAINT_COLUMN_MISSING


# ---------------------------------------------------------------------------
# What a finished run must offer before anything is read from it
# ---------------------------------------------------------------------------
def test_a_run_that_has_not_finished_raises_run_not_finished(tmp_path: Path, config_root: Path) -> None:
    storage = LocalStorage(tmp_path)
    run_id = write_run(storage, RunSpec(use_case_id=USE_CASE_ID, rows=40, config_root=config_root))
    key = run_key(run_id, "run.json")
    record = storage.read_model(key, RunRecord)
    storage.write_model(key, record.model_copy(update={"state": RunState.RUNNING}))
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter()
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        build_root_cause_summary(
            run_id,
            use_case=use_case,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )

    assert excinfo.value.code == RUN_NOT_FINISHED


def test_a_training_run_writes_no_scores_and_raises_run_without_scores(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    run_id = write_run(
        storage, RunSpec(use_case_id=USE_CASE_ID, mode=RunMode.TRAIN, rows=40, config_root=config_root)
    )
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter()
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        build_root_cause_summary(
            run_id,
            use_case=use_case,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )

    assert excinfo.value.code == RUN_WITHOUT_SCORES


def test_a_run_with_reasons_switched_off_raises_run_without_explanations(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    run_id = write_run(
        storage, RunSpec(use_case_id=USE_CASE_ID, rows=40, with_reasons=False, config_root=config_root)
    )
    use_case = load_use_case(USE_CASE_ID, config_root)
    meter = _meter()
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        build_root_cause_summary(
            run_id,
            use_case=use_case,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )

    assert excinfo.value.code == RUN_WITHOUT_EXPLANATIONS


# ---------------------------------------------------------------------------
# Segmenting by the row's own strongest reason, not only by band
# ---------------------------------------------------------------------------
def test_segmenting_by_top_reason_names_each_segment_after_a_feature(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    storage, run_id, use_case = run
    top_reason_use_case = _with_segment_by(use_case, SegmentBy.TOP_REASON)
    meter = _meter(FakeLLMMode.GROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary = build_root_cause_summary(
        run_id,
        use_case=top_reason_use_case,
        storage=storage,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    feature_names = {"usage_drop_30d", "outages_90d", "billing_disputes_90d", "price_change_flag"}
    assert summary.segment_by == "top_reason"
    assert summary.segments
    assert all(segment.segment in feature_names for segment in summary.segments)


# ---------------------------------------------------------------------------
# End to end: the artefact itself, round-tripped through storage
# ---------------------------------------------------------------------------
def test_the_root_cause_summary_artefact_validates_end_to_end(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    storage, run_id, use_case = run
    meter = _meter(FakeLLMMode.GROUNDED)
    guardrails = _guardrails(meter, config_root)

    summary = build_root_cause_summary(
        run_id,
        use_case=use_case,
        storage=storage,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    key = run_key(run_id, ROOT_CAUSE_SUMMARY_FILENAME)
    storage.write_model(key, summary)
    reloaded = storage.read_model(key, RootCauseSummary)

    assert reloaded.run_id == run_id
    assert reloaded.segment_by == "band"
    assert [segment.segment for segment in reloaded.segments] == ["Low", "Medium", "High"]
    assert reloaded.prompt_versions[ROOT_CAUSE_PROMPT] >= 1
    assert reloaded.prompt_hashes[ROOT_CAUSE_PROMPT]
    assert reloaded.complaint_source == "column:complaint_text"

    for segment in reloaded.segments:
        assert segment.evidence_pack.stats.rows > 0
        assert segment.summary is not None, segment.blocked_reason
        assert segment.summary.root_causes
        for cause in segment.summary.root_causes:
            assert cause.evidence_refs
            assert set(cause.evidence_refs) <= segment.evidence_pack.reference_ids


# ---------------------------------------------------------------------------
# A job that runs out of budget stops, rather than blaming each segment in turn
# ---------------------------------------------------------------------------
def test_running_out_of_budget_stops_the_job_rather_than_blocking_every_remaining_segment(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    """`BUDGET_EXCEEDED` is about the job: no later segment could have been generated either."""
    storage, run_id, use_case = run
    meter = Meter(
        FakeLLMClient(mode=FakeLLMMode.GROUNDED),
        job_id="rc_budget",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False, max_calls_per_run=1),
    )
    guardrails = _guardrails(meter, config_root)

    with pytest.raises(GenerativeError) as excinfo:
        build_root_cause_summary(
            run_id,
            use_case=use_case,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )

    assert excinfo.value.code == BUDGET_EXCEEDED
    assert meter.calls == 1, "the meter refused the second call rather than making it"


# ---------------------------------------------------------------------------
# Matching a complaint to the reasons it echoes, without matching it to the rest
# ---------------------------------------------------------------------------
def _reason(reason_id: str, feature: str) -> EvidenceReason:
    return EvidenceReason(
        id=reason_id,
        feature=feature,
        direction=Direction.UP,
        mean_abs_contribution=0.2,
        share_pct=20.0,
        rows=3,
    )


MATCH_REASONS = (
    _reason("r1", "outages_90d"),
    _reason("r2", "billing_disputes_90d"),
    _reason("r3", "usage_drop_30d"),
)


def test_a_complaint_matches_the_reason_whose_feature_name_its_own_words_contain() -> None:
    assert _match_reasons("the outage lasted all weekend", MATCH_REASONS) == ("r1",)


def test_a_complaint_does_not_match_a_reason_on_a_short_word_buried_in_its_feature_name() -> None:
    """Containment is symmetric: "i" is inside "billing", and a floor on one side is no floor."""
    assert _match_reasons("i am in it", MATCH_REASONS) == ()


def test_a_complaint_about_one_thing_does_not_match_every_other_reason_too() -> None:
    complaint = "i have a billing query and nobody has answered it"
    assert _match_reasons(complaint, MATCH_REASONS) == ("r2",)
