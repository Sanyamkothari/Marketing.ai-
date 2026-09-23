"""The business results report: one champion model, for a client's marketing head (Plan E M61, DEC-909).

Five questions, each answered from an artefact the engine already wrote, in business terms:

1. **What does it do?** The use case's description, one sentence (`run_config.json`).
2. **How good is it?** "The 10% of customers the model flags first contain X% of everyone who
   left" - the test split's first-decile capture (`decile_lift.json`) - beside what a random 10%
   would contain (10%, by definition) and the simple yardstick model's score on the model's own
   primary measure (`baseline.json`, `evaluation.json`). For an uplift champion: the response gain
   an offer made in the model's top 10%, against contacting a random 10% (`uplift_evaluation.json`).
3. **Why?** The top reasons across all customers (`feature_importance.json`) with plain names, and
   three example customers with their own reasons (`row_explanations.parquet` of the latest scoring
   run, else of the training run's test split). The examples are the top-scored customer of each
   band, shown as "Customer A/B/C" with no ID, and any free-text value masked.
4. **What to do?** Customers per band with the suggested action, the control group and who was
   left out and why (`scoring_summary.json` of the latest scoring run by this model); for an
   uplift model the four segments (`segments.json`).
5. **What are the limits?** The period the data covers (the dataset's prediction dates, or the
   split dates), and what the model cannot know.

No number is computed that an artefact does not hold, and a section whose artefact is missing says
so instead of showing a placeholder figure (Plan E §3; Phase 1 rule 3: nothing fabricated reaches
the UI).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar

from pydantic import BaseModel

from engine.pilot.document import (
    AnyBlock,
    Bars,
    Bullets,
    Heading,
    KeyValues,
    Paragraph,
    ReportDocument,
    Table,
    Verdict,
)

if TYPE_CHECKING:
    from engine.clients import ClientStore
    from engine.config import UseCaseConfig
    from engine.contracts import (
        BaselineComparison,
        DecileLift,
        EvaluationReport,
        FeatureImportance,
        ModelVersion,
        RowExplanation,
        RunRecord,
        ScoringSummary,
        SplitReport,
    )
    from engine.onboarding.specs import DatasetManifest
    from engine.registry import ModelRegistry
    from engine.storage import Storage
    from engine.uplift.contracts import SegmentReport, UpliftEvaluation

__all__ = ["ResultsFacts", "ResultsNotFoundError", "collect_results", "results_document"]

M = TypeVar("M", bound=BaseModel)

EXAMPLES: Final[int] = 3
TOP_REASONS: Final[int] = 5


class ResultsNotFoundError(LookupError):
    """No champion (or no such model) to report on."""


@dataclass(frozen=True)
class ResultsFacts:
    model: ModelVersion
    train_run: RunRecord
    config: UseCaseConfig
    client_name: str
    evaluation: EvaluationReport | None
    deciles: DecileLift | None
    baseline: BaselineComparison | None
    importance: FeatureImportance | None
    split: SplitReport | None
    manifest: DatasetManifest | None
    score_run: RunRecord | None
    summary: ScoringSummary | None
    examples: tuple[RowExplanation, ...]
    examples_from: str
    uplift: UpliftEvaluation | None
    segments: SegmentReport | None
    feature_names: dict[str, str]
    root: Path | None = None

    def plain(self, text: str) -> str:
        """Engine words in config text ("subscriber", "snapshot") in the client's words."""
        from engine.pilot.data_request import load_wording

        return load_wording(self.root).plain(text)


def _read(storage: Storage, key: str, model: type[M]) -> M | None:
    from engine.storage import StorageError

    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


def _latest_score_run(storage: Storage, model_id: str) -> RunRecord | None:
    """The newest finished scoring run of `model_id`, from the run records."""
    from engine.contracts import RunRecord, RunState
    from engine.runs import RUN_FILENAME

    best: RunRecord | None = None
    for key in storage.list_keys("runs/"):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        record = _read(storage, key, RunRecord)
        if (
            record is None
            or record.mode.value != "score"
            or record.state is not RunState.DONE
            or record.model_version_id != model_id
        ):
            continue
        if best is None or record.created_at > best.created_at:
            best = record
    return best


def _example_rows(
    storage: Storage, key: str, config: UseCaseConfig, *, uplift: bool
) -> tuple[RowExplanation, ...]:
    """The (at most three) example rows, validated one by one - never the whole file.

    A scoring run explains every row it scored, so its `row_explanations.parquet` can hold millions
    of rows. Only the score column is scanned to choose the examples (the top score of each band;
    for an uplift model the three largest predicted gains); only the chosen rows become models.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from engine.contracts import RowExplanation

    with storage.open_read(key) as handle:
        table = pq.read_table(handle)  # type: ignore[no-untyped-call]
    scores = np.asarray(table.column("score").to_numpy(zero_copy_only=False), dtype=float)
    if scores.size == 0:
        return ()
    chosen: list[int] = []
    if uplift:
        chosen = [int(i) for i in np.argsort(-scores, kind="stable")[:EXAMPLES]]
    else:
        upper = np.inf
        for band in config.actions.bands:
            inside = np.flatnonzero((scores >= band.min_score) & (scores < upper))
            if inside.size:
                chosen.append(int(inside[np.argmax(scores[inside])]))
            upper = band.min_score
            if len(chosen) == EXAMPLES:
                break
    rows = table.take(chosen).to_pylist()
    return tuple(RowExplanation.model_validate(row) for row in rows)


def _humanise(name: str) -> str:
    text = re.sub(r"_(\d+)d\b", r" in the last \1 days", name)
    text = re.sub(r"_(\d+)m\b", r" over \1 months", text)
    text = text.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _feature_names(
    config: UseCaseConfig, manifest: DatasetManifest | None, store: ClientStore | None
) -> dict[str, str]:
    """Readable names: the recipe's own feature descriptions, then the use case's suggested features,
    template columns and standard columns; anything else is the column name made readable."""
    names: dict[str, str] = {}
    for column in config.standard_schema.columns:
        if column.description:
            names[column.name] = column.description.split(". ")[0].rstrip(".")
    for template_column in config.template.columns:
        if template_column.description:
            names[template_column.name] = template_column.description.split(". ")[0].rstrip(".")
    for feature in config.suggested_features:
        if feature.description:
            names[feature.name] = feature.description.split("; ")[0].rstrip(".")
    if manifest is not None and store is not None:
        from engine.clients import ClientStoreError

        try:
            spec = store.get_spec(manifest.spec_id)
        except ClientStoreError:
            spec = None
        if spec is not None:
            for feature in spec.feature_spec.features:
                if feature.description:
                    names[feature.name] = feature.description.split("; ")[0].rstrip(".")
    return names


def collect_results(
    storage: Storage,
    registry: ModelRegistry,
    *,
    use_case_id: str | None = None,
    model_id: str | None = None,
    client_store: ClientStore | None = None,
    root: Path | None = None,
) -> ResultsFacts:
    """Everything the report shows for `model_id`, or for the champion of `use_case_id`."""
    from engine.clients import ClientStoreError
    from engine.config import ResolvedConfig, load_use_case
    from engine.contracts import (
        BaselineComparison,
        DecileLift,
        EvaluationReport,
        FeatureImportance,
        RunManifest,
        RunRecord,
        ScoringSummary,
        SplitReport,
    )
    from engine.onboarding.datasets import DATASET_MANIFEST_FILENAME, dataset_key
    from engine.onboarding.specs import DatasetManifest
    from engine.registry import RegistryError
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key
    from engine.uplift.contracts import (
        SEGMENTS_FILENAME,
        UPLIFT_EVALUATION_FILENAME,
        SegmentReport,
        UpliftEvaluation,
    )

    try:
        if model_id is not None:
            model = registry.get(model_id)
        elif use_case_id is not None:
            champion = registry.get_champion(use_case_id)
            if champion is None:
                raise ResultsNotFoundError(f"{use_case_id} has no champion model yet")
            model = champion
        else:
            raise ResultsNotFoundError("name a use case or a model")
    except RegistryError as exc:
        raise ResultsNotFoundError(str(exc)) from exc

    train = _read(storage, run_key(model.run_id, RUN_FILENAME), RunRecord)
    if train is None:
        raise ResultsNotFoundError(f"the training run {model.run_id} of {model.model_id} is not in the store")
    resolved = _read(storage, run_key(model.run_id, "run_config.json"), ResolvedConfig)
    config = resolved.config if resolved is not None else load_use_case(model.use_case_id, root)
    run_manifest = _read(storage, run_key(model.run_id, "run_manifest.json"), RunManifest)
    dataset_id = train.dataset_id or (run_manifest.dataset_id if run_manifest else None)
    manifest = (
        _read(storage, dataset_key(dataset_id, DATASET_MANIFEST_FILENAME), DatasetManifest)
        if dataset_id
        else None
    )
    client_name = ""
    client_id = train.client_id or (manifest.client_id if manifest else None)
    if client_id and client_store is not None:
        try:
            client_name = client_store.get_client(client_id).name
        except ClientStoreError:
            client_name = ""

    uplift_run = storage.exists(run_key(model.run_id, UPLIFT_EVALUATION_FILENAME))
    score_run = _latest_score_run(storage, model.model_id)
    summary = (
        _read(storage, run_key(score_run.run_id, "scoring_summary.json"), ScoringSummary)
        if score_run
        else None
    )
    examples: tuple[RowExplanation, ...] = ()
    examples_from = ""
    for run, label in ((score_run, "scored"), (train, "test")):
        if run is None:
            continue
        key = run_key(run.run_id, "row_explanations.parquet")
        if storage.exists(key):
            try:
                examples = _example_rows(storage, key, config, uplift=uplift_run)
                examples_from = label
                break
            except Exception:  # an unreadable file means no examples, never invented ones
                examples = ()
    segments = None
    if score_run is not None:
        segments = _read(storage, run_key(score_run.run_id, SEGMENTS_FILENAME), SegmentReport)
    if segments is None:
        segments = _read(storage, run_key(model.run_id, SEGMENTS_FILENAME), SegmentReport)
    return ResultsFacts(
        model=model,
        train_run=train,
        config=config,
        client_name=client_name,
        evaluation=_read(storage, run_key(model.run_id, "evaluation.json"), EvaluationReport),
        deciles=_read(storage, run_key(model.run_id, "decile_lift.json"), DecileLift),
        baseline=_read(storage, run_key(model.run_id, "baseline.json"), BaselineComparison),
        importance=_read(storage, run_key(model.run_id, "feature_importance.json"), FeatureImportance),
        split=_read(storage, run_key(model.run_id, "split.json"), SplitReport),
        manifest=manifest,
        score_run=score_run,
        summary=summary,
        examples=examples,
        examples_from=examples_from,
        uplift=_read(storage, run_key(model.run_id, UPLIFT_EVALUATION_FILENAME), UpliftEvaluation),
        segments=segments,
        feature_names=_feature_names(config, manifest, client_store),
        root=root,
    )


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def _plain(facts: ResultsFacts, feature: str) -> str:
    return facts.plain(facts.feature_names.get(feature) or _humanise(feature))


def _outcome_words(config: UseCaseConfig) -> str:
    """ "everyone who left" - from the target definition, lower-cased, as a noun phrase."""
    definition = (
        config.label.description
        if config.label is not None and config.label.description
        else config.target.definition
    )
    return definition.rstrip(".") if definition else "the outcome"


def _example_value(value: str) -> str:
    from engine.pii import redact_text

    text, _ = redact_text(value.removesuffix(" 00:00:00"))
    return text if len(text) <= 40 else text[:37] + "..."


def _examples(facts: ResultsFacts) -> list[tuple[str, RowExplanation]]:
    """The top-scored customer of each band, highest band first, at most three. An uplift model's
    score is a predicted gain, not a probability the bands were set for, so there it is simply the
    three customers with the largest predicted gain."""
    if facts.uplift is not None:
        top = sorted(facts.examples, key=lambda e: e.score, reverse=True)[:EXAMPLES]
        return [("", example) for example in top]
    bands = facts.config.actions.bands
    chosen: list[tuple[str, RowExplanation]] = []
    for band in bands:
        members = [e for e in facts.examples if facts.config.actions.band_for(e.score).name == band.name]
        if members:
            chosen.append((band.name, max(members, key=lambda e: e.score)))
        if len(chosen) == EXAMPLES:
            break
    return chosen


def results_document(
    facts: ResultsFacts, *, root: Path | None = None, now: datetime | None = None
) -> ReportDocument:
    """The results report of one model."""
    from engine.pilot.help import load_help
    from engine.pilot.plain import terms_used
    from engine.utils.time import utc_now

    help_catalogue = load_help(root)
    config = facts.config
    blocks: list[AnyBlock] = []
    uplift = facts.uplift

    # 1. what it does ------------------------------------------------------------------------------
    blocks += [Heading(text="What the model does"), Paragraph(text=facts.plain(config.description))]

    # 2. how good it is ----------------------------------------------------------------------------
    blocks.append(Heading(text="How good it is"))
    headline = ""
    if uplift is not None:
        top = next((u for u in uplift.uplift_at if abs(u.fraction - 0.1) < 1e-9), None)
        ate = uplift.average_treatment_effect
        if top is not None:
            headline = (
                f"Among the 10% of customers the model ranks first, an offer raised the response rate by "
                f"{top.uplift.value * 100:.1f} points"
                + (
                    f" (range {top.uplift.ci_low * 100:.1f} to {top.uplift.ci_high * 100:.1f})"
                    if top.uplift.ci_low is not None and top.uplift.ci_high is not None
                    else ""
                )
                + f"; offering it to everyone raised it by {ate.value * 100:.1f} points on average."
            )
            blocks.append(
                Verdict(
                    state="info",
                    title=headline,
                    text=(
                        "Offering it to a random 10% would raise it by about the average; the model's "
                        "ranking finds the customers an offer actually moves."
                        if uplift.measurable_uplift
                        else "The model's ranking is not yet measurably better than a random pick; treat its "
                        "list with care."
                    ),
                )
            )
        rows = [
            (
                f"Top {d.decile * 10}%" if d.decile == 1 else f"{(d.decile - 1) * 10}-{d.decile * 10}%",
                (
                    f"{d.observed_uplift * 100:+.1f} points"
                    if d.observed_uplift is not None
                    else "not measurable"
                ),
            )
            for d in uplift.deciles
        ]
        blocks.append(
            Table(
                columns=("Customers, ranked by the model", "Response gained by the offer"),
                rows=tuple(rows),
                caption=f"Measured on {uplift.rows_evaluated:,} customers of a past campaign the model did not learn from.",
            )
        )
    elif facts.deciles is not None and facts.deciles.bins:
        bins = facts.deciles.bins
        first = bins[0]
        outcome = facts.plain(_outcome_words(config))
        if first.cumulative_capture_pct is not None:
            headline = (
                f"The 10% of customers the model flags first contain {first.cumulative_capture_pct:.0f}% of all "
                f"customers with the outcome ({outcome.lower()})."
            )
            blocks.append(
                Verdict(
                    state="info",
                    title=headline,
                    text="A random 10% would contain 10% of them"
                    + (
                        f", so the model's first tenth finds {first.cumulative_lift:.1f} times as many."
                        if first.cumulative_lift is not None
                        else "."
                    ),
                )
            )
        items = [
            (
                f"Top {b.decile * 10}%",
                (b.cumulative_capture_pct or 0.0) / 100.0,
                (
                    f"{b.cumulative_capture_pct:.0f}% (random: {b.decile * 10}%)"
                    if b.cumulative_capture_pct is not None
                    else ""
                ),
            )
            for b in bins[:5]
        ]
        blocks.append(
            Bars(
                title="Share of all cases found, by how far down the list you go",
                items=tuple(items),
                reference_label=(
                    f"Measured on {facts.evaluation.rows_evaluated:,} customers the model did not learn from."
                    if facts.evaluation is not None
                    else ""
                ),
            )
        )
    else:
        blocks.append(Paragraph(text="The model's ranking was not recorded for this run.", muted=True))

    evaluation, baseline = facts.evaluation, facts.baseline
    if evaluation is not None and uplift is None:
        metric = help_catalogue.metrics.get(evaluation.primary_metric.value)
        row = (
            next((r for r in baseline.rows if r.id == evaluation.primary_metric.value), None)
            if baseline
            else None
        )
        values: list[tuple[str, str]] = [("This model", f"{evaluation.headline_score:.3f}")]
        if row is not None and row.baseline_value is not None:
            values.append(("A simple yardstick model", f"{row.baseline_value:.3f}"))
        if metric is not None and metric.random is not None:
            values.append(("A random pick", f"{metric.random:.3f}"))
        blocks += [
            Paragraph(
                text=f"Measured on the same customers: {metric.name if metric else evaluation.primary_metric_label}."
            ),
            KeyValues(rows=tuple(values)),
        ]
        if baseline is not None:
            blocks.append(
                Paragraph(
                    text=(
                        "It beats the simple yardstick model on the measure it was chosen on."
                        if baseline.model_beats_baseline
                        else "It does not beat the simple yardstick model on the measure it was chosen on; "
                        "treat its results with care and ask Minfy to review."
                    ),
                    muted=True,
                )
            )

    # 3. why -----------------------------------------------------------------------------------------
    blocks.append(Heading(text="Why customers are flagged"))
    if facts.importance is not None and facts.importance.items:
        top_items = facts.importance.items[:TOP_REASONS]
        widest = max((item.share_pct for item in top_items), default=0.0) or 1.0
        blocks.append(
            Bars(
                title="The details that matter most, across all customers",
                items=tuple(
                    (_plain(facts, item.feature), item.share_pct / widest, f"{item.share_pct:.0f}%")
                    for item in top_items
                ),
                reference_label="Share of the model's decisions each detail accounts for.",
            )
        )
    else:
        blocks.append(
            Paragraph(text="The reasons across customers were not recorded for this run.", muted=True)
        )
    examples = _examples(facts)
    if examples:
        letters = "ABC"
        rows_out = []
        for index, (band, example) in enumerate(examples):
            reasons = "; ".join(
                f"{_plain(facts, reason.feature)} {'raises' if reason.direction.value == 'up' else 'lowers' if reason.direction.value == 'down' else 'affects'} the score"
                + (
                    f" (value: {_example_value(reason.value)})"
                    if reason.value not in ("", "None", "nan")
                    else ""
                )
                for reason in example.reasons[:3]
            )
            score = f"{example.score * 100:+.1f} points" if uplift is not None else f"{example.score:.2f}"
            rows_out.append((f"Customer {letters[index]}", band or "-", score, reasons))
        blocks.append(
            Table(
                columns=(
                    "Example",
                    "Band",
                    "Predicted gain from an offer" if uplift is not None else "Score",
                    "Main reasons",
                ),
                rows=tuple(rows_out),
                caption=(
                    "Anonymised: the top-scored customer of each band"
                    + (
                        " in the latest scoring run"
                        if facts.examples_from == "scored"
                        else " among the test customers"
                    )
                    + ". IDs are not shown."
                ),
            )
        )

    # 4. what to do ------------------------------------------------------------------------------------
    blocks.append(Heading(text="What to do"))
    summary = facts.summary
    if facts.segments is not None and uplift is not None:
        blocks.append(
            Table(
                columns=("Group", "Customers", "Share", "Suggested action"),
                rows=tuple(
                    (s.label, f"{s.rows:,}", f"{s.share_pct:.0f}%", s.action) for s in facts.segments.segments
                ),
            )
        )
    if summary is not None:
        if uplift is None:
            blocks.append(
                Table(
                    columns=("Band", "Customers", "Share", "Suggested action"),
                    rows=tuple(
                        (b.name, f"{b.rows:,}", f"{b.share_pct:.0f}%", b.action) for b in summary.bands
                    ),
                    caption=f"From the latest scoring run ({summary.rows_scored:,} customers scored).",
                )
            )
        left_out = sum(s.rows for s in summary.suppressed)
        rows_kv: list[tuple[str, str]] = [
            (
                "Control group (not contacted, to measure the effect)",
                f"{summary.control_group_rows:,} customers",
            )
        ]
        if left_out:
            left_out_reasons = {
                "opted_out": "opted out",
                "recently_contacted": "contacted recently",
                "consent_false": "no consent",
            }
            rows_kv.append(
                (
                    "Left out of contact",
                    ", ".join(
                        f"{s.rows:,} {left_out_reasons.get(s.reason, s.reason)}" for s in summary.suppressed
                    ),
                )
            )
        blocks.append(KeyValues(rows=tuple(rows_kv)))
    elif uplift is None:
        blocks.append(
            Paragraph(
                text="No customer file has been scored with this model yet; the contact list appears here once one is."
            )
        )

    # 5. limits ----------------------------------------------------------------------------------------
    limits: list[str] = []
    period = _period(facts)
    if period:
        limits.append(
            f"The model learned from data covering {period}. Customers or conditions unlike that period may be scored less well."
        )
    limits += [
        "The model sees only the data sent to it. It cannot know about a competitor's offer, a network outage or a price change that is not in the data.",
        "A score is a likelihood, not a certainty: some flagged customers will not have the outcome, and some unflagged ones will.",
    ]
    limits.append(
        "Its ranking is estimated from one past campaign; the control group of the next one is what measures the real effect."
        if uplift is not None
        else "It shows who is likely to have the outcome, not whether contacting them changes it; that is what the control group measures."
    )
    if uplift is not None and not uplift.causal:
        limits.append(
            "The past campaign it learned from did not choose customers at random, so its effects are descriptive only."
        )
    blocks += [Heading(text="Limits"), Bullets(items=tuple(limits))]

    texts = [headline, *(_strings(block.model_dump()) for block in blocks)]
    used = terms_used(texts, help_catalogue.terms)
    if used:
        blocks += [
            Heading(text="Terms used", level=3),
            KeyValues(rows=tuple((term.capitalize(), help_catalogue.terms[term]) for term in used)),
        ]
    model = facts.model
    return ReportDocument(
        kind="results",
        title="Results report",
        subtitle=f"{config.name}" + (f" - {facts.client_name}" if facts.client_name else ""),
        client_name=facts.client_name,
        generated_at=now or utc_now(),
        facts=(
            ("Client", facts.client_name or "not recorded"),
            ("Use case", config.name),
            ("Model", f"version {model.version} ({model.status.value})"),
            ("Trained", model.created_at.strftime("%d %b %Y")),
            ("Data period", period or "not recorded"),
        ),
        blocks=tuple(blocks),
        footer="All figures are read from the platform's records of this model; nothing is estimated for this page.",
    )


def _strings(value: Any) -> str:
    """Every string inside a dumped block, joined: what the glossary check reads."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_strings(v) for v in value.values())
    if isinstance(value, list | tuple):
        return " ".join(_strings(v) for v in value)
    return ""


def _period(facts: ResultsFacts) -> str:
    if facts.manifest is not None and facts.manifest.snapshot_dates:
        dates = facts.manifest.snapshot_dates
        count = len(dates)
        if count == 1:
            return f"{_d(dates[0])} (1 prediction date)"
        return f"{_d(dates[0])} to {_d(dates[-1])} ({count} prediction dates)"
    split = facts.split
    if split is not None and split.train_cutoff is not None:
        starts = [p.start_date for p in split.parts if p.start_date is not None]
        ends = [p.end_date for p in split.parts if p.end_date is not None]
        if starts and ends:
            return f"{_d(min(starts))} to {_d(max(ends))}"
    return ""


def _d(value: date | datetime | Any) -> str:
    return value.strftime("%d %b %Y") if hasattr(value, "strftime") else str(value)
