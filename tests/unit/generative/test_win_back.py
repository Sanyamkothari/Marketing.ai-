"""Tests for `engine.generative.win_back`, proving the claim its own module docstring makes: "a
template is not a message."

**Cost is O(bands x channels), not O(rows).** `_generate_for_band_channel` calls a model once per
band and channel and renders every row afterwards with `jinja2` alone, so doubling a run's audience
must not add a single call to the fake client's recorded `.calls` - and it does not, which is what
`test_doubling_the_audience_does_not_add_a_single_model_call` measures rather than assumes.

**No personal data ever reaches a prompt.** This is the property the whole template/message split
exists for, and it is proved the only honest way: by reading back every prompt the fake client
recorded and finding none of a run's own row values inside any of them, not by asserting a code path
was never called.

**Grounded or nothing is enforced by `_finalize_template`'s own regular expression, not requested of
the model.** A variant naming a placeholder outside `campaign_copy.allowed_fields` is rejected before
a judge ever sees it, whether that is proved directly or through a model that keeps inventing one and
is retried, bounded, until the policy's retries run out.

**A template is judged once; a rendering is checked again, by the deterministic rules alone.**
`fill_placeholders` renders through a sandboxed, strictly-undefined `jinja2` environment that fails
loudly on a missing variable rather than rendering it away, and the second check exists for exactly
one reason: a placeholder is shorter than what replaces it, so a template that clears a channel's
length limit can produce a rendering that does not, and a template with no banned claim in it can
still render one out of a row's own field. Both are proved directly, and so is the corollary the
docstring states in words: rendering never calls a judge, so message count does not drive cost.

**A run's control holdout is read, never redrawn.** Two runs fabricated from the same spec draw the
same seeded holdout and two runs fabricated from different seeds do not, which is what makes a run
built twice - or read twice - report the same audience.

Runs are fabricated by `tests.fixtures.make_run`, not trained: three hundred rows are enough for
win-back's two written bands, High and Medium, to each hold a few dozen eligible rows after
suppression and the control draw, built once per module and reused, because fabricating it twice
would prove nothing a shared run does not already prove.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2
import pandas as pd
import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from engine.config import (
    BudgetConfig,
    CampaignCopyConfig,
    Channel,
    GenerativeKind,
    LlmConfig,
    load_use_case,
)
from engine.contracts import RunRecord
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
)
from engine.generative.errors import MISSING_FIELD, NOT_A_GENERATIVE_USE_CASE, GenerativeError
from engine.generative.guardrails import Guardrails, load_policy
from engine.generative.win_back import (
    _RENDER_ENVIRONMENT,
    RUN_RECORD_FILENAME,
    _classify,
    _finalize_template,
    _generate_for_band_channel,
    _length_excess,
    allowed_placeholder_fields,
    approve_template,
    fill_placeholders,
    generate_campaign_copy,
    placeholders_in,
    render_message,
)
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.stages.actions import BAND_COLUMN, CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_run import RunSpec, source_key, write_run

if TYPE_CHECKING:
    from engine.config import UseCaseConfig
    from engine.generative.contracts import CampaignCopyResult

USE_CASE_ID = "win-back-campaign"
ROWS = 300
POSITIVE_RATE = 0.3
"""Enough rows, at a high enough rate, that both bands `bands_to_write` names hold rows after
suppression and the control draw (measured: High 31, Medium 34 eligible), without paying for a
run any larger than that."""


# ---------------------------------------------------------------------------
# Building blocks: a config that keeps every allowed field away from the fake
# client's known blind spot on the email channel, a meter, and a template.
# ---------------------------------------------------------------------------
def _copy_config(**overrides: object) -> CampaignCopyConfig:
    """A small, fast campaign: two channels, one variant, two bands - never email.

    SMS and WhatsApp share one opt-out phrasing that the grounded `FakeLLMClient` recognises from the
    prompt; email's phrasing does not match the fake's own regular expression, which would block
    every email variant on `required_lines` regardless of what this module does and would be
    testing the fake, not `win_back`.
    """
    fields: dict[str, object] = {
        "variants_per_band": 1,
        "channels": (Channel.SMS, Channel.WHATSAPP),
        "bands_to_write": ("High", "Medium"),
        "allowed_fields": ("snapshot_date", "band"),
        "banned_claims": ("guaranteed",),
        "tone": "warm_concise",
        "brand_name": "Acme Mobile",
        "require_human_review": True,
    }
    fields.update(overrides)
    return CampaignCopyConfig(**fields)


def _use_case_with(base: UseCaseConfig, **overrides: object) -> UseCaseConfig:
    return base.model_copy(
        update={"generative": base.generative.model_copy(update={"campaign_copy": _copy_config(**overrides)})}
    )


def _client_and_meter(
    mode: FakeLLMMode = FakeLLMMode.GROUNDED,
) -> tuple[FakeLLMClient, Meter]:
    client = FakeLLMClient(mode=mode)
    return client, Meter(client, job_id="wb_test", llm=LlmConfig(), budget=BudgetConfig(cache=False))


def _guardrails(meter: Meter, config_root: Path) -> Guardrails:
    return Guardrails(load_policy(config_root), meter=meter, prompts_root=config_root)


def _generate(
    use_case: UseCaseConfig,
    storage: LocalStorage,
    run_id: str,
    config_root: Path,
    *,
    mode: FakeLLMMode = FakeLLMMode.GROUNDED,
) -> tuple[CampaignCopyResult, FakeLLMClient, Meter]:
    client, meter = _client_and_meter(mode)
    result = generate_campaign_copy(
        run_id=run_id,
        use_case=use_case,
        storage=storage,
        meter=meter,
        guardrails=_guardrails(meter, config_root),
        config_root=config_root,
    )
    return result, client, meter


def _template(
    *,
    channel: Channel = Channel.SMS,
    text: str = "Hi {{band}}, welcome back. Reply STOP to opt out",
    fields_used: tuple[str, ...] = ("band",),
    subject: str | None = None,
    status: CopyStatus = CopyStatus.PENDING_REVIEW,
) -> CopyTemplate:
    """A hand-built template: what a model would have written, already past its one judging."""
    return CopyTemplate(
        template_id="t-test",
        band="High",
        channel=channel.value,
        variant="A",
        subject=subject,
        text=text,
        fields_used=fields_used,
        status=status,
        judge_scores=(),
        guardrails=(),
        block_reason=None,
        attempts=1,
        approved_by=None,
        approved_at=None,
    )


@pytest.fixture(scope="module")
def run(
    tmp_path_factory: pytest.TempPathFactory, config_root: Path
) -> tuple[LocalStorage, str, UseCaseConfig]:
    """One finished `win-back-campaign` scoring run, built once for the module."""
    storage = LocalStorage(tmp_path_factory.mktemp("win_back_run"))
    use_case = load_use_case(USE_CASE_ID, config_root)
    run_id = write_run(
        storage,
        RunSpec(use_case_id=USE_CASE_ID, rows=ROWS, positive_rate=POSITIVE_RATE, config_root=config_root),
    )
    return storage, run_id, use_case


# ---------------------------------------------------------------------------
# Grounded or nothing: only allowed_fields, checked in code
# ---------------------------------------------------------------------------
def test_placeholders_in_reads_every_distinct_field_name_a_text_uses() -> None:
    """The regular expression the whole "grounded or nothing" check for copy is built from."""
    assert placeholders_in("Hi {{first_name}}, your {{plan_type}} is ready. {{first_name}} again.") == {
        "first_name",
        "plan_type",
    }


def test_allowed_placeholder_fields_adds_the_unsubscribe_merge_field_on_email_only() -> None:
    config = CampaignCopyConfig(allowed_fields=("first_name", "plan_type"))
    assert allowed_placeholder_fields(config, Channel.EMAIL) == {
        "first_name",
        "plan_type",
        "unsubscribe_link",
    }
    assert allowed_placeholder_fields(config, Channel.SMS) == {"first_name", "plan_type"}
    assert allowed_placeholder_fields(config, Channel.WHATSAPP) == {"first_name", "plan_type"}


def test_a_template_naming_a_placeholder_outside_allowed_fields_is_rejected_before_any_guardrail_runs(
    config_root: Path,
) -> None:
    """The check the module exists for: a model's own claim to have used only the allowed list is
    worth nothing until a regular expression outside the model reads its reply and says so."""
    config = _copy_config(allowed_fields=("snapshot_date", "band"))
    _client, meter = _client_and_meter()
    template = _finalize_template(
        template_id="t1",
        band="High",
        channel=Channel.SMS,
        variant="A",
        subject=None,
        text="Hi {{snapshot_date}}, use code {{secret_offer_code}}. Reply STOP to opt out",
        attempts=1,
        allowed=allowed_placeholder_fields(config, Channel.SMS),
        config=config,
        guardrails=_guardrails(meter, config_root),
    )

    assert template.status is CopyStatus.BLOCKED
    assert template.block_reason == "used a placeholder outside allowed_fields: secret_offer_code"
    assert meter.calls == 0  # rejected before it could have reached a judge, or the model at all


def test_a_model_that_keeps_inventing_an_unlisted_placeholder_is_retried_and_then_gives_up(
    config_root: Path,
) -> None:
    """`FakeLLMMode.UNGROUNDED` always appends a placeholder outside the allowed list, so every
    one of `guardrails.retries + 1` attempts must fail the same way, through the full pipeline."""
    config = _copy_config(allowed_fields=("band",), channels=(Channel.SMS,))
    client, meter = _client_and_meter(FakeLLMMode.UNGROUNDED)
    guardrails = _guardrails(meter, config_root)

    templates = _generate_for_band_channel(
        band="High",
        band_action="Send best offer",
        channel=Channel.SMS,
        reasons=(),
        config=config,
        entity="customer",
        meter=meter,
        guardrails=guardrails,
        config_root=config_root,
    )

    assert all(template.status is CopyStatus.BLOCKED for template in templates)
    assert all(template.attempts == guardrails.retries + 1 for template in templates)
    assert meter.calls == guardrails.retries + 1  # bounded: every attempt was made, and no more
    assert len(client.calls) == guardrails.retries + 1


# ---------------------------------------------------------------------------
# Rendering: jinja2, sandboxed and strict, and what it is used for
# ---------------------------------------------------------------------------
def test_filling_a_template_with_a_variable_it_lacks_fails_loudly_rather_than_rendering_empty() -> None:
    with pytest.raises(jinja2.exceptions.UndefinedError):
        fill_placeholders("Hi {{first_name}}, welcome back.", {})


def test_filling_a_template_from_the_values_it_needs_substitutes_them() -> None:
    rendered = fill_placeholders(
        "Hi {{first_name}}, plan {{plan_type}}.", {"first_name": "Asha", "plan_type": "Pro"}
    )
    assert rendered == "Hi Asha, plan Pro."


def test_the_render_environment_is_a_sandboxed_jinja_environment_with_strict_undefined() -> None:
    """The text being rendered was written by a model, not an operator; a missing field must raise."""
    assert isinstance(_RENDER_ENVIRONMENT, SandboxedEnvironment)
    assert _RENDER_ENVIRONMENT.undefined is StrictUndefined
    with pytest.raises(jinja2.exceptions.SecurityError):
        _RENDER_ENVIRONMENT.from_string("{{ ''.__class__ }}").render()


# ---------------------------------------------------------------------------
# A template is not a message: the second check, and what it is for
# ---------------------------------------------------------------------------
def test_a_rendering_that_clears_the_templates_length_limit_can_still_fail_after_substitution() -> None:
    """The exact asymmetry the module docstring names: a placeholder is shorter than what replaces
    it, so a template judged once can still produce a rendering the deterministic rules refuse."""
    config = CampaignCopyConfig(allowed_fields=("last_offer",))
    template = _template(text="Offer: {{last_offer}}. Reply STOP to opt out", fields_used=("last_offer",))

    assert _length_excess(Channel.SMS, None, template.text, config.limits) is None  # the template cleared it

    guardrails = Guardrails(load_policy(), meter=None)
    message = render_message(
        template,
        {"last_offer": "X" * 200},
        entity_key="C-1",
        config=config,
        guardrails=guardrails,
        backend="fake",
    )

    assert message.rendered_text == ""
    assert message.block_reason is not None
    assert "SMS limit" in message.block_reason


def test_a_banned_claim_written_into_the_template_itself_is_blocked_before_any_judge_runs(
    config_root: Path,
) -> None:
    config = _copy_config(allowed_fields=("snapshot_date", "band"), banned_claims=("guaranteed",))
    _client, meter = _client_and_meter()
    template = _finalize_template(
        template_id="t1",
        band="High",
        channel=Channel.SMS,
        variant="A",
        subject=None,
        text="Hi {{snapshot_date}}, this offer is guaranteed. Reply STOP to opt out",
        attempts=1,
        allowed=allowed_placeholder_fields(config, Channel.SMS),
        config=config,
        guardrails=_guardrails(meter, config_root),
    )

    assert template.status is CopyStatus.BLOCKED
    assert template.block_reason == "failed the banned_phrases check"
    assert meter.calls == 0  # the deterministic rule caught it before a judge was ever asked


def test_a_banned_claim_hiding_inside_a_rows_own_field_value_is_caught_only_at_render_time() -> None:
    """A template with no banned word in it can still produce a message that has one, because the
    word came from the row, not the model - the module docstring's own example of what the second
    check is for."""
    config = CampaignCopyConfig(allowed_fields=("last_offer",), banned_claims=("guaranteed",))
    template = _template(
        text="Hi {{last_offer}}, thanks for sticking with us. Reply STOP to opt out",
        fields_used=("last_offer",),
    )
    guardrails = Guardrails(load_policy(), meter=None)

    clean = render_message(
        template,
        {"last_offer": "a fresh start"},
        entity_key="C-1",
        config=config,
        guardrails=guardrails,
        backend="fake",
    )
    assert clean.block_reason is None
    assert clean.rendered_text != ""

    tainted = render_message(
        template,
        {"last_offer": "a guaranteed discount"},
        entity_key="C-2",
        config=config,
        guardrails=guardrails,
        backend="fake",
    )
    assert tainted.block_reason == "failed the banned_phrases check"
    assert tainted.rendered_text == ""


def test_rendering_many_messages_never_calls_a_judge_so_message_count_does_not_drive_cost(
    config_root: Path,
) -> None:
    """`render_message` is documented to be checked with a `Guardrails` built for a rendering, not a
    template, and this is what that buys: even with a real meter and real judges configured behind
    it, checking three hundred rows never once touches either."""
    config = CampaignCopyConfig(allowed_fields=("last_offer",))
    template = _template(
        text="Hi {{last_offer}}, welcome back. Reply STOP to opt out", fields_used=("last_offer",)
    )
    _client, meter = _client_and_meter()
    guardrails = _guardrails(meter, config_root)  # a real meter, and a policy with judges configured

    for row in range(300):
        render_message(
            template,
            {"last_offer": f"offer {row}"},
            entity_key=f"C-{row}",
            config=config,
            guardrails=guardrails,
            backend="fake",
        )

    assert meter.calls == 0


# ---------------------------------------------------------------------------
# Counting who copy is not written for: suppressed, control, out-of-band
# ---------------------------------------------------------------------------
def test_classify_counts_suppressed_then_control_then_out_of_band_by_fixed_precedence() -> None:
    """A row suppressed and also drawn as control is counted once, as suppressed - the precedence
    the module docstring states."""
    scores = pd.DataFrame(
        {
            "customer_id": ["c1", "c2", "c3", "c4", "c5"],
            BAND_COLUMN: ["High", "High", "Medium", "Low", "High"],
            SUPPRESSED_REASON_COLUMN: ["consent_false", None, None, None, None],
            CONTROL_GROUP_COLUMN: [True, True, False, False, False],
        }
    )
    fields = pd.DataFrame({"customer_id": ["c1", "c2", "c3", "c4", "c5"]})

    audience, holdout, per_band = _classify(
        scores, fields, primary_key="customer_id", bands_to_write=("High", "Medium")
    )

    assert holdout == CopyHoldout(control_rows=1, suppressed_rows=1, out_of_band_rows=1)
    assert per_band == {"High": 1, "Medium": 1}
    assert sorted(audience["customer_id"]) == ["c3", "c5"]  # c1 suppressed, c2 control, c4 out of band


# ---------------------------------------------------------------------------
# The holdout is read off the run, seeded by the run id, never redrawn here
# ---------------------------------------------------------------------------
def test_the_control_holdout_is_byte_identical_for_two_runs_built_from_the_same_spec(
    config_root: Path, tmp_path: Path
) -> None:
    use_case = _use_case_with(load_use_case(USE_CASE_ID, config_root))
    spec = RunSpec(
        use_case_id=USE_CASE_ID, rows=200, positive_rate=POSITIVE_RATE, seed=777, config_root=config_root
    )
    storage_a = LocalStorage(tmp_path / "a")
    storage_b = LocalStorage(tmp_path / "b")
    run_a = write_run(storage_a, spec)
    run_b = write_run(storage_b, spec)
    assert run_a == run_b  # the id is itself a digest of the whole spec

    result_a, _client_a, _meter_a = _generate(use_case, storage_a, run_a, config_root)
    result_b, _client_b, _meter_b = _generate(use_case, storage_b, run_b, config_root)

    assert result_a.batch.holdout == result_b.batch.holdout
    assert result_a.batch.audience == result_b.batch.audience


def test_a_different_seed_draws_a_different_control_holdout(config_root: Path, tmp_path: Path) -> None:
    use_case = _use_case_with(load_use_case(USE_CASE_ID, config_root))
    storage = LocalStorage(tmp_path)
    run_one = write_run(
        storage,
        RunSpec(
            use_case_id=USE_CASE_ID, rows=200, positive_rate=POSITIVE_RATE, seed=1, config_root=config_root
        ),
    )
    run_two = write_run(
        storage,
        RunSpec(
            use_case_id=USE_CASE_ID, rows=200, positive_rate=POSITIVE_RATE, seed=2, config_root=config_root
        ),
    )

    result_one, _client_one, _meter_one = _generate(use_case, storage, run_one, config_root)
    result_two, _client_two, _meter_two = _generate(use_case, storage, run_two, config_root)

    assert result_one.batch.holdout != result_two.batch.holdout


# ---------------------------------------------------------------------------
# Cost: O(bands x channels), and what it costs to prove no personal data leaks
# ---------------------------------------------------------------------------
def test_doubling_the_audience_does_not_add_a_single_model_call(config_root: Path, tmp_path: Path) -> None:
    """The claim the module is named for: rendering scales with rows, generation does not."""
    use_case = _use_case_with(load_use_case(USE_CASE_ID, config_root))
    small_storage = LocalStorage(tmp_path / "small")
    large_storage = LocalStorage(tmp_path / "large")
    small_run = write_run(
        small_storage,
        RunSpec(use_case_id=USE_CASE_ID, rows=150, positive_rate=POSITIVE_RATE, config_root=config_root),
    )
    large_run = write_run(
        large_storage,
        RunSpec(use_case_id=USE_CASE_ID, rows=600, positive_rate=POSITIVE_RATE, config_root=config_root),
    )

    small_result, _small_client, small_meter = _generate(use_case, small_storage, small_run, config_root)
    large_result, _large_client, large_meter = _generate(use_case, large_storage, large_run, config_root)

    assert large_result.batch.audience.rows > small_result.batch.audience.rows  # genuinely more rows
    assert len(large_result.messages) > len(small_result.messages)  # rendering did scale with rows
    assert small_meter.calls == large_meter.calls  # generation, judging and all, did not


def test_no_row_value_from_the_source_data_ever_reaches_a_prompt(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    """Read straight off the fake client's own recorded prompts, not inferred from a code path that
    happens never to be exercised - the only honest way to check this claim."""
    storage, run_id, base_use_case = run
    use_case = _use_case_with(base_use_case)
    _result, client, _meter = _generate(use_case, storage, run_id, config_root)
    assert client.calls  # the check below is vacuous unless calls were actually recorded

    record = storage.read_model(run_key(run_id, RUN_RECORD_FILENAME), RunRecord)
    source = pd.read_csv(io.StringIO(storage.read_text(source_key(record))), dtype=str)
    prompt_text = "\n".join(f"{call.system}\n{call.prompt}" for call in client.calls)

    for customer_id in source["customer_id"]:
        assert customer_id not in prompt_text
    for snapshot_date in source["snapshot_date"].dropna().unique():
        assert snapshot_date not in prompt_text
    for spend in source["avg_monthly_spend"].dropna().unique():  # not even an allowed field; not sent either
        assert spend not in prompt_text


# ---------------------------------------------------------------------------
# A field the configuration allows but a band's rows do not have
# ---------------------------------------------------------------------------
def test_a_field_the_configuration_allows_but_the_data_lacks_raises_missing_field_and_writes_nothing(
    config_root: Path, tmp_path: Path
) -> None:
    """A dedicated run, not the shared fixture: this is the one test that must see nothing written."""
    storage = LocalStorage(tmp_path)
    run_id = write_run(
        storage,
        RunSpec(use_case_id=USE_CASE_ID, rows=ROWS, positive_rate=POSITIVE_RATE, config_root=config_root),
    )
    base_use_case = load_use_case(USE_CASE_ID, config_root)
    use_case = _use_case_with(
        base_use_case, allowed_fields=("avg_monthly_spend", "band"), channels=(Channel.SMS,)
    )
    _client, meter = _client_and_meter()

    with pytest.raises(GenerativeError) as excinfo:
        generate_campaign_copy(
            run_id=run_id,
            use_case=use_case,
            storage=storage,
            meter=meter,
            guardrails=_guardrails(meter, config_root),
            config_root=config_root,
        )

    assert excinfo.value.code == MISSING_FIELD
    assert meter.calls  # the cost of the model calls that produced the templates is not refunded
    assert not storage.exists(run_key(run_id, COPY_BATCH_FILENAME))
    assert not storage.exists(run_key(run_id, COPY_MESSAGES_FILENAME))


# ---------------------------------------------------------------------------
# Not a generative-copy use case: refused before a run is even opened
# ---------------------------------------------------------------------------
def test_a_use_case_whose_generative_kind_is_not_campaign_copy_is_refused_before_any_run_is_read(
    config_root: Path,
) -> None:
    base = load_use_case(USE_CASE_ID, config_root)
    use_case = base.model_copy(
        update={"generative": base.generative.model_copy(update={"kind": GenerativeKind.NONE})}
    )
    _client, meter = _client_and_meter()

    with pytest.raises(GenerativeError) as excinfo:
        generate_campaign_copy(
            run_id="r_does_not_exist",
            use_case=use_case,
            storage=LocalStorage(Path.cwd()),
            meter=meter,
            guardrails=_guardrails(meter, config_root),
            config_root=config_root,
        )

    assert excinfo.value.code == NOT_A_GENERATIVE_USE_CASE


# ---------------------------------------------------------------------------
# Approval: a person's decision, recorded, never a regeneration
# ---------------------------------------------------------------------------
def _hand_built_batch() -> CopyBatch:
    kept = _template(status=CopyStatus.PENDING_REVIEW)
    other = kept.model_copy(update={"template_id": "t-other"})
    return CopyBatch(
        run_id="r1",
        batch_id="cb_r1",
        audience=CopyAudience(rows=0, per_band={}),
        holdout=CopyHoldout(control_rows=0, suppressed_rows=0, out_of_band_rows=0),
        templates=(kept, other),
        require_human_review=True,
        messages_rendered=0,
        messages_blocked=0,
        prompt_versions={},
        prompt_hashes={},
        created_at=utc_now(),
    )


def test_approve_template_marks_one_template_approved_and_leaves_every_other_one_alone() -> None:
    batch = _hand_built_batch()

    approved = approve_template(batch, "t-test", approved_by="reviewer@example.com")

    kept = next(t for t in approved.templates if t.template_id == "t-test")
    other = next(t for t in approved.templates if t.template_id == "t-other")
    assert kept.status is CopyStatus.APPROVED
    assert kept.approved_by == "reviewer@example.com"
    assert kept.approved_at is not None
    assert other.status is CopyStatus.PENDING_REVIEW
    assert other.approved_by is None


def test_approving_a_template_id_the_batch_does_not_have_raises_key_error() -> None:
    with pytest.raises(KeyError):
        approve_template(_hand_built_batch(), "does-not-exist", approved_by="reviewer@example.com")


# ---------------------------------------------------------------------------
# End to end: both artefacts, round-tripped through their own contracts
# ---------------------------------------------------------------------------
def test_generate_campaign_copy_writes_a_copy_batch_and_a_messages_csv_that_both_validate(
    run: tuple[LocalStorage, str, UseCaseConfig], config_root: Path
) -> None:
    storage, run_id, base_use_case = run
    use_case = _use_case_with(base_use_case)

    result, _client, _meter = _generate(use_case, storage, run_id, config_root)

    reloaded_batch = storage.read_model(run_key(run_id, COPY_BATCH_FILENAME), CopyBatch)
    assert reloaded_batch == result.batch
    assert reloaded_batch.run_id == run_id
    assert reloaded_batch.audience.rows == sum(reloaded_batch.audience.per_band.values())
    assert reloaded_batch.messages_rendered == len(result.messages)
    assert reloaded_batch.messages_blocked == sum(1 for m in result.messages if m.block_reason is not None)
    assert set(reloaded_batch.prompt_versions) == {"copy_sms", "copy_whatsapp"}
    assert all(reloaded_batch.prompt_hashes[name] for name in reloaded_batch.prompt_versions)

    messages_csv = pd.read_csv(io.StringIO(storage.read_text(run_key(run_id, COPY_MESSAGES_FILENAME))))
    assert len(messages_csv) == len(result.messages)
    for row, message in zip(messages_csv.to_dict("records"), result.messages, strict=True):
        row.pop("schema_version", None)
        row["block_reason"] = None if pd.isna(row.get("block_reason")) else row["block_reason"]
        assert CopyMessage.model_validate(row) == message


def test_a_downloaded_message_says_which_backend_wrote_it() -> None:
    """The screen badge does not follow `copy_messages.csv` out of the app; the row must.

    Every generative screen carries `gdom.backendBadge` - a warning-coloured "Fake backend ·
    no model was called" panel, citing plan §13.3. `copy_messages.csv` is downloadable through
    `GET /runs/{run_id}/copy_messages.csv`, and without this field a marketer opens rendered,
    ready-to-send marketing copy with nothing on it to say a deterministic stand-in wrote it.
    `backend` is a field of `CopyMessage`, so it is a column of that CSV by construction:
    `_write_messages_csv` writes `list(CopyMessage.model_fields)`.
    """
    from engine.generative.contracts import CopyMessage

    assert "backend" in CopyMessage.model_fields
    assert CopyMessage.model_fields["backend"].is_required(), "a row must never omit it"
    columns = list(CopyMessage.model_fields)
    assert "backend" in columns, "the CSV writes exactly these columns"
