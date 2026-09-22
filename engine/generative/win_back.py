"""Win-back campaign copy: one model call per band and channel, rendered per row without it.

The design is stated once, in `engine.generative.contracts`, and this module exists to honour it
rather than to repeat it: **a template is not a message**. `CopyTemplate` is prose a model wrote for
one band on one channel, with `{{field}}` placeholders and nothing else - no row, no customer, no
number the model invented - and it is judged once, by an LLM judge, because judging it again for
every row it will eventually reach would be judging the same words a thousand times. `CopyMessage`
is one rendering of that prose for one row, checked again, but only by the free deterministic rules,
because a placeholder is shorter than what replaces it: a template that clears a channel's character
limit with `{{last_offer}}` in it can produce a rendering that does not, once `last_offer` is a real
sentence. That asymmetry is **why** cost here is O(bands x channels) rather than O(rows): the
expensive call happens once per band and channel, and rendering ten thousand rows afterwards is ten
thousand calls to `jinja2`, not ten thousand calls to a model.

**No personal data ever reaches a prompt**, and that is a property of the call graph, not a filter
applied to it. The functions that build a prompt (`_generate_for_band_channel`, and everything it
calls) never open the run's scored rows at all; they see a band name, the feature names a template
is allowed to use, and the aggregated reasons behind that band, none of which is anybody's data.
The scored rows are read for the first time by `render_message`, which never calls a model. A test
that wants to prove this can only prove it the honest way - by reading back every prompt the fake
client recorded and finding no row's value in any of them - and that is what this module's own test
suite does, rather than asserting a code path was avoided.

**Grounded or nothing is a code-level check, not a prompt instruction.** A template may only reference
placeholders in `generative.campaign_copy.allowed_fields`, plus, on the email channel only, the one
reserved merge field an unsubscribe link is written into (`UNSUBSCRIBE_FIELD`). The prompt asks the
model to keep to that list, and `_finalize_template` checks the model's own reply against it with a
regular expression before anything is judged or stored, exactly as `allowed_fields_only` already does
for a rendered answer elsewhere in this package - a request the model happens to honour is not a
guarantee, and this package does not store the difference. That check reads `{{field}}` names, so it
is paired with `unsupported_markup`, which refuses everything else jinja2 would evaluate: a reply
whose only markup is `{% for %}` or `{{ 7*7 }}` uses no field at all by the first check's reading,
and renders a loop or an invented number by the renderer's.

**A run's control group and suppression rules are read, never redrawn.** `engine.stages.actions`
already drew a deterministic, per-customer control holdout from the run's own seed and already
suppressed rows that opted out or were contacted too recently; this module's only job is to read
`control_group` and `suppressed_reason` off `scores.csv` and count them into `CopyHoldout`, alongside
a third category unique to copy - `out_of_band_rows`, a row whose band nobody asked for copy on. The
three are counted by a fixed precedence (suppressed, then control, then out-of-band) so a row is
never counted twice and a customer's absence from the batch always has exactly one stated reason.
Because the undelying draw is seeded by the run id, calling this module twice over the same run - or
building the run twice from an identical spec - produces byte-identical holdout counts; nothing here
adds a second source of randomness on top of it.

**A field the configuration allows but the data does not have is a configuration error, not a per-row
gap.** `MISSING_FIELD` is raised, aborting the batch before anything is written, when any row a
surviving template would be rendered for has no value for a field that template actually used. The
cost already spent on the model calls that produced those templates is not refunded - a run that
stops this way stops after the expensive part, same as `Meter.complete` stopping a run that would
exceed its budget - but nothing partial is written, because a `copy_batch.json` that named messages
it could not actually produce would be a worse record than no file at all.

`pandas` is imported inside the functions that need it, never at module level, for the same reason
every other module in this package gives: `import engine` stays fast. Nothing in this module is
imported by `engine.pipeline` or `engine.stages`, and nothing here imports either of those two for
anything but reading the finished artefacts a predictive run already wrote - the dependency between
the predictive and generative halves of this engine runs one way (DEC-210).
"""

from __future__ import annotations

import io
import json
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from engine.config import CampaignCopyConfig, Channel, CopyLimits, GenerativeKind, UseCaseConfig, sole_key
from engine.contracts import DatasetProfile, RowExplanation, RunRecord, RunState
from engine.generative.budget import Meter
from engine.generative.contracts import (
    COPY_BATCH_FILENAME,
    COPY_MESSAGES_FILENAME,
    CopyAudience,
    CopyBatch,
    CopyHoldout,
    CopyMessage,
    CopyStatus,
    CopyTemplate,
    GenerativePurpose,
    GuardrailCheck,
    GuardrailOutcome,
)
from engine.generative.errors import (
    MISSING_FIELD,
    MODEL_OUTPUT_MALFORMED,
    NOT_A_GENERATIVE_USE_CASE,
    RUN_NOT_FINISHED,
    RUN_WITHOUT_SCORES,
    generative_error,
)
from engine.generative.guardrails import MAX_LENGTH, CheckContext, Guardrails
from engine.generative.prompts import load_prompt, prompt_hashes, prompt_versions, render
from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.stages.export import SCORES_CSV
from engine.storage import Storage, run_key, upload_key
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    import pandas as pd

__all__ = [
    "MAX_BAND_REASONS",
    "PROFILE_FILENAME",
    "RUN_RECORD_FILENAME",
    "UNSUBSCRIBE_FIELD",
    "VARIANT_LETTERS",
    "CampaignCopyResult",
    "allowed_placeholder_fields",
    "approve_template",
    "fill_placeholders",
    "generate_campaign_copy",
    "placeholders_in",
    "regenerate_template",
    "render_message",
    "unsupported_markup",
]

_LOGGER = get_logger(__name__)

RUN_RECORD_FILENAME: Final[str] = "run.json"
PROFILE_FILENAME: Final[str] = "profile.json"

UNSUBSCRIBE_FIELD: Final[str] = "unsubscribe_link"
"""The one placeholder an email template may use beyond `allowed_fields`.

An unsubscribe link is not customer data and it is not a feature the model reasoned about, but the
email prompt asks every template to end with `{{unsubscribe_link}}` on its own line, because Phase 1
of a real send fills it in per recipient and this engine does not send anything (`CopyBatch`'s own
docstring). Rendering it here does not build a real link either: a real one would be a URL, and
`configs/guardrails.yaml` ships `allowed_url_domains: []`, which blocks every URL until an operator
names a domain, so this module substitutes the field's own name back in - visible, harmless, and
still recognisable to whatever system sends the message later - rather than fail the one channel
that is supposed to carry it (DEC-201's `RequiredLines.email` default is this same string, chosen so
a template that keeps `{{unsubscribe_link}}` also satisfies the required-line check word for word).
"""

VARIANT_LETTERS: Final[str] = "ABCD"
"""Variant labels, in the order `CopyTemplate.variant` uses them; `campaign_copy.variants_per_band` caps how many."""

MAX_BAND_REASONS: Final[int] = 5
"""Reasons offered to a template prompt for one band. A prompt is read by a person reviewing copy,
not a dashboard, and five is already more than a marketer reads before choosing an angle."""

_CHANNEL_PROMPTS: Final[Mapping[Channel, str]] = {
    Channel.EMAIL: "copy_email",
    Channel.SMS: "copy_sms",
    Channel.WHATSAPP: "copy_whatsapp",
}
_CHANNEL_PURPOSES: Final[Mapping[Channel, GenerativePurpose]] = {
    Channel.EMAIL: GenerativePurpose.COPY_EMAIL,
    Channel.SMS: GenerativePurpose.COPY_SMS,
    Channel.WHATSAPP: GenerativePurpose.COPY_WHATSAPP,
}
_TEMPLATE_JUDGES: Final[tuple[str, ...]] = ("compliance", "toxicity")
_RESERVED_FIELDS: Final[frozenset[str]] = frozenset({"band", UNSUBSCRIBE_FIELD})
"""Placeholder names a template may use that are never read off the uploaded data."""

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_MARKUP: Final[re.Pattern[str]] = re.compile(r"{{.*?}}|{%.*?%}|{#.*?#}", re.DOTALL)
"""Every run of jinja2 markup, not only the ones that are a plain placeholder.

`_PLACEHOLDER` is what a template is *allowed* to contain, and on its own it is a filter rather than
a check: it silently sees nothing in `{{ 7*7 }}` or `{% for %}`, which the renderer then evaluates
anyway. Matching all three delimiter pairs is what lets `unsupported_markup` say "this reply is not
prose with placeholders" instead of quietly agreeing that it used no fields.
"""

_CODE_FENCE: Final[re.Pattern[str]] = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_RENDER_ENVIRONMENT: Final[SandboxedEnvironment] = SandboxedEnvironment(
    undefined=StrictUndefined, autoescape=False
)
"""Renders one template into one message. Sandboxed and strict for the reason `engine.generative.
prompts` gives its own environment: the text being rendered was written by a model, not an operator,
and a missing placeholder must fail loudly rather than render as an empty string a customer would
receive."""


@dataclass(frozen=True)
class CampaignCopyResult:
    """What one generation produced: the batch, and the rows of `copy_messages.csv` behind it."""

    batch: CopyBatch
    messages: tuple[CopyMessage, ...] = ()


# ---------------------------------------------------------------------------
# Rendering: a template plus one row's values, with no model involved
# ---------------------------------------------------------------------------
def placeholders_in(text: str) -> frozenset[str]:
    """Every `{{field}}` name in `text`, checked by regular expression rather than by asking the model.

    Half of "grounded or nothing" for copy: a template's own claim to use only allowed fields is
    worth nothing until something outside the model reads its reply and says so. The other half is
    `unsupported_markup`, because a name this function does not see is not a name the renderer will
    leave alone.
    """
    return frozenset(_PLACEHOLDER.findall(text))


def unsupported_markup(text: str) -> int:
    """How many runs of jinja2 markup in `text` are something other than a plain `{{field}}`.

    The allowed-fields check reads `{{field}}` names and nothing else, and a renderer that evaluated
    only those names would be safe. `_RENDER_ENVIRONMENT` evaluates whatever jinja2 accepts, so the
    two disagree about exactly the constructs a prompt forbids: `{{ 7*7 }}` names no field, passes
    the allowed-fields check with `fields_used` empty, and renders as an invented number in a
    customer's message - which is rule 2 of every copy prompt, broken by the one path that was
    supposed to enforce rule 1 in code. `{% for %}` is evaluated the same way, and `{{ a.b }}` and
    `{{ field|filter }}` reach `fill_placeholders` as names no row was ever checked for and raise
    there, aborting a batch that was already paid for.

    A count rather than the offending text: the text is a model's invention, the blocked template
    carries it verbatim for a reviewer to read, and a `block_reason` reaches a log and a screen.
    """
    return sum(1 for markup in _MARKUP.findall(text) if _PLACEHOLDER.fullmatch(markup) is None)


def allowed_placeholder_fields(config: CampaignCopyConfig, channel: Channel) -> frozenset[str]:
    """The full set of placeholders a template on `channel` may use: the configured fields, plus the
    unsubscribe merge field on email alone. Never customer data - only field *names*."""
    fields = set(config.allowed_fields)
    if channel is Channel.EMAIL:
        fields.add(UNSUBSCRIBE_FIELD)
    return frozenset(fields)


def fill_placeholders(text: str, values: Mapping[str, object]) -> str:
    """Render `text`'s `{{field}}` placeholders from `values`.

    A plain `jinja2` render and nothing more: the text was written by a model as prose with
    placeholders, never as a template with loops or filters, so no autoescaping and no environment
    beyond what `StrictUndefined` gives - a placeholder `values` does not cover raises immediately,
    which is the property `engine/generative/win_back.py`'s test suite pins directly.
    """
    return _RENDER_ENVIRONMENT.from_string(text).render(**values)


def _length_excess(channel: Channel, subject: str | None, text: str, limits: CopyLimits) -> str | None:
    """`None` when `text` (and `subject`, on email) clears its channel's hard limit, else why not.

    Checked here rather than left to the shared deterministic rules because email needs two
    different ceilings on two different strings - a character limit on the subject, a word limit on
    the body - and `engine.generative.guardrails.CheckContext` carries one text and one of each kind
    of ceiling. SMS and WhatsApp have a single ceiling on a single string and could use the shared
    rule; keeping all three channels here instead means one place states every channel's limit.
    """
    if channel is Channel.SMS and len(text) > limits.sms_chars:
        over = len(text) - limits.sms_chars
        return f"{over} characters over the {limits.sms_chars}-character SMS limit"
    if channel is Channel.WHATSAPP and len(text) > limits.whatsapp_chars:
        over = len(text) - limits.whatsapp_chars
        return f"{over} characters over the {limits.whatsapp_chars}-character WhatsApp limit"
    if channel is Channel.EMAIL:
        if subject is not None and len(subject) > limits.email_subject_chars:
            over = len(subject) - limits.email_subject_chars
            return f"the subject is {over} characters over the {limits.email_subject_chars}-character limit"
        words = len(text.split())
        if words > limits.email_body_words:
            over = words - limits.email_body_words
            return f"the body is {over} words over the {limits.email_body_words}-word limit"
    return None


def render_message(
    template: CopyTemplate,
    values: Mapping[str, object],
    *,
    entity_key: str,
    config: CampaignCopyConfig,
    guardrails: Guardrails,
) -> CopyMessage:
    """One rendering of `template` for one row, checked again by the deterministic rules only.

    `guardrails` is expected to have been built with `meter=None`: a rendering is not judged, because
    the prose was already judged once as a template and a placeholder cannot introduce a new claim,
    only a new length or a new value where a customer's own words might carry something the
    deterministic rules exist to catch (a banned word inside `last_offer`, an e-mail address inside a
    free-text field). `values` must already cover every name in `template.fields_used`; a missing one
    is a caller error and `fill_placeholders` raises loudly rather than guess.
    """
    rendered_subject = fill_placeholders(template.subject, values) if template.subject is not None else None
    rendered_text = fill_placeholders(template.text, values)
    check_text = rendered_text if rendered_subject is None else f"{rendered_subject}\n{rendered_text}"

    result = guardrails.check(
        check_text,
        CheckContext(
            target=template.template_id,
            required_line=config.required_lines.for_channel(Channel(template.channel)),
            allowed_fields=(),
            banned_phrases=config.banned_claims,
        ),
    )
    length_reason = _length_excess(Channel(template.channel), rendered_subject, rendered_text, config.limits)

    block_reason: str | None = None
    if length_reason is not None:
        block_reason = length_reason
    elif not result.passed:
        block_reason = f"failed the {result.blocked_by} check"

    whole = rendered_text if rendered_subject is None else f"Subject: {rendered_subject}\n\n{rendered_text}"
    return CopyMessage(
        entity_key=entity_key,
        band=template.band,
        channel=template.channel,
        variant=template.variant,
        template_id=template.template_id,
        rendered_text="" if block_reason is not None else whole,
        status=template.status,
        block_reason=block_reason,
    )


def _row_values(record: Mapping[str, object], template: CopyTemplate) -> dict[str, object]:
    """The render context for one row and one template: reserved fields synthesised, the rest read
    straight off the row. Never more than `template.fields_used` names, so a template that used three
    of five allowed fields never causes a fourth to be looked up."""
    values: dict[str, object] = {}
    for field in template.fields_used:
        if field == UNSUBSCRIBE_FIELD:
            values[field] = UNSUBSCRIBE_FIELD
        elif field == "band":
            values[field] = template.band
        else:
            values[field] = record[field]
    return values


# ---------------------------------------------------------------------------
# Templates: one model call per band and channel
# ---------------------------------------------------------------------------
def _limits_for(channel: Channel, limits: CopyLimits) -> dict[str, int]:
    """The subset of `limits` the prompt for `channel` actually names, in the shape its `# System`
    section reads with a dotted lookup."""
    if channel is Channel.SMS:
        return {"sms_chars": limits.sms_chars}
    if channel is Channel.WHATSAPP:
        return {"whatsapp_chars": limits.whatsapp_chars}
    return {"email_subject_chars": limits.email_subject_chars, "email_body_words": limits.email_body_words}


def _parse_variants(raw: str) -> list[dict[str, object]] | None:
    """The `variants` list of a copy prompt's reply, or `None` when the reply is not that shape.

    A code fence is stripped first, exactly as `engine.generative.assistant._json` does: a model
    asked for bare JSON supplies one fenced often enough that refusing over it would waste a retry on
    formatting rather than content.
    """
    try:
        payload = json.loads(_CODE_FENCE.sub("", raw).strip())
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    variants = payload.get("variants")
    if not isinstance(variants, list):
        return None
    return [entry for entry in variants if isinstance(entry, dict)]


def _finalize_template(
    *,
    template_id: str,
    band: str,
    channel: Channel,
    variant: str,
    subject: str | None,
    text: str,
    attempts: int,
    allowed: frozenset[str],
    config: CampaignCopyConfig,
    guardrails: Guardrails,
) -> CopyTemplate:
    """Check one candidate variant and return the `CopyTemplate` it becomes, stored or blocked.

    The two grounding checks run first and never reach the shared `Guardrails` at all when either
    fails: a template that names a field with no data behind it is not a text worth judging, and a
    judge call spent on it would be a judge call spent to confirm something a regular expression
    already knows for free (DEC-... `guardrails.py`'s own ordering rule, applied one layer up).

    Markup is checked before fields because it decides whether the field list means anything:
    `fields_used` is read off `{{field}}` placeholders, so a reply full of `{% %}` blocks or
    computed `{{ }}` expressions has an empty, entirely truthful-looking field list and would
    otherwise pass the check it most needs to fail.
    """
    markup = unsupported_markup(text) + (unsupported_markup(subject) if subject else 0)
    fields_used = tuple(
        sorted(placeholders_in(text) | (placeholders_in(subject) if subject else frozenset()))
    )
    unknown = sorted(set(fields_used) - allowed)
    if markup or unknown:
        reason = (
            f"used template markup that is not a plain placeholder, in {markup} place(s)"
            if markup
            else f"used a placeholder outside allowed_fields: {', '.join(unknown)}"
        )
        return CopyTemplate(
            template_id=template_id,
            band=band,
            channel=channel.value,
            variant=variant,
            subject=subject,
            text=text,
            fields_used=fields_used,
            status=CopyStatus.BLOCKED,
            judge_scores=(),
            guardrails=(),
            block_reason=reason,
            attempts=attempts,
            approved_by=None,
            approved_at=None,
        )

    check_text = text if subject is None else f"{subject}\n{text}"
    result = guardrails.check(
        check_text,
        CheckContext(
            target=template_id,
            required_line=config.required_lines.for_channel(channel),
            allowed_fields=tuple(allowed),
            banned_phrases=config.banned_claims,
            judges=_TEMPLATE_JUDGES,
            judge_values={
                "channel": channel.value,
                "tone": config.tone,
                "brand_name": config.brand_name,
                "allowed_fields": list(allowed),
                "banned_claims": list(config.banned_claims),
            },
        ),
    )
    length_reason = _length_excess(channel, subject, text, config.limits)
    checks = result.checks
    block_reason: str | None = None
    if length_reason is not None:
        block_reason = length_reason
        checks = (
            *checks,
            GuardrailCheck(
                target=template_id, rule=MAX_LENGTH, outcome=GuardrailOutcome.BLOCKED, detail=length_reason
            ),
        )
    elif not result.passed:
        block_reason = f"failed the {result.blocked_by} check"

    return CopyTemplate(
        template_id=template_id,
        band=band,
        channel=channel.value,
        variant=variant,
        subject=subject,
        text=text,
        fields_used=fields_used,
        status=CopyStatus.BLOCKED if block_reason is not None else CopyStatus.PENDING_REVIEW,
        judge_scores=result.judge_scores,
        guardrails=checks,
        block_reason=block_reason,
        attempts=attempts,
        approved_by=None,
        approved_at=None,
    )


def _generate_for_band_channel(
    *,
    band: str,
    band_action: str,
    channel: Channel,
    reasons: tuple[dict[str, str], ...],
    config: CampaignCopyConfig,
    entity: str,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
) -> tuple[CopyTemplate, ...]:
    """One band, one channel, one call - or, when every variant it produced is blocked, one retry at
    a time up to `guardrails.retries`. Never touches a scored row: everything this function sends a
    model is a band name, a list of field *names*, and aggregated reasons, none of which is any
    customer's data."""
    prompt_name = _CHANNEL_PROMPTS[channel]
    purpose = _CHANNEL_PURPOSES[channel]
    prompt = load_prompt(prompt_name, config_root)
    labels = tuple(VARIANT_LETTERS[: config.variants_per_band])
    allowed = allowed_placeholder_fields(config, channel)
    rendered = render(
        prompt,
        {
            "band": band,
            "band_action": band_action,
            "reasons": list(reasons),
            "tone": config.tone,
            "brand_name": config.brand_name,
            "allowed_fields": list(config.allowed_fields),
            "banned_claims": list(config.banned_claims),
            "limits": _limits_for(channel, config.limits),
            "variant_labels": list(labels),
            "entity": entity,
            "required_line": config.required_lines.for_channel(channel),
        },
    )

    attempts = 0
    while True:
        attempts += 1
        completion = meter.complete(rendered, purpose)
        variants = _parse_variants(completion.text)
        if variants is None:
            if attempts > guardrails.retries:
                raise generative_error(MODEL_OUTPUT_MALFORMED, prompt=prompt_name, attempts=attempts)
            continue
        templates = tuple(
            _finalize_template(
                template_id=f"{band}-{channel.value}-{label}",
                band=band,
                channel=channel,
                variant=label,
                subject=str(entry["subject"]) if channel is Channel.EMAIL and "subject" in entry else None,
                text=str(entry.get("body" if channel is Channel.EMAIL else "text") or ""),
                attempts=attempts,
                allowed=allowed,
                config=config,
                guardrails=guardrails,
            )
            for label, entry in zip(labels, variants, strict=False)
        )
        if any(t.status is not CopyStatus.BLOCKED for t in templates) or attempts > guardrails.retries:
            return templates


# ---------------------------------------------------------------------------
# Reading a finished run
# ---------------------------------------------------------------------------
def _read_scores(storage: Storage, key: str, primary_key: str) -> pd.DataFrame:
    """`scores.csv`, narrowed to the columns this module reads and never redraws."""
    import pandas as pd

    frame = pd.read_csv(
        io.StringIO(storage.read_text(key)),
        usecols=[primary_key, BAND_COLUMN, SUPPRESSED_REASON_COLUMN, CONTROL_GROUP_COLUMN],
    )
    frame[primary_key] = frame[primary_key].astype(str)
    frame[CONTROL_GROUP_COLUMN] = frame[CONTROL_GROUP_COLUMN].astype(bool)
    return frame


def _read_source_fields(
    storage: Storage, record: RunRecord, profile: DatasetProfile, fields: Sequence[str], primary_key: str
) -> pd.DataFrame:
    """The uploaded row values for `fields`, keyed by `primary_key`.

    A configured field absent from the upload altogether is not distinguished from one present with
    every cell blank: both become a column of nulls here, so `_check_field_coverage` catches "the
    column does not exist" and "the column exists but is empty for this band" with the same rule
    (which is also the one `MISSING_FIELD`'s own suggestion names: take the field out of
    `allowed_fields`, or put it in the data - both fixes read the same either way).
    """
    import pandas as pd

    key = upload_key(record.upload_id, f"source.{profile.file_format}")
    if profile.file_format == "parquet":
        with storage.open_read(key) as handle:
            raw = pd.read_parquet(handle)
    else:
        raw = pd.read_csv(io.StringIO(storage.read_text(key)))
    columns: dict[str, pd.Series] = {primary_key: raw[primary_key].astype(str)}
    for field in fields:
        columns[field] = raw[field] if field in raw.columns else pd.Series([None] * len(raw), dtype="object")
    return pd.DataFrame(columns)


def _classify(
    scores: pd.DataFrame, fields: pd.DataFrame, *, primary_key: str, bands_to_write: Sequence[str]
) -> tuple[pd.DataFrame, CopyHoldout, dict[str, int]]:
    """Every scored row sorted into exactly one of suppressed, control, out-of-band or eligible.

    Fixed precedence, suppressed first: a row Phase 1 refused to contact is refused for that reason
    however its band or its control flag reads, and a row held out as a control is held out whether
    or not its band is one copy was configured to write for. Nothing here draws anything; it reads
    `control_group` and `suppressed_reason` exactly as `engine.stages.actions.apply_actions` wrote
    them, seeded by the run id, so the same run always classifies the same way.
    """
    merged = scores.merge(fields, on=primary_key, how="left")
    suppressed = merged[SUPPRESSED_REASON_COLUMN].notna()
    control = merged[CONTROL_GROUP_COLUMN] & ~suppressed
    in_band = merged[BAND_COLUMN].isin(bands_to_write)
    out_of_band = ~suppressed & ~control & ~in_band
    eligible = ~suppressed & ~control & in_band

    holdout = CopyHoldout(
        control_rows=int(control.sum()),
        suppressed_rows=int(suppressed.sum()),
        out_of_band_rows=int(out_of_band.sum()),
    )
    audience = merged.loc[eligible].reset_index(drop=True)
    per_band = {band: int((audience[BAND_COLUMN] == band).sum()) for band in bands_to_write}
    per_band = {band: count for band, count in per_band.items() if count > 0}
    return audience, holdout, per_band


def _read_reasons(storage: Storage, record: RunRecord) -> dict[str, RowExplanation]:
    """Primary key -> the run's per-row explanation, or an empty map when the run carries none.

    `evaluation.shap: false` means no `row_explanations.parquet` was written at all - a legitimate
    state, not a failure, and a band whose reasons are then empty simply gets a prompt that names
    none, the same way `EvidencePack.complaints` is empty for a run without free text.
    """
    key = record.artefacts.get(ROW_EXPLANATIONS_FILENAME)
    if key is None:
        return {}
    return {
        explanation.primary_key: explanation for explanation in read_row_explanations(key, storage=storage)
    }


def _band_reasons(
    audience: pd.DataFrame, explanations: Mapping[str, RowExplanation], band: str, primary_key: str
) -> tuple[dict[str, str], ...]:
    """The `MAX_BAND_REASONS` (feature, direction) pairs most common among the band's rows' top
    reasons, most common first - what the template prompt calls "what these customers have in
    common", aggregated here so the model never sees an individual row's own reason."""
    if not explanations:
        return ()
    counts: Counter[tuple[str, str]] = Counter()
    for key in audience.loc[audience[BAND_COLUMN] == band, primary_key]:
        explanation = explanations.get(str(key))
        if explanation is None or not explanation.reasons:
            continue
        top = explanation.reasons[0]
        counts[(top.feature, top.direction.value)] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        {"feature": feature, "direction": direction} for (feature, direction), _n in ranked[:MAX_BAND_REASONS]
    )


def _check_field_coverage(
    audience: pd.DataFrame,
    templates: Iterable[CopyTemplate],
    *,
    primary_key: str,
    source_fields: frozenset[str],
) -> None:
    """Every non-blocked template's fields are covered by every row it would be rendered for, or the
    batch is refused before a message is rendered or anything is written (see the module docstring)."""
    del primary_key
    for template in templates:
        if template.status is CopyStatus.BLOCKED:
            continue
        band_rows = audience.loc[audience[BAND_COLUMN] == template.band]
        for field in template.fields_used:
            if field not in source_fields:
                continue
            missing = int(band_rows[field].isna().sum())
            if missing:
                raise generative_error(MISSING_FIELD, rows=missing, field=field)


def _write_messages_csv(storage: Storage, run_id: str, messages: Sequence[CopyMessage]) -> None:
    """`copy_messages.csv`, one row per `CopyMessage`, in the field order the contract declares."""
    import pandas as pd

    columns = list(CopyMessage.model_fields)
    rows = [message.model_dump(mode="json") for message in messages]
    frame = pd.DataFrame(rows, columns=columns)
    storage.write_text(
        run_key(run_id, COPY_MESSAGES_FILENAME), frame.to_csv(index=False, lineterminator="\n")
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def generate_campaign_copy(
    *,
    run_id: str,
    use_case: UseCaseConfig,
    storage: Storage,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None = None,
    now: datetime | None = None,
) -> CampaignCopyResult:
    """Write templates and messages over `run_id`'s finished scores, and return what was written.

    Writes `copy_batch.json` and `copy_messages.csv` into the run's own directory and returns both as
    `CampaignCopyResult`, mirroring `engine.generative.index.build_index`: the artefact a caller
    would otherwise have to read straight back. Raises `RUN_NOT_FINISHED` or `RUN_WITHOUT_SCORES`
    before a model is called at all when the run cannot supply what copy needs, and `MISSING_FIELD`
    after every template is generated but before anything is written, if a surviving template would
    be rendered for a row the data cannot fill it from.
    """
    started = time.monotonic()
    if use_case.generative.kind is not GenerativeKind.CAMPAIGN_COPY:
        raise generative_error(
            NOT_A_GENERATIVE_USE_CASE, use_case_id=use_case.id, kind=use_case.generative.kind.value
        )

    record = storage.read_model(run_key(run_id, RUN_RECORD_FILENAME), RunRecord)
    if record.state is not RunState.DONE:
        raise generative_error(RUN_NOT_FINISHED, run_id=run_id, state=record.state.value)
    scores_key = record.artefacts.get(SCORES_CSV)
    if scores_key is None:
        raise generative_error(RUN_WITHOUT_SCORES, run_id=run_id)

    copy = use_case.generative.campaign_copy
    primary_key = sole_key(record.primary_key, what=f"Campaign copy for run {run_id}")
    profile = storage.read_model(record.artefacts[PROFILE_FILENAME], DatasetProfile)
    source_fields = tuple(field for field in copy.allowed_fields if field not in _RESERVED_FIELDS)

    scores = _read_scores(storage, scores_key, primary_key)
    fields = _read_source_fields(storage, record, profile, source_fields, primary_key)
    audience, holdout, per_band = _classify(
        scores, fields, primary_key=primary_key, bands_to_write=copy.bands_to_write
    )
    explanations = _read_reasons(storage, record)
    band_actions = {band.name: band.action for band in use_case.actions.bands}

    templates: list[CopyTemplate] = []
    for band in sorted(per_band):
        reasons = _band_reasons(audience, explanations, band, primary_key)
        for channel in copy.channels:
            templates.extend(
                _generate_for_band_channel(
                    band=band,
                    band_action=band_actions.get(band, band),
                    channel=channel,
                    reasons=reasons,
                    config=copy,
                    entity=use_case.entity,
                    meter=meter,
                    guardrails=guardrails,
                    config_root=config_root,
                )
            )

    _check_field_coverage(
        audience, templates, primary_key=primary_key, source_fields=frozenset(source_fields)
    )

    messages: list[CopyMessage] = []
    for template in templates:
        if template.status is CopyStatus.BLOCKED:
            continue
        band_rows = audience.loc[audience[BAND_COLUMN] == template.band, (primary_key, *source_fields)]
        for row in band_rows.to_dict("records"):
            # `to_dict` is typed as `dict[Hashable, Any]` because a frame's columns need not be
            # strings. These are: they come from `primary_key` and `source_fields`, both of which
            # are configured names.
            row_map: dict[str, object] = {str(key): value for key, value in row.items()}
            entity_key = str(row_map[primary_key])
            values = _row_values(row_map, template)
            messages.append(
                render_message(template, values, entity_key=entity_key, config=copy, guardrails=guardrails)
            )

    used_names = sorted({_CHANNEL_PROMPTS[channel] for channel in copy.channels}) if per_band else []
    finished = now if now is not None else utc_now()
    sorted_templates = tuple(sorted(templates, key=lambda t: (t.band, t.channel, t.variant)))
    sorted_messages = tuple(sorted(messages, key=lambda m: (m.band, m.channel, m.variant, m.entity_key)))
    batch = CopyBatch(
        run_id=run_id,
        batch_id=f"cb_{run_id}",
        audience=CopyAudience(rows=sum(per_band.values()), per_band=per_band),
        holdout=holdout,
        templates=sorted_templates,
        require_human_review=copy.require_human_review,
        messages_rendered=len(sorted_messages),
        messages_blocked=sum(1 for message in sorted_messages if message.block_reason is not None),
        prompt_versions=prompt_versions(used_names, config_root),
        prompt_hashes=prompt_hashes(used_names, config_root),
        created_at=finished,
    )
    storage.write_model(run_key(run_id, COPY_BATCH_FILENAME), batch)
    _write_messages_csv(storage, run_id, sorted_messages)
    log_stage(
        _LOGGER,
        "win_back.generate_campaign_copy",
        rows=len(sorted_messages),
        seconds=time.monotonic() - started,
    )
    return CampaignCopyResult(batch=batch, messages=sorted_messages)


def approve_template(
    batch: CopyBatch, template_id: str, *, approved_by: str, now: datetime | None = None
) -> CopyBatch:
    """`batch` with one template marked approved by `approved_by`; every other template unchanged.

    `CopyBatch` and `CopyTemplate` are frozen, so approval is a new `CopyBatch` rather than a
    mutation - the caller writes it back with `storage.write_model` exactly as `generate_campaign_copy`
    wrote the original, and the previous version is simply what `copy_batch.json` held before that
    write, not a state this function tracks.

    A blocked template is refused rather than approved. `model_copy(update=...)` does not revalidate,
    so approving one would write `status: approved` beside the `block_reason` that says why it was
    not stored - a text a guardrail refused, recorded as one a person signed off. Which template is
    blocked is the caller's to check before offering it; `ValueError` says the caller did not.
    """
    found = False
    updated: list[CopyTemplate] = []
    for template in batch.templates:
        if template.template_id != template_id:
            updated.append(template)
            continue
        if template.status is CopyStatus.BLOCKED:
            raise ValueError(f"{template_id!r} was blocked and cannot be approved.")
        found = True
        updated.append(
            template.model_copy(
                update={
                    "status": CopyStatus.APPROVED,
                    "approved_by": approved_by,
                    "approved_at": now if now is not None else utc_now(),
                }
            )
        )
    if not found:
        raise KeyError(f"{batch.batch_id} has no template {template_id!r} to approve.")
    return batch.model_copy(update={"templates": tuple(updated)})


def regenerate_template(
    batch: CopyBatch,
    template_id: str,
    *,
    run_id: str,
    use_case: UseCaseConfig,
    storage: Storage,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None = None,
) -> CopyTemplate:
    """One fresh `CopyTemplate` in `template_id`'s place: the API's `POST .../regenerate` in full.

    A template is not generated alone - `_generate_for_band_channel` asks the model for every
    variant of one band and one channel in a single call, because that is the unit a prompt reasons
    about ("write A, B and C for the High band on email") - so there is no cheaper way to redo one
    variant than to redo that whole call and keep only the one this function was asked for. The
    audience, the band's aggregated reasons and the allowed-fields set are therefore rebuilt exactly
    as `generate_campaign_copy` built them the first time, from the run's own scores and explanations
    rather than from anything cached, so a regenerate reflects the run as it stands now.

    The replacement keeps `template_id`'s own id rather than minting a new one - `docs/generative-
    ui-endpoints.md` allows either, and the UI already matches a response back into its grid by
    whichever id comes back, so keeping it is one fewer thing for a caller to reconcile. `attempts`
    is the sum of what the original template had already spent and what this call spent: neither
    number alone would answer "how many generations has this variant cost in total", which is the
    question a reviewer staring at a still-blocked template after two regenerates is actually asking.

    Raises `KeyError` when `template_id` is not in `batch` - the same signal `approve_template` gives
    for the same condition, so a caller checks one exception type for "no such template" either way.
    """
    target = next((template for template in batch.templates if template.template_id == template_id), None)
    if target is None:
        raise KeyError(f"{batch.batch_id} has no template {template_id!r} to regenerate.")

    copy = use_case.generative.campaign_copy
    record = storage.read_model(run_key(run_id, RUN_RECORD_FILENAME), RunRecord)
    primary_key = sole_key(record.primary_key, what=f"Campaign copy for run {run_id}")
    profile = storage.read_model(record.artefacts[PROFILE_FILENAME], DatasetProfile)
    source_fields = tuple(field for field in copy.allowed_fields if field not in _RESERVED_FIELDS)

    scores = _read_scores(storage, record.artefacts[SCORES_CSV], primary_key)
    fields = _read_source_fields(storage, record, profile, source_fields, primary_key)
    audience, _holdout, _per_band = _classify(
        scores, fields, primary_key=primary_key, bands_to_write=copy.bands_to_write
    )
    explanations = _read_reasons(storage, record)
    band_actions = {band.name: band.action for band in use_case.actions.bands}
    reasons = _band_reasons(audience, explanations, target.band, primary_key)
    channel = Channel(target.channel)

    fresh = _generate_for_band_channel(
        band=target.band,
        band_action=band_actions.get(target.band, target.band),
        channel=channel,
        reasons=reasons,
        config=copy,
        entity=use_case.entity,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )
    replacement = next((template for template in fresh if template.variant == target.variant), fresh[0])
    return replacement.model_copy(
        update={"template_id": target.template_id, "attempts": target.attempts + replacement.attempts}
    )
