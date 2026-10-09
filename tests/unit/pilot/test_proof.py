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

import pandas as pd
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
from engine.pilot.document import render_html
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
    tmp: Path,
    n: int,
    *,
    seed: int = 1,
    campaign_id: str = CAMPAIGN_ID,
    measure: bool = True,
    basis: str = "engine_random",
    group_column: str = "band",
    offers: int = 0,
) -> Stored:
    """A campaign of `n` simulated customers in three groups, recorded and measured as the routes do it.

    `group_column` names the three groups' column (`band`, or `segment` for the predicted groups); `offers`
    names that many different offers, dealt in turn, as a run that chose the offer per customer would.
    """
    storage = LocalStorage(tmp)
    sim = segmented_campaign(n, 0.15, {"A": 0.04, "B": 0.0, "C": -0.06}, seed=seed)
    kw = sim.measure_kwargs
    scores = sim.scores.rename(columns={"band": group_column})
    assignment = build_assignment(scores, primary_key=kw["primary_key"])
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
        causal=basis != "not_random",
        causal_basis=basis,
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
            offers=(
                pd.Series([f"offer {i % offers}" for i in range(len(assignment))], index=assignment.index)
                if offers
                else None
            ),
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
    assert format_value("inr_unit", 0.3) == "₹0.30", "a value per unit keeps its paise"
    assert format_value("inr_unit", 1.5) == "₹1.50"
    assert format_value("inr_unit", 2000.0) == "₹2,000"
    assert format_value("inr_unit", 0.004) == "₹0.0040", "never rounded to nothing"


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


def test_a_number_written_into_the_packs_own_words_fails_the_check(tmp_path: Path) -> None:
    """Only figures may print digits: a label, note or reason with a number of its own is untraced."""
    stored = _store(tmp_path, 3_000)
    view = build_proof(stored.storage, stored.campaign_id)
    verify_provenance(view, stored.storage)
    method = next(s for s in view.sections if s.key == "method")
    planted = method.model_copy(update={"notes": (*method.notes, "About 4,321 customers were missed.")})
    tampered = view.model_copy(
        update={"sections": tuple(planted if s.key == "method" else s for s in view.sections)}
    )
    with pytest.raises(ProvenanceError, match="4,321"):
        verify_provenance(tampered, stored.storage)
    with pytest.raises(ProvenanceError, match="17"):
        verify_provenance(view.model_copy(update={"headline": "Up 17 points."}), stored.storage)


def test_a_kind_of_group_too_many_to_read_says_why_from_the_measurement(tmp_path: Path) -> None:
    """More than `MAX_GROUPS` offers are not split; the pack prints the reason the measurement recorded."""
    from engine.measurement.segments import MAX_GROUPS

    stored = _store(tmp_path, 6_000, offers=MAX_GROUPS + 10)
    groups = json.loads(
        stored.storage.read_bytes(f"campaigns/{stored.campaign_id}/{SEGMENT_EFFECTS_FILENAME}")
    )
    [note] = [note for note in groups["not_measured"] if note["dimension"] == "offer"]
    assert str(MAX_GROUPS) in note["reason"]
    view = build_proof(stored.storage, stored.campaign_id)
    backfire = next(s for s in view.sections if s.key == "backfire")
    assert backfire.status == "measured", "the bands are still read"
    line = next(line for line in backfire.lines if line.label == "Not read offer by offer")
    assert line.value is not None and line.value.value == note["reason"]
    assert line.value.sources[0].field.startswith("not_measured.")
    verify_provenance(view, stored.storage)


def test_a_campaign_with_only_unreadable_groups_gives_the_recorded_reason(tmp_path: Path) -> None:
    from engine.measurement.segments import MAX_GROUPS

    stored = _store(tmp_path, 6_000, offers=MAX_GROUPS + 10)
    key = f"campaigns/{stored.campaign_id}/{SEGMENT_EFFECTS_FILENAME}"
    groups = json.loads(stored.storage.read_bytes(key))
    groups["cells"], groups["family_size"] = [], 0
    stored.storage.write_bytes(key, json.dumps(groups).encode())
    view = build_proof(stored.storage, stored.campaign_id)
    backfire = next(s for s in view.sections if s.key == "backfire")
    assert backfire.status == "not_measured"
    assert "carry no band" not in (backfire.reason or ""), "a made-up reason is never given"
    assert [line.value.value for line in backfire.lines if line.value is not None] == [
        note["reason"] for note in groups["not_measured"]
    ]
    page = html.unescape(render_html(proof_document(view)))
    assert str(MAX_GROUPS) in page and "Not read offer by offer" in page
    verify_provenance(view, stored.storage)


def test_a_group_harmed_under_a_stated_random_assignment_is_only_conditionally_backfired(
    tmp_path: Path,
) -> None:
    stored = _store(tmp_path, 6_000, basis="declared_random", group_column="segment")
    view = build_proof(stored.storage, stored.campaign_id)
    assert view.claim == "stated_random"
    assert [p.segment.value for p in view.proposals] == ["C"]
    backfire = next(s for s in view.sections if s.key == "backfire")
    assert backfire.table is not None
    verdicts = {
        (row[1].value if isinstance(row[1], Figure) else row[1]): row[-1] for row in backfire.table.rows
    }
    assert verdicts["C"] == "If the groups were random as you said: backfired"
    offer_money = next(s for s in view.sections if s.key == "offer_money")
    assert offer_money.status == "measured"
    assert any("only if the groups were chosen at random as you said" in note for note in offer_money.notes)
    page = html.unescape(render_html(proof_document(view)))
    assert "If the groups were random as you said, leave C out of the next cycle" in page
    assert "Leave C out" not in page


def test_a_line_that_cannot_be_read_says_why(tmp_path: Path) -> None:
    """A field missing from the report gives each line its own reason, never a bare "Not measured."."""
    stored = _store(tmp_path, 3_000)
    key = f"campaigns/{stored.campaign_id}/{REPORT_FILENAME}"
    report = json.loads(stored.storage.read_bytes(key))
    for name in ("treated_conversions", "treated_rate", "control_rate", "rows_without_outcome"):
        report.pop(name)
    stored.storage.write_bytes(key, json.dumps(report).encode())
    view = build_proof(stored.storage, stored.campaign_id)
    missing = [
        (section.key, line.label, line.missing)
        for section in view.sections
        for line in section.lines
        if line.value is None
    ]
    assert len(missing) >= 4
    for key_, label, why in missing:
        assert (
            why
            and len(why) > len("Not measured.")
            and why.rstrip(".") not in ("Not measured", "Not recorded")
        ), (
            key_,
            label,
            why,
        )
    for section in view.sections:
        assert section.reason is None or len(section.reason) > len("Not measured."), section.key
