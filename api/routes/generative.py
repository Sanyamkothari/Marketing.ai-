"""The generative assistant, root-cause summaries and campaign copy - M20's API task (DEC-210).

**An index is not a run.** `engine.generative.__init__` says so in as many words, and `DEC-211`
gives an index build, a reference-set grading, a root-cause job and a campaign-copy job each their
own status document rather than writing into a finished run's `status.json`. So the RAG assistant
gets its own resource family here, `/use-cases/{id}/indexes` and `/indexes/{id}`, instead of being
squeezed through `POST /runs`; root-cause and campaign-copy jobs, which genuinely do run *over* a
finished run, reuse that run's own artefact route rather than inventing a second one - the one
change `runs.read_artefact` needed for that is made there, not here (DEC-210 again: it whitelists
the union of the predictive and the generative artefact maps).

**Nothing here is served by a fake backend without saying so.** Every response that carries
generated text also carries, directly or through `GenerativeLlmSummary`, the backend and the model
ids the call actually ran with. That is plan section 13.3 read literally: under the fake backend
`GenerativeLlmSummary.backend` reads `fake` and the model ids read `fake` too (`LlmConfig`'s own
resolution, not a rephrasing of it), so a screen rendering a fake answer cannot be mistaken for one
rendering a real one - nothing here is hidden behind a log line.

**Three engine limits are accepted rather than hidden.** `engine.generative.index.build_index` and
`engine.generative.evaluation.evaluate` are monolithic - there is no hook between "parse" and
"embed" to report through - so a job's `GenerativeStatus.stages` is coarse: one stage per engine
call this module actually makes, never a fabricated breakdown of what happens inside one.
`evaluate` also does not return the guardrail checks its own calls to `assistant.answer` produced,
so `guardrail_report.json` is never written for an index; `IndexDetailResponse.guardrails` stays
`null`, which is exactly what the contract says a check that has not run looks like. And
`model_choice`'s "try every candidate model and keep the best" is Phase 4 work with nothing behind
it yet in `engine.generative` - an explicit id overrides `generative.llm.generation_model_id`, and
`"__automl__"` keeps the use case's own configured model, which is the whole of what this build can
honour today.

**A job's usage accumulates rather than resets.** A run's copy can cost more than one job over its
life - a batch, then a regenerate, then another - as can an index: a build, and then every question
`POST /indexes/{id}/ask` is asked of it. Each is metered by its own fresh `Meter`, so `_write_usage`
folds a job's tally onto whatever `llm_usage.json` already held rather than overwriting it, because
"one more `by_purpose` entry" (the contract's own words for a regenerate) only means something if
the entries already there survive the write. Two jobs writing the same file at the same moment is
the accepted cost: the merge is read-modify-write with no lock, which a single-process deployment
running two job threads and a handful of asks does not lose a tally to in practice, and which a
Phase-4 move to object storage would have to solve at the store rather than here anyway.
"""

from __future__ import annotations

import io
import json
import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, TypeVar

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from api.deps import ConfigRootDep, JobsDep, StorageDep
from api.routes.runs import load_run, read_artefact
from api.routes.uploads import http_error, use_case_config
from api.schemas import (
    AssistantAskRequest,
    CampaignCopyRequest,
    CopyTemplateApproveRequest,
    ErrorResponse,
    GenerativeJobStartedResponse,
    GenerativeLlmSummary,
    IndexDetailResponse,
    IndexJobStartedResponse,
    IndexListResponse,
    IndexSummary,
    ReferenceSetResponse,
    RootCauseRequest,
)
from engine.aws_connection import load_connection
from engine.config import (
    GenerativeConfig,
    GenerativeKind,
    LlmConfig,
    StrictBase,
    UseCaseConfig,
    apply_overrides,
    get_catalog,
    leaf_paths,
)
from engine.contracts import LLMUsage, RunRecord, RunState
from engine.generative.assistant import answer
from engine.generative.budget import Meter
from engine.generative.contracts import (
    COPY_BATCH_FILENAME,
    COPY_MESSAGES_FILENAME,
    COPY_STATUS_FILENAME,
    DOC_INDEX_MANIFEST_FILENAME,
    GUARDRAIL_REPORT_FILENAME,
    INDEX_STATUS_FILENAME,
    LLM_USAGE_FILENAME,
    RAG_EVAL_FILENAME,
    ROOT_CAUSE_STATUS_FILENAME,
    ROOT_CAUSE_SUMMARY_FILENAME,
    AssistantAnswer,
    CopyBatch,
    CopyStatus,
    CopyTemplate,
    DocIndexManifest,
    GenerativeJobKind,
    GenerativeStage,
    GenerativeStatus,
    GuardrailCheck,
    GuardrailOutcome,
    GuardrailReport,
    GuardrailSummary,
    LlmUsageReport,
    ModelUsage,
    PurposeUsage,
    RagEval,
)
from engine.generative.errors import (
    BUDGET_EXCEEDED,
    INDEX_CORRUPT,
    INDEX_EMPTY,
    INDEX_NOT_FOUND,
    KNOWLEDGE_BASE_TOO_LARGE,
    NOT_A_GENERATIVE_USE_CASE,
    REFERENCE_SET_INVALID,
    RUN_NOT_FINISHED,
    RUN_WITHOUT_EXPLANATIONS,
    RUN_WITHOUT_SCORES,
    GenerativeError,
    generative_error,
)
from engine.generative.evaluation import SOURCE_DOC_COLUMN, evaluate
from engine.generative.guardrails import Guardrails, load_policy
from engine.generative.index import build_index, read_manifest
from engine.generative.root_cause import build_root_cause_summary
from engine.generative.vectorstore import LocalVectorStore, VectorStore
from engine.generative.win_back import approve_template, generate_campaign_copy, regenerate_template
from engine.jobs import CancelToken, JobFn
from engine.llm import build_client
from engine.settings import settings
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME
from engine.stages.export import SCORES_CSV
from engine.storage import Storage, StorageError, index_key, run_key
from engine.utils.ids import new_index_id
from engine.utils.time import utc_now

SAMPLE_DOCS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "docs"
"""The bundled Northwind Telecom corpus and its reference set, found by path and never by import.

This module used to import them from `tests.fixtures.make_docs`, and the production image ships no
`tests/` package - so `api.main`, which imports this module, raised `ModuleNotFoundError` at start-up
and the whole API, predictive routes included, could not serve a request. CI never saw it: the test
image is built *on top of* the API image with `tests/` copied back in. A path costs nothing when the
directory is absent, and a sample request on a deployment then gets `SAMPLES_UNAVAILABLE` instead of
taking the server down with it. `tests/integration/test_api_starts_without_tests.py` pins these
names against `make_docs`' own, so the two cannot drift apart.
"""
SAMPLE_SOURCE_DIR: Final[Path] = SAMPLE_DOCS_DIR / "source"
SAMPLE_REFERENCE_QA_FILENAME: Final[str] = "reference_qa.csv"

router: APIRouter = APIRouter(tags=["generative"])

M = TypeVar("M", bound=BaseModel)

_INDEX_OWNER_FILENAME: Final[str] = "index_owner.json"
"""This route's own bookkeeping, not a `GENERATIVE_ARTEFACTS` name: see `_IndexOwner`."""

_REFERENCE_SET_SOURCE_FILENAME: Final[str] = "source.csv"
_REFERENCE_SET_NAME_FILENAME: Final[str] = "original_name.txt"
"""The caller's own filename, kept beside the CSV so `IndexSummary.source_label` can name it later."""

UNMAPPED_STATUS: Final[int] = 500
"""A `GenerativeError` code this router has not been taught is a server fault, not the caller's."""

GENERATIVE_ERROR_STATUS: Final[dict[str, int]] = {
    INDEX_NOT_FOUND: 404,
    INDEX_EMPTY: 409,
    INDEX_CORRUPT: 409,
    BUDGET_EXCEEDED: 409,
    RUN_NOT_FINISHED: 409,
    RUN_WITHOUT_SCORES: 409,
    RUN_WITHOUT_EXPLANATIONS: 409,
    NOT_A_GENERATIVE_USE_CASE: 409,
    REFERENCE_SET_INVALID: 422,
    KNOWLEDGE_BASE_TOO_LARGE: 422,
}
"""`GenerativeError.code` -> HTTP status, the way `api.routes.runs.SCORE_ERROR_STATUS` maps
`ScoreError` codes: a "not found" is `404`, a well-formed request the current state refuses is
`409` (the root-cause and campaign-copy preconditions, an index with nothing to search), and a
request that was never going to be satisfiable is `422`. A code this table has not been taught is
`UNMAPPED_STATUS`, reported as a server fault rather than a guessed 4xx."""

_NOT_FOUND: dict[int | str, dict[str, object]] = {404: {"model": ErrorResponse}}
_GENERATIVE_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

_STAGE_TITLES: Final[dict[str, str]] = {
    "build": "Build the knowledge index",
    "evaluate": "Grade the answers against the reference set",
    "summarize": "Write the root-cause summary",
    "generate": "Generate the campaign copy",
}

DocumentsField = Annotated[list[UploadFile] | None, File(description="Knowledge-base files.")]
UseSampleDocumentsField = Annotated[
    bool, Form(description="Seed the build from the bundled sample corpus instead of an upload.")
]
ReferenceSetIdField = Annotated[
    str | None, Form(description="Id from POST .../reference-sets; omitted when no reference set is used.")
]
UseSampleQuestionsField = Annotated[bool, Form(description="Seed reference questions from the sample set.")]
PrimaryKeyField = Annotated[str | None, Form(description="Reference file column identifying each question.")]
ReferenceColumnField = Annotated[str | None, Form(description="Reference file column holding the answer.")]
ModelChoiceField = Annotated[str, Form(min_length=1, description="'__automl__' or an explicit model id.")]
OverridesField = Annotated[str, Form(description="JSON object of dotted generative.* overrides.")]


# ---------------------------------------------------------------------------
# Internal bookkeeping this route keeps beside the registered index artefacts
# ---------------------------------------------------------------------------
class _IndexOwner(StrictBase):
    """What neither `GenerativeStatus` nor `DocIndexManifest` carries: whose index this is.

    A manifest exists only once a build has actually finished, and `GET /use-cases/{id}/indexes` has
    to list a still-building or a failed index too, so the use-case association is kept here instead
    of being read off the manifest. `llm` is frozen at the moment the job that last touched this
    index was submitted, because `GET /indexes/{id}` answers "what did this index actually run
    with", not "what does the use case say today" - the two can differ once a deployment's config
    changes between one build and the next. Written before the job starts, never inside it: no
    reader of this file should ever have to wait for a background job to learn whose index it is.
    """

    use_case_id: str
    llm: GenerativeLlmSummary
    source_label: str


def _llm_summary(llm: LlmConfig) -> GenerativeLlmSummary:
    """The backend badge for one resolved `generative.llm` block (plan section 13.3)."""
    return GenerativeLlmSummary(
        backend=llm.backend,
        generation_model_id=llm.generation_model,
        judge_model_id=llm.judge_model,
        embedding_model_id=llm.embedding_model,
        region=llm.region,
    )


def _write_owner(storage: Storage, index_id: str, owner: _IndexOwner) -> None:
    storage.write_model(index_key(index_id, _INDEX_OWNER_FILENAME), owner)


def _read_owner(storage: Storage, index_id: str) -> _IndexOwner:
    try:
        return storage.read_model(index_key(index_id, _INDEX_OWNER_FILENAME), _IndexOwner)
    except StorageError as exc:
        raise generative_http(generative_error(INDEX_NOT_FOUND, index_id=index_id)) from exc


def _optional_model(storage: Storage, key: str, model: type[M]) -> M | None:
    """`model` read from `key`, or `None` when the job that would write it has not, yet or ever."""
    try:
        return storage.read_model(key, model)
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# GenerativeError -> HTTP, the way `api.routes.runs.score_http` maps `ScoreError`
# ---------------------------------------------------------------------------
def generative_http(exc: GenerativeError) -> HTTPException:
    """A `GenerativeError` in M1's envelope."""
    return http_error(GENERATIVE_ERROR_STATUS.get(exc.code, UNMAPPED_STATUS), exc.code, exc.message)


def _require_generative_kind(use_case: UseCaseConfig, kind: GenerativeKind) -> None:
    if use_case.generative.kind is not kind:
        raise generative_http(
            generative_error(
                NOT_A_GENERATIVE_USE_CASE, use_case_id=use_case.id, kind=use_case.generative.kind.value
            )
        )


def _require_finished_run(run: RunRecord, *, needs_explanations: bool) -> None:
    """The same precondition `build_root_cause_summary`/`generate_campaign_copy` check, run first
    on the request thread so a request that will never be satisfiable never reaches the job queue -
    `POST /runs` validates synchronously for the same reason (see `api.routes.runs`'s own docstring).
    """
    if run.state is not RunState.DONE:
        raise generative_http(generative_error(RUN_NOT_FINISHED, run_id=run.run_id, state=run.state.value))
    if SCORES_CSV not in run.artefacts:
        raise generative_http(generative_error(RUN_WITHOUT_SCORES, run_id=run.run_id))
    if needs_explanations and ROW_EXPLANATIONS_FILENAME not in run.artefacts:
        raise generative_http(generative_error(RUN_WITHOUT_EXPLANATIONS, run_id=run.run_id))


# ---------------------------------------------------------------------------
# The dotted-override mechanism, narrowed to `generative.*` (DEC-209 leaves it out of the
# catalog-wide table `RunRequest.overrides` is checked against)
# ---------------------------------------------------------------------------
def _merged_generative(config: UseCaseConfig, overrides: Mapping[str, Any]) -> GenerativeConfig:
    """`config.generative` with dotted `generative.*` `overrides` merged in and revalidated.

    The index build and evaluate routes' own convention: a key already spells the full path from the
    document root, `"generative.rag.top_k"`, exactly as `RunRequest.overrides` would for any other
    block. Reuses `engine.config.apply_overrides`'s merge and its `ConfigError` - `api.main`'s
    handler already renders that as this route's `422` - rather than re-deriving either.
    """
    if not overrides:
        return config.generative
    wrapped: dict[str, Any] = {"generative": config.generative.model_dump(mode="json")}
    merged = apply_overrides(wrapped, overrides, allowed=frozenset(leaf_paths(wrapped)))
    return GenerativeConfig.model_validate(merged["generative"])


def _merged_generative_sub(
    generative: GenerativeConfig, overrides: Mapping[str, Any], *, block: str
) -> GenerativeConfig:
    """`generative` with dotted `overrides`, relative to `generative.<block>`, merged in and
    revalidated - the root-cause and campaign-copy routes' own convention: a bare leaf such as
    `max_segments` rather than a path spelled from the document root."""
    if not overrides:
        return generative
    prefix = f"generative.{block}"
    wrapped: dict[str, Any] = {"generative": generative.model_dump(mode="json")}
    allowed = frozenset(f"{prefix}.{leaf}" for leaf in leaf_paths(wrapped["generative"][block]))
    prefixed = {f"{prefix}.{key}": value for key, value in overrides.items()}
    merged = apply_overrides(wrapped, prefixed, allowed=allowed)
    return GenerativeConfig.model_validate(merged["generative"])


def _with_reference_column(generative: GenerativeConfig, reference_column: str | None) -> GenerativeConfig:
    """`generative.reference_set.reference_column` set to the column the caller picked, if they did.

    The Setup screen offers two selects and only one of them names a column this engine reads.
    `primary_key` is the predictive screen's own row-identifier select reused on the generative one -
    `marketing-ai-prototype.html` copies the pair wholesale - and `docs/generative-ui-endpoints.md`
    keeps that meaning: "column of the reference file identifying each question", with `question_id`
    as its worked example. `ReferenceSetConfig` names a question column, a reference-answer column
    and a refusal column and no key at all, and nothing under `engine.generative.evaluation` reads a
    row id, so there is nothing here for `primary_key` to set. It is checked against the file's real
    columns by `_require_gradeable_reference_set` and then dropped, rather than folded into
    `question_column`: a build that folded it would send the judge `q001` where the question a
    customer asked belongs, score an answer against it, and report the result as a faithfulness
    number with nothing on the screen to say the grading was nonsense.
    """
    if reference_column is None:
        return generative
    return generative.model_copy(
        update={
            "reference_set": generative.reference_set.model_copy(
                update={"reference_column": reference_column}
            )
        }
    )


def _with_model_choice(
    generative: GenerativeConfig, model_choice: str, *, automl_value: str
) -> GenerativeConfig:
    """`generative.llm.generation_model_id` set to `model_choice`, unless it is the AutoML sentinel.

    `engine.generative` has no candidate-comparison flow to hand the sentinel to (see this module's
    own docstring), so `"__automl__"` is read as "keep the use case's own configured model" - the
    only one of the two meanings this build can actually honour today.
    """
    if model_choice == automl_value:
        return generative
    return generative.model_copy(
        update={"llm": generative.llm.model_copy(update={"generation_model_id": model_choice})}
    )


# ---------------------------------------------------------------------------
# Job status documents (DEC-211): one per job, coarse stages, never fabricated ones
# ---------------------------------------------------------------------------
def _stage(key: str, state: RunState, *, seconds: float = 0.0) -> GenerativeStage:
    return GenerativeStage(
        key=key,
        title=_STAGE_TITLES[key],
        state=state,
        detail="" if state in (RunState.PENDING, RunState.RUNNING) else "Finished.",
        seconds=seconds,
    )


def _progress(stages: Sequence[GenerativeStage]) -> int:
    if not stages:
        return 0
    finished = sum(1 for stage in stages if stage.state in (RunState.DONE, RunState.SKIPPED))
    return round(100 * finished / len(stages))


def _write_status(
    storage: Storage,
    key: str,
    *,
    job_id: str,
    kind: GenerativeJobKind,
    state: RunState,
    stages: Sequence[GenerativeStage],
    started_at: datetime,
    error: GenerativeError | None = None,
    now: datetime | None = None,
) -> GenerativeStatus:
    """Write one `GenerativeStatus`. A caller that writes `state=DONE` (or `FAILED`) does so only
    after every artefact that state promises is already on disk - `llm_usage.json`,
    `guardrail_report.json`, the summary or the batch - never before: a poller reading `done` as
    "this job's other files are all safe to read now" must find that true every time, not on
    average, and every job function in this module orders its writes so that it is."""
    status = GenerativeStatus(
        job_id=job_id,
        kind=kind,
        state=state,
        progress_pct=_progress(stages),
        stages=tuple(stages),
        started_at=started_at,
        updated_at=now if now is not None else utc_now(),
        error_code=None if error is None else error.code,
        error_message=None if error is None else error.message,
    )
    storage.write_model(key, status)
    return status


def _moved(
    stages: Sequence[GenerativeStage], key: str, state: RunState, *, seconds: float = 0.0
) -> tuple[GenerativeStage, ...]:
    return tuple(_stage(s.key, state, seconds=seconds) if s.key == key else s for s in stages)


def _failed(stages: Sequence[GenerativeStage], error: GenerativeError) -> tuple[GenerativeStage, ...]:
    """`stages` with whichever step was in flight marked `failed`, carrying why as its detail.

    A document that says `state: failed` while one of its steps still says `running` describes a
    spinner nobody will ever stop. `GenerativeStatus` is a registered artefact any client may read
    back through the run's own artefact route (DEC-210), not only the screen that happens to key its
    rendering off the overall state, so the step that was running when the error arrived is the step
    that gets written down as the one that failed.
    """
    return tuple(
        (
            stage.model_copy(update={"state": RunState.FAILED, "detail": error.message})
            if stage.state is RunState.RUNNING
            else stage
        )
        for stage in stages
    )


def _sum_optional(left: float | None, right: float | None) -> float | None:
    """`left + right`, or `None` the moment either side could not be priced (`Meter`'s own rule)."""
    return None if left is None or right is None else round(left + right, 6)


def _merged_model_usage(
    previous: Sequence[ModelUsage], fresh: Sequence[ModelUsage]
) -> tuple[ModelUsage, ...]:
    by_model: dict[str, ModelUsage] = {row.model_id: row for row in previous}
    for row in fresh:
        seen = by_model.get(row.model_id)
        by_model[row.model_id] = (
            row
            if seen is None
            else ModelUsage(
                model_id=row.model_id,
                calls=seen.calls + row.calls,
                input_tokens=seen.input_tokens + row.input_tokens,
                output_tokens=seen.output_tokens + row.output_tokens,
                cost_estimate_usd=_sum_optional(seen.cost_estimate_usd, row.cost_estimate_usd),
            )
        )
    return tuple(by_model[model_id] for model_id in sorted(by_model))


def _merged_purpose_usage(
    previous: Sequence[PurposeUsage], fresh: Sequence[PurposeUsage]
) -> tuple[PurposeUsage, ...]:
    by_purpose: dict[Any, PurposeUsage] = {row.purpose: row for row in previous}
    for row in fresh:
        seen = by_purpose.get(row.purpose)
        by_purpose[row.purpose] = (
            row
            if seen is None
            else PurposeUsage(
                purpose=row.purpose,
                calls=seen.calls + row.calls,
                input_tokens=seen.input_tokens + row.input_tokens,
                output_tokens=seen.output_tokens + row.output_tokens,
                cost_estimate_usd=_sum_optional(seen.cost_estimate_usd, row.cost_estimate_usd),
            )
        )
    return tuple(by_purpose[purpose] for purpose in sorted(by_purpose, key=lambda p: p.value))


def _write_usage(storage: Storage, key: str, meter: Meter) -> None:
    """`llm_usage.json`, folded onto whatever usage already sat at `key` (`Meter`'s own docs).

    A run's copy can cost more than one job - a batch, then a regenerate, then another - and each
    one is metered by its own fresh `Meter`, whose totals start at zero. Overwriting `llm_usage.json`
    with only the latest job's tally would make it forget every dollar an earlier job on the same
    run already spent; `docs/generative-ui-endpoints.md` says a regenerate costs "one more by_purpose
    entry", which only means something if the entries already there are kept. Written only when this
    job actually made a call (plan section 13.4): a job that made none has nothing to add.
    """
    if not meter.calls:
        return
    fresh = meter.usage()
    previous = _optional_model(storage, key, LlmUsageReport)
    if previous is None:
        storage.write_model(key, fresh)
        return
    combined = LlmUsageReport(
        job_id=fresh.job_id,
        totals=LLMUsage(
            calls=previous.totals.calls + fresh.totals.calls,
            input_tokens=previous.totals.input_tokens + fresh.totals.input_tokens,
            output_tokens=previous.totals.output_tokens + fresh.totals.output_tokens,
            cost_estimate_usd=_sum_optional(
                previous.totals.cost_estimate_usd, fresh.totals.cost_estimate_usd
            ),
            model_ids=tuple(sorted(set(previous.totals.model_ids) | set(fresh.totals.model_ids))),
        ),
        cache_hits=previous.cache_hits + fresh.cache_hits,
        budget_usd=fresh.budget_usd,
        budget_calls=fresh.budget_calls,
        by_model=_merged_model_usage(previous.by_model, fresh.by_model),
        by_purpose=_merged_purpose_usage(previous.by_purpose, fresh.by_purpose),
        warnings=tuple(dict.fromkeys((*previous.warnings, *fresh.warnings))),
        created_at=fresh.created_at,
    )
    storage.write_model(key, combined)


def _guardrail_summary(checks: Sequence[GuardrailCheck]) -> GuardrailSummary:
    return GuardrailSummary(
        checked=len(checks),
        passed=sum(1 for check in checks if check.outcome is GuardrailOutcome.PASSED),
        warned=sum(1 for check in checks if check.outcome is GuardrailOutcome.WARNED),
        blocked=sum(1 for check in checks if check.outcome is GuardrailOutcome.BLOCKED),
    )


def _write_guardrail_report(
    storage: Storage, key: str, *, job_id: str, kind: GenerativeJobKind, checks: Sequence[GuardrailCheck]
) -> None:
    storage.write_model(
        key,
        GuardrailReport(
            job_id=job_id,
            kind=kind,
            checks=tuple(checks),
            summary=_guardrail_summary(checks),
            created_at=utc_now(),
        ),
    )


def _new_job_id(prefix: str) -> str:
    """A fresh id for `JobRunner.submit`'s own bookkeeping - never a domain id, which can repeat
    across calls (an `evaluate` reuses its `index_id`; root-cause and copy can be re-run on one
    run_id), and `ThreadJobRunner` refuses a `job_id` it has already seen."""
    return f"{prefix}_{utc_now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"


def _client_meter_guardrails(
    use_case: UseCaseConfig, *, job_id: str, config_root: Path | None
) -> tuple[Meter, Guardrails]:
    generative = use_case.generative
    # The identity is read per request rather than at import: a person may switch profile on the
    # connection screen between two jobs, and the next job should run as whoever they chose.
    client = build_client(generative.llm, profile=load_connection(settings()).profile)
    meter = Meter(client, job_id=job_id, llm=generative.llm, budget=generative.budget)
    guardrails = Guardrails(load_policy(config_root), meter=meter, prompts_root=config_root)
    return meter, guardrails


# ---------------------------------------------------------------------------
# 1. POST /use-cases/{use_case_id}/reference-sets
# ---------------------------------------------------------------------------
def _reference_set_key(reference_set_id: str, filename: str) -> str:
    return f"reference_sets/{reference_set_id}/{filename}"


@router.post(
    "/use-cases/{use_case_id}/reference-sets",
    response_model=ReferenceSetResponse,
    responses=_NOT_FOUND,
    summary="Profile an uploaded reference-question file",
)
async def create_reference_set(
    use_case_id: str,
    root: ConfigRootDep,
    storage: StorageDep,
    file: Annotated[UploadFile, File(description="CSV.")],
) -> ReferenceSetResponse:
    """Profiles a reference Q&A file the way `POST /uploads` profiles a dataset (mock 09)."""
    use_case_config(use_case_id, root)  # 404 for a planned or unknown id, before anything is read
    reference_set_id = f"refset_{secrets.token_hex(6)}"
    data = await file.read()
    frame = _read_reference_frame(data)
    storage.write_bytes(_reference_set_key(reference_set_id, _REFERENCE_SET_SOURCE_FILENAME), data)
    storage.write_text(
        _reference_set_key(reference_set_id, _REFERENCE_SET_NAME_FILENAME), file.filename or reference_set_id
    )
    return ReferenceSetResponse(
        reference_set_id=reference_set_id, columns=tuple(frame.columns), row_count=len(frame)
    )


def _read_reference_frame(data: bytes) -> pd.DataFrame:
    try:
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise http_error(422, "REFERENCE_SET_UNREADABLE", f"Could not read this file as CSV: {exc}.") from exc


# ---------------------------------------------------------------------------
# 2 & 5. Index build and evaluate: shared document/reference-set resolution
# ---------------------------------------------------------------------------
def _persisted_documents(
    storage: Storage, index_id: str, uploads: Sequence[tuple[str, bytes]]
) -> tuple[Path, ...]:
    """Each uploaded document's bytes, written under this index's own directory, as real paths.

    `build_index` reads `Path.read_bytes()` directly - it parses PDFs and DOCX files, which no
    `Storage` method hands back as anything but bytes - so `storage.local_path` is used exactly as
    its own docstring says a library that "insists on a real directory" should use it (DEC-016's one
    escape hatch), rather than this route re-inventing a second way to reach the filesystem. Each
    document gets its own numbered subdirectory rather than a numbered prefix on its own name,
    because `path.name` becomes `IndexedDocument.name` - the filename a citation shows - and a
    reader comparing what they uploaded against what a citation quotes must see their own name back.
    """
    paths: list[Path] = []
    for position, (filename, data) in enumerate(uploads):
        safe_name = Path(filename).name or f"document_{position}"
        key = index_key(index_id, "source", f"{position:03d}", safe_name)
        storage.write_bytes(key, data)
        paths.append(storage.local_path(key))
    return tuple(paths)


def _sample_document_paths() -> tuple[Path, ...]:
    """The bundled sample corpus (`tests/fixtures/make_docs.py`'s committed Markdown source).

    Read directly off `SOURCE_DIR` rather than materialised through `build_knowledge_base`: the
    Markdown files are already real, committed, on-disk documents `build_index` can parse as-is, and
    a rebuild into every format `build_knowledge_base` also writes would only cost time to prove a
    parser this route does not need proved again (`tests/unit/generative/test_index.py` already does).
    """
    paths = tuple(sorted(SAMPLE_SOURCE_DIR.glob("*.md")))
    if not paths:
        raise _samples_unavailable()
    return paths


def _sample_reference_set_path() -> Path:
    path = SAMPLE_DOCS_DIR / SAMPLE_REFERENCE_QA_FILENAME
    if not path.is_file():
        raise _samples_unavailable()
    return path


def _samples_unavailable() -> HTTPException:
    """What a deployment says when asked for the sample corpus it does not ship."""
    return http_error(
        409,
        "SAMPLES_UNAVAILABLE",
        "The sample documents and questions are not part of this deployment. Upload your own.",
    )


def _resolve_reference_set_path(
    storage: Storage, *, reference_set_id: str | None, use_sample_questions: bool
) -> Path | None:
    if use_sample_questions:
        return _sample_reference_set_path()
    if reference_set_id is None:
        return None
    key = _reference_set_key(reference_set_id, _REFERENCE_SET_SOURCE_FILENAME)
    if not storage.exists(key):
        raise http_error(404, "REFERENCE_SET_NOT_FOUND", f"No reference set with id {reference_set_id!r}.")
    return storage.local_path(key)


def _require_gradeable_reference_set(
    path: Path, generative: GenerativeConfig, *, primary_key: str | None
) -> None:
    """Refuse, on the request thread, a reference set the grader would refuse minutes later.

    `engine.generative.evaluation` reads four columns - the three `ReferenceSetConfig` names plus
    its own fixed `source_doc` - and raises `REFERENCE_SET_INVALID` for the first one missing.
    Raised inside the job that is a `202` followed by a failed status document the caller has to go
    and poll for; raised here it is the `422` `GENERATIVE_ERROR_STATUS` already promises that code,
    naming the column to add. It is the same reason `_require_finished_run` runs the root-cause
    preconditions in front of the queue rather than inside it: a request that was never going to be
    satisfiable should not cost a job slot to find out. `primary_key` is checked here too - it sets
    nothing (see `_with_reference_column`), but a caller who names a column that is not in the file
    has misread their own upload, and saying so is better than accepting it silently. Only the
    header row is parsed, so a long reference set costs no more to check than a short one.
    """
    reference_set = generative.reference_set
    required = [
        reference_set.question_column,
        reference_set.refusal_column,
        reference_set.reference_column,
        SOURCE_DOC_COLUMN,
    ]
    if primary_key is not None:
        required.append(primary_key)
    try:
        header = tuple(pd.read_csv(path, nrows=0).columns)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise generative_http(generative_error(REFERENCE_SET_INVALID, column=required[0])) from exc
    missing = [column for column in required if column not in header]
    if missing:
        raise generative_http(generative_error(REFERENCE_SET_INVALID, column=missing[0]))


def _source_label(paths: Sequence[Path]) -> str:
    return f"{len(paths)} document{'s' if len(paths) != 1 else ''}"


# ---------------------------------------------------------------------------
# 2. POST /use-cases/{use_case_id}/indexes
# ---------------------------------------------------------------------------
@router.post(
    "/use-cases/{use_case_id}/indexes",
    response_model=IndexJobStartedResponse,
    status_code=202,
    responses=_GENERATIVE_ERRORS,
    summary="Start a knowledge-index build, and grade it when a reference set is given",
)
async def create_index(
    use_case_id: str,
    root: ConfigRootDep,
    storage: StorageDep,
    jobs: JobsDep,
    model_choice: ModelChoiceField,
    documents: DocumentsField = None,
    use_sample_documents: UseSampleDocumentsField = False,
    reference_set_id: ReferenceSetIdField = None,
    use_sample_questions: UseSampleQuestionsField = False,
    primary_key: PrimaryKeyField = None,
    reference_column: ReferenceColumnField = None,
    overrides: OverridesField = "{}",
) -> IndexJobStartedResponse:
    """Reads, chunks, redacts and embeds the documents; grades them too when a reference set is given."""
    config = use_case_config(use_case_id, root)
    _require_generative_kind(config, GenerativeKind.RAG_ASSISTANT)
    parsed_overrides = _parse_overrides(overrides)
    catalog = get_catalog(root)
    generative = _merged_generative(config, parsed_overrides)
    generative = _with_model_choice(generative, model_choice, automl_value=catalog.automl_choice.value)
    generative = _with_reference_column(generative, reference_column)
    config = config.model_copy(update={"generative": generative})

    if not documents and not use_sample_documents:
        raise http_error(
            422, "INDEX_DOCUMENTS_REQUIRED", "Upload at least one document, or use the sample set."
        )

    # Everything the request can be refused for is checked before one byte of it is written down:
    # a build refused after its documents are on disk leaves an index directory nothing will ever
    # finish or clean up.
    reference_path = _resolve_reference_set_path(
        storage, reference_set_id=reference_set_id, use_sample_questions=use_sample_questions
    )
    if reference_path is not None:
        _require_gradeable_reference_set(reference_path, generative, primary_key=primary_key)

    index_id = new_index_id()
    if use_sample_documents:
        paths = _sample_document_paths()
    else:
        uploads = [(file.filename or "document", await file.read()) for file in documents or ()]
        paths = _persisted_documents(storage, index_id, uploads)

    started = utc_now()
    stage_keys = ("build",) + (("evaluate",) if reference_path is not None else ())
    initial = tuple(_stage(key, RunState.PENDING) for key in stage_keys)
    _write_owner(
        storage,
        index_id,
        _IndexOwner(
            use_case_id=config.id, llm=_llm_summary(generative.llm), source_label=_source_label(paths)
        ),
    )
    _write_status(
        storage,
        index_key(index_id, INDEX_STATUS_FILENAME),
        job_id=index_id,
        kind=GenerativeJobKind.INDEX_BUILD,
        state=RunState.PENDING,
        stages=initial,
        started_at=started,
    )

    jobs.submit(
        _new_job_id("index_build"),
        _index_build_job(
            storage,
            use_case=config,
            index_id=index_id,
            paths=paths,
            reference_path=reference_path,
            stage_keys=stage_keys,
            started_at=started,
            config_root=root,
        ),
    )
    return IndexJobStartedResponse(index_id=index_id)


def _parse_overrides(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise http_error(422, "OVERRIDES_NOT_JSON", f"overrides was not valid JSON: {exc}.") from exc
    if not isinstance(parsed, dict):
        raise http_error(422, "OVERRIDES_NOT_AN_OBJECT", "overrides must be a JSON object.")
    return parsed


def _index_build_job(
    storage: Storage,
    *,
    use_case: UseCaseConfig,
    index_id: str,
    paths: Sequence[Path],
    reference_path: Path | None,
    stage_keys: Sequence[str],
    started_at: datetime,
    config_root: Path | None,
) -> JobFn:
    status_key = index_key(index_id, INDEX_STATUS_FILENAME)
    store: VectorStore = LocalVectorStore(storage)
    meter, guardrails = _client_meter_guardrails(use_case, job_id=index_id, config_root=config_root)

    def job(_cancel: CancelToken) -> None:
        stages = tuple(_stage(key, RunState.PENDING) for key in stage_keys)
        stages = _moved(stages, "build", RunState.RUNNING)
        _write_status(
            storage,
            status_key,
            job_id=index_id,
            kind=GenerativeJobKind.INDEX_BUILD,
            state=RunState.RUNNING,
            stages=stages,
            started_at=started_at,
        )
        try:
            began = time.monotonic()
            build_index(
                list(paths),
                index_id=index_id,
                use_case=use_case,
                storage=storage,
                store=store,
                meter=meter,
                config_root=config_root,
            )
            stages = _moved(stages, "build", RunState.DONE, seconds=time.monotonic() - began)
            if reference_path is not None:
                stages = _moved(stages, "evaluate", RunState.RUNNING)
                _write_status(
                    storage,
                    status_key,
                    job_id=index_id,
                    kind=GenerativeJobKind.INDEX_BUILD,
                    state=RunState.RUNNING,
                    stages=stages,
                    started_at=started_at,
                )
                began = time.monotonic()
                evaluate(
                    index_id=index_id,
                    use_case=use_case,
                    reference_set_path=reference_path,
                    storage=storage,
                    store=store,
                    meter=meter,
                    guardrails=guardrails,
                    config_root=config_root,
                )
                stages = _moved(stages, "evaluate", RunState.DONE, seconds=time.monotonic() - began)
        except GenerativeError as exc:
            _write_status(
                storage,
                status_key,
                job_id=index_id,
                kind=GenerativeJobKind.INDEX_BUILD,
                state=RunState.FAILED,
                stages=_failed(stages, exc),
                started_at=started_at,
                error=exc,
            )
            _write_usage(storage, index_key(index_id, LLM_USAGE_FILENAME), meter)
            return
        _write_usage(storage, index_key(index_id, LLM_USAGE_FILENAME), meter)
        _write_status(
            storage,
            status_key,
            job_id=index_id,
            kind=GenerativeJobKind.INDEX_BUILD,
            state=RunState.DONE,
            stages=stages,
            started_at=started_at,
        )

    return job


# ---------------------------------------------------------------------------
# 3. GET /use-cases/{use_case_id}/indexes
# ---------------------------------------------------------------------------
@router.get(
    "/use-cases/{use_case_id}/indexes",
    response_model=IndexListResponse,
    responses=_NOT_FOUND,
    summary="Every index this use case has built or graded, newest first",
)
def list_indexes(use_case_id: str, root: ConfigRootDep, storage: StorageDep) -> IndexListResponse:
    use_case_config(use_case_id, root)  # 404 for a planned or unknown id
    owned = [
        (key.split("/")[1], owner)
        for key in storage.list_keys("indexes/")
        if key.endswith(f"/{_INDEX_OWNER_FILENAME}")
        for owner in (storage.read_model(key, _IndexOwner),)
        if owner.use_case_id == use_case_id
    ]
    rows = [_index_summary(storage, index_id, owner) for index_id, owner in owned]
    champion_id = _champion_index_id(storage, [row.index_id for row in rows])
    rows = [row.model_copy(update={"champion": row.index_id == champion_id}) for row in rows]
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return IndexListResponse(indexes=tuple(rows))


def _index_summary(storage: Storage, index_id: str, owner: _IndexOwner) -> IndexSummary:
    status = storage.read_model(index_key(index_id, INDEX_STATUS_FILENAME), GenerativeStatus)
    rag_eval = _optional_model(storage, index_key(index_id, RAG_EVAL_FILENAME), RagEval)
    kind: Literal["build", "evaluate"] = (
        "build" if status.kind is GenerativeJobKind.INDEX_BUILD else "evaluate"
    )
    return IndexSummary(
        index_id=index_id,
        kind=kind,
        champion=False,
        state=status.state,
        llm=owner.llm,
        faithfulness=None if rag_eval is None else rag_eval.aggregates.mean_faithfulness,
        source_label=owner.source_label,
        created_at=status.updated_at,
    )


def _champion_index_id(storage: Storage, index_ids: Sequence[str]) -> str | None:
    """The use case's current best index: highest mean faithfulness, else the newest build.

    Mirrors `ModelVersionResponse.is_champion`'s own rule for the predictive registry - server
    decided, so a screen never ranks indexes itself.
    """
    graded: list[tuple[float, str]] = []
    newest: tuple[datetime, str] | None = None
    for index_id in index_ids:
        status = storage.read_model(index_key(index_id, INDEX_STATUS_FILENAME), GenerativeStatus)
        if newest is None or status.updated_at > newest[0]:
            newest = (status.updated_at, index_id)
        rag_eval = _optional_model(storage, index_key(index_id, RAG_EVAL_FILENAME), RagEval)
        if rag_eval is not None and rag_eval.aggregates.mean_faithfulness is not None:
            graded.append((rag_eval.aggregates.mean_faithfulness, index_id))
    if graded:
        return max(graded)[1]
    return None if newest is None else newest[1]


# ---------------------------------------------------------------------------
# 4. GET /indexes/{index_id}
# ---------------------------------------------------------------------------
@router.get(
    "/indexes/{index_id}",
    response_model=IndexDetailResponse,
    responses=_NOT_FOUND,
    summary="One index in full: its status, its manifest and, once graded, its evaluation",
)
def read_index(index_id: str, storage: StorageDep) -> IndexDetailResponse:
    try:
        status = storage.read_model(index_key(index_id, INDEX_STATUS_FILENAME), GenerativeStatus)
    except StorageError as exc:
        raise generative_http(generative_error(INDEX_NOT_FOUND, index_id=index_id)) from exc
    owner = _read_owner(storage, index_id)
    return IndexDetailResponse(
        index_id=index_id,
        llm=owner.llm,
        status=status,
        manifest=_optional_model(storage, index_key(index_id, DOC_INDEX_MANIFEST_FILENAME), DocIndexManifest),
        rag_eval=_optional_model(storage, index_key(index_id, RAG_EVAL_FILENAME), RagEval),
        llm_usage=_optional_model(storage, index_key(index_id, LLM_USAGE_FILENAME), LlmUsageReport),
        guardrails=_optional_model(storage, index_key(index_id, GUARDRAIL_REPORT_FILENAME), GuardrailReport),
    )


# ---------------------------------------------------------------------------
# 5. POST /indexes/{index_id}/evaluate
# ---------------------------------------------------------------------------
@router.post(
    "/indexes/{index_id}/evaluate",
    response_model=IndexJobStartedResponse,
    status_code=202,
    responses=_GENERATIVE_ERRORS,
    summary="Re-grade an already-built index against a reference set, without rebuilding it",
)
async def create_evaluation(
    index_id: str,
    root: ConfigRootDep,
    storage: StorageDep,
    jobs: JobsDep,
    reference_set_id: ReferenceSetIdField = None,
    use_sample_questions: UseSampleQuestionsField = False,
    primary_key: PrimaryKeyField = None,
    reference_column: ReferenceColumnField = None,
) -> IndexJobStartedResponse:
    try:
        read_manifest(storage, index_id)
    except GenerativeError as exc:
        raise generative_http(exc) from exc
    owner = _read_owner(storage, index_id)
    config = use_case_config(owner.use_case_id, root)
    _require_generative_kind(config, GenerativeKind.RAG_ASSISTANT)
    generative = _with_reference_column(config.generative, reference_column)
    config = config.model_copy(update={"generative": generative})

    reference_path = _resolve_reference_set_path(
        storage, reference_set_id=reference_set_id, use_sample_questions=use_sample_questions
    )
    if reference_path is None:
        raise http_error(422, "REFERENCE_SET_REQUIRED", "Name a reference_set_id or use_sample_questions.")
    _require_gradeable_reference_set(reference_path, generative, primary_key=primary_key)

    started = utc_now()
    label = (
        SAMPLE_REFERENCE_QA_FILENAME
        if use_sample_questions
        else _reference_set_filename(storage, reference_set_id)
    )
    _write_owner(
        storage,
        index_id,
        owner.model_copy(update={"llm": _llm_summary(generative.llm), "source_label": label}),
    )
    stages = (_stage("evaluate", RunState.PENDING),)
    _write_status(
        storage,
        index_key(index_id, INDEX_STATUS_FILENAME),
        job_id=index_id,
        kind=GenerativeJobKind.REFERENCE_EVAL,
        state=RunState.PENDING,
        stages=stages,
        started_at=started,
    )
    jobs.submit(
        _new_job_id("index_eval"),
        _evaluate_job(
            storage,
            use_case=config,
            index_id=index_id,
            reference_path=reference_path,
            started_at=started,
            config_root=root,
        ),
    )
    return IndexJobStartedResponse(index_id=index_id)


def _reference_set_filename(storage: Storage, reference_set_id: str | None) -> str:
    """The caller's own filename for `reference_set_id`, or a plain label when there is none to read."""
    if reference_set_id is None:
        return "reference set"
    try:
        return storage.read_text(_reference_set_key(reference_set_id, _REFERENCE_SET_NAME_FILENAME))
    except StorageError:
        return reference_set_id


def _evaluate_job(
    storage: Storage,
    *,
    use_case: UseCaseConfig,
    index_id: str,
    reference_path: Path,
    started_at: datetime,
    config_root: Path | None,
) -> JobFn:
    status_key = index_key(index_id, INDEX_STATUS_FILENAME)
    store: VectorStore = LocalVectorStore(storage)
    meter, guardrails = _client_meter_guardrails(use_case, job_id=index_id, config_root=config_root)

    def job(_cancel: CancelToken) -> None:
        stages = (_stage("evaluate", RunState.RUNNING),)
        _write_status(
            storage,
            status_key,
            job_id=index_id,
            kind=GenerativeJobKind.REFERENCE_EVAL,
            state=RunState.RUNNING,
            stages=stages,
            started_at=started_at,
        )
        try:
            began = time.monotonic()
            evaluate(
                index_id=index_id,
                use_case=use_case,
                reference_set_path=reference_path,
                storage=storage,
                store=store,
                meter=meter,
                guardrails=guardrails,
                config_root=config_root,
            )
            stages = (_stage("evaluate", RunState.DONE, seconds=time.monotonic() - began),)
        except GenerativeError as exc:
            _write_status(
                storage,
                status_key,
                job_id=index_id,
                kind=GenerativeJobKind.REFERENCE_EVAL,
                state=RunState.FAILED,
                stages=_failed(stages, exc),
                started_at=started_at,
                error=exc,
            )
            _write_usage(storage, index_key(index_id, LLM_USAGE_FILENAME), meter)
            return
        _write_usage(storage, index_key(index_id, LLM_USAGE_FILENAME), meter)
        _write_status(
            storage,
            status_key,
            job_id=index_id,
            kind=GenerativeJobKind.REFERENCE_EVAL,
            state=RunState.DONE,
            stages=stages,
            started_at=started_at,
        )

    return job


# ---------------------------------------------------------------------------
# 6. POST /indexes/{index_id}/ask
# ---------------------------------------------------------------------------
@router.post(
    "/indexes/{index_id}/ask",
    response_model=AssistantAnswer,
    responses=_NOT_FOUND,
    summary="Answer one question from an index, grounded in its documents or refused",
)
def ask_index(
    index_id: str, body: AssistantAskRequest, root: ConfigRootDep, storage: StorageDep
) -> AssistantAnswer:
    try:
        read_manifest(storage, index_id)
    except GenerativeError as exc:
        raise generative_http(exc) from exc
    owner = _read_owner(storage, index_id)
    config = use_case_config(owner.use_case_id, root)
    store: VectorStore = LocalVectorStore(storage)
    meter, guardrails = _client_meter_guardrails(config, job_id=f"ask_{index_id}", config_root=root)
    try:
        return answer(
            body.question,
            index_id=index_id,
            use_case=config,
            store=store,
            meter=meter,
            guardrails=guardrails,
            config_root=root,
        )
    except GenerativeError as exc:
        raise generative_http(exc) from exc
    finally:
        # An ask is short, but it is not free, and the third rule does not stop at the request
        # thread: this call embedded the question and usually generated an answer, and both are
        # metered calls that `llm_usage.json` has to carry or the index's Cost card is a record of
        # the build alone while the bill keeps growing. `_write_usage` folds them onto what the
        # build left, and returns without writing when a refusal cost nothing to make. It runs in
        # `finally` because a budget the answer walked into is money already spent - an exception is
        # the one case where forgetting it would flatter the number most.
        _write_usage(storage, index_key(index_id, LLM_USAGE_FILENAME), meter)


# ---------------------------------------------------------------------------
# 7. POST /runs/{run_id}/root-cause
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/root-cause",
    response_model=GenerativeJobStartedResponse,
    status_code=202,
    responses=_GENERATIVE_ERRORS,
    summary="Start a root-cause summary over a finished scoring run",
)
def create_root_cause(
    run_id: str, body: RootCauseRequest, root: ConfigRootDep, storage: StorageDep, jobs: JobsDep
) -> GenerativeJobStartedResponse:
    run = load_run(storage, run_id)
    config = use_case_config(run.use_case_id, root)
    _require_generative_kind(config, GenerativeKind.ROOT_CAUSE_SUMMARY)
    _require_finished_run(run, needs_explanations=True)
    generative = _merged_generative_sub(config.generative, body.overrides, block="root_cause")
    config = config.model_copy(update={"generative": generative})

    started = utc_now()
    stages = (_stage("summarize", RunState.PENDING),)
    status_key = run_key(run_id, ROOT_CAUSE_STATUS_FILENAME)
    _write_status(
        storage,
        status_key,
        job_id=run_id,
        kind=GenerativeJobKind.ROOT_CAUSE,
        state=RunState.PENDING,
        stages=stages,
        started_at=started,
    )
    job_id = _new_job_id("root_cause")
    jobs.submit(
        job_id, _root_cause_job(storage, use_case=config, run_id=run_id, started_at=started, config_root=root)
    )
    return GenerativeJobStartedResponse(run_id=run_id, job_id=job_id)


def _root_cause_job(
    storage: Storage, *, use_case: UseCaseConfig, run_id: str, started_at: datetime, config_root: Path | None
) -> JobFn:
    status_key = run_key(run_id, ROOT_CAUSE_STATUS_FILENAME)
    meter, guardrails = _client_meter_guardrails(use_case, job_id=run_id, config_root=config_root)

    def job(_cancel: CancelToken) -> None:
        stages = (_stage("summarize", RunState.RUNNING),)
        _write_status(
            storage,
            status_key,
            job_id=run_id,
            kind=GenerativeJobKind.ROOT_CAUSE,
            state=RunState.RUNNING,
            stages=stages,
            started_at=started_at,
        )
        try:
            began = time.monotonic()
            summary = build_root_cause_summary(
                run_id,
                use_case=use_case,
                storage=storage,
                meter=meter,
                guardrails=guardrails,
                config_root=config_root,
            )
            stages = (_stage("summarize", RunState.DONE, seconds=time.monotonic() - began),)
        except GenerativeError as exc:
            _write_status(
                storage,
                status_key,
                job_id=run_id,
                kind=GenerativeJobKind.ROOT_CAUSE,
                state=RunState.FAILED,
                stages=_failed(stages, exc),
                started_at=started_at,
                error=exc,
            )
            _write_usage(storage, run_key(run_id, LLM_USAGE_FILENAME), meter)
            return
        storage.write_model(run_key(run_id, ROOT_CAUSE_SUMMARY_FILENAME), summary)
        _write_usage(storage, run_key(run_id, LLM_USAGE_FILENAME), meter)
        checks = [check for segment in summary.segments for check in segment.guardrails]
        _write_guardrail_report(
            storage,
            run_key(run_id, GUARDRAIL_REPORT_FILENAME),
            job_id=run_id,
            kind=GenerativeJobKind.ROOT_CAUSE,
            checks=checks,
        )
        # `state=DONE` is written last, after every artefact it promises: a poller that sees `done`
        # must never race a write still in flight (see `_write_status`'s own note on this).
        _write_status(
            storage,
            status_key,
            job_id=run_id,
            kind=GenerativeJobKind.ROOT_CAUSE,
            state=RunState.DONE,
            stages=stages,
            started_at=started_at,
        )

    return job


# ---------------------------------------------------------------------------
# 8. POST /runs/{run_id}/campaign-copy
# ---------------------------------------------------------------------------
@router.post(
    "/runs/{run_id}/campaign-copy",
    response_model=GenerativeJobStartedResponse,
    status_code=202,
    responses=_GENERATIVE_ERRORS,
    summary="Start campaign-copy generation over a finished scoring run",
)
def create_campaign_copy(
    run_id: str, body: CampaignCopyRequest, root: ConfigRootDep, storage: StorageDep, jobs: JobsDep
) -> GenerativeJobStartedResponse:
    run = load_run(storage, run_id)
    config = use_case_config(run.use_case_id, root)
    _require_generative_kind(config, GenerativeKind.CAMPAIGN_COPY)
    _require_finished_run(run, needs_explanations=False)
    generative = _merged_generative_sub(config.generative, body.overrides, block="campaign_copy")
    config = config.model_copy(update={"generative": generative})

    started = utc_now()
    stages = (_stage("generate", RunState.PENDING),)
    status_key = run_key(run_id, COPY_STATUS_FILENAME)
    _write_status(
        storage,
        status_key,
        job_id=run_id,
        kind=GenerativeJobKind.CAMPAIGN_COPY,
        state=RunState.PENDING,
        stages=stages,
        started_at=started,
    )
    job_id = _new_job_id("campaign_copy")
    jobs.submit(
        job_id,
        _campaign_copy_job(storage, use_case=config, run_id=run_id, started_at=started, config_root=root),
    )
    return GenerativeJobStartedResponse(run_id=run_id, job_id=job_id)


def _campaign_copy_job(
    storage: Storage, *, use_case: UseCaseConfig, run_id: str, started_at: datetime, config_root: Path | None
) -> JobFn:
    status_key = run_key(run_id, COPY_STATUS_FILENAME)
    meter, guardrails = _client_meter_guardrails(use_case, job_id=run_id, config_root=config_root)

    def job(_cancel: CancelToken) -> None:
        stages = (_stage("generate", RunState.RUNNING),)
        _write_status(
            storage,
            status_key,
            job_id=run_id,
            kind=GenerativeJobKind.CAMPAIGN_COPY,
            state=RunState.RUNNING,
            stages=stages,
            started_at=started_at,
        )
        try:
            began = time.monotonic()
            result = generate_campaign_copy(
                run_id=run_id,
                use_case=use_case,
                storage=storage,
                meter=meter,
                guardrails=guardrails,
                config_root=config_root,
            )
            stages = (_stage("generate", RunState.DONE, seconds=time.monotonic() - began),)
        except GenerativeError as exc:
            _write_status(
                storage,
                status_key,
                job_id=run_id,
                kind=GenerativeJobKind.CAMPAIGN_COPY,
                state=RunState.FAILED,
                stages=_failed(stages, exc),
                started_at=started_at,
                error=exc,
            )
            _write_usage(storage, run_key(run_id, LLM_USAGE_FILENAME), meter)
            return
        _write_usage(storage, run_key(run_id, LLM_USAGE_FILENAME), meter)
        checks = [check for template in result.batch.templates for check in template.guardrails]
        _write_guardrail_report(
            storage,
            run_key(run_id, GUARDRAIL_REPORT_FILENAME),
            job_id=run_id,
            kind=GenerativeJobKind.CAMPAIGN_COPY,
            checks=checks,
        )
        _write_status(
            storage,
            status_key,
            job_id=run_id,
            kind=GenerativeJobKind.CAMPAIGN_COPY,
            state=RunState.DONE,
            stages=stages,
            started_at=started_at,
        )

    return job


# ---------------------------------------------------------------------------
# 9 & 10. Campaign-copy templates: approve and regenerate
# ---------------------------------------------------------------------------
def _load_copy_batch(storage: Storage, run_id: str) -> CopyBatch:
    try:
        return storage.read_model(run_key(run_id, COPY_BATCH_FILENAME), CopyBatch)
    except StorageError as exc:
        raise http_error(
            404, "COPY_BATCH_NOT_FOUND", f"Run {run_id!r} has not generated campaign copy."
        ) from exc


def _find_template(batch: CopyBatch, template_id: str) -> CopyTemplate:
    for template in batch.templates:
        if template.template_id == template_id:
            return template
    raise http_error(404, "COPY_TEMPLATE_NOT_FOUND", f"No template with id {template_id!r} in this batch.")


@router.post(
    "/runs/{run_id}/campaign-copy/templates/{template_id}/approve",
    response_model=CopyTemplate,
    responses=_GENERATIVE_ERRORS,
    summary="Record that a person approved one campaign-copy template",
)
def approve_copy_template(
    run_id: str, template_id: str, body: CopyTemplateApproveRequest, storage: StorageDep
) -> CopyTemplate:
    load_run(storage, run_id)
    batch = _load_copy_batch(storage, run_id)
    target = _find_template(batch, template_id)
    if target.status is not CopyStatus.PENDING_REVIEW:
        raise http_error(
            409,
            "COPY_TEMPLATE_NOT_PENDING",
            f"Template {template_id!r} is {target.status.value}, not pending review.",
        )
    updated = approve_template(batch, template_id, approved_by=body.approved_by)
    storage.write_model(run_key(run_id, COPY_BATCH_FILENAME), updated)
    return _find_template(updated, template_id)


@router.post(
    "/runs/{run_id}/campaign-copy/templates/{template_id}/regenerate",
    response_model=CopyTemplate,
    responses=_GENERATIVE_ERRORS,
    summary="Re-run generation for one campaign-copy template in place",
)
def regenerate_copy_template(
    run_id: str, template_id: str, root: ConfigRootDep, storage: StorageDep
) -> CopyTemplate:
    run = load_run(storage, run_id)
    config = use_case_config(run.use_case_id, root)
    batch = _load_copy_batch(storage, run_id)
    _find_template(batch, template_id)  # 404 before a model is called
    meter, guardrails = _client_meter_guardrails(config, job_id=f"regen_{run_id}", config_root=root)
    try:
        replacement = regenerate_template(
            batch,
            template_id,
            run_id=run_id,
            use_case=config,
            storage=storage,
            meter=meter,
            guardrails=guardrails,
            config_root=root,
        )
    except GenerativeError as exc:
        raise generative_http(exc) from exc
    updated_templates = tuple(
        replacement if template.template_id == template_id else template for template in batch.templates
    )
    updated_batch = batch.model_copy(update={"templates": updated_templates})
    storage.write_model(run_key(run_id, COPY_BATCH_FILENAME), updated_batch)
    _write_usage(storage, run_key(run_id, LLM_USAGE_FILENAME), meter)
    return replacement


# ---------------------------------------------------------------------------
# 11. GET /runs/{run_id}/copy_messages.csv
# ---------------------------------------------------------------------------
@router.get(
    "/runs/{run_id}/copy_messages.csv",
    response_class=Response,
    responses=_NOT_FOUND,
    summary="The rendered campaign-copy messages of a run, one row per scored entity",
)
def read_copy_messages(run_id: str, storage: StorageDep) -> Response:
    """Registered like `GET /runs/{run_id}/scores.csv`; `404 ARTEFACT_NOT_FOUND` before any copy exists."""
    return read_artefact(run_id, COPY_MESSAGES_FILENAME, storage)
