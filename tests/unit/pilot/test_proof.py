"""Plan J M104 (DEC-1314): the Value Proof Pack's provenance rule, its formatting and its growth with size.

The campaigns are made by the engine itself - the simulator's customers, `build_assignment`, `measure_campaign`
and `measure_campaign_segments`, stored with `save_campaign` as `POST /campaigns` stores them - so the pack is
read from the very files a real campaign leaves. The API journey is `tests/integration/pilot/test_proof_pack.py`.
"""

from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    INTENDED_COLUMN,
    REPORT_FILENAME,
    Campaign,
    CampaignKind,
    CampaignOutcomes,
    CampaignStatus,
    InMemoryCampaignStore,
    assignment_counts,
    build_assignment,
    campaign_key,
    save_campaign,
    write_frame,
)
from engine.measurement.measure import measure_campaign, measure_campaign_segments
from engine.measurement.segments import SEGMENT_EFFECTS_FILENAME
from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, segmented_campaign
from engine.pilot.proof import (
    Figure,
    ProofRefusedError,
    ProofView,
    ProvenanceError,
    Source,
    build_proof,
    derived,
    figures_of,
    format_value,
    proof_document,
    verify_provenance,
)
from engine.storage import LocalStorage

CAMPAIGN_ID = "c_20261009_0000a104"


@dataclass(frozen=True)
class Stored:
    storage: LocalStorage
    campaign_id: str


def _store(
    tmp: Path, n: int, *, seed: int = 1, campaign_id: str = CAMPAIGN_ID, measure: bool = True
) -> Stored:
    """A campaign of `n` simulated customers in three groups, recorded and measured as the routes do it."""
    storage = LocalStorage(tmp)
    sim = segmented_campaign(n, 0.15, {"A": 0.04, "B": 0.0, "C": -0.06}, seed=seed)
    kw = sim.measure_kwargs
    assignment = build_assignment(sim.scores, primary_key=kw["primary_key"])
    start = kw["treatment_time"]
    campaign = Campaign(
        campaign_id=campaign_id,
        kind=CampaignKind.SCORED,
        name="Simulated campaign",
        primary_key=kw["primary_key"],
        treatment_start=start,
        treatment_start_source="entered",
        outcome_window_days=OUTCOME_WINDOW_DAYS,
        population="intended",
        causal=True,
        causal_basis="engine_random",
        counts=assignment_counts(assignment),
        status=CampaignStatus.LIVE,
        outcomes=CampaignOutcomes(
            upload_id="u_simulated",
            file_name="outcomes.csv",
            outcome_column=kw["outcome_column"],
            outcome_named=True,
            treatment_date_column=kw["treatment_date_column"],
            rows=len(sim.outcomes),
            added_at=AS_OF,
        ),
        created_at=AS_OF,
        created_by="test",
    )
    write_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME), assignment)
    save_campaign(InMemoryCampaignStore(), storage, campaign, create=True)
    if measure:
        report = measure_campaign(
            assignment,
            sim.outcomes,
            run_id=campaign_id,
            primary_key=kw["primary_key"],
            outcome_column=kw["outcome_column"],
            intended_column=INTENDED_COLUMN,
            treatment_time=start,
            treatment_date_column=kw["treatment_date_column"],
            outcome_window_days=OUTCOME_WINDOW_DAYS,
            as_of=AS_OF,
            campaign_id=campaign_id,
        )
        segments = measure_campaign_segments(
            assignment,
            sim.outcomes,
            report,
            primary_key=kw["primary_key"],
            outcome_column=kw["outcome_column"],
            intended_column=INTENDED_COLUMN,
            treatment_time=start,
            treatment_date_column=kw["treatment_date_column"],
        )
        storage.write_model(campaign_key(campaign_id, REPORT_FILENAME), report)
        storage.write_model(campaign_key(campaign_id, SEGMENT_EFFECTS_FILENAME), segments)
    return Stored(storage, campaign_id)


def test_every_figure_of_a_built_pack_resolves(tmp_path: Path) -> None:
    stored = _store(tmp_path, 6_000)
    view = build_proof(stored.storage, stored.campaign_id)
    figures = list(figures_of(view))
    assert len(figures) > 20
    verify_provenance(view, stored.storage)
    assert {s.key for s in view.sections} >= {"incremental", "backfire", "method"}
    assert [p.segment.value for p in view.proposals] == ["C"], "the planted harmful group is flagged"


def test_a_figure_that_does_not_match_its_source_fails_the_check(tmp_path: Path) -> None:
    stored = _store(tmp_path, 4_000)
    view = build_proof(stored.storage, stored.campaign_id)
    report = f"campaigns/{stored.campaign_id}/{REPORT_FILENAME}"
    wrong = Figure(
        value=1, text="1", format="count", sources=(Source(artefact=report, field="treated_rows"),)
    )
    missing = Figure(
        value=1, text="1", format="count", sources=(Source(artefact=report, field="no_such_field"),)
    )
    badly_printed = Figure(
        value=json.loads(stored.storage.read_bytes(report))["treated_rows"],
        text="many",
        format="count",
        sources=(Source(artefact=report, field="treated_rows"),),
    )
    for figure in (wrong, missing, badly_printed):
        tampered = view.model_copy(update={"campaign_name": figure})
        with pytest.raises(ProvenanceError):
            verify_provenance(tampered, stored.storage)


def test_a_formula_may_only_combine_its_sources() -> None:
    a = Figure(value=3, text="3", format="count", sources=(Source(artefact="x.json", field="a"),))
    b = Figure(value=4, text="4", format="count", sources=(Source(artefact="x.json", field="b"),))
    product = derived([a, b], "s0*s1 - s0", "count")
    assert product is not None and product.value == 9 and len(product.sources) == 2
    assert derived([a, b], "s0*2", "count") is None, "a constant is not a source"
    assert derived([a, b], "__import__('os')", "count") is None
    assert derived([a, None], "s0+s1", "count") is None, "a missing part is never filled in"
    assert derived([a, b], "s0/(s1-s1)", "count") is None


def test_the_printed_text_is_one_rule_per_format() -> None:
    assert format_value("count", 1234.4) == "1,234"
    assert format_value("signed_count", -0.2) == "+0"
    assert format_value("signed_count", 52.6) == "+53"
    assert format_value("share", 0.95) == "95%"
    assert format_value("share", 0.98333) == "98.3%"
    assert format_value("points", -0.0263) == "-2.6 points"
    assert format_value("inr", 373797.2) == "₹3,73,797 (3.74 lakh)"
    assert format_value("date", "2026-09-01T00:00:00Z") == "1 Sep 2026"
    assert format_value("days", 90) == "90 days"
    assert format_value("times", 3.19) == "3.2x"


def test_an_unmeasured_campaign_is_refused_with_the_day_it_matures(tmp_path: Path) -> None:
    stored = _store(tmp_path, 2_000, measure=False)
    with pytest.raises(ProofRefusedError) as refused:
        build_proof(stored.storage, stored.campaign_id)
    assert refused.value.code == "PROOF_NOT_MATURE"
    assert refused.value.results_available_on is not None


def test_a_segment_file_from_an_older_measurement_is_not_read(tmp_path: Path) -> None:
    stored = _store(tmp_path, 3_000)
    key = f"campaigns/{stored.campaign_id}/{SEGMENT_EFFECTS_FILENAME}"
    document = json.loads(stored.storage.read_bytes(key))
    document["report_computed_at"] = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    stored.storage.write_bytes(key, json.dumps(document).encode())
    view = build_proof(stored.storage, stored.campaign_id)
    backfire = next(s for s in view.sections if s.key == "backfire")
    assert backfire.status == "not_measured" and "earlier measurement" in (backfire.reason or "")
    assert view.proposals == ()


def test_the_document_prints_only_figure_text_and_plain_words(tmp_path: Path) -> None:
    import re

    from engine.pilot.plain import jargon_in

    stored = _store(tmp_path, 4_000)
    view = build_proof(stored.storage, stored.campaign_id)
    document = proof_document(view)
    words = json.dumps(document.model_dump(mode="json", exclude={"schema_version", "generated_at", "footer"}))
    assert jargon_in(words) == ()
    from engine.pilot.document import render_html

    allowed = {t for f in figures_of(view) for t in re.findall(r"\d(?:[\d,]*\d)?(?:\.\d+)?", f.text)}
    page = html.unescape(
        re.sub(r"<style>.*?</style>|<footer>.*?</footer>|<[^>]+>", " ", render_html(document), flags=re.S)
    )
    stray = set(re.findall(r"\d(?:[\d,]*\d)?(?:\.\d+)?", page)) - allowed
    assert not stray, stray
    assert ProofView.model_validate_json(view.model_dump_json()) == view


def _pack_seconds(tmp: Path, n: int) -> float:
    """Measuring the groups and building the pack (the report is measured first, as the route does)."""
    start = time.perf_counter()
    stored = _store(tmp, n)
    build_proof(stored.storage, stored.campaign_id)
    return time.perf_counter() - start


GROWTH_LIMIT = 30.0
"""Ten times the customers may take at most this many times as long: linear is about 10x, quadratic 100x."""


def test_building_a_pack_grows_linearly_with_the_campaign(tmp_path: Path) -> None:
    small = min(_pack_seconds(tmp_path / f"s{i}", 10_000) for i in range(2))
    large = min(_pack_seconds(tmp_path / f"l{i}", 100_000) for i in range(2))
    assert large / small < GROWTH_LIMIT, f"{small:.2f}s for 10k customers, {large:.2f}s for 100k"


@pytest.mark.slow
def test_a_one_million_customer_campaigns_pack_is_built_in_linear_time(tmp_path: Path) -> None:
    small = _pack_seconds(tmp_path / "small", 100_000)
    large = _pack_seconds(tmp_path / "large", 1_000_000)
    assert large / small < GROWTH_LIMIT, f"{small:.1f}s for 100k customers, {large:.1f}s for 1M"
    stored = Stored(LocalStorage(tmp_path / "large"), CAMPAIGN_ID)
    start = time.perf_counter()
    build_proof(stored.storage, stored.campaign_id)
    assert time.perf_counter() - start < 5.0, "the pack itself reads aggregate files only"
