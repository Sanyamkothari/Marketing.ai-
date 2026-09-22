"""Artefact contracts for the generative engine: one pydantic model per file a flow writes.

These follow every rule `engine/contracts.py` sets - frozen, `extra="forbid"`, tuples rather than
lists, timezone-aware datetimes, one `Field(description=...)` per field, lists arrived at pre-sorted
because the UI renders and never computes - and they live in their own module for one reason:
`ARTEFACT_REGISTRY` is pinned name-for-name by `tests/unit/test_artefact_registry.py`, and a
generative artefact is not a predictive one. `GENERATIVE_ARTEFACTS` is the parallel map, the two are
proved disjoint, and the artefact route serves their union (DEC-210).

Three shapes recur and are worth knowing before reading:

**An evidence pack is the whole input to a prompt.** `EvidencePack` carries a segment's size, its
score statistics, its strongest aggregated reasons and a sample of redacted complaint text - and
nothing else. Every `RootCause.evidence_refs` entry is the `id` of something in that pack, checked
against it before the summary is stored, so a claim that points nowhere cannot be written down.

**A template is not a message.** `CopyTemplate` is what a model wrote: prose with `{{field}}`
placeholders, judged once. `CopyMessage` is one rendering of it for one row, checked again by the
deterministic rules because a placeholder can push a rendering past a length limit that the
template cleared. Cost is therefore O(bands x channels), and no personal data reaches a prompt.

**A number the model produced is never a number on the screen.** Sizes, rates, token counts and
costs are measured by the engine and carried here; the generated text is prose alongside them.
Where a cost could not be worked out because the price table does not know a model,
`cost_estimate_usd` is null and a `PRICE_UNKNOWN` warning names it, exactly as `CostEstimate`
already does for compute (DEC-208).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from engine.contracts import Artefact, Direction, RunState

__all__ = [
    "CHUNKS_FILENAME",
    "COPY_BATCH_FILENAME",
    "COPY_MESSAGES_FILENAME",
    "COPY_STATUS_FILENAME",
    "DOC_INDEX_MANIFEST_FILENAME",
    "EMBEDDINGS_FILENAME",
    "GENERATIVE_ARTEFACTS",
    "GENERATIVE_TABULAR_SCHEMAS",
    "GUARDRAIL_REPORT_FILENAME",
    "INDEX_ARTEFACTS",
    "INDEX_STATUS_FILENAME",
    "LLM_USAGE_FILENAME",
    "PRICE_UNKNOWN",
    "RAG_EVAL_FILENAME",
    "ROOT_CAUSE_STATUS_FILENAME",
    "ROOT_CAUSE_SUMMARY_FILENAME",
    "RUN_GENERATIVE_ARTEFACTS",
    "AssistantAnswer",
    "Chunk",
    "ChunkConfig",
    "ChunkEmbedding",
    "Citation",
    "Confidence",
    "CopyAudience",
    "CopyBatch",
    "CopyHoldout",
    "CopyMessage",
    "CopyStatus",
    "CopyTemplate",
    "DocIndexManifest",
    "EvidencePack",
    "EvidenceReason",
    "GenerativeJobKind",
    "GenerativePurpose",
    "GenerativeStage",
    "GenerativeStatus",
    "GuardrailCheck",
    "GuardrailOutcome",
    "GuardrailReport",
    "GuardrailSummary",
    "IndexedDocument",
    "JudgeScore",
    "LlmUsage",
    "ModelUsage",
    "PurposeUsage",
    "RagEval",
    "RagEvalAggregates",
    "RagEvalQuestion",
    "RedactedComplaint",
    "RootCause",
    "RootCauseSummary",
    "SegmentStats",
    "SegmentSummary",
    "SummarisedSegment",
    "generative_artefact_model",
]


PRICE_UNKNOWN: Final[str] = "PRICE_UNKNOWN"
"""The warning `LlmUsage.warnings` carries when `configs/llm_prices.yaml` has no row for a model.

Cost is then null rather than zero, for the reason `CostEstimate` gives about compute: a zero is a
measurement, and there was none. The warning names the model so an operator knows which row to add
(DEC-208).
"""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class GenerativeJobKind(StrEnum):
    """What a generative job is doing, which decides the stages its status document carries."""

    INDEX_BUILD = "index_build"
    REFERENCE_EVAL = "reference_eval"
    ROOT_CAUSE = "root_cause"
    CAMPAIGN_COPY = "campaign_copy"


class GenerativePurpose(StrEnum):
    """What one LLM call was for. `llm_usage.json` reports cost by purpose, so this is the key."""

    EMBEDDING = "embedding"
    ASSISTANT_ANSWER = "assistant_answer"
    ROOT_CAUSE_SUMMARY = "root_cause_summary"
    COPY_EMAIL = "copy_email"
    COPY_SMS = "copy_sms"
    COPY_WHATSAPP = "copy_whatsapp"
    JUDGE_FAITHFULNESS = "judge_faithfulness"
    JUDGE_CORRECTNESS = "judge_correctness"
    JUDGE_COMPLIANCE = "judge_compliance"
    JUDGE_TOXICITY = "judge_toxicity"


class GuardrailOutcome(StrEnum):
    """What a guardrail decided: `warned` is recorded and let through, `blocked` is not stored."""

    PASSED = "passed"
    WARNED = "warned"
    BLOCKED = "blocked"


class CopyStatus(StrEnum):
    """Where one template stands. Nothing is ever sent from here; `approved` means a person said so."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    BLOCKED = "blocked"


class Confidence(StrEnum):
    """How strongly the evidence pack supports one root cause, as the summary itself judged it."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Cost and guardrails, shared by every flow
# ---------------------------------------------------------------------------
class ModelUsage(Artefact):
    """What one model was asked to do across a run."""

    model_id: str = Field(description="Model the calls were made against.")
    calls: int = Field(description="Calls made to this model, cache hits excluded.")
    input_tokens: int = Field(description="Input tokens sent to this model.")
    output_tokens: int = Field(description="Output tokens received from this model.")
    cost_estimate_usd: float | None = Field(
        default=None,
        description="Cost in US dollars; null when the price table has no row for this model.",
    )


class PurposeUsage(Artefact):
    """What one kind of work cost across a run: generating, judging or embedding."""

    purpose: GenerativePurpose = Field(description="What the calls were for.")
    calls: int = Field(description="Calls made for this purpose, cache hits excluded.")
    input_tokens: int = Field(description="Input tokens sent for this purpose.")
    output_tokens: int = Field(description="Output tokens received for this purpose.")
    cost_estimate_usd: float | None = Field(
        default=None,
        description="Cost in US dollars; null when any model used for this purpose had no price.",
    )


class LlmUsage(Artefact):
    """`llm_usage.json` - every LLM call a run made, what it returned and what it cost.

    Written for any run that made at least one call, and written even when the run failed, because
    a run that stopped halfway still spent what it spent. Tokens and calls are always real
    measurements; `cost_estimate_usd` is null rather than zero when the price table does not know a
    model, and `warnings` then carries `PRICE_UNKNOWN` naming it (DEC-208).
    """

    job_id: str = Field(description="Run or index build these calls belong to.")
    calls: int = Field(description="Calls actually made to a model.")
    cache_hits: int = Field(description="Calls answered from the on-disk cache, costing nothing.")
    input_tokens: int = Field(description="Input tokens across every call made.")
    output_tokens: int = Field(description="Output tokens across every call made.")
    cost_estimate_usd: float | None = Field(
        default=None,
        description="Total cost in US dollars; null when any model used had no price (PRICE_UNKNOWN).",
    )
    budget_usd: float | None = Field(
        default=None,
        description="The run's cost ceiling; null when no price was known, so none could be enforced.",
    )
    budget_calls: int = Field(description="The run's call ceiling, which applies whether or not prices do.")
    by_model: tuple[ModelUsage, ...] = Field(default=(), description="Usage per model, sorted by model id.")
    by_purpose: tuple[PurposeUsage, ...] = Field(
        default=(), description="Usage per purpose, sorted by purpose."
    )
    warnings: tuple[str, ...] = Field(
        default=(), description="Machine-readable warnings raised while metering, in the order raised."
    )
    created_at: AwareDatetime = Field(description="UTC time the usage record was written.")


class JudgeScore(Artefact):
    """One judge's verdict on one generated text."""

    purpose: GenerativePurpose = Field(description="Which judge produced this score.")
    score: float = Field(description="The judge's score, 0 to 1, where 1 is wholly acceptable.")
    threshold: float = Field(description="Score the configuration required, 0 to 1.")
    passed: bool = Field(description="Whether the score reached the threshold.")
    reason: str = Field(description="The judge's one-sentence explanation, verbatim.")


class GuardrailCheck(Artefact):
    """One rule applied to one generated text."""

    target: str = Field(description="What was checked: a template id, a segment name or a question.")
    rule: str = Field(description="Rule name from configs/guardrails.yaml.")
    outcome: GuardrailOutcome = Field(description="Whether the text passed, was warned about or blocked.")
    detail: str = Field(description="What the rule found, with no generated text or PII quoted back.")


class GuardrailSummary(Artefact):
    """How a batch of checks came out, so a screen needs no arithmetic."""

    checked: int = Field(description="Texts checked.")
    passed: int = Field(description="Texts that passed every rule.")
    warned: int = Field(description="Texts that raised a warning but were stored.")
    blocked: int = Field(description="Texts a rule refused, which were not stored.")


class GuardrailReport(Artefact):
    """`guardrail_report.json` - every rule that ran over everything one job generated.

    Separate from the artefact carrying the generated text, because a blocked text is not stored
    and its check still has to be: "nothing was written for the Medium band" is an answer a
    reviewer needs, and an empty slot is not one.
    """

    job_id: str = Field(description="Run or index build whose output was checked.")
    kind: GenerativeJobKind = Field(description="Which flow produced the text that was checked.")
    checks: tuple[GuardrailCheck, ...] = Field(
        default=(), description="Every check that ran, in the order it ran."
    )
    summary: GuardrailSummary = Field(description="Counts over those checks.")
    created_at: AwareDatetime = Field(description="UTC time the report was written.")


# ---------------------------------------------------------------------------
# Progress: a generative job is not a pipeline run, and says so in its own document
# ---------------------------------------------------------------------------
class GenerativeStage(Artefact):
    """One step of a generative job, as the Running screen shows it.

    `key` is a plain string, not a `StageKey`: a root-cause job has one step per segment and their
    names are data (`generate:High`), where the predictive stage vocabulary is closed and pinned.
    """

    key: str = Field(description="Step name, unique within the job.")
    title: str = Field(description="What the screen calls this step.")
    state: RunState = Field(description="Step state: pending, running, done, failed or skipped.")
    detail: str = Field(description="One line about what the step did; empty until it has run.")
    seconds: float = Field(description="Wall-clock seconds the step took; 0 until it has finished.")


class GenerativeStatus(Artefact):
    """`index_status.json`, `root_cause_status.json`, `copy_status.json` - a job in progress.

    A generative job that runs over a finished predictive run must not write into that run's
    `status.json`: the run is done, its document is the record of how it went, and appending to it
    would rewrite history. Each generative job keeps its own, with the same vocabulary of states so
    a screen can render either (DEC-211).
    """

    job_id: str = Field(description="Run or index build this status describes.")
    kind: GenerativeJobKind = Field(description="Which flow is running.")
    state: RunState = Field(description="Overall state, derived from the steps.")
    progress_pct: int = Field(description="Share of steps finished, as a percentage from 0 to 100.")
    stages: tuple[GenerativeStage, ...] = Field(default=(), description="Every step, in the order they run.")
    started_at: AwareDatetime | None = Field(
        default=None, description="UTC time the first step started; null before it did."
    )
    updated_at: AwareDatetime = Field(description="UTC time this document was last written.")
    error_code: str | None = Field(
        default=None, description="Machine-readable failure code; null unless the job failed."
    )
    error_message: str | None = Field(
        default=None, description="Business-language failure message; null unless the job failed."
    )


# ---------------------------------------------------------------------------
# The knowledge index
# ---------------------------------------------------------------------------
class ChunkConfig(Artefact):
    """The settings an index was built with, so a rebuild can be compared with it."""

    chunk_tokens: int = Field(description="Target size of one chunk, in tokens.")
    chunk_overlap: float = Field(description="Share of a chunk repeated from the one before, 0 to 1.")
    embedding_model_id: str = Field(description="Model whose vectors this index holds.")
    dimensions: int = Field(description="Length of one embedding vector.")


class IndexedDocument(Artefact):
    """One document in a knowledge index."""

    doc_id: str = Field(description="Stable id of the document within this index.")
    name: str = Field(description="Filename as uploaded, which is what a citation shows.")
    media_type: str = Field(description="File type the parser read: pdf, docx, md or txt.")
    fingerprint: str = Field(description="Content hash; an unchanged one is skipped on a rebuild.")
    bytes: int = Field(description="Size of the uploaded file in bytes.")
    pages: int | None = Field(
        default=None, description="Pages the document has; null for a format without pages."
    )
    sections: int = Field(description="Headed sections the parser found; 1 when it found no headings.")
    chunks: int = Field(description="Chunks this document contributed to the index.")
    warnings: tuple[str, ...] = Field(
        default=(), description="Machine-readable warnings raised while parsing, in the order raised."
    )


class DocIndexManifest(Artefact):
    """`doc_index_manifest.json` - what is in a knowledge index and how it was built.

    An index is the generative counterpart of a trained model: the thing an answer is produced
    from, and the thing a reader asks "which documents, which settings, when?" about. A rebuild is
    incremental by `IndexedDocument.fingerprint`, so this file is also the record of what could be
    skipped.
    """

    index_id: str = Field(description="Sortable index id, also the index directory name.")
    use_case_id: str = Field(description="Use case this index belongs to.")
    client_id: str | None = Field(
        default=None, description="Client the documents belong to; null when none was given."
    )
    documents: tuple[IndexedDocument, ...] = Field(
        default=(), description="Documents in the index, sorted by name."
    )
    total_chunks: int = Field(description="Chunks across every document.")
    chunk_config: ChunkConfig = Field(description="Settings the index was built with.")
    prompt_versions: dict[str, int] = Field(
        default_factory=dict,
        description="Prompt name to version, for every prompt this index's answers will use.",
    )
    built_at: AwareDatetime = Field(description="UTC time the build finished.")
    build_seconds: float = Field(description="Wall-clock seconds the build took.")
    warnings: tuple[str, ...] = Field(
        default=(),
        description="Index-wide warnings, such as PII_IN_DOCS when a document carries personal data.",
    )


class Chunk(Artefact):
    """One row of `chunks.parquet`: a passage of a document, as retrieved and as cited."""

    chunk_id: str = Field(description="Stable id of this chunk within the index.")
    doc_id: str = Field(description="Document the chunk came from.")
    document: str = Field(description="Document filename, denormalised so a citation needs one read.")
    section: str = Field(description="Heading the chunk sits under; the document name when it has none.")
    ordinal: int = Field(description="Position of the chunk within its document, from 0.")
    page: int | None = Field(default=None, description="Page the chunk starts on; null when unpaged.")
    tokens: int = Field(description="Approximate tokens in the chunk text.")
    text: str = Field(description="The passage itself, as the parser read it.")


class ChunkEmbedding(Artefact):
    """One row of `embeddings.parquet`: the vector for one chunk, kept beside it rather than in it.

    Two files rather than one because the text is read on every citation and the vectors are read
    on every question; keeping them apart means a citation does not pay for a float array.
    """

    chunk_id: str = Field(description="Chunk this vector belongs to.")
    embedding: tuple[float, ...] = Field(description="The vector, in the index's embedding model.")


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------
class Citation(Artefact):
    """Where one claim in an answer came from."""

    chunk_id: str = Field(description="Chunk the claim was drawn from.")
    document: str = Field(description="Document filename, as the screen shows it.")
    section: str = Field(description="Heading the chunk sits under.")
    quote: str = Field(description="At most 25 words copied from the chunk, so a reader can check it.")
    similarity: float = Field(description="Cosine similarity of the chunk to the question, 0 to 1.")


class AssistantAnswer(Artefact):
    """One answer to one question: what was said, what it was drawn from and what it cost.

    Not a file. It is the body of `POST /indexes/{id}/ask` and a row of `rag_eval.json`, and it is
    a contract because the refusal is: `refused` is true and `citations` is empty whenever the
    retrieved chunks did not answer the question, including when nothing was retrieved at all and
    no model was called.
    """

    question: str = Field(description="The question as it was asked.")
    answer: str = Field(description="The answer, or the configured refusal sentence verbatim.")
    refused: bool = Field(description="True when the documents did not answer and the assistant said so.")
    citations: tuple[Citation, ...] = Field(
        default=(), description="Chunks the answer drew on, strongest first; empty for a refusal."
    )
    retrieved: int = Field(description="Chunks retrieved above the similarity floor, before the answer.")
    called_model: bool = Field(
        description="False when nothing passed the similarity floor, so the refusal cost nothing."
    )
    prompt_version: int = Field(description="Version of the answering prompt that produced this.")
    latency_ms: int = Field(description="Milliseconds from question to answer, retrieval included.")
    guardrails: tuple[GuardrailCheck, ...] = Field(
        default=(), description="Checks that ran over the answer, in the order they ran."
    )


# ---------------------------------------------------------------------------
# Grading an index against a reference set
# ---------------------------------------------------------------------------
class RagEvalQuestion(Artefact):
    """One graded question: what was asked, what came back and how each check scored it."""

    question: str = Field(description="The question from the reference set.")
    answer: str = Field(description="What the assistant replied.")
    refused: bool = Field(description="Whether the assistant refused.")
    expect_refusal: bool = Field(description="Whether the reference set says it should have refused.")
    retrieval_hit: bool | None = Field(
        default=None,
        description="Whether a retrieved chunk overlaps the reference answer; null for a refusal row.",
    )
    faithfulness: float | None = Field(
        default=None,
        description="Judge score for "
        "every claim being supported by the retrieved chunks, 0 to 1; null when not judged.",
    )
    correctness: float | None = Field(
        default=None,
        description="Judge score for agreement with the reference answer, 0 to 1; null when not judged.",
    )
    passed: bool = Field(description="Whether this question met every check its row was subject to.")
    failure: str | None = Field(
        default=None, description="Which check failed first; null when the question passed."
    )
    latency_ms: int = Field(description="Milliseconds the answer took.")
    cost_estimate_usd: float | None = Field(
        default=None, description="Cost of answering and judging this row; null when a price was unknown."
    )


class RagEvalAggregates(Artefact):
    """The numbers the Results screen shows, computed here so the screen computes nothing."""

    questions: int = Field(description="Questions graded.")
    passed: int = Field(description="Questions that passed.")
    pass_rate: float = Field(description="Share of questions that passed, 0 to 1.")
    pass_threshold: float = Field(description="Share the configuration required, 0 to 1.")
    meets_threshold: bool = Field(description="Whether the pass rate reached the threshold.")
    retrieval_hit_rate: float | None = Field(
        default=None, description="Share of answerable questions that retrieved the right document, 0 to 1."
    )
    mean_faithfulness: float | None = Field(
        default=None, description="Mean faithfulness over judged questions, 0 to 1; null when none was."
    )
    mean_correctness: float | None = Field(
        default=None, description="Mean correctness over judged questions, 0 to 1; null when none was."
    )
    refusal_accuracy: float | None = Field(
        default=None,
        description="Share of rows where refusing or answering matched the reference set, 0 to 1.",
    )


class RagEval(Artefact):
    """`rag_eval.json` - how an index answered a reference set, question by question.

    The ten worst questions are what a person actually acts on, so `questions` is stored sorted
    worst first: a screen shows the head of the list rather than sorting one itself.
    """

    index_id: str = Field(description="Index that was graded.")
    reference_set_fingerprint: str = Field(description="Content hash of the reference file that graded it.")
    questions: tuple[RagEvalQuestion, ...] = Field(
        default=(), description="Every graded question, worst first."
    )
    aggregates: RagEvalAggregates = Field(description="Counts and rates over those questions.")
    prompt_versions: dict[str, int] = Field(
        default_factory=dict, description="Prompt name to version, for every prompt the grading used."
    )
    graded_at: AwareDatetime = Field(description="UTC time the grading finished.")


# ---------------------------------------------------------------------------
# Root-cause summaries
# ---------------------------------------------------------------------------
class EvidenceReason(Artefact):
    """One model reason, aggregated over a segment's rows, as the prompt sees it."""

    id: str = Field(description="Reference a summary cites, unique within the evidence pack.")
    feature: str = Field(description="Feature the reason is about.")
    direction: Direction = Field(description="Whether the feature pushed the score up or down.")
    mean_abs_contribution: float = Field(description="Mean absolute SHAP contribution over the segment.")
    share_pct: float = Field(description="Share of the segment's total contribution, as a percentage.")
    rows: int = Field(description="Rows in the segment for which this reason was among the strongest.")


class RedactedComplaint(Artefact):
    """One complaint from a segment, redacted, as the prompt sees it.

    No entity key: the pack is the whole input to a model, and a model that was handed a customer
    id could put one in an answer. The id stays in the run's own artefacts, where it belongs.
    """

    id: str = Field(description="Reference a summary cites, unique within the evidence pack.")
    text: str = Field(description="The complaint with every detected identifier replaced.")
    matched_reason_ids: tuple[str, ...] = Field(
        default=(), description="Reasons this complaint was chosen for, by keyword overlap, strongest first."
    )


class SegmentStats(Artefact):
    """What is measurably true of a segment, computed by the engine and never by a model."""

    rows: int = Field(description="Rows in the segment.")
    share_pct: float = Field(description="Share of the run's scored rows in this segment, as a percentage.")
    mean_score: float = Field(description="Mean model score over the segment, 0 to 1.")
    min_score: float = Field(description="Lowest model score in the segment, 0 to 1.")
    max_score: float = Field(description="Highest model score in the segment, 0 to 1.")
    positive_rate: float | None = Field(
        default=None,
        description="Share of the segment whose outcome was positive, 0 to 1; null when unlabelled.",
    )


class EvidencePack(Artefact):
    """Everything one summary was allowed to know, and the only thing its prompt was given.

    It is stored beside the summary rather than thrown away, because "is this claim supported?" is
    a question a reader asks months later, and the only honest answer is the input the model had.
    """

    segment: str = Field(description="Segment this pack describes.")
    stats: SegmentStats = Field(description="Measured facts about the segment.")
    reasons: tuple[EvidenceReason, ...] = Field(
        default=(), description="Strongest aggregated reasons, strongest first."
    )
    complaints: tuple[RedactedComplaint, ...] = Field(
        default=(), description="Redacted complaint samples; empty when the run carried no text column."
    )

    @property
    def reference_ids(self) -> frozenset[str]:
        """Every id a summary of this pack may cite. A claim citing anything else is ungrounded."""
        return frozenset(
            [reason.id for reason in self.reasons] + [complaint.id for complaint in self.complaints]
        )


class RootCause(Artefact):
    """One cause a summary put forward, and what in the pack it rests on."""

    cause: str = Field(description="The cause, in one or two sentences of business language.")
    evidence_refs: tuple[str, ...] = Field(
        description="Ids from the evidence pack that support this cause; never empty, never invented."
    )
    confidence: Confidence = Field(description="How strongly the pack supports it: high, medium or low.")

    @model_validator(mode="after")
    def _has_evidence(self) -> Self:
        # The grounding rule is a property of the artefact, not only of the parser that built it:
        # a cause with no reference could otherwise be written by any future caller.
        if not self.evidence_refs:
            raise ValueError("a root cause must cite at least one evidence reference")
        return self


class SegmentSummary(Artefact):
    """What the model wrote about one segment, once it had passed the guardrails."""

    headline: str = Field(description="One sentence naming what is going on in the segment.")
    root_causes: tuple[RootCause, ...] = Field(description="Causes put forward, strongest first.")
    recommended_actions: tuple[str, ...] = Field(
        default=(), description="What a retention team could do, one sentence each."
    )
    caveats: tuple[str, ...] = Field(
        default=(), description="What this evidence cannot settle, one sentence each."
    )


class SummarisedSegment(Artefact):
    """One segment: its measured facts, the pack they were packaged into, and what came back."""

    segment: str = Field(description="Segment name, from the band or the reason it was cut on.")
    evidence_pack: EvidencePack = Field(description="The whole input the summary was written from.")
    summary: SegmentSummary | None = Field(
        default=None, description="What the model wrote; null when every attempt was blocked."
    )
    guardrails: tuple[GuardrailCheck, ...] = Field(
        default=(), description="Checks that ran over this segment's summary, in the order they ran."
    )
    attempts: int = Field(description="Generations made for this segment, retries included.")
    blocked_reason: str | None = Field(
        default=None, description="Why no summary was stored; null when one was."
    )


class RootCauseSummary(Artefact):
    """`root_cause_summary.json` - plain-language root causes for each segment of a finished run.

    Written over a run, never inside it: the run trained or scored and is finished, and this is a
    second reading of what it produced. Everything numeric on the screen comes from
    `SummarisedSegment.evidence_pack`, never from the generated prose, so a model that miscounts
    cannot put a wrong number in front of anyone.
    """

    run_id: str = Field(description="Completed run these summaries were written over.")
    segment_by: str = Field(description="What the rows were cut on: a band, or the strongest reason.")
    segments: tuple[SummarisedSegment, ...] = Field(default=(), description="Segments, largest first.")
    prompt_versions: dict[str, int] = Field(
        default_factory=dict, description="Prompt name to version, for every prompt used."
    )
    prompt_hashes: dict[str, str] = Field(
        default_factory=dict, description="Prompt name to content hash, so a rendering is reproducible."
    )
    complaint_source: str | None = Field(
        default=None,
        description="Where complaint text came from: a dataset column, an upload, or null for none.",
    )
    generated_at: AwareDatetime = Field(description="UTC time generation finished.")


# ---------------------------------------------------------------------------
# Campaign copy
# ---------------------------------------------------------------------------
class CopyTemplate(Artefact):
    """One variant a model wrote for one band on one channel: prose with placeholders, not a message."""

    template_id: str = Field(description="Stable id of this template within the batch.")
    band: str = Field(description="Band the template was written for.")
    channel: str = Field(description="Channel it was written for: email, sms or whatsapp.")
    variant: str = Field(description="Variant label within the band and channel: A, B, C or D.")
    subject: str | None = Field(
        default=None, description="Subject line; null on a channel that has no subject."
    )
    text: str = Field(description="The template body, with its {{placeholders}} left unrendered.")
    fields_used: tuple[str, ...] = Field(
        default=(), description="Placeholders the template names, sorted; always within allowed_fields."
    )
    status: CopyStatus = Field(description="pending_review, approved or blocked.")
    judge_scores: tuple[JudgeScore, ...] = Field(
        default=(), description="Judge verdicts on this template, in the order they ran."
    )
    guardrails: tuple[GuardrailCheck, ...] = Field(
        default=(), description="Checks that ran over this template, in the order they ran."
    )
    block_reason: str | None = Field(
        default=None, description="Why the template was blocked; null unless status is blocked."
    )
    attempts: int = Field(description="Generations made for this variant, retries included.")
    approved_by: str | None = Field(
        default=None,
        description="Who approved it, as they claimed; null until approved. Not a verified identity.",
    )
    approved_at: AwareDatetime | None = Field(
        default=None, description="UTC time it was approved; null until approved."
    )


class CopyAudience(Artefact):
    """Who the batch was written for, counted before a word was generated."""

    rows: int = Field(description="Rows eligible for copy after suppression and the control holdout.")
    per_band: dict[str, int] = Field(default_factory=dict, description="Eligible rows per band name.")


class CopyHoldout(Artefact):
    """Who was deliberately left out, so an uplift comparison later has its control.

    Phase 1 already assigns a control group and suppresses rows that opted out or were contacted
    recently. Copy respects both and records them, because "why is this customer not in the batch?"
    has two different answers and they must not be confused (Phase 3b reads this).
    """

    control_rows: int = Field(description="Rows held out as the control group, which get no message.")
    suppressed_rows: int = Field(description="Rows suppressed by consent or recent contact.")
    out_of_band_rows: int = Field(description="Rows in a band the configuration does not write copy for.")


class CopyBatch(Artefact):
    """`copy_batch.json` - the templates written over a finished scoring run, and their review state.

    Cost is O(bands x channels) because a model writes templates and the engine renders them, so no
    personal data ever reaches a prompt. Nothing here is ever sent: `approved` records that a person
    said a template is fit to use, and sending is somebody else's system.
    """

    run_id: str = Field(description="Completed scoring run this batch was written over.")
    batch_id: str = Field(description="Stable id of this batch within the run.")
    audience: CopyAudience = Field(description="Rows the batch covers.")
    holdout: CopyHoldout = Field(description="Rows deliberately left out, and why.")
    templates: tuple[CopyTemplate, ...] = Field(
        default=(), description="Templates, sorted by band, channel then variant."
    )
    require_human_review: bool = Field(
        description="Whether a template must be approved by a person before it counts as usable."
    )
    messages_rendered: int = Field(description="Rows a template was rendered for, across every channel.")
    messages_blocked: int = Field(description="Renderings a deterministic rule refused.")
    prompt_versions: dict[str, int] = Field(
        default_factory=dict, description="Prompt name to version, for every prompt used."
    )
    prompt_hashes: dict[str, str] = Field(
        default_factory=dict, description="Prompt name to content hash, so a rendering is reproducible."
    )
    created_at: AwareDatetime = Field(description="UTC time the batch was written.")


class CopyMessage(Artefact):
    """One row of `copy_messages.csv`: one template rendered for one scored row.

    Checked again after rendering, because a placeholder is shorter than what replaces it: a
    template that cleared the SMS limit can produce a rendering that does not.
    """

    entity_key: str = Field(description="Primary key of the scored row this message is for.")
    band: str = Field(description="Band the row fell into.")
    channel: str = Field(description="Channel the message is for.")
    variant: str = Field(description="Variant label the message was rendered from.")
    template_id: str = Field(description="Template the message was rendered from.")
    rendered_text: str = Field(description="The message as rendered; empty when a rule refused it.")
    status: CopyStatus = Field(description="State of the template this rendering came from.")
    block_reason: str | None = Field(
        default=None, description="Why this rendering was refused; null when it was not."
    )


# ---------------------------------------------------------------------------
# The registry. `ARTEFACT_REGISTRY` stays pinned; this is the parallel map (DEC-210).
# ---------------------------------------------------------------------------
DOC_INDEX_MANIFEST_FILENAME: Final[str] = "doc_index_manifest.json"
INDEX_STATUS_FILENAME: Final[str] = "index_status.json"
RAG_EVAL_FILENAME: Final[str] = "rag_eval.json"
CHUNKS_FILENAME: Final[str] = "chunks.parquet"
EMBEDDINGS_FILENAME: Final[str] = "embeddings.parquet"
ROOT_CAUSE_SUMMARY_FILENAME: Final[str] = "root_cause_summary.json"
ROOT_CAUSE_STATUS_FILENAME: Final[str] = "root_cause_status.json"
COPY_BATCH_FILENAME: Final[str] = "copy_batch.json"
COPY_STATUS_FILENAME: Final[str] = "copy_status.json"
COPY_MESSAGES_FILENAME: Final[str] = "copy_messages.csv"
GUARDRAIL_REPORT_FILENAME: Final[str] = "guardrail_report.json"
LLM_USAGE_FILENAME: Final[str] = "llm_usage.json"

GENERATIVE_ARTEFACTS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        DOC_INDEX_MANIFEST_FILENAME: DocIndexManifest,
        INDEX_STATUS_FILENAME: GenerativeStatus,
        RAG_EVAL_FILENAME: RagEval,
        ROOT_CAUSE_SUMMARY_FILENAME: RootCauseSummary,
        ROOT_CAUSE_STATUS_FILENAME: GenerativeStatus,
        COPY_BATCH_FILENAME: CopyBatch,
        COPY_STATUS_FILENAME: GenerativeStatus,
        GUARDRAIL_REPORT_FILENAME: GuardrailReport,
        LLM_USAGE_FILENAME: LlmUsage,
    }
)
"""Generative artefact filename -> the model that validates it. Disjoint from `ARTEFACT_REGISTRY`."""

GENERATIVE_TABULAR_SCHEMAS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        CHUNKS_FILENAME: Chunk,
        EMBEDDINGS_FILENAME: ChunkEmbedding,
        COPY_MESSAGES_FILENAME: CopyMessage,
    }
)
"""Generative table filename -> the model of one row. Disjoint from `TABULAR_SCHEMAS`."""

INDEX_ARTEFACTS: Final[frozenset[str]] = frozenset(
    {
        DOC_INDEX_MANIFEST_FILENAME,
        INDEX_STATUS_FILENAME,
        CHUNKS_FILENAME,
        EMBEDDINGS_FILENAME,
    }
)
"""What a finished index build writes. `rag_eval.json` joins them only once a reference set is graded."""

RUN_GENERATIVE_ARTEFACTS: Final[frozenset[str]] = frozenset(
    {
        ROOT_CAUSE_SUMMARY_FILENAME,
        ROOT_CAUSE_STATUS_FILENAME,
        COPY_BATCH_FILENAME,
        COPY_STATUS_FILENAME,
        COPY_MESSAGES_FILENAME,
        GUARDRAIL_REPORT_FILENAME,
        LLM_USAGE_FILENAME,
    }
)
"""What a generative flow may add to a finished run's directory. It adds; it never rewrites."""


def generative_artefact_model(filename: str) -> type[BaseModel] | None:
    """The model validating `filename`, or `None` when this package does not own that name.

    `None` rather than a raise, because the caller that asks is the artefact route, which asks this
    map and the predictive one in turn and wants to fall through rather than catch.
    """
    return GENERATIVE_ARTEFACTS.get(filename) or GENERATIVE_TABULAR_SCHEMAS.get(filename)
