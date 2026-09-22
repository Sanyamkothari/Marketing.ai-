"""Root-cause summaries: an LLM reading of a *finished* churn run, never a stage of producing one.

A predictive run already says who is at risk and, per row, why - `scores.csv` carries the band and
`row_explanations.parquet` carries the SHAP reasons the explain stage measured. What it does not
carry is a sentence a retention manager can act on, and that is the one thing this module adds.
**It reads three artefacts and writes a fourth.** `scores.csv` gives the band per row,
`row_explanations.parquet` gives the per-row reasons those bands rest on, and `feature_importance.json`
gives the run's global ranking, used only to break a tie between two reasons of equal weight inside
one segment. Nothing here retrains, rescoles or joins a second dataset; a run that has not finished
raises `RUN_NOT_FINISHED` rather than being read half-built, because a summary of a run still writing
its own files would be a summary of numbers about to change.

**A segment is rows, not a query.** `generative.root_cause.segment_by` cuts the scored rows either by
band or by each row's own strongest reason, and `build_evidence_pack` turns one segment into the
`EvidencePack` its prompt is allowed to see: the segment's size, its score statistics computed here in
plain arithmetic, its strongest *aggregated* reasons - one row's SHAP value is noise, the mean over a
few hundred rows that share a band is a pattern - and, when the use case configures one, a sample of
the free-text column redacted before it is read into the pack (DEC-216). A model is handed the pack
and nothing else; it has no route back to the run, the source file or a customer's name.

**Grounded or nothing is checked in code, not asked for in prose.** The prompt tells the model every
`evidence_refs` entry must name an id from the pack, and a model that ignores that instruction is the
whole reason the check exists. `grounded_causes` drops any `RootCause` whose refs are not a subset of
`EvidencePack.reference_ids` before anything is stored, so an invented reference cannot reach a screen
no matter how the model was asked to behave. A reply that grounds nothing survives no better than one
that parses to nothing: both count as a failed attempt, retried up to `guardrails.retries` times and
then raised as `UNGROUNDED_CLAIM` or `MODEL_OUTPUT_MALFORMED`, naming the segment and the attempt
count rather than the model's words.

**A segment's failure is not the job's failure, and the job's failure is not a segment's.**
`generate_segment_summary` raises once a segment is out of retries; `build_root_cause_summary`
catches that one segment's exception and stores a `SummarisedSegment` with `summary=None` and
`blocked_reason` set, then moves on to the next segment. The alternative - one bad segment aborting
the whole run - would throw away every other segment's evidence pack along with it, and the pack is
the artefact a reader can act on even where the prose could not be produced. `BUDGET_EXCEEDED` is
the one code that is not a segment's to absorb: the meter refuses the call before making it, so
every remaining segment would be refused identically, and a job that ran out of money is a fact
about the job.
"""

from __future__ import annotations

import io
import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pandas as pd

from engine.config import RootCauseConfig, SegmentBy, UseCaseConfig
from engine.contracts import DatasetProfile, Direction, FeatureImportance, RowExplanation, RunRecord, RunState
from engine.generative.budget import Meter
from engine.generative.contracts import (
    Confidence,
    EvidencePack,
    EvidenceReason,
    GenerativePurpose,
    GuardrailCheck,
    RedactedComplaint,
    RootCause,
    RootCauseSummary,
    SegmentStats,
    SegmentSummary,
    SummarisedSegment,
)
from engine.generative.errors import (
    BUDGET_EXCEEDED,
    COMPLAINT_COLUMN_MISSING,
    GUARDRAIL_BLOCKED,
    MODEL_OUTPUT_MALFORMED,
    RUN_NOT_FINISHED,
    RUN_WITHOUT_EXPLANATIONS,
    RUN_WITHOUT_SCORES,
    UNGROUNDED_CLAIM,
    GenerativeError,
    generative_error,
)
from engine.generative.guardrails import CheckContext, Guardrails
from engine.generative.prompts import load_prompt, prompt_hashes, prompt_versions, render
from engine.generative.redaction import redact
from engine.stages.actions import BAND_COLUMN
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.stages.export import SCORES_CSV
from engine.stages.ingest import read_upload
from engine.storage import Storage, run_key, upload_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

__all__ = [
    "ROOT_CAUSE_PROMPT",
    "aggregate_reasons",
    "build_evidence_pack",
    "build_root_cause_summary",
    "generate_segment_summary",
    "grounded_causes",
    "segment_stats",
]

_LOGGER = get_logger(__name__)

ROOT_CAUSE_PROMPT: Final[str] = "root_cause_summary"

_RUN_RECORD_FILENAME: Final[str] = "run.json"
_PROFILE_FILENAME: Final[str] = "profile.json"
_FEATURE_IMPORTANCE_FILENAME: Final[str] = "feature_importance.json"
"""No shared constant names this one (`engine.pipeline` and `engine.stages.register` each spell it
independently), so this module spells it too rather than importing either for one string."""

_CONTRIBUTION_DECIMALS: Final[int] = 6
_SCORE_DECIMALS: Final[int] = 4
_SHARE_DECIMALS: Final[int] = 1

_CODE_FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z]+")
_MIN_MATCH_TOKEN: Final[int] = 4
"""Shortest feature-name token worth matching against a complaint; shorter ones match too much."""


# ---------------------------------------------------------------------------
# Reading the run
# ---------------------------------------------------------------------------
def _load_finished_run(run_id: str, storage: Storage) -> RunRecord:
    """`run.json`, or the coded failure a caller cannot work around by reading more files.

    Checked in this order because each later check assumes the one before it: a run that is not
    `done` may not have written `scores.csv` yet at all, and a run without scores has no rows for
    `row_explanations.parquet` to explain.
    """
    run = storage.read_model(run_key(run_id, _RUN_RECORD_FILENAME), RunRecord)
    if run.state is not RunState.DONE:
        raise generative_error(RUN_NOT_FINISHED, run_id=run_id, state=run.state.value)
    if SCORES_CSV not in run.artefacts:
        raise generative_error(RUN_WITHOUT_SCORES, run_id=run_id)
    if ROW_EXPLANATIONS_FILENAME not in run.artefacts:
        raise generative_error(RUN_WITHOUT_EXPLANATIONS, run_id=run_id)
    return run


def _primary_key_column(use_case: UseCaseConfig) -> str:
    """The template's primary-key column name; every predictive template declares exactly one."""
    column = use_case.template.primary_key
    if column is None:
        raise ValueError(f"{use_case.id} has no primary_key column in its template")
    return column.name


def _read_scores(storage: Storage, key: str) -> pd.DataFrame:
    """`scores.csv` as strings throughout: a band name and a primary key are never arithmetic."""
    return pd.read_csv(io.StringIO(storage.read_text(key)), dtype=str, keep_default_na=False)


def _read_complaints(
    run: RunRecord, use_case: UseCaseConfig, storage: Storage
) -> tuple[dict[str, str] | None, str | None]:
    """Primary key -> raw complaint text from the run's own upload, and where it came from.

    `None, None` when the use case names no complaint column at all - the common, unremarkable case
    for a use case with no free text to read, and not an error. Reading the upload only happens once
    a column *is* configured, because a use case with no such column should cost this module nothing.
    """
    column = use_case.generative.root_cause.complaint_text_column
    if column is None:
        return None, None
    profile = storage.read_model(run.artefacts[_PROFILE_FILENAME], DatasetProfile)
    source = upload_key(run.upload_id, f"source.{profile.file_format}")
    frame = read_upload(storage, source, file_format=profile.file_format).frame
    if column not in frame.columns:
        raise generative_error(COMPLAINT_COLUMN_MISSING, column=column, run_id=run.run_id)
    primary_key = _primary_key_column(use_case)
    keys: list[str] = [str(value) for value in frame[primary_key]]
    texts: list[str] = [str(value).strip() for value in frame[column].fillna("")]
    by_key = {key: text for key, text in zip(keys, texts, strict=True) if text}
    return by_key, f"column:{column}"


# ---------------------------------------------------------------------------
# Segmenting the scored rows
# ---------------------------------------------------------------------------
def _segment_name(
    cfg: RootCauseConfig,
    key: str,
    band_by_key: Mapping[str, str],
    explanations_by_key: Mapping[str, RowExplanation],
) -> str | None:
    """The segment `key` falls into, or `None` when it has nothing to segment on."""
    if cfg.segment_by is SegmentBy.BAND:
        return band_by_key.get(key)
    explanation = explanations_by_key.get(key)
    if explanation is None or not explanation.reasons:
        return None
    return explanation.reasons[0].feature


def _segment_groups(
    cfg: RootCauseConfig,
    keys_ordered: Sequence[str],
    band_by_key: Mapping[str, str],
    explanations_by_key: Mapping[str, RowExplanation],
) -> list[tuple[str, list[str]]]:
    """Segment name -> the keys in it, largest segment first, capped at `max_segments`."""
    groups: dict[str, list[str]] = {}
    for key in keys_ordered:
        name = _segment_name(cfg, key, band_by_key, explanations_by_key)
        if name is None:
            continue
        groups.setdefault(name, []).append(key)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    return ordered[: cfg.max_segments]


# ---------------------------------------------------------------------------
# Pure arithmetic: stats and aggregated reasons, neither of which ever touches a model
# ---------------------------------------------------------------------------
def _share_pct(part: float, whole: float) -> float:
    """`part` as a percentage of `whole`; an empty `whole` is zero, not a division error."""
    if whole <= 0:
        return 0.0
    return round(100.0 * part / whole, _SHARE_DECIMALS)


def segment_stats(scores: Sequence[float], *, total_rows: int) -> SegmentStats:
    """What is measurably true of a segment, from the scores its rows carry and nothing else.

    `positive_rate` stays null: this module reads a finished *scoring* run, and a scoring run's own
    fixture never carries an outcome column - the target is exactly what scoring exists to predict.
    The field is on the contract for a labelled file no flow here produces.
    """
    rows = len(scores)
    mean_score = math.fsum(scores) / rows if rows else 0.0
    return SegmentStats(
        rows=rows,
        share_pct=_share_pct(rows, total_rows),
        mean_score=round(mean_score, _SCORE_DECIMALS),
        min_score=round(min(scores), _SCORE_DECIMALS) if rows else 0.0,
        max_score=round(max(scores), _SCORE_DECIMALS) if rows else 0.0,
    )


def aggregate_reasons(
    explanations: Sequence[RowExplanation],
    *,
    limit: int,
    importance_rank: Mapping[str, int] = MappingProxyType({}),
) -> tuple[EvidenceReason, ...]:
    """A segment's strongest *aggregated* reasons, strongest first, ids `r1`, `r2`, ... within the pack.

    One row's SHAP contribution is noise; the mean absolute contribution over every row in the
    segment for which a feature was among its own top reasons is the pattern the segment shares, and
    `rows` on the result is exactly that row count - not the segment's size, which a reader gets from
    `SegmentStats` instead. `direction` is the majority of the per-row directions measured for the
    feature, ties going up; a feature every row measured as `none` (a general, unmeasured reason)
    stays `none` rather than being called a direction nobody measured. `importance_rank` breaks a tie
    between two features of equal segment weight using the run's own global ranking, so the order is
    never an artefact of iteration.
    """
    per_feature: dict[str, list[tuple[float, Direction]]] = {}
    for explanation in explanations:
        for reason in explanation.reasons:
            per_feature.setdefault(reason.feature, []).append((reason.contribution, reason.direction))
    if not per_feature:
        return ()
    aggregated: list[tuple[float, int, str, Direction]] = []
    for feature, values in per_feature.items():
        mean_abs = math.fsum(abs(contribution) for contribution, _direction in values) / len(values)
        up = sum(1 for _contribution, direction in values if direction is Direction.UP)
        down = sum(1 for _contribution, direction in values if direction is Direction.DOWN)
        if up == 0 and down == 0:
            direction = Direction.NONE
        elif up >= down:
            direction = Direction.UP
        else:
            direction = Direction.DOWN
        aggregated.append((mean_abs, len(values), feature, direction))
    total = math.fsum(mean_abs for mean_abs, *_rest in aggregated)
    fallback_rank = len(importance_rank) + 1
    aggregated.sort(key=lambda entry: (-entry[0], importance_rank.get(entry[2], fallback_rank), entry[2]))
    return tuple(
        EvidenceReason(
            id=f"r{position}",
            feature=feature,
            direction=direction,
            mean_abs_contribution=round(mean_abs, _CONTRIBUTION_DECIMALS),
            share_pct=_share_pct(mean_abs, total),
            rows=rows,
        )
        for position, (mean_abs, rows, feature, direction) in enumerate(aggregated[:limit], start=1)
    )


def _match_reasons(text: str, reasons: Sequence[EvidenceReason]) -> tuple[str, ...]:
    """Which of a segment's reasons this complaint's words echo, strongest overlap first.

    A soft, best-effort heuristic - a feature name and a customer's sentence share a vocabulary only
    loosely - so it is scored by substring overlap between a feature's own tokens and the complaint's
    words rather than by an exact match a real complaint would rarely produce.
    """
    words = _WORD.findall(text.lower())
    scored: list[tuple[int, str]] = []
    for reason in reasons:
        tokens = [token for token in _WORD.findall(reason.feature.lower()) if len(token) >= _MIN_MATCH_TOKEN]
        overlap = sum(1 for token in tokens for word in words if token in word or word in token)
        if overlap:
            scored.append((overlap, reason.id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(ref for _overlap, ref in scored)


def _complaint_samples(
    keys: Sequence[str],
    complaints_by_key: Mapping[str, str] | None,
    reasons: Sequence[EvidenceReason],
    *,
    limit: int,
) -> tuple[RedactedComplaint, ...]:
    """Up to `limit` complaints from this segment, each redacted before it becomes part of the pack.

    Redaction happens here, on the raw text read from the run's own upload, before a `RedactedComplaint`
    is ever constructed - there is no code path in this module that builds one from unredacted text
    (DEC-216).
    """
    if not complaints_by_key or limit <= 0:
        return ()
    samples: list[RedactedComplaint] = []
    for key in keys:
        if len(samples) >= limit:
            break
        raw = complaints_by_key.get(key)
        if not raw:
            continue
        redacted, _kinds = redact(raw)
        samples.append(
            RedactedComplaint(
                id=f"c{len(samples) + 1}",
                text=redacted,
                matched_reason_ids=_match_reasons(redacted, reasons),
            )
        )
    return tuple(samples)


def build_evidence_pack(
    segment: str,
    *,
    keys: Sequence[str],
    scores_by_key: Mapping[str, float],
    explanations_by_key: Mapping[str, RowExplanation],
    total_rows: int,
    complaints_by_key: Mapping[str, str] | None,
    config: RootCauseConfig,
    importance_rank: Mapping[str, int] = MappingProxyType({}),
) -> EvidencePack:
    """Everything a segment's prompt is allowed to know, and nothing else.

    `keys` are this segment's rows; every other argument is the whole run's data, read once and
    handed to every segment so no file is read twice for a run with several segments.
    """
    scores = [scores_by_key[key] for key in keys]
    explanations = [explanations_by_key[key] for key in keys if key in explanations_by_key]
    reasons = aggregate_reasons(
        explanations, limit=config.reasons_per_segment, importance_rank=importance_rank
    )
    complaints = _complaint_samples(
        keys, complaints_by_key, reasons, limit=config.complaint_samples_per_segment
    )
    return EvidencePack(
        segment=segment,
        stats=segment_stats(scores, total_rows=total_rows),
        reasons=reasons,
        complaints=complaints,
    )


# ---------------------------------------------------------------------------
# Grounding: the check that decides what may ever reach storage
# ---------------------------------------------------------------------------
def grounded_causes(raw_causes: Iterable[Any], pack: EvidencePack) -> tuple[RootCause, ...]:
    """The model's proposed causes, minus every one that cites an id `pack` did not supply.

    Checked here, against `EvidencePack.reference_ids`, rather than trusted from the prompt that
    asked for it - the single check this module exists to make. A cause that does not parse into a
    `RootCause` at all (no `cause` text, an empty `evidence_refs`, an unrecognised `confidence`) is
    dropped the same way an ungrounded one is: a claim this module cannot make sense of is a claim it
    does not store, not a reason to fail the whole segment when another cause in the same reply is
    perfectly fine.
    """
    kept: list[RootCause] = []
    for item in raw_causes:
        if not isinstance(item, Mapping):
            continue
        raw_refs = item.get("evidence_refs")
        refs = tuple(str(ref) for ref in raw_refs) if isinstance(raw_refs, list) else ()
        try:
            cause = RootCause(
                cause=str(item.get("cause", "")).strip(),
                evidence_refs=refs,
                confidence=Confidence(str(item.get("confidence", "low"))),
            )
        except ValueError:
            continue
        if not set(cause.evidence_refs) <= pack.reference_ids:
            continue
        kept.append(cause)
    return tuple(kept)


def _texts(value: object) -> tuple[str, ...]:
    """A list of prompt-supplied strings, blanks dropped; anything else is treated as none at all."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _parse_reply(raw: str) -> dict[str, Any] | None:
    """The model's reply as an object, or `None` when it is not one - a code fence stripped first."""
    try:
        parsed = json.loads(_CODE_FENCE.sub("", raw).strip())
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _prose(summary: SegmentSummary) -> str:
    """Every sentence a guardrail should read: the headline, each cause, each action, each caveat."""
    lines = [summary.headline, *(cause.cause for cause in summary.root_causes), *summary.recommended_actions]
    lines.extend(summary.caveats)
    return "\n".join(line for line in lines if line)


def _render_evidence(pack: EvidencePack) -> str:
    """The pack as the JSON object the prompt shows the model - the whole pack, nothing summarised."""
    return json.dumps(pack.model_dump(mode="json"), indent=2)


def generate_segment_summary(
    segment: str,
    pack: EvidencePack,
    *,
    use_case: UseCaseConfig,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None = None,
) -> tuple[SegmentSummary, tuple[GuardrailCheck, ...], int]:
    """Call the model for one segment, ground and guardrail-check the reply, retrying on failure.

    Every attempt goes through `meter.complete`, so a retry is a call the budget sees and the usage
    report counts. An attempt fails for one of three reasons, tried in this order because each is
    cheaper to detect than the next: the reply does not parse as JSON at all; it parses but grounds
    nothing (`grounded_causes` kept none of its causes); or it grounds fine but a guardrail blocks the
    prose. `guardrails.retries` more attempts follow a first failure, and once every attempt is spent
    this raises the code of the *last* attempt's failure - `MODEL_OUTPUT_MALFORMED`,
    `UNGROUNDED_CLAIM` or `GUARDRAIL_BLOCKED` - naming the segment and how many tries were made. The
    caller decides what a segment with no summary means for the job; this function only ever speaks
    for the one segment it was asked about.
    """
    prompt = load_prompt(ROOT_CAUSE_PROMPT, config_root)
    evidence_text = _render_evidence(pack)
    root_cause_cfg = use_case.generative.root_cause
    values: dict[str, str] = {
        "segment": segment,
        "evidence": evidence_text,
        "tone": root_cause_cfg.tone,
        "entity": use_case.entity,
    }
    checks: list[GuardrailCheck] = []
    attempts = guardrails.retries + 1
    failure_code = MODEL_OUTPUT_MALFORMED
    failure_kwargs: dict[str, object] = {"prompt": prompt.name, "attempts": attempts}
    for attempt in range(1, attempts + 1):
        completion = meter.complete(render(prompt, values), GenerativePurpose.ROOT_CAUSE_SUMMARY)
        payload = _parse_reply(completion.text)
        if payload is None:
            failure_code, failure_kwargs = MODEL_OUTPUT_MALFORMED, {
                "prompt": prompt.name,
                "attempts": attempt,
            }
            continue
        causes = grounded_causes(payload.get("root_causes") or [], pack)
        if not causes:
            failure_code, failure_kwargs = UNGROUNDED_CLAIM, {"segment": segment, "attempts": attempt}
            continue
        summary = SegmentSummary(
            headline=str(payload.get("headline", "")).strip(),
            root_causes=causes,
            recommended_actions=_texts(payload.get("recommended_actions")),
            caveats=_texts(payload.get("caveats")),
        )
        result = guardrails.check(
            _prose(summary),
            CheckContext(target=segment, source=evidence_text, judges=("faithfulness",)),
        )
        checks.extend(result.checks)
        if result.passed:
            return summary, tuple(checks), attempt
        failure_code = GUARDRAIL_BLOCKED
        failure_kwargs = {"target": segment, "rule": result.blocked_by}
    raise generative_error(failure_code, **failure_kwargs)


def _summarise_segment(
    segment: str,
    pack: EvidencePack,
    *,
    use_case: UseCaseConfig,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
) -> SummarisedSegment:
    """One segment's row in `RootCauseSummary.segments`, whether generation succeeded or not.

    Only a failure that is *about this segment* becomes a row. `BUDGET_EXCEEDED` is about the job:
    the meter refused the call before making it, every later segment would be refused the same way,
    and recording it per segment turns one clean stop into a summary whose segments each say "this
    run reached its call limit" as though the evidence had been weighed and found wanting. It is
    re-raised, which is what `budget.Meter` promises a caller - a run that stops this way stops
    cleanly, with the segments it did finish and a usage record that says why.
    """
    try:
        summary, checks, attempts = generate_segment_summary(
            segment, pack, use_case=use_case, meter=meter, guardrails=guardrails, config_root=config_root
        )
    except GenerativeError as exc:
        if exc.code == BUDGET_EXCEEDED:
            raise
        _LOGGER.info("root_cause.blocked segment=%s code=%s", segment, exc.code)
        return SummarisedSegment(
            segment=segment,
            evidence_pack=pack,
            summary=None,
            guardrails=(),
            attempts=guardrails.retries + 1,
            blocked_reason=exc.message,
        )
    return SummarisedSegment(
        segment=segment,
        evidence_pack=pack,
        summary=summary,
        guardrails=checks,
        attempts=attempts,
        blocked_reason=None,
    )


def build_root_cause_summary(
    run_id: str,
    *,
    use_case: UseCaseConfig,
    storage: Storage,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None = None,
) -> RootCauseSummary:
    """`root_cause_summary.json` for a finished run: one `EvidencePack` and one model call per segment.

    Reads `scores.csv`, `row_explanations.parquet` and `feature_importance.json` once each, however
    many segments the run has, and never touches `engine.pipeline` or a stage's own writer - the
    inputs are the finished files a stage already produced, read back the way any other reader would.
    """
    started = time.perf_counter()
    run = _load_finished_run(run_id, storage)
    cfg = use_case.generative.root_cause
    primary_key = _primary_key_column(use_case)

    scores_frame = _read_scores(storage, run.artefacts[SCORES_CSV])
    keys_ordered: list[str] = [str(value) for value in scores_frame[primary_key]]
    bands: list[str] = [str(value) for value in scores_frame[BAND_COLUMN]]
    band_by_key: dict[str, str] = dict(zip(keys_ordered, bands, strict=True))
    raw_scores: list[float] = [float(value) for value in scores_frame[use_case.actions.score_field]]
    score_by_key: dict[str, float] = dict(zip(keys_ordered, raw_scores, strict=True))

    explanations = read_row_explanations(run.artefacts[ROW_EXPLANATIONS_FILENAME], storage=storage)
    explanations_by_key = {explanation.primary_key: explanation for explanation in explanations}

    importance = storage.read_model(run.artefacts[_FEATURE_IMPORTANCE_FILENAME], FeatureImportance)
    importance_rank = {item.feature: item.rank for item in importance.items}

    complaints_by_key, complaint_source = _read_complaints(run, use_case, storage)
    groups = _segment_groups(cfg, keys_ordered, band_by_key, explanations_by_key)
    total_rows = len(keys_ordered)

    segments = tuple(
        _summarise_segment(
            name,
            build_evidence_pack(
                name,
                keys=keys,
                scores_by_key=score_by_key,
                explanations_by_key=explanations_by_key,
                total_rows=total_rows,
                complaints_by_key=complaints_by_key,
                config=cfg,
                importance_rank=importance_rank,
            ),
            use_case=use_case,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )
        for name, keys in groups
    )
    log_stage(_LOGGER, "root_cause", rows=total_rows, seconds=time.perf_counter() - started)
    return RootCauseSummary(
        run_id=run_id,
        segment_by=cfg.segment_by.value,
        segments=segments,
        prompt_versions=prompt_versions([ROOT_CAUSE_PROMPT], config_root),
        prompt_hashes=prompt_hashes([ROOT_CAUSE_PROMPT], config_root),
        complaint_source=complaint_source,
        generated_at=utc_now(),
    )
