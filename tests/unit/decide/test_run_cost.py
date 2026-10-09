"""M108 (DEC-1318): the cost of a run, said before the run starts, and the cap on it.

Every expected figure below is recomputed from the price list the test itself loads
(`configs/aws_prices.yaml`, the generated fixture; no AWS pricing API is ever called), so no dollar
amount is written down here that nobody sourced. What is pinned is the rule: a number when prices
and a time limit exist, `None` with a plain reason when they do not, never a zero standing in for
nothing, and Indian rupees only beside the Admin's own exchange rate and its source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.aws.prices import PRICES_FILENAME, SECONDS_PER_HOUR, PriceTable, load_price_table
from engine.aws.run_cost import (
    RUN_COST_CODES,
    CostLine,
    FxRate,
    estimate_run_cost,
    running_cost_usd,
)
from engine.config import (
    BudgetConfig,
    GenerativeConfig,
    GenerativeKind,
    GovernanceConfig,
    LlmBackend,
    LlmConfig,
    RunMode,
    UseCaseConfig,
    load_use_case,
)
from engine.generative.budget import ModelPrice
from engine.generative.budget import PriceTable as LlmPriceTable
from engine.pilot.plain import jargon_in
from tests.fixtures.settings import local_settings, sagemaker_settings

USE_CASE = "targeted-advertisement"
TRAIN_INSTANCE = "ml.m5.2xlarge"
SCORE_INSTANCE = "ml.m5.xlarge"
LIMIT_S = 7200
REGION = "ap-south-1"
FX = FxRate(inr_per_usd=84.5, source="Finance sheet, 1 October 2026", as_of="2026-10-01")


@pytest.fixture
def table(config_root: Path) -> PriceTable:
    loaded = load_price_table(config_root / PRICES_FILENAME)
    assert loaded is not None, "the generated rate card is missing from configs/"
    return loaded


@pytest.fixture
def config(config_root: Path) -> UseCaseConfig:
    return load_use_case(USE_CASE, config_root)


def deployment(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "sagemaker_instance_type": TRAIN_INSTANCE,
        "sagemaker_processing_instance_type": SCORE_INSTANCE,
        "sagemaker_max_runtime_seconds": LIMIT_S,
    }
    values.update(overrides)
    return sagemaker_settings(**values)


def rate(table: PriceTable, component: str, instance_type: str) -> float:
    found = table.rate(component=component, instance_type=instance_type, region=REGION)
    assert found is not None
    return found.usd_per_hour


def estimate(config: UseCaseConfig, table: PriceTable | None, **kwargs: Any) -> Any:
    return estimate_run_cost(
        config,
        kwargs.pop("mode", RunMode.TRAIN),
        settings=kwargs.pop("settings", deployment()),
        table=table,
        llm_prices=kwargs.pop("llm_prices", LlmPriceTable()),
        fx=kwargs.pop("fx", None),
    )


def with_cap(config: UseCaseConfig, cap: float | None) -> UseCaseConfig:
    return config.model_copy(
        update={"governance": config.governance.model_copy(update={"max_run_cost_usd": cap})}
    )


def with_text_service(config: UseCaseConfig, ceiling: float) -> UseCaseConfig:
    generative = GenerativeConfig(
        kind=GenerativeKind.CAMPAIGN_COPY,
        llm=LlmConfig(
            backend=LlmBackend.BEDROCK,
            generation_model_id="gen-model",
            judge_model_id="judge-model",
            embedding_model_id="embed-model",
        ),
        budget=BudgetConfig(max_cost_usd_per_run=ceiling),
    )
    return config.model_copy(update={"generative": generative})


# ---------------------------------------------------------------------------
# The number, and when there is none
# ---------------------------------------------------------------------------
def test_a_training_estimate_is_the_time_limit_times_instances_times_the_list_rate(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(config, table, settings=deployment(sagemaker_instance_count=2))
    expected = LIMIT_S * 2 * rate(table, "training", TRAIN_INSTANCE) / SECONDS_PER_HOUR
    assert result.estimated_usd == pytest.approx(expected, abs=1e-4)
    assert result.backend == "sagemaker"
    assert result.reason is None
    assert "list price" in result.basis.lower()
    assert "not a bill" in result.basis.lower()


def test_a_scoring_estimate_uses_the_processing_rate_and_instance(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(config, table, mode=RunMode.SCORE)
    expected = LIMIT_S * rate(table, "processing", SCORE_INSTANCE) / SECONDS_PER_HOUR
    assert result.estimated_usd == pytest.approx(expected, abs=1e-4)
    assert [line.kind for line in result.lines] == ["scoring"]


def test_a_training_estimate_also_shows_the_later_scoring_run_but_does_not_count_it(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(config, table)
    kinds = {line.kind: line for line in result.lines}
    assert set(kinds) == {"training", "scoring"}
    assert kinds["training"].in_total is True
    assert kinds["scoring"].in_total is False
    assert kinds["scoring"].usd == pytest.approx(
        LIMIT_S * rate(table, "processing", SCORE_INSTANCE) / SECONDS_PER_HOUR, abs=1e-4
    )
    assert result.estimated_usd == kinds["training"].usd


def test_the_estimate_is_null_without_a_price_list(config: UseCaseConfig) -> None:
    result = estimate(config, None)
    assert result.estimated_usd is None
    assert result.known_usd is None
    assert result.reason is not None and "price list" in result.reason
    assert all(line.usd is None for line in result.lines)
    assert result.inr is None


def test_the_estimate_is_null_for_an_instance_the_price_list_does_not_carry(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(config, table, settings=deployment(sagemaker_instance_type="ml.nonexistent.9xlarge"))
    assert result.estimated_usd is None
    assert result.reason is not None and "ml.nonexistent.9xlarge" in result.reason


def test_the_estimate_is_null_without_a_time_limit(config: UseCaseConfig, table: PriceTable) -> None:
    result = estimate(config, table, settings=deployment(sagemaker_max_runtime_seconds=None))
    assert result.estimated_usd is None
    assert result.reason is not None and "time limit" in result.reason


def test_a_run_on_this_machine_has_no_cloud_estimate_and_it_is_not_zero(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(config, table, settings=local_settings())
    assert result.backend == "local"
    assert result.estimated_usd is None
    assert result.reason is not None and "own machine" in result.reason
    assert result.needs_confirmation is False


# ---------------------------------------------------------------------------
# Rupees only with the Admin's rate and its source
# ---------------------------------------------------------------------------
def test_no_rupees_without_an_exchange_rate(config: UseCaseConfig, table: PriceTable) -> None:
    result = estimate(config, table)
    assert result.estimated_usd is not None
    assert result.inr is None
    assert result.inr_reason is not None and "exchange rate" in result.inr_reason


def test_rupees_carry_the_rate_its_source_and_its_date(config: UseCaseConfig, table: PriceTable) -> None:
    result = estimate(config, table, fx=FX)
    assert result.estimated_usd is not None
    assert result.inr is not None
    assert result.inr.amount == pytest.approx(result.estimated_usd * FX.inr_per_usd, abs=0.01)
    assert result.inr.inr_per_usd == FX.inr_per_usd
    assert result.inr.source == FX.source
    assert result.inr.as_of == FX.as_of
    assert result.inr_reason is None


def test_an_exchange_rate_never_makes_a_rupee_figure_out_of_a_missing_dollar_figure(
    config: UseCaseConfig,
) -> None:
    result = estimate(config, None, fx=FX)
    assert result.estimated_usd is None
    assert result.inr is None


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_an_exchange_rate_must_be_a_positive_finite_number(bad: float) -> None:
    with pytest.raises(ValueError):
        FxRate(inr_per_usd=bad, source="Finance", as_of="2026-10-01")


def test_an_exchange_rate_must_say_where_it_came_from() -> None:
    with pytest.raises(ValueError):
        FxRate(inr_per_usd=84.0, source="   ", as_of="2026-10-01")


# ---------------------------------------------------------------------------
# The AI text ceiling
# ---------------------------------------------------------------------------
def test_the_ai_text_ceiling_is_counted_when_the_text_service_is_billed_and_priced(
    config: UseCaseConfig, table: PriceTable
) -> None:
    prices = LlmPriceTable(
        prices={
            "gen-model": ModelPrice(1.0, 2.0),
            "judge-model": ModelPrice(1.0, 2.0),
            "embed-model": ModelPrice(0.1, 0.0),
        },
        as_of="2026-10-01",
        source="provider price page",
    )
    base = estimate(config, table).estimated_usd
    result = estimate(with_text_service(config, 1.25), table, llm_prices=prices)
    text = next(line for line in result.lines if line.kind == "ai_text")
    assert text.usd == 1.25 and text.in_total is True
    assert result.estimated_usd == pytest.approx(base + 1.25, abs=1e-4)


def test_the_ai_text_ceiling_is_unknown_when_the_models_have_no_price(
    config: UseCaseConfig, table: PriceTable
) -> None:
    result = estimate(with_text_service(config, 1.25), table, llm_prices=LlmPriceTable())
    text = next(line for line in result.lines if line.kind == "ai_text")
    assert text.usd is None and text.reason is not None
    assert result.estimated_usd is None, "a total that leaves a cost out would understate it"
    assert result.known_usd is not None, "what is known is still shown"


def test_a_use_case_with_no_text_service_has_no_ai_text_line(config: UseCaseConfig, table: PriceTable) -> None:
    assert all(line.kind != "ai_text" for line in estimate(config, table).lines)


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------
def test_a_use_case_has_no_cap_unless_one_is_set(config: UseCaseConfig, table: PriceTable) -> None:
    assert GovernanceConfig().max_run_cost_usd is None
    assert config.governance.max_run_cost_usd is None
    result = estimate(config, table)
    assert result.cap_usd is None and result.over_cap is None and result.needs_confirmation is False


def test_an_estimate_above_the_cap_needs_confirmation(config: UseCaseConfig, table: PriceTable) -> None:
    ceiling = estimate(config, table).estimated_usd
    result = estimate(with_cap(config, ceiling / 2), table)
    assert result.over_cap is True and result.needs_confirmation is True
    assert result.confirmation_reason is not None


def test_an_estimate_within_the_cap_needs_nothing(config: UseCaseConfig, table: PriceTable) -> None:
    ceiling = estimate(config, table).estimated_usd
    result = estimate(with_cap(config, ceiling * 2), table)
    assert result.over_cap is False and result.needs_confirmation is False
    assert result.confirmation_reason is None


def test_an_estimate_exactly_at_the_cap_is_within_it(config: UseCaseConfig, table: PriceTable) -> None:
    ceiling = estimate(config, table).estimated_usd
    assert estimate(with_cap(config, ceiling), table).needs_confirmation is False


def test_a_cap_that_cannot_be_checked_needs_confirmation_on_a_billed_deployment(
    config: UseCaseConfig,
) -> None:
    result = estimate(with_cap(config, 5.0), None)
    assert result.over_cap is None
    assert result.needs_confirmation is True
    assert result.confirmation_reason is not None and "cannot" in result.confirmation_reason


def test_a_cap_on_a_deployment_nothing_bills_never_asks(config: UseCaseConfig, table: PriceTable) -> None:
    result = estimate(with_cap(config, 0.01), table, settings=local_settings())
    assert result.needs_confirmation is False


def test_a_known_part_already_over_the_cap_needs_confirmation_even_when_another_part_is_unknown(
    config: UseCaseConfig, table: PriceTable
) -> None:
    ceiling = estimate(config, table).estimated_usd
    capped = with_cap(with_text_service(config, 1.0), ceiling / 2)
    result = estimate(capped, table, llm_prices=LlmPriceTable())
    assert result.estimated_usd is None
    assert result.over_cap is True and result.needs_confirmation is True


@pytest.mark.parametrize("bad", [0.0, -3.0])
def test_a_cap_must_be_a_positive_amount(bad: float) -> None:
    with pytest.raises(ValueError):
        GovernanceConfig(max_run_cost_usd=bad)


# ---------------------------------------------------------------------------
# The running cost, which uses the same arithmetic as the estimate
# ---------------------------------------------------------------------------
def test_the_running_cost_is_elapsed_time_times_the_same_rate(
    config: UseCaseConfig, table: PriceTable
) -> None:
    settings = deployment(sagemaker_instance_count=2)
    priced = running_cost_usd(RunMode.TRAIN, elapsed_seconds=1800.0, settings=settings, table=table)
    assert priced.usd == pytest.approx(1800 * 2 * rate(table, "training", TRAIN_INSTANCE) / 3600, abs=1e-4)
    full = running_cost_usd(RunMode.TRAIN, elapsed_seconds=float(LIMIT_S), settings=settings, table=table)
    assert full.usd == pytest.approx(estimate(config, table, settings=settings).estimated_usd, abs=1e-4)


def test_the_running_cost_is_null_when_nothing_is_billed_or_priced(table: PriceTable) -> None:
    assert running_cost_usd(RunMode.TRAIN, elapsed_seconds=60.0, settings=local_settings(), table=table).usd is None
    assert running_cost_usd(RunMode.TRAIN, elapsed_seconds=60.0, settings=deployment(), table=None).usd is None


# ---------------------------------------------------------------------------
# Words a person reads
# ---------------------------------------------------------------------------
def test_every_sentence_in_an_estimate_is_plain_words(config: UseCaseConfig, table: PriceTable) -> None:
    ceiling = estimate(config, table).estimated_usd
    cases = [
        estimate(config, None, fx=FX),
        estimate(config, table),
        estimate(config, table, settings=local_settings()),
        estimate(config, table, settings=deployment(sagemaker_max_runtime_seconds=None)),
        estimate(with_cap(with_text_service(config, 1.0), ceiling / 2), table, llm_prices=LlmPriceTable()),
        estimate(with_cap(config, 5.0), None),
    ]
    sentences: list[str] = []
    for result in cases:
        sentences += [result.basis, result.reason or "", result.inr_reason or "", result.confirmation_reason or ""]
        for line in result.lines:
            assert isinstance(line, CostLine)
            sentences += [line.label, line.detail, line.reason or ""]
    assert [text for text in sentences if jargon_in(text)] == []


def test_the_new_codes_are_the_two_a_person_can_meet() -> None:
    assert RUN_COST_CODES == frozenset({"RUN_COST_NEEDS_CONFIRMATION", "RUN_COST_CAP_REACHED"})
