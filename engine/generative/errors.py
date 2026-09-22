"""Every failure the generative engine reports, as a code, a message and a suggestion.

The shape `engine.errors.ENGINE_ERRORS` already uses, and here for the same three reasons. A code
that is written down can be mapped onto an HTTP status by `api.routes.generative`, can be listed in
the generated documentation, and can be asserted by a test; a code that exists only as a string
literal at its raise site can be none of those. `generative_error` is the only way this package
raises, so an unregistered code cannot reach a user (DEC-212).

Every message names a configuration key, a channel, a document name, a rule or a count. None of
them quotes a customer's value, a prompt's contents or a model's completion, because a message
reaches a log and a screen and those three are data (plan section 13.7).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = [
    "BUDGET_EXCEEDED",
    "COMPLAINT_COLUMN_MISSING",
    "DOCUMENT_EMPTY",
    "DOCUMENT_TYPE_UNSUPPORTED",
    "DOCUMENT_UNREADABLE",
    "GENERATIVE_ERRORS",
    "GUARDRAIL_BLOCKED",
    "INDEX_EMPTY",
    "INDEX_NOT_FOUND",
    "KNOWLEDGE_BASE_TOO_LARGE",
    "MISSING_FIELD",
    "MODEL_OUTPUT_MALFORMED",
    "NOT_A_GENERATIVE_USE_CASE",
    "PROMPT_NOT_FOUND",
    "PROMPT_VARIABLE_MISSING",
    "REFERENCE_SET_INVALID",
    "RUN_NOT_FINISHED",
    "RUN_WITHOUT_EXPLANATIONS",
    "RUN_WITHOUT_SCORES",
    "UNGROUNDED_CLAIM",
    "GenerativeError",
    "generative_error",
]

# --- the model and the money ----------------------------------------------------------------
BUDGET_EXCEEDED: Final[str] = "BUDGET_EXCEEDED"
"""The run reached its call or cost ceiling, so no further call was made and it stopped cleanly."""

MODEL_OUTPUT_MALFORMED: Final[str] = "MODEL_OUTPUT_MALFORMED"
"""The model did not return the JSON its prompt asked for, after every retry the budget allowed."""

UNGROUNDED_CLAIM: Final[str] = "UNGROUNDED_CLAIM"
"""A summary cited evidence that is not in the pack it was given, after every retry."""

GUARDRAIL_BLOCKED: Final[str] = "GUARDRAIL_BLOCKED"
"""A generated text failed a blocking rule and was not stored; the report names which rule."""

# --- prompts ----------------------------------------------------------------------------------
PROMPT_NOT_FOUND: Final[str] = "PROMPT_NOT_FOUND"
PROMPT_VARIABLE_MISSING: Final[str] = "PROMPT_VARIABLE_MISSING"

# --- documents and indexes ----------------------------------------------------------------------
DOCUMENT_TYPE_UNSUPPORTED: Final[str] = "DOCUMENT_TYPE_UNSUPPORTED"
DOCUMENT_UNREADABLE: Final[str] = "DOCUMENT_UNREADABLE"
DOCUMENT_EMPTY: Final[str] = "DOCUMENT_EMPTY"
KNOWLEDGE_BASE_TOO_LARGE: Final[str] = "KNOWLEDGE_BASE_TOO_LARGE"
INDEX_NOT_FOUND: Final[str] = "INDEX_NOT_FOUND"
INDEX_EMPTY: Final[str] = "INDEX_EMPTY"
REFERENCE_SET_INVALID: Final[str] = "REFERENCE_SET_INVALID"

# --- what a flow needs from the run it reads ------------------------------------------------
RUN_NOT_FINISHED: Final[str] = "RUN_NOT_FINISHED"
RUN_WITHOUT_SCORES: Final[str] = "RUN_WITHOUT_SCORES"
RUN_WITHOUT_EXPLANATIONS: Final[str] = "RUN_WITHOUT_EXPLANATIONS"
COMPLAINT_COLUMN_MISSING: Final[str] = "COMPLAINT_COLUMN_MISSING"
NOT_A_GENERATIVE_USE_CASE: Final[str] = "NOT_A_GENERATIVE_USE_CASE"
MISSING_FIELD: Final[str] = "MISSING_FIELD"


GENERATIVE_ERRORS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        BUDGET_EXCEEDED: (
            "This run reached its {limit} limit after {calls} calls, so it stopped before finishing.",
            "Raise generative.budget for this use case, or narrow what the run is asked to generate.",
        ),
        MODEL_OUTPUT_MALFORMED: (
            "The model did not answer in the shape the {prompt} prompt asked for, after {attempts} tries.",
            "Try again; if it keeps happening, the prompt and the model no longer agree on the format.",
        ),
        UNGROUNDED_CLAIM: (
            "The summary for {segment} cited evidence that was not in the pack, after {attempts} tries.",
            "Nothing was stored for that segment. Re-run it, or give the segment more evidence to work from.",
        ),
        GUARDRAIL_BLOCKED: (
            "{target} failed the {rule} check and was not stored.",
            "Read guardrail_report.json for what the rule found, then adjust the prompt or the rule.",
        ),
        PROMPT_NOT_FOUND: (
            "There is no prompt called {name} in the prompts directory.",
            "Check configs/prompts/ for the file name and its version suffix.",
        ),
        PROMPT_VARIABLE_MISSING: (
            "The {name} prompt needs {missing}, which the caller did not supply.",
            "The prompt's front matter lists what it expects; supply it, or take it out of that list.",
        ),
        DOCUMENT_TYPE_UNSUPPORTED: (
            "{name} is a {extension} file, which this knowledge base does not accept.",
            "Accepted types are set by generative.knowledge_base.accepted_types.",
        ),
        DOCUMENT_UNREADABLE: (
            "{name} could not be read; the file may be corrupt or password-protected.",
            "Open it locally to check it, then upload it again.",
        ),
        DOCUMENT_EMPTY: (
            "{name} has no text in it, so there is nothing to index.",
            "A scanned document needs to be made searchable before it can be indexed.",
        ),
        KNOWLEDGE_BASE_TOO_LARGE: (
            "This knowledge base would hold {documents} documents and {megabytes} MB.",
            "The limits are generative.knowledge_base.max_docs and max_mb.",
        ),
        INDEX_NOT_FOUND: (
            "There is no knowledge index called {index_id}.",
            "Build one first, or check the id against the list of indexes.",
        ),
        INDEX_EMPTY: (
            "The {index_id} index holds no chunks, so no question can be answered from it.",
            "Every uploaded document failed to parse. The build report names each one.",
        ),
        REFERENCE_SET_INVALID: (
            "The reference set is missing the {column} column.",
            "The column names are set by generative.reference_set; the upload template shows them.",
        ),
        RUN_NOT_FINISHED: (
            "Run {run_id} is {state}, so there is nothing finished to write about yet.",
            "Wait for the run to finish, then ask again.",
        ),
        RUN_WITHOUT_SCORES: (
            "Run {run_id} wrote no scores, so there are no rows to work from.",
            "Score some data with this use case first, then come back to this run.",
        ),
        RUN_WITHOUT_EXPLANATIONS: (
            "Run {run_id} wrote no per-row reasons, so there is no evidence to summarise.",
            "Turn evaluation.shap on and run again; a summary with no evidence would be a guess.",
        ),
        COMPLAINT_COLUMN_MISSING: (
            "Column {column} is not in the data run {run_id} read.",
            "Set generative.root_cause.complaint_text_column to a column the dataset has, or leave it unset.",
        ),
        NOT_A_GENERATIVE_USE_CASE: (
            "Use case {use_case_id} has generative.kind set to {kind}, which does not do this.",
            "Set generative.kind for this use case, or ask a use case that already has it.",
        ),
        MISSING_FIELD: (
            "{rows} rows have no value for {field}, so no message could be rendered for them.",
            "Take the field out of generative.campaign_copy.allowed_fields, or fill it in the data.",
        ),
    }
)
"""Code -> (message template, suggestion) for every failure this package reports (plan section 13.4).

The same shape as `engine.errors.ENGINE_ERRORS`, and here for the same reason:
`api.routes.generative` maps these codes onto HTTP statuses, which it can only do for codes that
are written down, and an unmapped one is a server fault rather than a guessed 4xx.
"""


class GenerativeError(Exception):
    """A generative flow cannot produce an honest result.

    `code` and `message` are the two fields `engine.errors.run_error` reads structurally, so one of
    these becomes a `RunError` in a status document with no edit to that module. `suggestion` is
    what a caller shows next to the message; `RunError` has no field for it, so it reaches the log
    and the API rather than the status document - which is where `EngineError.suggestion` already
    sits (DEC-052).
    """

    def __init__(self, code: str, message: str, *, suggestion: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.suggestion = suggestion or GENERATIVE_ERRORS.get(code, ("", ""))[1]


def generative_error(code: str, **values: object) -> GenerativeError:
    """Build the error for `code`, filling its message template from `values`.

    `GENERATIVE_ERRORS[code]` raises `KeyError` for a code nobody wrote down, which is the point: a
    new failure mode has to be described before it can be raised.
    """
    message, suggestion = GENERATIVE_ERRORS[code]
    return GenerativeError(code, message.format(**values), suggestion=suggestion)
