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

**A band is one way to cut an audience; `campaign_copy.segment_by` picks another (DEC-1240).** `band`
is the default and the original behaviour, output for output and prompt for prompt. `top_reason`
cuts the eligible rows by each row's own strongest SHAP reason with the grouping root-cause summaries
use (`engine.generative.segments`), at most `max_segments` of them with an "other" bucket last.
`uplift_segment` writes for an uplift scoring run's persuadables only, and counts sure things, lost
causes and sleeping dogs as rows deliberately not written to, after suppression and the control
group and by the same fixed precedence. Either way the cost is O(segments x channels), the prompt is
a `copy_segment_*` prompt built from a stored `CopySegment` - a label, the rule that formed it and
aggregate figures, never a row - and each row is rendered from its own segment's template.

`pandas` is imported inside the functions that need it, never at module level, for the same reason
every other module in this package gives: `import engine` stays fast. Nothing in this module is
imported by `engine.pipeline` or `engine.stages`, and nothing here imports either of those two for
anything but reading the finished artefacts a predictive run already wrote - the dependency between
the predictive and generative halves of this engine runs one way (DEC-210).
"""

from __future__ import annotations

import csv
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

from engine.config import (
    CampaignCopyConfig,
    Channel,
    CopyLimits,
    CopySegmentBy,
    GenerativeKind,
    ProblemType,
    UseCaseConfig,
    sole_key,
)
from engine.contracts import DatasetProfile, RowExplanation, RunRecord, RunState
from engine.generative.budget import Meter
from engine.generative.contracts import (
    COPY_BATCH_FILENAME,
    COPY_MESSAGES_FILENAME,
    CopyAudience,
    CopyBatch,
    CopyHoldout,
    CopyMessage,
    CopySegment,
    CopyStatus,
    CopyTemplate,
    EvidenceReason,
    GenerativePurpose,
    GuardrailCheck,
    GuardrailOutcome,
)
from engine.generative.errors import (
    COPY_NEEDS_UPLIFT_RUN,
    MISSING_FIELD,
    MODEL_OUTPUT_MALFORMED,
    NOT_A_GENERATIVE_USE_CASE,
    RUN_NOT_FINISHED,
    RUN_WITHOUT_EXPLANATIONS,
    RUN_WITHOUT_SCORES,
    generative_error,
)
from engine.generative.guardrails import MAX_LENGTH, CheckContext, Guardrails
from engine.generative.prompts import load_prompt, prompt_hashes, prompt_versions, render
from engine.generative.segments import cap_with_other, group_keys, top_reason_feature
from engine.onboarding.datasets import run_source_key
from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
from engine.stages.explain import ROW_EXPLANATIONS_FILENAME, read_row_explanations
from engine.stages.export import SCORES_CSV
from engine.storage import Storage, run_key
from engine.uplift.actions import INTENDED_TREATMENT_COLUMN, SEGMENT_COLUMN
from engine.uplift.contracts import SEGMENT_ACTIONS, SEGMENT_LABELS, Segment
from engine.utils.logging import get_logger, log_stage
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    import pandas as pd

__all__ = [
    "MAX_BAND_REASONS",
    "OPT_OUT_LINK_FIELD",
    "OTHER_REASONS_SEGMENT",
    "OVER_BUDGET_SEGMENT",
    "PROFILE_FILENAME",
    "RUN_RECORD_FILENAME",
    "UNSUBSCRIBE_FIELD",
    "VARIANT_LETTERS",
    "CampaignCopyResult",
    "allowed_placeholder_fields",
    "approve_template",
    "fill_placeholders",
    "generate_campaign_copy",
    "is_uplift_scoring_run",
    "placeholders_in",
    "regenerate_template",
    "render_message",
    "required_line_for",
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

OPT_OUT_LINK_FIELD: Final[str] = "opt_out_link"
"""The SMS counterpart of `UNSUBSCRIBE_FIELD`, for a one-way sender ID (`campaign_copy.sms_sender:
one_way`, M91).

"Reply STOP to opt out" cannot work when the sender ID cannot receive replies (India's DLT headers,
for one), so the SMS required line becomes `{{opt_out_link}}` and the message sends the customer to a
link instead. It is allowed on SMS only, and only when `sms_sender` is `one_way`. Rendering here does
not build a real link, for the reason `UNSUBSCRIBE_FIELD` gives: it is filled with its own name for
whatever system sends the message to replace per recipient.
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
_SEGMENT_PROMPTS: Final[Mapping[Channel, str]] = {
    Channel.EMAIL: "copy_segment_email",
    Channel.SMS: "copy_segment_sms",
    Channel.WHATSAPP: "copy_segment_whatsapp",
}
"""The prompts for copy written per segment (DEC-1244). Separate files rather than a branch inside the
band prompts, so a batch written per band renders - and hashes - exactly the prompt it always did."""
_SEGMENT_PURPOSES: Final[Mapping[Channel, GenerativePurpose]] = {
    Channel.EMAIL: GenerativePurpose.COPY_SEGMENT_EMAIL,
    Channel.SMS: GenerativePurpose.COPY_SEGMENT_SMS,
    Channel.WHATSAPP: GenerativePurpose.COPY_SEGMENT_WHATSAPP,
}

OTHER_REASONS_SEGMENT: Final[str] = "other_reasons"
"""The id of the `top_reason` bucket that holds every reason too rare for a segment of its own."""

OVER_BUDGET_SEGMENT: Final[str] = "persuadable_over_budget"
"""The `uplift_segment` segment of persuadables the policy left outside the contact budget (DEC-1243)."""

_REASON_SEGMENT_PREFIX: Final[str] = "reason_"
_UNIT: Final[str] = "__copy_unit__"
"""Column added to the audience frame naming the band or segment each row's copy is written for."""

_UPLIFT_SCORE_COLUMN: Final[str] = "uplift"
"""`engine.uplift.flow.UPLIFT_COLUMN`, spelled here because importing that module would import
`engine.pipeline`, which nothing in this package does (DEC-210)."""

_SHARE_DECIMALS: Final[int] = 1
_SCORE_DECIMALS: Final[int] = 4

_TEMPLATE_JUDGES: Final[tuple[str, ...]] = ("compliance", "toxicity")
_RESERVED_FIELDS: Final[frozenset[str]] = frozenset({"band", UNSUBSCRIBE_FIELD, OPT_OUT_LINK_FIELD})
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
    unsubscribe merge field on email and the opt-out link on SMS sent from a one-way sender ID. Never
    customer data - only field *names*."""
    fields = set(config.allowed_fields)
    if channel is Channel.EMAIL:
        fields.add(UNSUBSCRIBE_FIELD)
    if _one_way_sms(config, channel):
        fields.add(OPT_OUT_LINK_FIELD)
    return frozenset(fields)


def _prompt_allowed_fields(config: CampaignCopyConfig, channel: Channel) -> list[str]:
    """The "Placeholders you may use" list a prompt shows. On one-way SMS it names `opt_out_link`, which
    the required line asks for: the v1 SMS prompt cannot name it in its own text, and a list that left it
    out would contradict the line (rule 1, "only allowed fields", outranks rule 7). Otherwise unchanged."""
    fields = list(config.allowed_fields)
    if _one_way_sms(config, channel) and OPT_OUT_LINK_FIELD not in fields:
        fields.append(OPT_OUT_LINK_FIELD)
    return fields


def _one_way_sms(config: CampaignCopyConfig, channel: Channel) -> bool:
    """True for SMS from a sender ID that cannot receive replies (`campaign_copy.sms_sender`)."""
    return channel is Channel.SMS and config.sms_sender == "one_way"


def required_line_for(config: CampaignCopyConfig, channel: Channel) -> str:
    """The line a template on `channel` must carry: `required_lines`, except that a one-way SMS sender
    cannot ask for a reply, so its line *is* the `{{opt_out_link}}` placeholder, whatever wording the
    operator gave (the default is "Reply STOP to opt out"). The one line kept is an operator's own that
    already carries the placeholder, e.g. "Unsubscribe: {{opt_out_link}}"."""
    line = config.required_lines.for_channel(channel)
    if _one_way_sms(config, channel):
        if OPT_OUT_LINK_FIELD in _PLACEHOLDER.findall(line):
            return normalise_placeholders(line)
        return "{{" + OPT_OUT_LINK_FIELD + "}}"
    return line


def normalise_placeholders(text: str) -> str:
    """`text` with every placeholder written `{{name}}`: `{{ name }}` is a placeholder too (it is
    parsed and filled), but a required line is matched as exact text."""
    return _PLACEHOLDER.sub(lambda match: "{{" + match.group(1) + "}}", text)


def _rendered_line_for(config: CampaignCopyConfig, channel: Channel) -> str:
    """What `required_line_for` reads like once a message is rendered. A merge-field line is written
    into a message as its own name (see `UNSUBSCRIBE_FIELD`), so the rendered check looks for the name."""
    return _PLACEHOLDER.sub(lambda match: match.group(1), required_line_for(config, channel))


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
    backend: str,
    band: str | None = None,
) -> CopyMessage:
    """One rendering of `template` for one row, checked again by the deterministic rules only.

    `band` is the row's own band, which differs from `template.band` only for a template written per
    segment (a segment can span bands); left out, the message carries the template's band.

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
            required_line=_rendered_line_for(config, Channel(template.channel)),
            one_way_sender=_one_way_sms(config, Channel(template.channel)),
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
        band=band if band is not None else template.band,
        channel=template.channel,
        variant=template.variant,
        template_id=template.template_id,
        rendered_text="" if block_reason is not None else whole,
        status=template.status,
        block_reason=block_reason,
        segment=template.segment,
        backend=backend,
    )


def _band_text(value: object) -> str | None:
    """A row's band as text, or `None` when the row has none (a missing cell reads back as NaN)."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    return str(value)


def _row_values(record: Mapping[str, object], template: CopyTemplate) -> dict[str, object]:
    """The render context for one row and one template: reserved fields synthesised, the rest read
    straight off the row. Never more than `template.fields_used` names, so a template that used three
    of five allowed fields never causes a fourth to be looked up. `{{band}}` is the row's own band
    when the row carries one - the template's band per band, and the only honest answer when a
    segment's template is rendered for rows from several bands."""
    values: dict[str, object] = {}
    for field in template.fields_used:
        if field in (UNSUBSCRIBE_FIELD, OPT_OUT_LINK_FIELD):
            values[field] = field
        elif field == "band":
            values[field] = record.get(BAND_COLUMN, template.band)
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
    segment: str | None = None,
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
            segment=segment,
        )

    check_text = text if subject is None else f"{subject}\n{text}"
    if _one_way_sms(config, channel):
        # The required line is matched as exact text, so `{{ opt_out_link }}` is read as `{{opt_out_link}}`.
        check_text = normalise_placeholders(check_text)
    result = guardrails.check(
        check_text,
        CheckContext(
            target=template_id,
            required_line=required_line_for(config, channel),
            one_way_sender=_one_way_sms(config, channel),
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
        segment=segment,
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
    labels = tuple(VARIANT_LETTERS[: config.variants_per_band])
    return _generate_templates(
        prompt_name=_CHANNEL_PROMPTS[channel],
        purpose=_CHANNEL_PURPOSES[channel],
        values={
            "band": band,
            "band_action": band_action,
            "reasons": list(reasons),
            "tone": config.tone,
            "brand_name": config.brand_name,
            "allowed_fields": _prompt_allowed_fields(config, channel),
            "banned_claims": list(config.banned_claims),
            "limits": _limits_for(channel, config.limits),
            "variant_labels": list(labels),
            "entity": entity,
            "required_line": required_line_for(config, channel),
        },
        unit=band,
        segment=None,
        channel=channel,
        labels=labels,
        config=config,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )


def _generate_for_segment_channel(
    *,
    segment: CopySegment,
    segment_by: CopySegmentBy,
    channel: Channel,
    config: CampaignCopyConfig,
    entity: str,
    score_name: str,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
) -> tuple[CopyTemplate, ...]:
    """One segment, one channel, one call (plus retries), exactly as `_generate_for_band_channel`.

    What the prompt sees is built from `segment` alone - its label, a sentence saying how it was
    formed, the engine's own aggregate figures about it and its aggregated reasons - all of it counted
    or averaged over the segment's rows by `_plan`, none of it any row's value (DEC-1245). Because
    the inputs are the stored `CopySegment`, a regenerate re-asks exactly what the first call asked.
    """
    labels = tuple(VARIANT_LETTERS[: config.variants_per_band])
    return _generate_templates(
        prompt_name=_SEGMENT_PROMPTS[channel],
        purpose=_SEGMENT_PURPOSES[channel],
        values={
            "segment": segment.label,
            "segment_rule": _segment_rule(segment_by, segment.segment, entity),
            "figures": list(_segment_figures(segment, entity=entity, score_name=score_name)),
            "reasons": [
                {"feature": reason.feature, "direction": reason.direction.value} for reason in segment.reasons
            ],
            "tone": config.tone,
            "brand_name": config.brand_name,
            "allowed_fields": _prompt_allowed_fields(config, channel),
            "banned_claims": list(config.banned_claims),
            "limits": _limits_for(channel, config.limits),
            "variant_labels": list(labels),
            "entity": entity,
            "required_line": required_line_for(config, channel),
        },
        unit=segment.segment,
        segment=segment.segment,
        channel=channel,
        labels=labels,
        config=config,
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )


def _generate_templates(
    *,
    prompt_name: str,
    purpose: GenerativePurpose,
    values: Mapping[str, object],
    unit: str,
    segment: str | None,
    channel: Channel,
    labels: tuple[str, ...],
    config: CampaignCopyConfig,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
) -> tuple[CopyTemplate, ...]:
    """The call-parse-check-retry loop both kinds of template share; `unit` names the band or segment."""
    prompt = load_prompt(prompt_name, config_root)
    allowed = allowed_placeholder_fields(config, channel)
    rendered = render(prompt, values)

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
                template_id=f"{unit}-{channel.value}-{label}",
                band=unit,
                channel=channel,
                variant=label,
                subject=str(entry["subject"]) if channel is Channel.EMAIL and "subject" in entry else None,
                text=str(entry.get("body" if channel is Channel.EMAIL else "text") or ""),
                attempts=attempts,
                allowed=allowed,
                config=config,
                guardrails=guardrails,
                segment=segment,
            )
            for label, entry in zip(labels, variants, strict=False)
        )
        if any(t.status is not CopyStatus.BLOCKED for t in templates) or attempts > guardrails.retries:
            return templates


# ---------------------------------------------------------------------------
# Reading a finished run
# ---------------------------------------------------------------------------
def _read_scores(storage: Storage, key: str, primary_key: str, extra: Sequence[str] = ()) -> pd.DataFrame:
    """`scores.csv`, narrowed to the columns this module reads and never redraws.

    `extra` names columns read too when the file has them - the score a segment's average is taken
    over, and an uplift run's `segment`. A batch written per band asks for none, and reads exactly
    the four columns it always read.
    """
    import pandas as pd

    text = storage.read_text(key)
    columns = [primary_key, BAND_COLUMN, SUPPRESSED_REASON_COLUMN, CONTROL_GROUP_COLUMN]
    if extra:
        header = set(pd.read_csv(io.StringIO(text), nrows=0).columns)
        columns += [column for column in extra if column in header and column not in columns]
    frame = pd.read_csv(io.StringIO(text), usecols=columns)
    frame[primary_key] = frame[primary_key].astype(str)
    frame[CONTROL_GROUP_COLUMN] = frame[CONTROL_GROUP_COLUMN].astype(bool)
    return frame


def is_uplift_scoring_run(storage: Storage, record: RunRecord) -> bool:
    """Whether `record` scored an uplift model, so its `scores.csv` carries a `segment` per row.

    Both are checked: `run.json` says `uplift` once the uplift score flow knows what it scored, and
    the scores header is what `segment_by: uplift_segment` actually reads. Only the header line is
    read, so the route can ask before it queues a job (DEC-1243).
    """
    if record.problem_type is not ProblemType.UPLIFT:
        return False
    key = record.artefacts.get(SCORES_CSV)
    if key is None:
        return False
    with storage.open_read(key) as handle:
        first = handle.readline().decode("utf-8")
    header = next(csv.reader([first]), [])
    return SEGMENT_COLUMN in header


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

    key = run_source_key(record, profile.file_format)
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


def _unit_of(template: CopyTemplate) -> str:
    """The band or segment a template was written for: its `segment` when it has one, else its band."""
    return template.segment if template.segment is not None else template.band


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
        unit_rows = audience.loc[audience[_UNIT] == _unit_of(template)]
        for field in template.fields_used:
            if field not in source_fields:
                continue
            missing = int(unit_rows[field].isna().sum())
            if missing:
                raise generative_error(MISSING_FIELD, rows=missing, field=field)


def _write_messages_csv(storage: Storage, run_id: str, messages: Sequence[CopyMessage]) -> None:
    """`copy_messages.csv`, one row per `CopyMessage`, in the field order the contract declares.

    `segment` is a column only when a message has one: a batch written per band writes the columns
    it always wrote, byte for byte (DEC-1242).
    """
    import pandas as pd

    columns = list(CopyMessage.model_fields)
    if all(message.segment is None for message in messages):
        columns.remove("segment")
    rows = [message.model_dump(mode="json") for message in messages]
    frame = pd.DataFrame(rows, columns=columns)
    storage.write_text(
        run_key(run_id, COPY_MESSAGES_FILENAME), frame.to_csv(index=False, lineterminator="\n")
    )


# ---------------------------------------------------------------------------
# Planning: which band or segment each eligible row's copy is written for
# ---------------------------------------------------------------------------
def _classify_uplift(
    scores: pd.DataFrame, fields: pd.DataFrame, *, primary_key: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, CopyHoldout, bool]:
    """`_classify` for an uplift run: suppressed, then control, then not a persuadable, then over
    the contact budget, then eligible.

    The same fixed precedence and the same promise - every row counted once, with exactly one stated
    reason - with `segment` in the place `bands_to_write` holds per band: a sure thing, a lost cause or
    a sleeping dog gets no message whatever its band, because the uplift model already said a contact
    is wasted on it or makes things worse; and a persuadable the policy did not choose
    (`intended_treatment` false) is over the contact budget and gets none either (DEC-1243). A scores
    file without `intended_treatment` cannot say who is inside the budget, so every persuadable is
    eligible and the last value returned, whether the budget was applied, is `False`.

    Returns the eligible persuadables, the rows that are not persuadables, the persuadables over
    budget, the holdout and whether the budget was applied.
    """
    import pandas as pd

    merged = scores.merge(fields, on=primary_key, how="left")
    suppressed = merged[SUPPRESSED_REASON_COLUMN].notna()
    control = merged[CONTROL_GROUP_COLUMN] & ~suppressed
    persuadable = merged[SEGMENT_COLUMN] == Segment.PERSUADABLE.value
    budget_applied = INTENDED_TREATMENT_COLUMN in merged.columns
    if budget_applied:
        intended = merged[INTENDED_TREATMENT_COLUMN].fillna(False).astype(bool)
    else:
        intended = pd.Series(True, index=merged.index)
    left = ~suppressed & ~control
    not_persuadable = left & ~persuadable
    over_budget = left & persuadable & ~intended
    eligible = left & persuadable & intended
    holdout = CopyHoldout(
        control_rows=int(control.sum()),
        suppressed_rows=int(suppressed.sum()),
        out_of_band_rows=0,
        not_persuadable_rows=int(not_persuadable.sum()),
        outside_budget_rows=int(over_budget.sum()) if budget_applied else None,
    )
    return (
        merged.loc[eligible].reset_index(drop=True),
        merged.loc[not_persuadable].reset_index(drop=True),
        merged.loc[over_budget].reset_index(drop=True),
        holdout,
        budget_applied,
    )


@dataclass(frozen=True)
class _Unit:
    """One band or segment templates are written for: the band's action and reasons per band, the
    stored `CopySegment` per segment."""

    unit_id: str
    band_action: str = ""
    reasons: tuple[dict[str, str], ...] = ()
    segment: CopySegment | None = None

    @property
    def written(self) -> bool:
        return self.segment is None or self.segment.written


@dataclass(frozen=True)
class _Plan:
    """Everything `generate_campaign_copy` needs before its first model call, built by `_plan`."""

    primary_key: str
    source_fields: tuple[str, ...]
    audience: pd.DataFrame
    holdout: CopyHoldout
    per_band: dict[str, int]
    units: tuple[_Unit, ...]
    explanations: Mapping[str, RowExplanation]
    budget_applied: bool | None = None


def _score_name(segment_by: CopySegmentBy, use_case: UseCaseConfig) -> str:
    """What a segment's average is an average of, in the words its prompt uses."""
    if segment_by is CopySegmentBy.UPLIFT_SEGMENT:
        return "predicted uplift"
    return use_case.actions.score_field


def _score_column(segment_by: CopySegmentBy, use_case: UseCaseConfig) -> str:
    """The `scores.csv` column a segment's average is taken over."""
    if segment_by is CopySegmentBy.UPLIFT_SEGMENT:
        return _UPLIFT_SCORE_COLUMN
    return use_case.actions.score_field


def _segment_rule(segment_by: CopySegmentBy, segment_id: str, entity: str) -> str:
    """One sentence saying how a segment was formed - the rule, never a row."""
    if segment_by is CopySegmentBy.UPLIFT_SEGMENT:
        return (
            f"an uplift model predicts that contacting these {entity}s makes them more likely to "
            "respond (persuadables); sure things, lost causes and sleeping dogs get no message"
        )
    if segment_id == OTHER_REASONS_SEGMENT:
        return (
            f"{entity}s whose strongest reason is shared by too few others to have a segment of its "
            "own, together with any whose reasons were not measured"
        )
    feature = segment_id.removeprefix(_REASON_SEGMENT_PREFIX)
    return f"every {entity} in it has {feature} as the strongest reason behind their score"


def _segment_figures(segment: CopySegment, *, entity: str, score_name: str) -> tuple[str, ...]:
    """The aggregate figures a segment's prompt shows: its size, its share, its bands, its average."""
    figures = [f"{segment.rows} {entity}s, {segment.share_pct}% of those this campaign writes to"]
    if segment.per_band:
        by_band = ", ".join(f"{band} {count}" for band, count in sorted(segment.per_band.items()))
        figures.append(f"By band: {by_band}")
    if segment.mean_score is not None:
        figures.append(f"Average {score_name}: {segment.mean_score}")
    return tuple(figures)


def _share(part: int, whole: int) -> float:
    """`part` as a percentage of `whole`; an empty `whole` is zero, not a division error."""
    return round(100.0 * part / whole, _SHARE_DECIMALS) if whole > 0 else 0.0


def _per_band(rows: pd.DataFrame) -> dict[str, int]:
    """Rows per band name, bands sorted; a row with no band is not counted in any."""
    counts = rows[BAND_COLUMN].dropna().astype(str).value_counts()
    return {str(band): int(counts[band]) for band in sorted(counts.index)}


def _mean_score(rows: pd.DataFrame, column: str) -> float | None:
    """The segment's average score, or `None` when the run has no such column or no such rows."""
    if column not in rows.columns or rows.empty:
        return None
    values = rows[column].dropna()
    if values.empty:
        return None
    return round(float(values.astype(float).mean()), _SCORE_DECIMALS)


def _segment_reasons(
    keys: Sequence[str], explanations: Mapping[str, RowExplanation], *, leading: str | None
) -> tuple[EvidenceReason, ...]:
    """A segment's strongest aggregated reasons, `MAX_BAND_REASONS` at most, `leading` first.

    The aggregation is `engine.generative.root_cause.aggregate_reasons`, the one a root-cause
    evidence pack uses, so "what these customers have in common" means the same on both screens.
    `leading` is the feature a `top_reason` segment was formed on; it is moved to the front when the
    aggregation ranked another reason above it, because it is the one reason every row shares.
    """
    from engine.generative.root_cause import aggregate_reasons

    rows = [explanations[key] for key in keys if key in explanations]
    every_feature = {reason.feature for explanation in rows for reason in explanation.reasons}
    ranked = list(aggregate_reasons(rows, limit=max(len(every_feature), 1)))
    if leading is not None:
        ranked.sort(key=lambda reason: reason.feature != leading)
    return tuple(
        reason.model_copy(update={"id": f"r{position}"})
        for position, reason in enumerate(ranked[:MAX_BAND_REASONS], start=1)
    )


def _plan_top_reason(
    audience: pd.DataFrame,
    *,
    primary_key: str,
    explanations: Mapping[str, RowExplanation],
    max_segments: int,
    score_column: str,
) -> tuple[CopySegment, ...]:
    """Cut `audience` by each row's strongest reason and add the `_UNIT` column naming its segment.

    The grouping is `engine.generative.segments`, shared with root-cause summaries; past
    `max_segments` the smaller groups, and any row with no measured reason, share one "other" bucket,
    because unlike a summary copy cannot leave an eligible row without a message (DEC-1241).
    """
    keys = [str(key) for key in audience[primary_key]]
    segment_of = {
        key: None if feature is None else f"{_REASON_SEGMENT_PREFIX}{feature}"
        for key in keys
        for feature in (top_reason_feature(explanations.get(key)),)
    }
    capped = cap_with_other(
        group_keys(keys, segment_of.__getitem__),
        max_segments=max_segments,
        other=OTHER_REASONS_SEGMENT,
        extra=[key for key in keys if segment_of[key] is None],
    )
    unit_by_key = {key: unit_id for unit_id, members in capped for key in members}
    audience[_UNIT] = audience[primary_key].astype(str).map(unit_by_key)
    segments: list[CopySegment] = []
    for unit_id, members in capped:
        rows = audience.loc[audience[_UNIT] == unit_id]
        leading = None if unit_id == OTHER_REASONS_SEGMENT else unit_id.removeprefix(_REASON_SEGMENT_PREFIX)
        segments.append(
            CopySegment(
                segment=unit_id,
                label="Other reasons" if leading is None else f"Main reason: {leading}",
                rows=len(members),
                share_pct=_share(len(members), len(keys)),
                per_band=_per_band(rows),
                mean_score=_mean_score(rows, score_column),
                reasons=_segment_reasons(members, explanations, leading=leading),
                written=True,
            )
        )
    return tuple(segments)


def _plan_uplift(
    audience: pd.DataFrame,
    skipped: pd.DataFrame,
    over_budget: pd.DataFrame,
    *,
    primary_key: str,
    explanations: Mapping[str, RowExplanation],
    score_column: str,
) -> tuple[CopySegment, ...]:
    """Persuadables inside the budget first, written for; then the persuadables over the budget and
    each other uplift segment present, counted and skipped.

    A skipped segment carries the reason its rows get no message: "Over the contact budget", or the
    uplift action `scores.csv` already records per row in its `action` column (DEC-1243).
    """
    audience[_UNIT] = Segment.PERSUADABLE.value
    considered = len(audience) + len(skipped) + len(over_budget)
    keys = [str(key) for key in audience[primary_key]]
    persuadables = len(audience)
    segments = [
        CopySegment(
            segment=Segment.PERSUADABLE.value,
            label=SEGMENT_LABELS[Segment.PERSUADABLE],
            rows=persuadables,
            share_pct=_share(persuadables, considered),
            per_band=_per_band(audience),
            mean_score=_mean_score(audience, score_column),
            reasons=_segment_reasons(keys, explanations, leading=None),
            written=persuadables > 0,
            skipped_reason=(
                None
                if persuadables
                else "No persuadable is left after suppression, the control group and the contact budget."
            ),
        )
    ]
    if not over_budget.empty:
        segments.append(
            CopySegment(
                segment=OVER_BUDGET_SEGMENT,
                label="Persuadables over budget",
                rows=len(over_budget),
                share_pct=_share(len(over_budget), considered),
                per_band=_per_band(over_budget),
                mean_score=_mean_score(over_budget, score_column),
                written=False,
                skipped_reason="Over the contact budget, so no message is written for them.",
            )
        )
    for kind in (Segment.SURE_THING, Segment.LOST_CAUSE, Segment.SLEEPING_DOG):
        rows = skipped.loc[skipped[SEGMENT_COLUMN] == kind.value]
        if rows.empty:
            continue
        segments.append(
            CopySegment(
                segment=kind.value,
                label=SEGMENT_LABELS[kind],
                rows=len(rows),
                share_pct=_share(len(rows), considered),
                per_band=_per_band(rows),
                mean_score=_mean_score(rows, score_column),
                written=False,
                skipped_reason=f"{SEGMENT_ACTIONS[kind]}, so no message is written for them.",
            )
        )
    return tuple(segments)


def _plan(*, run_id: str, record: RunRecord, use_case: UseCaseConfig, storage: Storage) -> _Plan:
    """Read the run once and decide who gets copy and which band or segment it is written for.

    Raises `COPY_NEEDS_UPLIFT_RUN` for `uplift_segment` over a run that is not an uplift scoring run,
    and `RUN_WITHOUT_EXPLANATIONS` for `top_reason` over a run with no per-row reasons, both before a
    single model call - a segment nobody can form is not a cost worth paying for.
    """
    copy = use_case.generative.campaign_copy
    segment_by = copy.segment_by
    primary_key = sole_key(record.primary_key, what=f"Campaign copy for run {run_id}")
    if segment_by is CopySegmentBy.UPLIFT_SEGMENT and not is_uplift_scoring_run(storage, record):
        raise generative_error(COPY_NEEDS_UPLIFT_RUN, run_id=run_id)
    if segment_by is CopySegmentBy.TOP_REASON and ROW_EXPLANATIONS_FILENAME not in record.artefacts:
        raise generative_error(RUN_WITHOUT_EXPLANATIONS, run_id=run_id)

    profile = storage.read_model(record.artefacts[PROFILE_FILENAME], DatasetProfile)
    source_fields = tuple(field for field in copy.allowed_fields if field not in _RESERVED_FIELDS)
    score_column = _score_column(segment_by, use_case)
    extra: tuple[str, ...] = ()
    if segment_by is CopySegmentBy.TOP_REASON:
        extra = (score_column,)
    elif segment_by is CopySegmentBy.UPLIFT_SEGMENT:
        extra = (score_column, SEGMENT_COLUMN, INTENDED_TREATMENT_COLUMN)

    scores = _read_scores(storage, record.artefacts[SCORES_CSV], primary_key, extra)
    fields = _read_source_fields(storage, record, profile, source_fields, primary_key)
    explanations = _read_reasons(storage, record)

    if segment_by is CopySegmentBy.UPLIFT_SEGMENT:
        audience, skipped, over_budget, holdout, budget_applied = _classify_uplift(
            scores, fields, primary_key=primary_key
        )
        segments = _plan_uplift(
            audience,
            skipped,
            over_budget,
            primary_key=primary_key,
            explanations=explanations,
            score_column=score_column,
        )
        units = tuple(_Unit(unit_id=segment.segment, segment=segment) for segment in segments)
        return _Plan(
            primary_key,
            source_fields,
            audience,
            holdout,
            _per_band(audience),
            units,
            explanations,
            budget_applied,
        )

    audience, holdout, per_band = _classify(
        scores, fields, primary_key=primary_key, bands_to_write=copy.bands_to_write
    )
    if segment_by is CopySegmentBy.TOP_REASON:
        segments = _plan_top_reason(
            audience,
            primary_key=primary_key,
            explanations=explanations,
            max_segments=copy.max_segments,
            score_column=score_column,
        )
        units = tuple(_Unit(unit_id=segment.segment, segment=segment) for segment in segments)
        return _Plan(primary_key, source_fields, audience, holdout, per_band, units, explanations)

    audience[_UNIT] = audience[BAND_COLUMN]
    band_actions = {band.name: band.action for band in use_case.actions.bands}
    units = tuple(
        _Unit(
            unit_id=band,
            band_action=band_actions.get(band, band),
            reasons=_band_reasons(audience, explanations, band, primary_key),
        )
        for band in sorted(per_band)
    )
    return _Plan(primary_key, source_fields, audience, holdout, per_band, units, explanations)


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
    be rendered for a row the data cannot fill it from. With `segment_by` other than `band` it also
    raises, before any call, `RUN_WITHOUT_EXPLANATIONS` (`top_reason` over a run with no per-row
    reasons) or `COPY_NEEDS_UPLIFT_RUN` (`uplift_segment` over a run that is not an uplift scoring run).
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
    segment_by = copy.segment_by
    plan = _plan(run_id=run_id, record=record, use_case=use_case, storage=storage)
    primary_key = plan.primary_key
    source_fields = plan.source_fields
    audience = plan.audience
    score_name = _score_name(segment_by, use_case)

    templates: list[CopyTemplate] = []
    for unit in plan.units:
        if not unit.written:
            continue
        for channel in copy.channels:
            if unit.segment is None:
                templates.extend(
                    _generate_for_band_channel(
                        band=unit.unit_id,
                        band_action=unit.band_action,
                        channel=channel,
                        reasons=unit.reasons,
                        config=copy,
                        entity=use_case.entity,
                        meter=meter,
                        guardrails=guardrails,
                        config_root=config_root,
                    )
                )
            else:
                templates.extend(
                    _generate_for_segment_channel(
                        segment=unit.segment,
                        segment_by=segment_by,
                        channel=channel,
                        config=copy,
                        entity=use_case.entity,
                        score_name=score_name,
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
        unit_rows = audience.loc[
            audience[_UNIT] == _unit_of(template), (primary_key, BAND_COLUMN, *source_fields)
        ]
        for row in unit_rows.to_dict("records"):
            # `to_dict` is typed as `dict[Hashable, Any]` because a frame's columns need not be
            # strings. These are: they come from `primary_key` and `source_fields`, both of which
            # are configured names.
            row_map: dict[str, object] = {str(key): value for key, value in row.items()}
            entity_key = str(row_map[primary_key])
            values = _row_values(row_map, template)
            messages.append(
                render_message(
                    template,
                    values,
                    entity_key=entity_key,
                    config=copy,
                    guardrails=guardrails,
                    backend=meter.backend,
                    band=_band_text(row_map.get(BAND_COLUMN)),
                )
            )

    written = any(unit.written for unit in plan.units)
    prompts = _CHANNEL_PROMPTS if segment_by is CopySegmentBy.BAND else _SEGMENT_PROMPTS
    used_names = sorted({prompts[channel] for channel in copy.channels}) if written else []
    finished = now if now is not None else utc_now()
    if segment_by is CopySegmentBy.BAND:
        sorted_templates = tuple(sorted(templates, key=lambda t: (t.band, t.channel, t.variant)))
        sorted_messages = tuple(sorted(messages, key=lambda m: (m.band, m.channel, m.variant, m.entity_key)))
        audience_rows = sum(plan.per_band.values())
    else:
        order = {unit.unit_id: position for position, unit in enumerate(plan.units)}
        sorted_templates = tuple(
            sorted(templates, key=lambda t: (order.get(_unit_of(t), len(order)), t.channel, t.variant))
        )
        sorted_messages = tuple(
            sorted(
                messages,
                key=lambda m: (order.get(m.segment or "", len(order)), m.channel, m.variant, m.entity_key),
            )
        )
        audience_rows = len(audience)
    batch = CopyBatch(
        run_id=run_id,
        batch_id=f"cb_{run_id}",
        audience=CopyAudience(rows=audience_rows, per_band=plan.per_band),
        holdout=plan.holdout,
        templates=sorted_templates,
        require_human_review=copy.require_human_review,
        messages_rendered=len(sorted_messages),
        messages_blocked=sum(1 for message in sorted_messages if message.block_reason is not None),
        prompt_versions=prompt_versions(used_names, config_root),
        prompt_hashes=prompt_hashes(used_names, config_root),
        created_at=finished,
        segment_by=None if segment_by is CopySegmentBy.BAND else segment_by.value,
        uplift_budget_applied=plan.budget_applied,
        segments=(
            None
            if segment_by is CopySegmentBy.BAND
            else tuple(unit.segment for unit in plan.units if unit.segment is not None)
        ),
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
    if target.segment is not None:
        replacement = _regenerate_segment_template(
            batch,
            target,
            use_case=use_case,
            meter=meter,
            guardrails=guardrails,
            config_root=config_root,
        )
        return replacement.model_copy(
            update={"template_id": target.template_id, "attempts": target.attempts + replacement.attempts}
        )

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


def _regenerate_segment_template(
    batch: CopyBatch,
    target: CopyTemplate,
    *,
    use_case: UseCaseConfig,
    meter: Meter,
    guardrails: Guardrails,
    config_root: Path | None,
) -> CopyTemplate:
    """A fresh variant for a template written per segment, asked exactly what the first call asked.

    The segment is the one the batch stored, and `segment_by` is the batch's own rather than the
    use case's: a batch written per main reason was asked for through an override the regenerate
    route does not repeat, and re-cutting the run with the use case's default would ask about a
    different group of customers than the one the reviewer is looking at (DEC-1246).
    """
    stored = next((item for item in batch.segments or () if item.segment == target.segment), None)
    if stored is None or batch.segment_by is None:
        raise KeyError(f"{batch.batch_id} has no segment {target.segment!r} to regenerate for.")
    segment_by = CopySegmentBy(batch.segment_by)
    fresh = _generate_for_segment_channel(
        segment=stored,
        segment_by=segment_by,
        channel=Channel(target.channel),
        config=use_case.generative.campaign_copy,
        entity=use_case.entity,
        score_name=_score_name(segment_by, use_case),
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )
    return next((template for template in fresh if template.variant == target.variant), fresh[0])
