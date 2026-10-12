"""Plan O M234 acceptance: `verdict == ready` implies the dry run passed and no probe failed (DEC-1804 (c)).

Three layers. The verdict rule itself, over every combination of its inputs. The report model, which
refuses to be built in a state that breaks the invariant. And the whole report on generated files with
the gaps mixed in at random: whenever it says `ready`, `check_plan` passes and the engine's own prepare
and split - seeded by a run id the report never saw - finish with both outcome values in every part.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.doctor.readiness import decide_verdict
from engine.doctor.readiness_contracts import (
    PROBE_CODES,
    PROBE_GATES,
    ApprovalRequired,
    DataReadinessReport,
    OpenQuestion,
    ProbeResult,
    ReadinessRoles,
    SourceFacts,
)
from engine.doctor.readiness_probes import label_key
from engine.stages import prepare
from tests.unit.doctor.readiness_fixtures import assets, config, customers, dry_run, report

_STATUSES = st.sampled_from(["pass", "fail", "warn", "skipped"])


def _probe(code: str, status: str) -> ProbeResult:
    return ProbeResult(
        code=code,
        gate=PROBE_GATES[code],
        status=status,  # type: ignore[arg-type]
        skip_reason="not applicable" if status == "skipped" else None,
    )


@st.composite
def _verdict_inputs(draw: Any) -> dict[str, Any]:
    statuses = draw(st.lists(_STATUSES, min_size=len(PROBE_CODES), max_size=len(PROBE_CODES)))
    questions = draw(
        st.lists(
            st.builds(
                OpenQuestion,
                id=st.just("q"),
                blocking=st.booleans(),
                text=st.just("Which column is the outcome?"),
                answered=st.booleans(),
            ),
            max_size=3,
        )
    )
    approvals = draw(
        st.lists(
            st.builds(
                ApprovalRequired,
                kind=st.just("ack_error"),
                key=st.just("LEAKAGE_SUSPECTED:x"),
                recorded=st.booleans(),
            ),
            max_size=3,
        )
    )
    return {
        "dry_run_passed": draw(st.booleans()),
        "probes": [_probe(code, status) for code, status in zip(PROBE_CODES, statuses, strict=True)],
        "open_questions": questions,
        "approvals_required": approvals,
        "warnings": draw(st.integers(min_value=0, max_value=3)),
    }


@given(_verdict_inputs())
def test_the_verdict_rule_never_says_ready_over_a_failure(inputs: dict[str, Any]) -> None:
    verdict, reason = decide_verdict(**inputs)
    failing = [probe.code for probe in inputs["probes"] if probe.status == "fail"]
    if verdict in ("ready", "ready_with_warnings"):
        assert inputs["dry_run_passed"] and not failing
        assert not any(q.blocking and not q.answered for q in inputs["open_questions"])
        assert all(a.recorded for a in inputs["approvals_required"])
    if verdict == "ready":
        assert inputs["warnings"] == 0 and not any(p.status == "warn" for p in inputs["probes"])
    if not inputs["dry_run_passed"] or failing:
        assert verdict == "not_ready"
        for code in failing:
            assert code in reason


@given(_verdict_inputs(), st.sampled_from(["ready", "ready_with_warnings"]))
def test_the_report_cannot_be_built_ready_over_a_failure(inputs: dict[str, Any], claimed: str) -> None:
    broken = (
        not inputs["dry_run_passed"]
        or any(p.status == "fail" for p in inputs["probes"])
        or any(q.blocking and not q.answered for q in inputs["open_questions"])
        or any(not a.recorded for a in inputs["approvals_required"])
    )
    fields = {
        "generated_at": "2026-10-12T00:00:00Z",
        "agent_version": "t",
        "engine_version": "t",
        "use_case_id": "targeted-advertisement",
        "use_case_type": "propensity",
        "mode": "train",
        "source": SourceFacts(upload_id="u", rows_parsed=1, profile_row_cap=1, sampled=False),
        "roles": ReadinessRoles(),
        "requirements": (),
        "probes": tuple(inputs["probes"]),
        "open_questions": tuple(inputs["open_questions"]),
        "approvals_required": tuple(inputs["approvals_required"]),
        "dry_run_passed": inputs["dry_run_passed"],
        "verdict": claimed,
        "verdict_reason": "claimed",
        "report_hash": "x",
    }
    if broken:
        with pytest.raises(ValueError, match="verdict ready"):
            DataReadinessReport(**fields)
    else:
        DataReadinessReport(**fields)


# ---------------------------------------------------------------------------------------------------
# The whole report on generated files with gaps mixed in
# ---------------------------------------------------------------------------------------------------
_TIME_GAPS = st.fixed_dictionaries(
    {
        "rows": st.integers(min_value=1300, max_value=3000),
        "days": st.sampled_from([2, 30, 120, 120]),
        "empty_dates": st.sampled_from([0, 0, 0, 5]),
        "positive_rate": st.sampled_from([0.004, 0.2, 0.3, 0.3]),
        "immature_days": st.sampled_from([0, 0, 0, 15]),
        "consent": st.sampled_from([None, None, "plain", "words"]),
        "seed": st.integers(min_value=0, max_value=50),
    }
)


def _assert_the_run_goes_through(frame: Any, cfg: Any, key: str, target: str) -> None:
    assert dry_run(frame, cfg).passed
    rows, _plan = prepare.prepare_rows(frame, cfg, primary_key=(key,), target=target)
    parts, _report = prepare.split_dataset(
        rows, cfg, run_id="a-real-run-id-the-report-never-saw", target=target
    )
    for name, part in parts.items():
        assert not part.empty, name
        assert part[target].dropna().map(label_key).nunique() == 2, name


def _with_consent(frame: Any, how: str | None, seed: int) -> tuple[Any, dict[str, Any]]:
    if how is None:
        return frame, {}
    rng = np.random.default_rng(seed)
    yes = rng.random(len(frame)) < 0.8
    frame["consent"] = (
        np.where(yes, "yes", "no") if how == "plain" else np.where(yes, "Opted In", "Opted Out")
    )
    return frame, {"governance": {"consent_column": "consent"}}


@settings(max_examples=16, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_TIME_GAPS)
def test_ready_by_date_means_the_split_and_the_outcome_parts_work(gaps: dict[str, Any]) -> None:
    frame = assets(gaps["rows"], seed=gaps["seed"], days=gaps["days"], positive_rate=gaps["positive_rate"])
    if gaps["empty_dates"]:
        frame.loc[frame.sample(n=gaps["empty_dates"], random_state=gaps["seed"]).index, "snapshot_date"] = (
            None
        )
    if gaps["immature_days"]:
        dates = frame["snapshot_date"].dropna()
        cutoff = sorted(dates)[int(len(dates) * 0.8)]
        frame.loc[frame["snapshot_date"] >= cutoff, "fault_next_72h"] = 0
    frame, sections = _with_consent(frame, gaps["consent"], gaps["seed"])
    cfg = config("fault-prediction", **sections)
    result = report(frame, cfg)
    if result.verdict in ("ready", "ready_with_warnings"):
        assert result.dry_run_passed and not result.failing_probes
        _assert_the_run_goes_through(frame, cfg, "asset_id", "fault_next_72h")


_RANDOM_GAPS = st.fixed_dictionaries(
    {
        "rows": st.integers(min_value=1300, max_value=3000),
        "positive_rate": st.sampled_from([0.002, 0.2, 0.3, 0.3]),
        "groups": st.sampled_from([None, None, 2, 3, 8]),
        "consent": st.sampled_from([None, None, "plain", "words"]),
        "seed": st.integers(min_value=0, max_value=50),
    }
)


@settings(max_examples=16, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_RANDOM_GAPS)
def test_ready_at_random_means_the_split_and_the_outcome_parts_work(gaps: dict[str, Any]) -> None:
    frame = customers(gaps["rows"], seed=gaps["seed"], positive_rate=gaps["positive_rate"])
    sections: dict[str, Any] = {}
    if gaps["groups"] is not None:
        frame["branch"] = [f"b{i % gaps['groups']}" for i in range(len(frame))]
        sections["split"] = {"group_column": "branch"}
    frame, consent = _with_consent(frame, gaps["consent"], gaps["seed"])
    sections.update(consent)
    cfg = config("targeted-advertisement", **sections)
    result = report(frame, cfg)
    if result.verdict in ("ready", "ready_with_warnings"):
        assert result.dry_run_passed and not result.failing_probes
        _assert_the_run_goes_through(frame, cfg, "customer_id", "converted_30d")
