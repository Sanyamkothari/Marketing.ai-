"""Plan K M140 (DEC-1407): message variants as measured arms - the rules, one at a time.

The journey through the API is `tests/integration/decide/test_variant_arms_journey.py`; the nightly check that a
planted best message is found within its interval is `tests/statistical/test_variant_arms.py`. Here:

* the catalogue reference binds a registration to a variant's exact words;
* the arm draw is per customer key (seeded, never by position), in equal shares, and linear in rows;
* the messages are compared with each other on `measure_campaign`'s own report, and a winner is named only when
  its interval is above every other message's;
* `measure_campaign`'s arguments for a campaign with message arms, and none without;
* the setting is off by default and left out of a configuration dump, and a variant's catalogue action never
  sets a channel's contact cost.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from engine.config import GenerativeConfig
from engine.decide.catalogue import ActionCatalogue
from engine.decide.variant_arms import (
    CONTROL_TEMPLATE_LEVEL,
    NO_MESSAGE_LEVEL,
    VARIANT_ARM_CODES,
    VARIANT_ARM_COLUMN,
    compare_variant_arms,
    draw_arms,
    is_variant_template_ref,
    variant_level,
    variant_measure_arguments,
    variant_template_ref,
)
from engine.generative.contracts import CopyStatus, CopyTemplate
from engine.measurement.measure import measure_campaign
from engine.measurement.simulate import multi_arm_campaign

LEVELS = (NO_MESSAGE_LEVEL, CONTROL_TEMPLATE_LEVEL, "variant_a", "variant_b")


def _template(
    text: str, *, channel: str = "sms", variant: str = "A", subject: str | None = None
) -> CopyTemplate:
    return CopyTemplate(
        template_id=f"High-{channel}-{variant}",
        band="High",
        channel=channel,
        variant=variant,
        subject=subject,
        text=text,
        status=CopyStatus.APPROVED,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# The catalogue reference
# ---------------------------------------------------------------------------
def test_the_reference_names_the_exact_words_on_their_channel() -> None:
    words = "Hi {{band}}, your line is ready. Reply STOP to opt out"
    first = variant_template_ref(_template(words))
    assert first == variant_template_ref(_template(words, variant="B"))  # the words, not the letter
    assert first.startswith("copy:") and len(first) == len("copy:") + 16
    assert (
        is_variant_template_ref(first)
        and not is_variant_template_ref("CEP-42")
        and not is_variant_template_ref(None)
    )
    assert variant_template_ref(_template(words + "!")) != first
    assert variant_template_ref(_template(words, channel="whatsapp")) != first
    assert variant_template_ref(_template(words, channel="email", subject="Back?")) != variant_template_ref(
        _template(words, channel="email", subject="Come back?")
    )


def test_a_variant_level_is_spelled_as_an_m100_level() -> None:
    from engine.uplift.config import level_text

    assert variant_level("B") == "variant_b" == level_text(" Variant_B ")


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------
def test_each_customer_s_arm_depends_on_their_own_key_and_the_seed_only() -> None:
    keys = [f"C{index:06d}" for index in range(5_000)]
    arms = draw_arms(keys, LEVELS[1:], seed=42)
    by_key = dict(zip(keys, arms, strict=True))
    shuffled = list(np.random.default_rng(1).permutation(keys))
    again = draw_arms(shuffled, LEVELS[1:], seed=42)
    assert all(by_key[key] == arm for key, arm in zip(shuffled, again, strict=True)), "never by position"
    fewer = keys[::7]
    assert list(draw_arms(fewer, LEVELS[1:], seed=42)) == [
        by_key[key] for key in fewer
    ], "nor by who else is in"
    assert list(draw_arms(keys, LEVELS[1:], seed=43)) != list(arms), "another run draws again"


def test_every_arm_gets_an_equal_share() -> None:
    keys = [str(index) for index in range(60_000)]
    arms = pd.Series(draw_arms(keys, LEVELS[1:], seed=7))
    shares = arms.value_counts(normalize=True)
    se = np.sqrt((1 / 3) * (2 / 3) / len(keys))
    assert set(shares.index) == set(LEVELS[1:])
    assert (abs(shares - 1 / 3) <= 4 * se).all(), shares.to_dict()


def _draw_seconds(rows: int) -> float:
    keys = [f"K{index}" for index in range(rows)]
    started = time.perf_counter()
    draw_arms(keys, LEVELS[1:], seed=3)
    return time.perf_counter() - started


def test_the_draw_is_linear_in_rows() -> None:
    small = large = float("inf")
    for _ in range(3):  # best of three, the sizes taking turns (a burst of load slows both)
        small = min(small, _draw_seconds(20_000))
        large = min(large, _draw_seconds(200_000))
    print(f"\n[Perf] variant arms drawn for 200,000 customers in {large:.2f}s (1M estimate {large * 5:.1f}s)")
    assert large < 30 * max(small, 0.01), "ten times the rows took far more than ten times as long"


# ---------------------------------------------------------------------------
# Comparing the messages, on measure_campaign's own report
# ---------------------------------------------------------------------------
def _report(effects: tuple[float, ...], *, seed: int, n: int = 12_000) -> object:
    campaign = multi_arm_campaign(n, 0.10, effects, seed=seed, levels=LEVELS[: len(effects) + 1])
    return measure_campaign(
        campaign.scores, campaign.outcomes, **campaign.measure_kwargs, combine_offers=True
    )


def test_a_clearly_better_message_is_named_the_winner() -> None:
    report = _report((0.03, 0.03, 0.15), seed=5)
    comparison = compare_variant_arms(report)  # type: ignore[arg-type]
    assert comparison is not None
    assert comparison.arms == LEVELS[1:]
    assert comparison.family_size == 3 and comparison.family_confidence == pytest.approx(1 - 0.05 / 3)
    assert comparison.leader == comparison.winner == "variant_b"
    assert (
        comparison.margin is not None
        and comparison.margin.ci_low is not None
        and comparison.margin.ci_low > 0
    )
    assert comparison.margin.confidence_level == pytest.approx(1 - 0.05 / 3)
    assert {(pair.first, pair.second) for pair in comparison.pairs} == {
        ("control_template", "variant_a"),
        ("control_template", "variant_b"),
        ("variant_a", "variant_b"),
    }
    assert comparison.report_computed_at == report.computed_at  # type: ignore[attr-defined]


def test_no_winner_is_named_when_the_leader_is_not_clearly_ahead() -> None:
    report = _report((0.03, 0.03, 0.035), seed=6)
    comparison = compare_variant_arms(report)  # type: ignore[arg-type]
    assert comparison is not None and comparison.winner is None
    assert comparison.leader in LEVELS[1:] and comparison.margin is not None
    assert comparison.margin.ci_low is not None and comparison.margin.ci_low <= 0
    assert "No message is named" in comparison.summary


def test_a_report_of_one_message_is_not_compared() -> None:
    assert compare_variant_arms(_report((0.05,), seed=7)) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# measure_campaign's arguments
# ---------------------------------------------------------------------------
def test_measure_arguments_put_the_control_template_first_and_measure_against_no_message() -> None:
    assignment = pd.DataFrame(
        {VARIANT_ARM_COLUMN: ["variant_b", None, "control_template", "variant_a", None]}
    )
    arguments = variant_measure_arguments(assignment)
    assert arguments == {
        "arm_column": VARIANT_ARM_COLUMN,
        "arms": ["control_template", "variant_a", "variant_b"],
        "control_level": NO_MESSAGE_LEVEL,
        "combine_offers": True,
    }
    assert variant_measure_arguments(pd.DataFrame({"arm": ["treated"]})) == {}
    assert variant_measure_arguments(pd.DataFrame({VARIANT_ARM_COLUMN: [None, None]})) == {}


def test_an_amount_or_a_covariate_is_measured_as_a_whole_campaign() -> None:
    """`measure_campaign` compares arms on a yes/no outcome without a covariate only (review fix)."""
    assignment = pd.DataFrame({VARIANT_ARM_COLUMN: ["variant_b", "control_template", "variant_a"]})
    assert variant_measure_arguments(assignment, outcome_kind="continuous") == {}
    assert variant_measure_arguments(assignment, covariate="spend_before") == {}
    assert variant_measure_arguments(assignment, outcome_kind="binary")["arm_column"] == VARIANT_ARM_COLUMN


# ---------------------------------------------------------------------------
# Defaults and the catalogue
# ---------------------------------------------------------------------------
def test_the_setting_is_off_by_default_and_left_out_of_a_dump() -> None:
    assert GenerativeConfig().variant_arms is False
    assert "variant_arms" not in GenerativeConfig().model_dump()
    assert GenerativeConfig(variant_arms=True).model_dump()["variant_arms"] is True


def test_a_variant_s_catalogue_action_never_prices_a_channel() -> None:
    catalogue = ActionCatalogue.model_validate(
        {
            "actions": [
                {
                    "action_id": "v1",
                    "label": "Variant",
                    "channels": ["sms"],
                    "contact_cost": 9.0,
                    "template_ref": "copy:00ff00ff00ff00ff",
                },
                {"action_id": "base", "label": "Offer", "channels": ["sms", "email"], "contact_cost": 0.25},
            ]
        }
    )
    assert catalogue.channel_contact_costs() == {"sms": 0.25, "email": 0.25}


def test_the_codes_are_the_four_the_help_text_covers() -> None:
    assert {
        "VARIANT_TEMPLATE_UNREGISTERED",
        "VARIANT_ACTION_MISSING",
        "VARIANT_ARMS_NOT_MADE",
        "VARIANT_ARMS_NOT_ARBITRATED",
    } == VARIANT_ARM_CODES


def test_the_codes_are_plan_k_codes() -> None:
    from engine.agentic.codes import PLAN_K_CODES, VARIANT_CODES

    assert VARIANT_ARM_CODES == VARIANT_CODES
    assert VARIANT_CODES <= PLAN_K_CODES


# ---------------------------------------------------------------------------
# Plan K M145 (DEC-1412): arms the compliance gate stopped are set aside, never compared
# ---------------------------------------------------------------------------
def test_an_arm_set_aside_is_listed_as_not_sent_and_left_out_of_every_pair() -> None:
    report = _report((0.03, 0.03, 0.15), seed=5)
    reason = "Not sent: the compliance gate stopped its words for every customer drawn into it."
    comparison = compare_variant_arms(report, set_aside={"variant_b": reason})  # type: ignore[arg-type]
    assert comparison is not None
    assert comparison.arms == ("control_template", "variant_a")
    assert comparison.family_size == 1 and comparison.family_confidence == pytest.approx(0.95)
    assert [(pair.first, pair.second) for pair in comparison.pairs] == [("control_template", "variant_a")]
    assert comparison.leader != "variant_b" and comparison.winner != "variant_b"
    assert comparison.not_sent is not None
    (aside,) = comparison.not_sent
    assert (aside.arm, aside.reason, aside.difference) == ("variant_b", reason, None)
    assert "Not sent, so not compared: variant_b." in comparison.summary
    dumped = comparison.model_dump(mode="json")
    (listed,) = dumped["not_sent"]
    assert (listed["arm"], listed["reason"], listed["difference"]) == ("variant_b", reason, None)


def test_without_arms_set_aside_the_comparison_is_unchanged() -> None:
    report = _report((0.03, 0.03, 0.15), seed=5)
    plain = compare_variant_arms(report)  # type: ignore[arg-type]
    empty = compare_variant_arms(report, set_aside={})  # type: ignore[arg-type]
    assert plain is not None and empty is not None
    assert plain.not_sent is None and "not_sent" not in plain.model_dump(mode="json")
    assert plain.model_dump_json() == empty.model_dump_json()


def test_one_message_left_after_the_set_aside_is_not_compared() -> None:
    report = _report((0.03, 0.15), seed=5)
    assert compare_variant_arms(report, set_aside={"variant_a": "x"}) is None  # type: ignore[arg-type]


def test_the_arms_not_sent_are_read_from_the_gated_treat_list(tmp_path: object) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary
    from engine.decide.variant_arms import variant_arms_not_sent
    from engine.storage import LocalStorage, run_key

    run = "r_20261011_0c145a02"
    storage = LocalStorage(Path(str(tmp_path)))
    assert variant_arms_not_sent(storage, run) == {}, "no list: nothing set aside"
    base = TreatListSummary(
        run_id=run,
        use_case_id="win-back-campaign",
        total_rows=100,
        treat_rows=40,
        created_at=datetime.now(UTC),
    )
    counts = {"control_template": 20, "variant_a": 20, "variant_b": 0, "variant_c": 5, "variant_d": 0}
    cases = {
        "gate off": (base.model_copy(update={"variant_arm_counts": counts}), {}),
        "gated": (
            base.model_copy(
                update={
                    "compliance_rule_version": "r" * 64,
                    "variant_arm_counts": counts,
                    "variant_arm_withheld": {
                        "control_template": 3,  # consent only: kept, intention to treat
                        "variant_a": 0,
                        "variant_b": 20,  # every customer, for its words
                        "variant_c": 15,  # some of them for its words
                        "variant_d": 20,  # nobody allowed, for consent alone
                    },
                    "variant_arm_words_withheld": {
                        "control_template": 0,
                        "variant_a": 0,
                        "variant_b": 20,
                        "variant_c": 2,
                        "variant_d": 0,
                    },
                }
            ),
            {"variant_b", "variant_c", "variant_d"},
        ),
    }
    for name, (summary, expected) in cases.items():
        storage.write_model(run_key(run, TREAT_LIST_SUMMARY_FILENAME), summary)
        found = variant_arms_not_sent(storage, run)
        assert set(found) == set(expected), name
    assert found["variant_b"].startswith("Not sent: the compliance gate stopped its words for every")
    assert found["variant_c"].startswith("Set aside: the compliance gate stopped its words for some")
    assert found["variant_d"].startswith("Not sent: the compliance gate allowed none")
    assert not any(character.isdigit() for reason in found.values() for character in reason)
