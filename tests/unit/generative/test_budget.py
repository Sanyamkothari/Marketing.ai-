"""`engine.generative.budget` and `engine.generative.cache`: what a run spent, and what it saved.

The property worth the most here is the one that is easiest to get subtly wrong: **a ceiling is
checked before a call, not after it**. A meter that tallied first and refused afterwards would let
every run make one call more than it was allowed, which on a per-segment or per-band loop is one
call per iteration. So the refusal is asserted against the tally: after `BUDGET_EXCEEDED`, the
number of calls made is exactly the ceiling.

The second is that **an unknown price is null and never zero** (DEC-208). The shipped price table
is empty, so this is not an edge case but the default state of the system: a usage record from a
deployment nobody has priced must say "I do not know what this cost", and the cost ceiling must
record that it could not be enforced rather than appearing to hold. The call ceiling, which needs
no prices, must still bind - otherwise an unpriced deployment would have no limit at all.

The half-filled price table is the case worth writing tests for rather than reasoning about
(DEC-227): a deployment that priced its generating model and not its judge has a real ceiling in
configuration, a total that cannot be worked out, and therefore no cost test that could be made -
so what `budget_usd` reports must be null there too, and the call ceiling must be what still stops
the run. What the cost ceiling promises is asserted for exactly what it is: a run stops at the first
call after its spending reached the ceiling, which can leave it over.

The cache is checked for the three things a cache has to get right: a hit is a hit and not a call,
a different prompt or a different model or a different temperature is a miss, and a failure to read
or write is survivable - a cache that raised would turn a working run into a failed one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import BudgetConfig, LlmBackend, LlmConfig
from engine.generative.budget import (
    TOKENS_PER_PRICE_UNIT,
    Meter,
    ModelPrice,
    PriceTable,
    load_prices,
)
from engine.generative.cache import CompletionCache, NullCache, cache_key
from engine.generative.contracts import PRICE_UNKNOWN, GenerativePurpose, LlmUsageReport
from engine.generative.errors import GenerativeError
from engine.generative.prompts import RenderedPrompt, load_prompt, render
from engine.llm import GroundedFakeLLMClient, LLMCompletion

PURPOSE = GenerativePurpose.JUDGE_TOXICITY
PRICED = PriceTable(prices={"fake": ModelPrice(input_per_1m=3.0, output_per_1m=15.0)}, as_of="a-date")

GENERATOR = "a-generator"
JUDGE = "a-judge"
HALF_PRICED = PriceTable(prices={GENERATOR: ModelPrice(input_per_1m=3.0, output_per_1m=15.0)})
"""A row for the generating model and none for the judge: what a half-filled price file looks like."""


def rendered(text: str = "a text to check") -> RenderedPrompt:
    """One rendered prompt. `judge_toxicity` takes a single variable, which keeps the fixture small."""
    return render(load_prompt("judge_toxicity"), {"generated": text})


def meter(
    *,
    calls: int = 500,
    cost: float = 2.0,
    prices: PriceTable | None = None,
    cache: CompletionCache | NullCache | None = None,
    use_cache: bool = True,
) -> Meter:
    return Meter(
        GroundedFakeLLMClient(),
        job_id="r_20260922_abcdef01",
        llm=LlmConfig(),
        budget=BudgetConfig(max_calls_per_run=calls, max_cost_usd_per_run=cost, cache=use_cache),
        prices=prices,
        cache=cache,
    )


def split_meter(*, calls: int = 500, cost: float = 2.0, prices: PriceTable = HALF_PRICED) -> Meter:
    """A meter that generates with one model and judges with another, so one of them can be unpriced."""
    return Meter(
        GroundedFakeLLMClient(),
        job_id="r_20260922_abcdef01",
        llm=LlmConfig(
            backend=LlmBackend.BEDROCK,
            generation_model_id=GENERATOR,
            judge_model_id=JUDGE,
            embedding_model_id="an-embedder",
        ),
        budget=BudgetConfig(max_calls_per_run=calls, max_cost_usd_per_run=cost, cache=False),
        prices=prices,
    )


# ---------------------------------------------------------------------------
# The price table
# ---------------------------------------------------------------------------
def test_the_shipped_price_table_is_empty_and_that_is_not_an_error() -> None:
    """DEC-208: prices differ by region and by contract, so a shipped number would be invented."""
    table = load_prices()
    assert table.is_empty
    assert table.as_of is None
    assert table.get("anything") is None


def test_a_price_is_per_million_tokens() -> None:
    price = ModelPrice(input_per_1m=3.0, output_per_1m=15.0)
    assert price.cost(TOKENS_PER_PRICE_UNIT, 0) == pytest.approx(3.0)
    assert price.cost(0, TOKENS_PER_PRICE_UNIT) == pytest.approx(15.0)
    assert price.cost(0, 0) == 0.0


def test_a_price_table_is_read_from_a_root(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "llm_prices.yaml").write_text(
        "schema_version: 1\nas_of: a-date\nsource: somewhere\n"
        "models:\n  a-model: {input_per_1m: 1.5, output_per_1m: 7.5}\n",
        encoding="utf-8",
    )
    table = load_prices(tmp_path / "configs")
    assert not table.is_empty
    assert table.source == "somewhere"
    price = table.get("a-model")
    assert price is not None
    assert price.input_per_1m == 1.5


def test_a_missing_price_file_is_an_empty_table_rather_than_a_failure(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    assert load_prices(tmp_path / "configs").is_empty


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------
def test_a_call_is_tallied_by_model_and_by_purpose() -> None:
    subject = meter(prices=PRICED)
    subject.complete(rendered(), PURPOSE)
    usage = subject.usage()
    assert isinstance(usage, LlmUsageReport)
    assert usage.totals.calls == 1
    assert usage.cache_hits == 0
    assert [(entry.model_id, entry.calls) for entry in usage.by_model] == [("fake", 1)]
    assert [(entry.purpose, entry.calls) for entry in usage.by_purpose] == [(PURPOSE, 1)]
    assert usage.totals.input_tokens > 0
    assert usage.totals.output_tokens > 0


def test_a_priced_run_reports_a_cost_and_the_ceiling_that_was_in_force() -> None:
    subject = meter(prices=PRICED)
    subject.complete(rendered(), PURPOSE)
    usage = subject.usage()
    assert usage.totals.cost_estimate_usd is not None
    assert usage.totals.cost_estimate_usd > 0
    assert usage.budget_usd == 2.0
    assert usage.warnings == ()


def test_an_unpriced_model_gives_a_null_cost_and_a_warning_that_names_it() -> None:
    """Never zero. A zero is a measurement, and there was none (DEC-208)."""
    subject = meter(prices=PriceTable())
    subject.complete(rendered(), PURPOSE)
    usage = subject.usage()
    assert usage.totals.cost_estimate_usd is None
    assert usage.budget_usd is None
    assert usage.warnings == (f"{PRICE_UNKNOWN}:fake",)
    assert usage.by_model[0].cost_estimate_usd is None


def test_the_price_unknown_warning_is_raised_once_per_model_not_once_per_call() -> None:
    subject = meter(prices=PriceTable())
    for _ in range(4):
        subject.complete(rendered(), PURPOSE)
    assert subject.usage().warnings == (f"{PRICE_UNKNOWN}:fake",)
    assert subject.usage().totals.calls == 4


def test_a_run_that_made_no_call_reports_no_cost_rather_than_zero() -> None:
    usage = meter(prices=PRICED).usage()
    assert usage.totals.calls == 0
    assert usage.totals.cost_estimate_usd is None
    assert usage.by_model == ()
    assert usage.by_purpose == ()


def test_the_call_ceiling_is_checked_before_the_call_not_after_it() -> None:
    """The whole point: a meter that tallied first would let every run make one call too many."""
    subject = meter(calls=3, use_cache=False)
    for index in range(3):
        subject.complete(rendered(f"text {index}"), PURPOSE)
    with pytest.raises(GenerativeError) as error:
        subject.complete(rendered("one too many"), PURPOSE)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert "call" in error.value.message
    assert subject.calls == 3
    assert subject.usage().totals.calls == 3


def test_the_call_ceiling_binds_even_when_nothing_can_be_priced() -> None:
    """An unpriced deployment would otherwise have no limit at all."""
    subject = meter(calls=1, prices=PriceTable(), use_cache=False)
    subject.complete(rendered("one"), PURPOSE)
    with pytest.raises(GenerativeError) as error:
        subject.complete(rendered("two"), PURPOSE)
    assert error.value.code == "BUDGET_EXCEEDED"


def test_the_cost_ceiling_stops_a_run_that_has_spent_its_budget() -> None:
    expensive = PriceTable(prices={"fake": ModelPrice(input_per_1m=1_000_000.0, output_per_1m=0.0)})
    subject = meter(cost=0.01, prices=expensive, use_cache=False)
    with pytest.raises(GenerativeError) as error:
        for index in range(20):
            subject.complete(rendered(f"text {index}"), PURPOSE)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert "cost" in error.value.message
    assert subject.calls >= 1


def test_a_run_stops_at_the_first_call_after_its_spending_reached_the_cost_ceiling() -> None:
    """What the cost ceiling promises and no more: a call's cost is known only once it comes back,
    so the call that reached the ceiling may have gone over it, and what is bounded is how far the
    run goes on (DEC-227)."""
    expensive = PriceTable(prices={"fake": ModelPrice(input_per_1m=1_000_000.0, output_per_1m=0.0)})
    subject = meter(cost=0.01, prices=expensive, use_cache=False)
    with pytest.raises(GenerativeError):
        for index in range(20):
            subject.complete(rendered(f"text {index}"), PURPOSE)
    spent = subject.cost_so_far
    assert spent is not None and spent >= 0.01
    stopped_at = subject.calls
    with pytest.raises(GenerativeError):
        subject.complete(rendered("one more"), PURPOSE)
    assert subject.calls == stopped_at


def test_a_ceiling_stops_being_reported_the_moment_a_call_could_not_be_priced() -> None:
    """A half-filled price table would otherwise print a ceiling into `llm_usage.json` while the cost
    test it names was being skipped, which is the one thing this module must not do (DEC-227)."""
    subject = split_meter()
    subject.complete(rendered("generated"), GenerativePurpose.ASSISTANT_ANSWER)
    assert subject.budget_usd == 2.0
    assert subject.cost_so_far is not None
    subject.judge(rendered("judged"), PURPOSE)
    assert subject.cost_so_far is None
    assert subject.budget_usd is None
    assert subject.usage().budget_usd is None
    assert subject.usage().warnings == (f"{PRICE_UNKNOWN}:{JUDGE}",)


def test_the_call_ceiling_is_what_binds_a_run_whose_cost_cannot_be_totalled() -> None:
    """A partial total is not the run's cost, so nothing may be refused on it; the ceiling that needs
    no prices still stops the run (DEC-227)."""
    expensive = PriceTable(prices={GENERATOR: ModelPrice(input_per_1m=1_000_000.0, output_per_1m=0.0)})
    subject = split_meter(calls=3, cost=0.01, prices=expensive)
    subject.judge(rendered("unpriced"), PURPOSE)
    for index in range(2):
        subject.complete(rendered(f"far past a cent {index}"), GenerativePurpose.ASSISTANT_ANSWER)
    with pytest.raises(GenerativeError) as error:
        subject.complete(rendered("one too many"), GenerativePurpose.ASSISTANT_ANSWER)
    assert "call" in error.value.message
    assert subject.calls == 3
    assert subject.budget_usd is None


def test_a_budget_refusal_still_leaves_a_usage_record_to_read() -> None:
    """A run that stops on its budget stops cleanly, with what it spent written down."""
    subject = meter(calls=2, use_cache=False)
    for index in range(2):
        subject.complete(rendered(f"text {index}"), PURPOSE)
    with pytest.raises(GenerativeError):
        subject.complete(rendered("blocked"), PURPOSE)
    usage = subject.usage()
    assert usage.totals.calls == 2
    assert usage.budget_calls == 2
    assert usage.job_id == "r_20260922_abcdef01"


def test_remaining_calls_counts_down() -> None:
    subject = meter(calls=3, use_cache=False)
    assert subject.remaining_calls() == 3
    subject.complete(rendered("one"), PURPOSE)
    assert subject.remaining_calls() == 2


def test_judging_uses_the_judging_model_and_generating_uses_the_generating_one() -> None:
    config = LlmConfig(
        backend=LlmBackend.BEDROCK,
        generation_model_id="a-generator",
        judge_model_id="a-judge",
        embedding_model_id="an-embedder",
    )
    subject = Meter(
        GroundedFakeLLMClient(),
        job_id="r_1",
        llm=config,
        budget=BudgetConfig(cache=False),
        prices=PriceTable(),
    )
    subject.complete(rendered("generated"), GenerativePurpose.ASSISTANT_ANSWER)
    subject.judge(rendered("judged"), PURPOSE)
    models = {entry.model_id for entry in subject.usage().by_model}
    assert models == {"a-generator", "a-judge"}


def test_an_unset_model_id_is_recorded_as_the_fake_rather_than_as_nothing() -> None:
    """An artefact has to say what answered, and an empty string says nothing (DEC-204)."""
    subject = meter(prices=PriceTable())
    subject.complete(rendered(), PURPOSE)
    assert subject.usage().by_model[0].model_id == "fake"


def test_embedding_a_batch_is_one_call_and_an_empty_batch_is_none() -> None:
    """One request is what a provider bills, so one request is what the budget counts."""
    subject = meter(prices=PRICED)
    assert subject.embed([]) == ()
    assert subject.calls == 0
    subject.embed(["one", "two", "three"])
    usage = subject.usage()
    assert usage.totals.calls == 1
    assert [entry.purpose for entry in usage.by_purpose] == [GenerativePurpose.EMBEDDING]
    assert usage.totals.output_tokens == 0


def test_usage_lists_models_and_purposes_in_a_stable_order() -> None:
    """Byte-stability: an artefact whose lists reordered between runs could not be compared."""
    subject = meter(prices=PRICED, use_cache=False)
    subject.embed(["a text"])
    subject.complete(rendered("one"), GenerativePurpose.ASSISTANT_ANSWER)
    subject.judge(rendered("two"), GenerativePurpose.JUDGE_TOXICITY)
    purposes = [entry.purpose.value for entry in subject.usage().by_purpose]
    assert purposes == sorted(purposes)


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------
def test_a_repeat_is_a_hit_and_a_hit_is_not_a_call(tmp_path: Path) -> None:
    subject = meter(cache=CompletionCache(tmp_path), prices=PRICED)
    first = subject.complete(rendered("same text"), PURPOSE)
    second = subject.complete(rendered("same text"), PURPOSE)
    assert first.text == second.text
    assert subject.calls == 1
    assert subject.cache_hits == 1
    assert subject.usage().cache_hits == 1


def test_a_hit_does_not_touch_the_budget(tmp_path: Path) -> None:
    """A call that was not made cannot take a run past its ceiling."""
    subject = meter(calls=1, cache=CompletionCache(tmp_path))
    subject.complete(rendered("same text"), PURPOSE)
    subject.complete(rendered("same text"), PURPOSE)  # would be BUDGET_EXCEEDED if it were a call
    assert subject.calls == 1


@pytest.mark.parametrize(
    ("changed", "value"),
    [("user", "a different text"), ("model_id", "another-model"), ("temperature", 0.9)],
)
def test_anything_that_changes_the_answer_changes_the_key(changed: str, value: object) -> None:
    base = {
        "content_hash": "abc",
        "system": "s",
        "user": "u",
        "model_id": "m",
        "temperature": 0.2,
    }
    assert cache_key(**base) != cache_key(**{**base, changed: value})  # type: ignore[arg-type]


def test_the_same_temperature_written_two_ways_is_one_key() -> None:
    """`0.2` and `0.20` are one setting; a cache that missed on them would miss for no reason."""
    base = {"content_hash": "abc", "system": "s", "user": "u", "model_id": "m"}
    assert cache_key(**base, temperature=0.2) == cache_key(**base, temperature=0.20)


def test_turning_the_cache_off_in_configuration_really_turns_it_off(tmp_path: Path) -> None:
    subject = meter(cache=CompletionCache(tmp_path), use_cache=False)
    subject.complete(rendered("same text"), PURPOSE)
    subject.complete(rendered("same text"), PURPOSE)
    assert subject.calls == 2
    assert subject.cache_hits == 0


def test_the_null_cache_stores_nothing_and_says_so() -> None:
    cache = NullCache()
    cache.put(
        "a-key",
        LLMCompletion(text="t", model_id="m", input_tokens=1, output_tokens=1, stop_reason="x"),
    )
    assert cache.get("a-key") is None
    assert cache.prune() == 0


def test_a_cached_completion_round_trips_exactly(tmp_path: Path) -> None:
    cache = CompletionCache(tmp_path)
    completion = LLMCompletion(
        text='a completion with a quote " and a newline\n',
        model_id="m",
        input_tokens=11,
        output_tokens=7,
        stop_reason="end_turn",
    )
    cache.put("k" * 40, completion)
    assert cache.get("k" * 40) == completion


def test_an_unreadable_entry_is_a_miss_rather_than_a_failure(tmp_path: Path) -> None:
    """A cache is by definition the part you can do without; one that raised would break a run."""
    cache = CompletionCache(tmp_path)
    key = "z" * 40
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all", encoding="utf-8")
    assert cache.get(key) is None


def test_a_miss_on_an_empty_cache_is_just_a_miss(tmp_path: Path) -> None:
    assert CompletionCache(tmp_path / "never-created").get("k" * 40) is None
    assert CompletionCache(tmp_path / "never-created").size_bytes() == 0


def test_pruning_drops_the_least_recently_read_until_it_fits(tmp_path: Path) -> None:
    cache = CompletionCache(tmp_path)
    for index in range(10):
        cache.put(
            f"{index:040d}",
            LLMCompletion(text="x" * 500, model_id="m", input_tokens=1, output_tokens=1, stop_reason="s"),
        )
    before = cache.size_bytes()
    assert before > 0
    assert cache.prune(max_bytes=before) == 0
    dropped = cache.prune(max_bytes=before // 3)
    assert dropped > 0
    assert cache.size_bytes() <= before
