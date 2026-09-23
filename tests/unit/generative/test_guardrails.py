"""`engine.generative.guardrails` and `engine.generative.redaction`.

Four properties are worth the most, and each is proved against something that could plausibly be
got wrong rather than against a restatement of the code:

**A blocked text never reaches a judge.** The deterministic rules are free and the judges cost
money, so the order is not a preference. It is asserted by counting the meter's calls after a text
that fails a free rule: the count must be zero.

**Nothing is edited.** A guardrail that quietly stripped an address would make the artefact a
record of something no model produced. `check` is asked for its verdict, and the text it was given
is asserted unchanged afterwards.

**A failure names the rule, never the value.** Every `detail` string produced by every rule over
every planted input is checked for the value that triggered it. A check reaches a log and a screen,
and the thing it caught is exactly the thing that must not.

**A rule that is off produces no check.** Not a passing one - a report that said "checked for PII:
passed" when the rule was off would be a report that lies about what was looked at.

The redaction half is checked against the fixtures that plant PII, with the Phase 1 detectors'
own patterns, so the two cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import BudgetConfig, LlmConfig
from engine.generative.budget import Meter
from engine.generative.contracts import GenerativePurpose, GuardrailOutcome
from engine.generative.guardrails import (
    ALLOWED_FIELDS_ONLY,
    BANNED_PHRASES,
    EMPTY_OUTPUT,
    JUDGE_PROMPTS,
    LANGUAGE_MATCH,
    MAX_LENGTH,
    PII_IN_OUTPUT,
    REQUIRED_LINES,
    RULES,
    URL_WHITELIST,
    CheckContext,
    GuardrailAction,
    GuardrailPolicy,
    Guardrails,
    JudgeRule,
    load_policy,
    summarise,
)
from engine.generative.redaction import contains_pii, find, marker_for, redact
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.stages.ingest import PII_DETECTORS
from tests.fixtures.make_docs import COMPLAINT_PII_KINDS, generate_complaints

OPT_OUT = "Reply STOP to opt out"
CLEAN = f"Hi {{{{first_name}}}}, your line is ready whenever you are. {OPT_OUT}"


def context(**overrides: object) -> CheckContext:
    """A copy-shaped context: an SMS template with one allowed field and an opt-out line."""
    base: dict[str, object] = {
        "target": "High/sms/A",
        "max_chars": 160,
        "required_line": OPT_OUT,
        "allowed_fields": ("first_name",),
    }
    base.update(overrides)
    return CheckContext(**base)  # type: ignore[arg-type]


def guardrails(*, meter: Meter | None = None) -> Guardrails:
    return Guardrails(load_policy(), meter=meter)


def meter(mode: FakeLLMMode = FakeLLMMode.GROUNDED) -> Meter:
    return Meter(FakeLLMClient(mode=mode), job_id="r_1", llm=LlmConfig(), budget=BudgetConfig(cache=False))


# ---------------------------------------------------------------------------
# The shipped policy
# ---------------------------------------------------------------------------
def test_the_shipped_policy_configures_every_rule_the_code_implements() -> None:
    """A rule in the file with no code silently does nothing; code with no rule can never run."""
    policy = load_policy()
    assert set(policy.deterministic) == set(RULES)
    assert set(policy.judges) <= set(JUDGE_PROMPTS)


def test_the_shipped_policy_blocks_on_everything_that_matters_and_warns_on_the_one_it_cannot_be_sure_of() -> (
    None
):
    policy = load_policy()
    assert policy.action(LANGUAGE_MATCH) is GuardrailAction.WARN
    for rule in set(RULES) - {LANGUAGE_MATCH}:
        assert policy.action(rule) is GuardrailAction.BLOCK, rule


def test_the_shipped_policy_allows_no_link_at_all_until_a_client_names_a_domain() -> None:
    """An empty allow-list is the safe default: a link nobody approved is a link nobody approved."""
    assert load_policy().allowed_url_domains == ()


def test_a_missing_policy_file_checks_nothing_rather_than_failing(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    policy = load_policy(tmp_path / "configs")
    assert all(policy.action(rule) is GuardrailAction.OFF for rule in RULES)


# ---------------------------------------------------------------------------
# The deterministic rules
# ---------------------------------------------------------------------------
def test_a_clean_template_passes_every_rule() -> None:
    """The baseline: without this, every blocking test below could be passing for the wrong reason."""
    result = guardrails().check(CLEAN, context())
    assert result.outcome is GuardrailOutcome.PASSED
    assert result.passed
    assert result.blocked_by is None
    assert {check.rule for check in result.checks} == set(RULES)
    assert all(check.outcome is GuardrailOutcome.PASSED for check in result.checks)


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        (EMPTY_OUTPUT, "   \n  "),
        (MAX_LENGTH, f"Hi {{{{first_name}}}}, {'x' * 200} {OPT_OUT}"),
        (REQUIRED_LINES, "Hi {{first_name}}, your line is ready whenever you are."),
        (ALLOWED_FIELDS_ONLY, f"Hi {{{{secret_offer_code}}}}, come back. {OPT_OUT}"),
        (BANNED_PHRASES, f"Hi {{{{first_name}}}}, guaranteed savings. {OPT_OUT}"),
        (PII_IN_OUTPUT, f"Hi {{{{first_name}}}}, write to a.person@example.invalid. {OPT_OUT}"),
        (URL_WHITELIST, f"Hi {{{{first_name}}}}, see https://elsewhere.example/x. {OPT_OUT}"),
    ],
)
def test_each_rule_blocks_the_thing_it_is_named_for(rule: str, text: str) -> None:
    result = guardrails().check(text, context())
    assert result.blocked_by == rule
    assert result.outcome is GuardrailOutcome.BLOCKED
    assert not result.passed


def test_a_word_limit_is_enforced_where_a_channel_counts_words() -> None:
    """Email counts words, SMS counts characters; the same rule has to do both."""
    long_body = " ".join(["word"] * 60) + f"\n{OPT_OUT}"
    passing = guardrails().check(long_body, context(max_chars=None, max_words=100))
    failing = guardrails().check(long_body, context(max_chars=None, max_words=20))
    assert passing.passed
    assert failing.blocked_by == MAX_LENGTH


def test_a_banned_phrase_is_matched_on_word_boundaries() -> None:
    """`cheapest` is banned; `cheapestimate` is a word nobody banned."""
    banned = guardrails().check(f"Hi, the cheapest plan. {OPT_OUT}", context(allowed_fields=()))
    innocent = guardrails().check(f"Hi, a cheapestimate of yours. {OPT_OUT}", context(allowed_fields=()))
    assert banned.blocked_by == BANNED_PHRASES
    assert innocent.passed


def test_a_use_case_adds_its_own_banned_phrases_to_the_global_ones() -> None:
    result = guardrails().check(
        f"Hi {{{{first_name}}}}, the lowest price anywhere. {OPT_OUT}",
        context(banned_phrases=("lowest price",)),
    )
    assert result.blocked_by == BANNED_PHRASES


def test_a_link_to_an_allowed_domain_or_its_subdomain_passes() -> None:
    policy = GuardrailPolicy(
        deterministic={URL_WHITELIST: GuardrailAction.BLOCK}, allowed_url_domains=("example.test",)
    )
    subject = Guardrails(policy)
    assert subject.check("see https://example.test/a", context()).passed
    assert subject.check("see https://help.example.test/a", context()).passed
    assert not subject.check("see https://example.test.evil.test/a", context()).passed


def test_a_redaction_marker_is_not_read_as_personal_data() -> None:
    """An evidence pack quoted back into a summary is full of markers; a marker is the absence of one."""
    text = f"The complaint said to call {marker_for('phone')} about it. {OPT_OUT}"
    assert guardrails().check(text, context(allowed_fields=())).passed


def test_the_language_rule_warns_rather_than_blocking_and_does_not_guess_on_a_short_text() -> None:
    """It is a script check, not a language one, so it says only what a character class can see."""
    devanagari = "यह उत्तर हिंदी में है और यह अंग्रेजी में नहीं है इसलिए यह नियम चेतावनी देगा। " + OPT_OUT
    result = guardrails().check(
        devanagari, context(allowed_fields=(), max_chars=None, expected_language="en")
    )
    warned = [check for check in result.checks if check.rule == LANGUAGE_MATCH]
    assert warned[0].outcome is GuardrailOutcome.WARNED
    assert result.outcome is GuardrailOutcome.WARNED
    assert result.passed, "a warning is recorded and let through"
    short = guardrails().check(f"ठीक {OPT_OUT}", context(allowed_fields=(), expected_language="en"))
    assert all(check.outcome is GuardrailOutcome.PASSED for check in short.checks)


def test_a_rule_that_is_off_produces_no_check_at_all() -> None:
    """Not a passing one: a report must not claim a text was checked for something nobody checked."""
    policy = GuardrailPolicy(deterministic={EMPTY_OUTPUT: GuardrailAction.BLOCK})
    result = Guardrails(policy).check("a text with an address a.b@example.invalid", context())
    assert [check.rule for check in result.checks] == [EMPTY_OUTPUT]
    assert result.passed


def test_the_first_failing_rule_is_the_one_reported_and_the_rest_still_run() -> None:
    """A reviewer wants every failure, and a fixer wants to know which to start with."""
    text = "x" * 500 + " guaranteed a.b@example.invalid"
    result = guardrails().check(text, context())
    assert result.blocked_by == MAX_LENGTH
    blocked = {check.rule for check in result.checks if check.outcome is GuardrailOutcome.BLOCKED}
    assert {MAX_LENGTH, BANNED_PHRASES, PII_IN_OUTPUT, REQUIRED_LINES} <= blocked


def test_no_detail_quotes_the_value_that_triggered_it() -> None:
    """The detail reaches a log and a screen; the value it found is exactly what must not."""
    text = (
        f"Hi {{{{first_name}}}}, write to secret.person@example.invalid or see https://evil.test/x. {OPT_OUT}"
    )
    result = guardrails().check(text, context())
    joined = " ".join(check.detail for check in result.checks)
    assert "secret.person@example.invalid" not in joined
    assert "evil.test" not in joined
    assert "personal data" in joined


def test_checking_a_text_does_not_change_it() -> None:
    text = f"Hi {{{{first_name}}}}, write to a.b@example.invalid. {OPT_OUT}"
    before = text
    guardrails().check(text, context())
    assert text == before


# ---------------------------------------------------------------------------
# The judges
# ---------------------------------------------------------------------------
def test_a_text_that_fails_a_free_rule_never_reaches_a_judge() -> None:
    """The order is the whole design: a judge costs money to score a text nobody will read."""
    subject_meter = meter()
    result = Guardrails(load_policy(), meter=subject_meter).check(
        "", context(judges=("toxicity",), judge_values={})
    )
    assert result.blocked_by == EMPTY_OUTPUT
    assert result.judge_scores == ()
    assert subject_meter.calls == 0


def test_a_passing_text_is_judged_and_the_verdict_is_recorded() -> None:
    subject_meter = meter()
    result = Guardrails(load_policy(), meter=subject_meter).check(
        CLEAN, context(judges=("toxicity",), judge_values={})
    )
    assert result.passed
    assert [score.purpose for score in result.judge_scores] == [GenerativePurpose.JUDGE_TOXICITY]
    assert result.judge_scores[0].passed
    assert result.judge_scores[0].threshold == 0.9
    assert subject_meter.calls == 1


def test_a_judge_below_its_threshold_blocks_the_text() -> None:
    result = Guardrails(load_policy(), meter=meter(FakeLLMMode.FAILING_JUDGE)).check(
        CLEAN, context(judges=("toxicity",), judge_values={})
    )
    assert result.outcome is GuardrailOutcome.BLOCKED
    assert result.blocked_by == GenerativePurpose.JUDGE_TOXICITY.value
    assert not result.judge_scores[0].passed


def test_a_judge_that_does_not_answer_in_json_scores_zero() -> None:
    """A verdict nobody can read is not a pass; treating it as one would disable the guardrail."""
    result = Guardrails(load_policy(), meter=meter(FakeLLMMode.MALFORMED)).check(
        CLEAN, context(judges=("toxicity",), judge_values={})
    )
    assert result.judge_scores[0].score == 0.0
    assert result.outcome is GuardrailOutcome.BLOCKED


def test_with_no_meter_the_judges_are_skipped_and_that_is_not_a_failure() -> None:
    """Which is what checking ten thousand renderings of an already-judged template needs."""
    result = guardrails().check(CLEAN, context(judges=("toxicity",)))
    assert result.passed
    assert result.judge_scores == ()


def test_a_judge_the_policy_turns_off_is_not_asked() -> None:
    policy = GuardrailPolicy(judges={"toxicity": JudgeRule(threshold=0.9, on_fail=GuardrailAction.OFF)})
    subject_meter = meter()
    result = Guardrails(policy, meter=subject_meter).check(CLEAN, context(judges=("toxicity",)))
    assert result.judge_scores == ()
    assert subject_meter.calls == 0


def test_the_faithfulness_judge_is_given_what_the_text_was_supposed_to_rest_on() -> None:
    subject_meter = meter()
    result = Guardrails(load_policy(), meter=subject_meter).check(
        CLEAN,
        context(judges=("faithfulness",), source="Your line is ready whenever you are."),
    )
    assert [score.purpose for score in result.judge_scores] == [GenerativePurpose.JUDGE_FAITHFULNESS]
    assert subject_meter.calls == 1


def test_a_summary_counts_a_batch_so_a_screen_does_no_arithmetic() -> None:
    subject = guardrails()
    results = [
        subject.check(CLEAN, context()),
        subject.check("", context()),
        subject.check("x" * 500, context()),
    ]
    summary = summarise(results)
    assert (summary.checked, summary.passed, summary.blocked) == (3, 1, 2)
    assert summary.warned == 0


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", COMPLAINT_PII_KINDS)
def test_every_planted_shape_is_found_in_free_text(kind: str) -> None:
    """The Phase 1 detectors full-match a cell; a complaint hides its identifier mid-sentence."""
    text = " ".join(generate_complaints(200)["text"])
    assert kind in find(text)


def test_redaction_removes_every_identifier_and_leaves_the_sentence_readable() -> None:
    """Whatever was there is gone, and the sentence around it still reads as a sentence."""
    for row in generate_complaints(200).itertuples():
        redacted, kinds = redact(str(row.text))
        assert not contains_pii(redacted), row.entity_key
        assert bool(kinds) == bool(find(str(row.text)))
        assert set(kinds) <= set(find(str(row.text)))
        assert len(redacted.split()) >= len(str(row.text).split()) - 2


def test_the_kinds_reported_are_what_was_replaced_not_what_was_present() -> None:
    """The two differ where patterns overlap, and the replaced set is the one a reader can act on.

    A phone number's digits also satisfy the Aadhaar pattern. `find` reports both, because both
    patterns match the text it was given; `redact` runs the detectors in the Phase 1 order, so the
    phone match is taken out first and the Aadhaar pattern then has nothing left to match. What is
    reported is therefore what was actually removed, which is the statement an evidence pack wants
    to carry - and either way nothing survives, which is what the test above pins.
    """
    text = "Please call +91 90000 12345 about the outage."
    present = find(text)
    redacted, replaced = redact(text)
    assert set(replaced) <= set(present)
    assert replaced
    assert not contains_pii(redacted)


def test_a_marker_says_which_detector_fired() -> None:
    redacted, kinds = redact("Reach me on user0001@example.invalid about it.")
    assert kinds == ("email",)
    assert marker_for("email") in redacted
    assert "user0001" not in redacted


def test_text_with_no_identifier_comes_back_unchanged() -> None:
    text = "The line has been down again since Monday and this is the third outage this quarter."
    redacted, kinds = redact(text)
    assert (redacted, kinds) == (text, ())


def test_the_name_detector_is_deliberately_not_run_over_prose() -> None:
    """Its pattern matches the first word of every sentence; over a column it is fine, here it is not."""
    assert "name" in {detector.kind for detector in PII_DETECTORS}
    redacted, kinds = redact("Northwind Telecom restored the line on Monday in Chennai.")
    assert "name" not in kinds
    assert redacted.startswith("Northwind Telecom")


# ---------------------------------------------------------------------------
# Two bypasses the URL whitelist had, and a judge verdict that scored itself perfect
# ---------------------------------------------------------------------------
def test_userinfo_does_not_pass_a_link_off_as_an_allowed_domain() -> None:
    """`https://allowed.test@phish.test/x` navigates to phish.test; the host is what must be read.

    The rule used to capture `([A-Za-z0-9.\\-]+)` after the scheme. `@` is outside that class, so
    the capture stopped at it and returned the *userinfo* - the allowed domain - while every mail
    client sends the reader to the host after it.
    """
    policy = GuardrailPolicy(
        deterministic={URL_WHITELIST: GuardrailAction.BLOCK}, allowed_url_domains=("example.test",)
    )
    result = Guardrails(policy).check("see https://example.test@phish.test/win", context())
    assert not result.passed
    assert result.blocked_by == URL_WHITELIST


def test_an_uppercase_scheme_does_not_skip_the_rule_entirely() -> None:
    """A rule whose loop body never runs reports "nothing found", which reads as PASSED.

    This is the shipped default's failure mode: `allowed_url_domains` is empty, meaning no link may
    appear in any output, so a link the pattern cannot see is a link nothing stops.
    """
    policy = GuardrailPolicy(deterministic={URL_WHITELIST: GuardrailAction.BLOCK})
    assert not Guardrails(policy).check("see HTTPS://phish.test/x", context()).passed
    assert not Guardrails(policy).check("see HtTpS://phish.test/x", context()).passed


def test_an_allowed_link_still_passes_whatever_punctuation_follows_it() -> None:
    """The fix must not cost the ordinary case: a URL at the end of a sentence is still that URL."""
    policy = GuardrailPolicy(
        deterministic={URL_WHITELIST: GuardrailAction.BLOCK}, allowed_url_domains=("example.test",)
    )
    subject = Guardrails(policy)
    for text in (
        "see https://example.test/a.",
        "see (https://help.example.test/a)",
        "see https://example.test/a, then go",
    ):
        assert subject.check(text, context()).passed, text


@pytest.mark.parametrize("literal", ["NaN", "-NaN", "Infinity", "-Infinity"])
def test_a_judge_that_answers_with_a_non_finite_score_scores_zero(literal: str) -> None:
    """`json.loads` accepts these, and `min(1.0, nan)` returns 1.0 - the clamp made a broken judge perfect.

    `nan < 1.0` is False, so `min` keeps its first argument. A judge nobody can read is not a pass,
    which is the rule `_parse_verdict` already states for a reply that is not JSON at all.
    """
    from engine.generative.guardrails import _parse_verdict

    score, _ = _parse_verdict(f'{{"score": {literal}}}')
    assert score == 0.0


def test_a_readable_judge_verdict_is_untouched() -> None:
    from engine.generative.guardrails import _parse_verdict

    assert _parse_verdict('{"score": 0.9, "reason": "grounded"}') == (0.9, "grounded")
    assert _parse_verdict('{"score": 7}')[0] == 1.0, "a real number still clamps"
    assert _parse_verdict('{"score": -3}')[0] == 0.0
