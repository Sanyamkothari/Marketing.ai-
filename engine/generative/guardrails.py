"""What must be true of a generated text before it is stored.

Two layers, in this order and never the other way round. The **deterministic rules** are ordinary
code: they cost nothing, they cannot be talked out of a verdict, and they catch the failures that
matter most - an identifier in an answer, a placeholder with no data behind it, a message over a
channel's hard limit. The **judges** are themselves LLM calls, so they are metered and budgeted
like any other, and they are only asked about the things a regular expression cannot decide: is
every claim supported, does this read as pressure, would this offend the person who gets it.

A text that fails a deterministic rule is never sent to a judge. That is not only thrift: a judge
asked to score a text that is already going to be refused would spend money to produce a number
nobody will read.

Three properties are deliberate:

**Nothing here edits its input.** A guardrail that rewrote a text would make the artefact a record
of something no model produced, and the next reader would have no way to tell. A failing text is
refused and the failure is recorded; regenerating is the caller's decision, bounded by `retries`.

**A failure names the rule, never the value.** `GuardrailCheck.detail` says "an e-mail address" and
"three characters over the limit"; it never quotes the address or the sentence, because a check
reaches a log and a screen and the thing it caught is exactly the thing that must not (plan
section 13.7).

**A rule that is off is off, not silently passing.** `configs/guardrails.yaml` sets each rule to
`block`, `warn` or `off`, and a rule set to `off` produces no check at all rather than a passing
one - so a report cannot claim a text was checked for something nobody checked it for.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

from engine.config import config_root
from engine.generative.budget import Meter
from engine.generative.contracts import (
    GenerativePurpose,
    GuardrailCheck,
    GuardrailOutcome,
    GuardrailSummary,
    JudgeScore,
)
from engine.generative.prompts import load_prompt, render
from engine.generative.redaction import REDACTION_PATTERN
from engine.generative.redaction import find as find_pii
from engine.utils.logging import get_logger

__all__ = [
    "GUARDRAILS_FILENAME",
    "JUDGE_PROMPTS",
    "RULES",
    "CheckContext",
    "GuardrailAction",
    "GuardrailPolicy",
    "GuardrailResult",
    "Guardrails",
    "JudgeRule",
    "load_policy",
    "summarise",
]

_LOGGER = get_logger(__name__)

GUARDRAILS_FILENAME: Final[str] = "guardrails.yaml"

# Rule names, which are also the keys of `deterministic:` in the configuration file.
EMPTY_OUTPUT: Final[str] = "empty_output"
MAX_LENGTH: Final[str] = "max_length"
REQUIRED_LINES: Final[str] = "required_lines"
ALLOWED_FIELDS_ONLY: Final[str] = "allowed_fields_only"
BANNED_PHRASES: Final[str] = "banned_phrases"
PII_IN_OUTPUT: Final[str] = "pii_in_output"
URL_WHITELIST: Final[str] = "url_whitelist"
LANGUAGE_MATCH: Final[str] = "language_match"

RULES: Final[tuple[str, ...]] = (
    EMPTY_OUTPUT,
    MAX_LENGTH,
    REQUIRED_LINES,
    ALLOWED_FIELDS_ONLY,
    BANNED_PHRASES,
    PII_IN_OUTPUT,
    URL_WHITELIST,
    LANGUAGE_MATCH,
)
"""Every deterministic rule, in the order they run.

Cheapest and most certain first: an empty output makes every later rule meaningless, and a length
failure is worth reporting before a phrase failure inside a text that was never going to be used.
"""

JUDGE_PROMPTS: Final[Mapping[str, GenerativePurpose]] = {
    "faithfulness": GenerativePurpose.JUDGE_FAITHFULNESS,
    "compliance": GenerativePurpose.JUDGE_COMPLIANCE,
    "toxicity": GenerativePurpose.JUDGE_TOXICITY,
    "correctness": GenerativePurpose.JUDGE_CORRECTNESS,
}
"""Judge name in the configuration -> the purpose its calls are metered under."""

_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_URL: Final[re.Pattern[str]] = re.compile(r"https?://\S+", re.IGNORECASE)
"""A whole URL token, case-insensitively.

Two things this spelling exists to avoid, both of which the previous one - a lower-case scheme and
a capture of `([A-Za-z0-9.\\-]+)` - let through silently.

`@` was outside the character class, so the capture stopped at it and returned the *userinfo*
rather than the host: `https://acme.com@phish.example/x` captured `acme.com`, which a whitelist
for `acme.com` then allowed, while every mail client navigates to `phish.example`.

Without `IGNORECASE`, `HTTPS://phish.example/x` matched nothing at all, and a rule whose loop body
never runs returns "nothing found" - which reads as PASSED. That defeated the rule in its shipped
default state, where `allowed_url_domains` is empty and means no link may appear in any output.

The host is taken from `urlsplit().hostname`, which strips userinfo, lower-cases, and drops the
port, so the comparison below sees a host and only a host.
"""
_URL_TRAILING: Final[str] = ".,;:!?'\")]}>"
"""Characters that end a sentence but never a URL; stripped before the host is read."""

_WORD_BOUNDARY: Final[str] = r"(?<![A-Za-z0-9]){phrase}(?![A-Za-z0-9])"
_LATIN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]")
_LETTER: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]")
_MIN_LATIN_SHARE: Final[float] = 0.70
"""How much of a text must be Latin script before it is taken to be in a Latin-script language."""


class GuardrailAction(StrEnum):
    """What happens when a rule finds something: refuse, record, or do not run the rule at all."""

    BLOCK = "block"
    WARN = "warn"
    OFF = "off"


@dataclass(frozen=True)
class JudgeRule:
    """One judge's threshold and what happens below it."""

    threshold: float
    on_fail: GuardrailAction


@dataclass(frozen=True)
class GuardrailPolicy:
    """`configs/guardrails.yaml`, parsed. What is checked, how hard, and how often it may be retried."""

    deterministic: Mapping[str, GuardrailAction] = field(default_factory=dict)
    judges: Mapping[str, JudgeRule] = field(default_factory=dict)
    retries: int = 2
    global_banned_phrases: tuple[str, ...] = ()
    allowed_url_domains: tuple[str, ...] = ()
    max_output_chars: int = 8_000

    def action(self, rule: str) -> GuardrailAction:
        """What `rule` does when it fires; a rule the file does not mention is off."""
        return self.deterministic.get(rule, GuardrailAction.OFF)


@dataclass(frozen=True)
class CheckContext:
    """Everything a check needs to know about the text it is about to look at.

    Built by the flow that generated the text, because only the flow knows which channel a message
    is for, which fields it was allowed and what the model was supposed to have grounded on.
    """

    target: str
    max_chars: int | None = None
    max_words: int | None = None
    required_line: str | None = None
    allowed_fields: tuple[str, ...] = ()
    banned_phrases: tuple[str, ...] = ()
    expected_language: str | None = None
    source: str | None = None
    judges: tuple[str, ...] = ()
    judge_values: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardrailResult:
    """What every rule said about one text, and whether it may be stored."""

    target: str
    outcome: GuardrailOutcome
    checks: tuple[GuardrailCheck, ...] = ()
    judge_scores: tuple[JudgeScore, ...] = ()
    blocked_by: str | None = None

    @property
    def passed(self) -> bool:
        """True when the text may be stored, whether or not anything warned about it."""
        return self.outcome is not GuardrailOutcome.BLOCKED


def load_policy(root: Path | None = None) -> GuardrailPolicy:
    """Read `configs/guardrails.yaml`, or return a policy that checks nothing when it is absent."""
    path = config_root(root) / GUARDRAILS_FILENAME
    if not path.is_file():
        return GuardrailPolicy()
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    deterministic = {
        str(rule): GuardrailAction(str(action))
        for rule, action in (document.get("deterministic") or {}).items()
    }
    judges = {
        str(name): JudgeRule(
            threshold=float(rule.get("threshold", 0.0)),
            on_fail=GuardrailAction(str(rule.get("on_fail", "warn"))),
        )
        for name, rule in (document.get("llm_judge") or {}).items()
    }
    return GuardrailPolicy(
        deterministic=deterministic,
        judges=judges,
        retries=int(document.get("retries", 2)),
        global_banned_phrases=tuple(str(item) for item in document.get("global_banned_phrases") or ()),
        allowed_url_domains=tuple(str(item) for item in document.get("allowed_url_domains") or ()),
        max_output_chars=int(document.get("max_output_chars", 8_000)),
    )


# ---------------------------------------------------------------------------
# The deterministic rules. Each returns what it found, or None when it found nothing.
# ---------------------------------------------------------------------------
def _empty_output(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    del context, policy
    return "the model returned nothing to store" if not text.strip() else None


def _max_length(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    ceiling = min(filter(None, (context.max_chars, policy.max_output_chars)), default=None)
    if ceiling is not None and len(text) > ceiling:
        return f"{len(text) - ceiling} characters over the {ceiling}-character limit"
    if context.max_words is not None:
        words = len(text.split())
        if words > context.max_words:
            return f"{words - context.max_words} words over the {context.max_words}-word limit"
    return None


def _required_lines(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    del policy
    if context.required_line and context.required_line not in text:
        return "the line this channel must end with is missing"
    return None


def _allowed_fields_only(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    del policy
    used = set(_PLACEHOLDER.findall(text))
    unknown = sorted(used - set(context.allowed_fields))
    if unknown:
        # The names are the model's invention rather than a customer's value, so naming them is
        # what makes the failure fixable.
        return f"placeholders with no data behind them: {', '.join(unknown)}"
    return None


def _banned_phrases(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    lowered = text.lower()
    for phrase in (*policy.global_banned_phrases, *context.banned_phrases):
        if re.search(_WORD_BOUNDARY.format(phrase=re.escape(phrase.lower())), lowered):
            return f"the banned phrase {phrase!r}"
    return None


def _pii_in_output(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    del context, policy
    # A marker is the *absence* of an identifier, so a redacted evidence pack quoted back must not
    # be read as a leak.
    kinds = find_pii(REDACTION_PATTERN.sub("", text))
    return f"personal data of kind: {', '.join(kinds)}" if kinds else None


def _url_whitelist(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    del context
    for token in _URL.findall(text):
        host = urlsplit(token.rstrip(_URL_TRAILING)).hostname
        if host is None:
            # A link the engine cannot resolve to a host is not a link it can vouch for.
            return "a link whose domain could not be read"
        allowed = (domain.lower().lstrip(".") for domain in policy.allowed_url_domains)
        if not any(host == domain or host.endswith(f".{domain}") for domain in allowed):
            return "a link to a domain that is not on the allowed list"
    return None


def _language_match(text: str, context: CheckContext, policy: GuardrailPolicy) -> str | None:
    """A script check rather than a language one, which is why this rule warns rather than blocks.

    Telling Hindi from Marathi needs a model; telling Devanagari from Latin needs a character
    class. The second catches the failure that actually happens - a model answering in the wrong
    script entirely - and says nothing about the one it cannot see.
    """
    del policy
    expected = context.expected_language
    if not expected or expected == "auto":
        return None
    letters = _LETTER.findall(text)
    if len(letters) < 20:
        return None
    latin_share = len(_LATIN.findall(text)) / len(letters)
    if expected == "en" and latin_share < _MIN_LATIN_SHARE:
        return "the answer is not in the script the question was asked in"
    return None


_CHECKS: Final[Mapping[str, Any]] = {
    EMPTY_OUTPUT: _empty_output,
    MAX_LENGTH: _max_length,
    REQUIRED_LINES: _required_lines,
    ALLOWED_FIELDS_ONLY: _allowed_fields_only,
    BANNED_PHRASES: _banned_phrases,
    PII_IN_OUTPUT: _pii_in_output,
    URL_WHITELIST: _url_whitelist,
    LANGUAGE_MATCH: _language_match,
}
"""Rule name -> the function that runs it. A rule with no function here cannot be configured on."""


class Guardrails:
    """Runs the rules. Holds the policy, and a meter when there are judges to pay for.

    A `Guardrails` with no meter runs the deterministic rules and skips the judges, which is what
    a caller wants when it is checking a *rendering* of an already-judged template: the template
    was judged once, and judging each of ten thousand renderings would cost ten thousand calls to
    answer a question the deterministic rules already answer.
    """

    def __init__(
        self,
        policy: GuardrailPolicy,
        *,
        meter: Meter | None = None,
        prompts_root: Path | None = None,
    ) -> None:
        self._policy = policy
        self._meter = meter
        self._root = prompts_root

    @property
    def policy(self) -> GuardrailPolicy:
        """The rules in force."""
        return self._policy

    @property
    def retries(self) -> int:
        """How many times a caller may regenerate a text before a failure is final."""
        return self._policy.retries

    def check(self, text: str, context: CheckContext) -> GuardrailResult:
        """Run every configured rule over `text`, deterministic first, judges only if those pass."""
        checks: list[GuardrailCheck] = []
        blocked_by: str | None = None
        warned = False
        for rule in RULES:
            action = self._policy.action(rule)
            if action is GuardrailAction.OFF:
                continue
            detail = _CHECKS[rule](text, context, self._policy)
            outcome = _outcome(detail, action)
            checks.append(
                GuardrailCheck(
                    target=context.target,
                    rule=rule,
                    outcome=outcome,
                    detail=detail or "nothing found",
                )
            )
            if outcome is GuardrailOutcome.BLOCKED and blocked_by is None:
                blocked_by = rule
            warned = warned or outcome is GuardrailOutcome.WARNED
        if blocked_by is not None:
            _LOGGER.info("guardrail.blocked rule=%s", blocked_by)
            return GuardrailResult(
                target=context.target,
                outcome=GuardrailOutcome.BLOCKED,
                checks=tuple(checks),
                blocked_by=blocked_by,
            )

        scores = self._judge(text, context)
        for score in scores:
            rule = score.purpose.value
            action = self._policy.judges[_judge_name(score.purpose)].on_fail
            outcome = _outcome(None if score.passed else score.reason, action)
            checks.append(
                GuardrailCheck(
                    target=context.target,
                    rule=rule,
                    outcome=outcome,
                    detail=score.reason,
                )
            )
            if outcome is GuardrailOutcome.BLOCKED and blocked_by is None:
                blocked_by = rule
            warned = warned or outcome is GuardrailOutcome.WARNED
        if blocked_by is not None:
            _LOGGER.info("guardrail.blocked rule=%s", blocked_by)
            outcome = GuardrailOutcome.BLOCKED
        else:
            outcome = GuardrailOutcome.WARNED if warned else GuardrailOutcome.PASSED
        return GuardrailResult(
            target=context.target,
            outcome=outcome,
            checks=tuple(checks),
            judge_scores=tuple(scores),
            blocked_by=blocked_by,
        )

    def _judge(self, text: str, context: CheckContext) -> tuple[JudgeScore, ...]:
        """Ask each configured judge about `text`. No meter means no judges, and that is not a failure."""
        if self._meter is None or not context.judges:
            return ()
        scores: list[JudgeScore] = []
        for name in context.judges:
            rule = self._policy.judges.get(name)
            if rule is None or rule.on_fail is GuardrailAction.OFF:
                continue
            purpose = JUDGE_PROMPTS[name]
            prompt = load_prompt(purpose.value, self._root)
            values: dict[str, object] = {"generated": text, **dict(context.judge_values)}
            if context.source is not None:
                values.setdefault("source", context.source)
            completion = self._meter.judge(render(prompt, values), purpose)
            score, reason = _parse_verdict(completion.text)
            scores.append(
                JudgeScore(
                    purpose=purpose,
                    score=score,
                    threshold=rule.threshold,
                    passed=score >= rule.threshold,
                    reason=reason,
                )
            )
        return tuple(scores)


def _judge_name(purpose: GenerativePurpose) -> str:
    """The configuration key for a judging purpose: the inverse of `JUDGE_PROMPTS`."""
    return next(name for name, member in JUDGE_PROMPTS.items() if member is purpose)


def _outcome(detail: str | None, action: GuardrailAction) -> GuardrailOutcome:
    """A rule that found nothing passed; one that found something does what it is configured to do."""
    if detail is None:
        return GuardrailOutcome.PASSED
    return GuardrailOutcome.BLOCKED if action is GuardrailAction.BLOCK else GuardrailOutcome.WARNED


def _parse_verdict(text: str) -> tuple[float, str]:
    """A judge's score and its one-sentence reason.

    A judge that does not answer in JSON scores 0: a verdict nobody can read is not a pass, and
    treating it as one would turn a broken judge into a silently disabled guardrail.

    `NaN` is the same failure wearing a number. `json.loads` accepts the bare `NaN` literal, and
    `min(1.0, nan)` returns 1.0 rather than nan - `nan < 1.0` is False, so `min` keeps its first
    argument - so a clamp alone turned an unreadable verdict into a *perfect* one. It scores 0 for
    the reason the docstring already gives, and so does an infinity.
    """
    try:
        payload = json.loads(text)
        score = float(payload["score"])
    except (ValueError, KeyError, TypeError):
        _LOGGER.warning("guardrail.judge_unreadable")
        return 0.0, "the judge did not answer in the shape its prompt asked for"
    if not math.isfinite(score):
        _LOGGER.warning("guardrail.judge_unreadable")
        return 0.0, "the judge did not answer in the shape its prompt asked for"
    reason = str(payload.get("reason", "")) if isinstance(payload, dict) else ""
    return max(0.0, min(1.0, score)), reason


def summarise(results: Sequence[GuardrailResult]) -> GuardrailSummary:
    """Counts over a batch of results, so a screen needs no arithmetic."""
    return GuardrailSummary(
        checked=len(results),
        passed=sum(1 for result in results if result.outcome is GuardrailOutcome.PASSED),
        warned=sum(1 for result in results if result.outcome is GuardrailOutcome.WARNED),
        blocked=sum(1 for result in results if result.outcome is GuardrailOutcome.BLOCKED),
    )
