"""M91 (c): the SMS opt-out line for one-way sender IDs.

"Reply STOP to opt out" cannot work when the sender ID is one-way (India's DLT headers, for one):
the customer has nothing to reply to. `generative.campaign_copy.sms_sender: one_way` swaps the SMS
required line for an `{{opt_out_link}}` merge field, filled the way `{{unsubscribe_link}}` is, and
the `sms_reply_stop_one_way` guardrail rule blocks any "Reply STOP" wording a model still writes.

The default, `two_way`, is today's behaviour; the first tests pin that, and every other test in
`test_win_back.py` and `test_guardrails.py` runs unchanged.

`engine.generative.win_back` is read as a module (`win_back.X`) so that on a tree without the change
each test fails on its own assertion rather than the whole file failing to import.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.config import (
    BudgetConfig,
    CampaignCopyConfig,
    Channel,
    CopySegmentBy,
    LlmConfig,
    load_use_case,
)
from engine.generative import prompts, win_back
from engine.generative.budget import Meter
from engine.generative.contracts import CopySegment, CopyStatus, CopyTemplate
from engine.generative.guardrails import CheckContext, GuardrailAction, Guardrails, load_policy
from engine.generative.prompts import Prompt, RenderedPrompt
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.storage import LocalStorage
from tests.fixtures.make_run import RunSpec, write_run

RULE = "sms_reply_stop_one_way"
ONE_WAY_TEXT = "Hi {{band}}, welcome back to Acme Mobile. {{opt_out_link}}"
REPLY_STOP_TEXT = "Hi {{band}}, welcome back to Acme Mobile. Reply STOP to opt out"


def _config(**overrides: object) -> CampaignCopyConfig:
    fields: dict[str, object] = {
        "variants_per_band": 1,
        "channels": (Channel.SMS, Channel.WHATSAPP),
        "bands_to_write": ("High", "Medium"),
        "allowed_fields": ("snapshot_date", "band"),
        "banned_claims": ("guaranteed",),
        "brand_name": "Acme Mobile",
    }
    fields.update(overrides)
    return CampaignCopyConfig(**fields)  # type: ignore[arg-type]


def _guardrails() -> Guardrails:
    return Guardrails(load_policy(), meter=None)


def _finalize(text: str, config: CampaignCopyConfig) -> CopyTemplate:
    return win_back._finalize_template(
        template_id="High-sms-A",
        band="High",
        channel=Channel.SMS,
        variant="A",
        subject=None,
        text=text,
        attempts=1,
        allowed=win_back.allowed_placeholder_fields(config, Channel.SMS),
        config=config,
        guardrails=Guardrails(
            load_policy(),
            meter=Meter(
                FakeLLMClient(mode=FakeLLMMode.GROUNDED),
                job_id="one_way",
                llm=LlmConfig(),
                budget=BudgetConfig(cache=False),
            ),
        ),
        segment=None,
    )


# ---------------------------------------------------------------------------
# The setting, and the default that keeps today's behaviour
# ---------------------------------------------------------------------------
def test_sms_sender_defaults_to_two_way_and_accepts_only_the_two_values() -> None:
    assert CampaignCopyConfig().sms_sender == "two_way"
    assert CampaignCopyConfig(sms_sender="one_way").sms_sender == "one_way"
    with pytest.raises(ValidationError):
        CampaignCopyConfig(sms_sender="shortcode")  # type: ignore[arg-type]


def test_the_shipped_engine_yaml_documents_the_default() -> None:
    from engine.config import load_engine_config

    assert load_engine_config().defaults["generative"]["campaign_copy"]["sms_sender"] == "two_way"


def test_opt_out_link_is_a_reserved_field_beside_unsubscribe_link() -> None:
    assert win_back.OPT_OUT_LINK_FIELD == "opt_out_link"
    assert win_back.OPT_OUT_LINK_FIELD in win_back._RESERVED_FIELDS
    assert win_back.UNSUBSCRIBE_FIELD in win_back._RESERVED_FIELDS


def test_allowed_placeholder_fields_returns_opt_out_link_for_sms_only_and_only_when_one_way() -> None:
    one_way = _config(sms_sender="one_way")
    assert win_back.OPT_OUT_LINK_FIELD in win_back.allowed_placeholder_fields(one_way, Channel.SMS)
    assert win_back.OPT_OUT_LINK_FIELD not in win_back.allowed_placeholder_fields(one_way, Channel.WHATSAPP)
    assert win_back.OPT_OUT_LINK_FIELD not in win_back.allowed_placeholder_fields(one_way, Channel.EMAIL)

    default = _config()
    for channel in Channel:
        assert win_back.OPT_OUT_LINK_FIELD not in win_back.allowed_placeholder_fields(default, channel)


# ---------------------------------------------------------------------------
# The guardrail rule
# ---------------------------------------------------------------------------
def test_the_shipped_policy_blocks_on_sms_reply_stop_one_way() -> None:
    assert load_policy().action(RULE) is GuardrailAction.BLOCK


@pytest.mark.parametrize(
    "wording",
    [
        "Reply STOP to opt out",
        "reply stop to unsubscribe",
        "Reply 'STOP' to opt out",
        "Text STOP to opt out",
        "Send STOP to 56767",
        "SMS STOP to 1909",
        "To opt out SMS STOP to 1909",
        "Reply with the word STOP",
        "Reply with the word 'STOP' to opt out",
        "Typing STOP will opt you out",
        "Messaging STOP opts you out",
        "Texting STOP is all it takes",
        "Respond STOP to leave",
    ],
)
def test_reply_stop_wording_is_blocked_when_the_sender_is_one_way(wording: str) -> None:
    result = _guardrails().check(
        f"Hi {{{{first_name}}}}, welcome back. {wording}",
        CheckContext(target="t", one_way_sender=True, allowed_fields=("first_name",)),
    )
    assert not result.passed
    assert result.blocked_by == RULE
    detail = next(check.detail for check in result.checks if check.rule == RULE)
    assert "STOP" not in detail  # a failure names the rule, never the value


def test_reply_stop_wording_is_not_blocked_by_this_rule_when_the_sender_is_two_way() -> None:
    result = _guardrails().check(
        "Hi {{first_name}}, welcome back. Reply STOP to opt out",
        CheckContext(target="t", required_line="Reply STOP to opt out", allowed_fields=("first_name",)),
    )
    assert result.passed
    assert [check.outcome.value for check in result.checks if check.rule == RULE] == ["passed"]


def test_a_one_way_message_with_the_link_and_no_reply_stop_wording_passes_the_rule() -> None:
    result = _guardrails().check(
        "Hi {{first_name}}, welcome back. Stop by any time. {{opt_out_link}}",
        CheckContext(
            target="t",
            one_way_sender=True,
            required_line="{{opt_out_link}}",
            allowed_fields=("first_name", "opt_out_link"),
        ),
    )
    assert result.passed, result


# ---------------------------------------------------------------------------
# Templates and rendering
# ---------------------------------------------------------------------------
def test_the_one_way_sms_required_line_is_the_opt_out_link_placeholder() -> None:
    assert win_back.required_line_for(_config(sms_sender="one_way"), Channel.SMS) == "{{opt_out_link}}"
    # WhatsApp has a reply channel of its own, so its line does not change.
    assert win_back.required_line_for(_config(sms_sender="one_way"), Channel.WHATSAPP) == (
        "Reply STOP to opt out"
    )
    assert win_back.required_line_for(_config(), Channel.SMS) == "Reply STOP to opt out"


def test_a_custom_line_that_the_reply_stop_pattern_misses_is_still_replaced_when_one_way() -> None:
    """Under one_way the line *is* the placeholder, whatever the operator's wording was."""
    config = _config(
        sms_sender="one_way",
        required_lines={"sms": "To opt out SMS STOP to 1909"},
    )
    assert win_back.required_line_for(config, Channel.SMS) == "{{opt_out_link}}"
    # The default sender keeps the operator's line exactly.
    two_way = _config(required_lines={"sms": "To opt out SMS STOP to 1909"})
    assert win_back.required_line_for(two_way, Channel.SMS) == "To opt out SMS STOP to 1909"


def test_a_custom_line_that_already_holds_the_placeholder_is_kept_when_one_way() -> None:
    line = "Unsubscribe: {{opt_out_link}}"
    config = _config(sms_sender="one_way", required_lines={"sms": line})
    assert win_back.required_line_for(config, Channel.SMS) == line


def test_a_template_ending_in_the_spaced_placeholder_passes_required_lines_when_one_way() -> None:
    config = _config(sms_sender="one_way")
    template = _finalize("Hi {{band}}, welcome back to Acme Mobile. {{ opt_out_link }}", config)
    assert template.status is CopyStatus.PENDING_REVIEW, template.block_reason
    assert template.fields_used == ("band", "opt_out_link")


def test_a_template_ending_in_opt_out_link_passes_every_check_when_one_way() -> None:
    config = _config(sms_sender="one_way")
    template = _finalize(ONE_WAY_TEXT, config)

    assert template.status is CopyStatus.PENDING_REVIEW, template.block_reason
    assert template.fields_used == ("band", "opt_out_link")
    by_rule = {check.rule: check.outcome.value for check in template.guardrails}
    assert by_rule["allowed_fields_only"] == "passed"
    assert by_rule["required_lines"] == "passed"
    assert by_rule[RULE] == "passed"


def test_the_same_template_is_refused_by_default_because_opt_out_link_is_not_an_allowed_field() -> None:
    template = _finalize(ONE_WAY_TEXT, _config())
    assert template.status is CopyStatus.BLOCKED
    assert template.block_reason is not None
    assert "opt_out_link" in template.block_reason


def test_a_reply_stop_template_is_blocked_when_one_way_and_passes_by_default() -> None:
    blocked = _finalize(REPLY_STOP_TEXT, _config(sms_sender="one_way"))
    assert blocked.status is CopyStatus.BLOCKED
    assert blocked.block_reason == f"failed the {RULE} check"

    default = _finalize(REPLY_STOP_TEXT, _config())
    assert default.status is CopyStatus.PENDING_REVIEW, default.block_reason


def test_a_one_way_template_renders_with_the_merge_field_filled_like_unsubscribe_link() -> None:
    config = _config(sms_sender="one_way")
    template = _finalize(ONE_WAY_TEXT, config)

    message = win_back.render_message(
        template,
        win_back._row_values({"band": "High"}, template),
        entity_key="C-1",
        config=config,
        guardrails=_guardrails(),
        backend="fake",
    )

    assert message.block_reason is None
    assert message.rendered_text == "Hi High, welcome back to Acme Mobile. opt_out_link"


def test_a_rendering_that_says_reply_stop_is_blocked_when_one_way() -> None:
    """A customer's own field value can carry the wording the template did not."""
    config = _config(sms_sender="one_way", allowed_fields=("last_offer",))
    template = CopyTemplate(
        template_id="t",
        band="High",
        channel=Channel.SMS.value,
        variant="A",
        subject=None,
        text="Offer: {{last_offer}} {{opt_out_link}}",
        fields_used=("last_offer", "opt_out_link"),
        status=CopyStatus.PENDING_REVIEW,
        judge_scores=(),
        guardrails=(),
        block_reason=None,
        attempts=1,
        approved_by=None,
        approved_at=None,
    )
    message = win_back.render_message(
        template,
        {"last_offer": "Reply STOP to opt out", "opt_out_link": "opt_out_link"},
        entity_key="C-1",
        config=config,
        guardrails=_guardrails(),
        backend="fake",
    )
    assert message.rendered_text == ""
    assert message.block_reason == f"failed the {RULE} check"


# ---------------------------------------------------------------------------
# End to end, through the grounded fake model
# ---------------------------------------------------------------------------
def test_generating_copy_with_a_one_way_sender_writes_sms_ending_in_the_opt_out_link(
    config_root: Path, tmp_path: Path
) -> None:
    use_case_id = "win-back-campaign"
    use_case = load_use_case(use_case_id, config_root)
    use_case = use_case.model_copy(
        update={
            "generative": use_case.generative.model_copy(
                update={"campaign_copy": _config(sms_sender="one_way")}
            )
        }
    )
    storage = LocalStorage(tmp_path)
    run_id = write_run(
        storage, RunSpec(use_case_id=use_case_id, rows=150, positive_rate=0.3, config_root=config_root)
    )
    client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    meter = Meter(client, job_id="one_way_e2e", llm=LlmConfig(), budget=BudgetConfig(cache=False))

    result = win_back.generate_campaign_copy(
        run_id=run_id,
        use_case=use_case,
        storage=storage,
        meter=meter,
        guardrails=Guardrails(load_policy(config_root), meter=meter, prompts_root=config_root),
        config_root=config_root,
    )

    sms = [m for m in result.messages if m.channel == Channel.SMS.value]
    assert sms, "the run produced no SMS messages"
    assert all(m.block_reason is None for m in sms), {m.block_reason for m in sms}
    assert all(m.rendered_text.endswith("opt_out_link") for m in sms)
    assert all("STOP" not in m.rendered_text for m in sms)
    whatsapp = [m for m in result.messages if m.channel == Channel.WHATSAPP.value]
    assert whatsapp and all(m.rendered_text.endswith("Reply STOP to opt out") for m in whatsapp)


# ---------------------------------------------------------------------------
# What the model is told: the allowed list must not contradict the required line
# ---------------------------------------------------------------------------
_PLACEHOLDERS_LINE = "Placeholders you may use:"


def _prompt_user_texts(monkeypatch: pytest.MonkeyPatch, config: CampaignCopyConfig) -> dict[str, str]:
    """Render the SMS band prompt and the SMS segment prompt, return each one's user text by name."""
    seen: dict[str, str] = {}
    real_render = prompts.render

    def recording_render(prompt: Prompt, values: Mapping[str, object]) -> RenderedPrompt:
        rendered = real_render(prompt, values)
        seen[rendered.name] = rendered.user
        return rendered

    monkeypatch.setattr("engine.generative.win_back.render", recording_render)
    meter = Meter(
        FakeLLMClient(mode=FakeLLMMode.GROUNDED),
        job_id="one_way_prompt",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False),
    )
    guardrails = Guardrails(load_policy(), meter=meter)
    win_back._generate_for_band_channel(
        band="High",
        band_action="call",
        channel=Channel.SMS,
        reasons=(),
        config=config,
        entity="customer",
        meter=meter,
        guardrails=guardrails,
        config_root=None,
    )
    segment = CopySegment(
        segment="reason:tenure_months",
        label="Main reason: tenure_months",
        rows=10,
        share_pct=50.0,
        written=True,
    )
    win_back._generate_for_segment_channel(
        segment=segment,
        segment_by=CopySegmentBy.TOP_REASON,
        channel=Channel.SMS,
        config=config,
        entity="customer",
        score_name="churn risk",
        meter=meter,
        guardrails=guardrails,
        config_root=None,
    )
    return seen


def _placeholders_line(user_text: str) -> str:
    return next(line for line in user_text.splitlines() if line.startswith(_PLACEHOLDERS_LINE))


def test_the_one_way_sms_prompts_list_opt_out_link_among_the_placeholders_you_may_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _prompt_user_texts(monkeypatch, _config(sms_sender="one_way"))
    assert set(seen) == {"copy_sms", "copy_segment_sms"}
    for name, user_text in seen.items():
        line = _placeholders_line(user_text)
        assert "opt_out_link" in line, name
        assert "snapshot_date" in line, name  # the configured fields are still there


def test_the_default_sms_prompts_are_unchanged_and_do_not_mention_opt_out_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _prompt_user_texts(monkeypatch, _config())
    for name, user_text in seen.items():
        assert _placeholders_line(user_text) == f"{_PLACEHOLDERS_LINE} snapshot_date, band", name
        assert "opt_out_link" not in user_text, name
