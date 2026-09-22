"""The meter every generative call goes through: cache, budget, tally.

`Meter` wraps an `LLMClient` and is the only thing a flow talks to. Three things happen on the way
through, in this order, and the order is the point:

1. **The cache is asked.** A hit costs nothing, is counted as a hit and never as a call, and is
   returned without the budget being touched - a call that was not made cannot exceed a ceiling.
2. **The budget is checked.** A call that would take the run past `max_calls_per_run`, or past
   `max_cost_usd_per_run` given what has been spent so far, is refused with `BUDGET_EXCEEDED`
   *before* it is made. A run that stops this way stops cleanly, with its partial artefacts and a
   usage record that says why.
3. **The call is made and tallied** - tokens, latency, and cost when the price table knows the
   model. When it does not, the cost is `None` and a `PRICE_UNKNOWN` warning names the model
   (DEC-208). `None` is not `0.0`: a zero is a measurement and there was none.

The cost ceiling can only bind on what can be priced. A run whose models are all unpriced records
that its cost ceiling could not be enforced rather than pretending it was - and the call ceiling,
which needs no prices, still binds. That is why `LlmUsage` carries both, and why `budget_usd` is
nullable.

Prices come from `configs/llm_prices.yaml`, which ships empty on purpose. Filling it in is a
documented one-file change; inventing a row here would be inventing a number on a screen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

import yaml

from engine.config import BudgetConfig, LlmConfig, config_root
from engine.contracts import LLMUsage
from engine.generative.cache import CompletionCache, NullCache, cache_key
from engine.generative.contracts import (
    PRICE_UNKNOWN,
    TOKENS_ESTIMATED,
    GenerativePurpose,
    LlmUsageReport,
    ModelUsage,
    PurposeUsage,
)
from engine.generative.errors import BUDGET_EXCEEDED, generative_error
from engine.generative.prompts import RenderedPrompt
from engine.llm import APPROX_CHARS_PER_TOKEN, LLMClient, LLMCompletion
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "PRICES_FILENAME",
    "TOKENS_PER_PRICE_UNIT",
    "Meter",
    "ModelPrice",
    "PriceTable",
    "load_prices",
]

_LOGGER = get_logger(__name__)

PRICES_FILENAME: Final[str] = "llm_prices.yaml"
TOKENS_PER_PRICE_UNIT: Final[int] = 1_000_000
"""Prices are quoted per million tokens, which is how every provider quotes them."""

_CALLS_LIMIT: Final[str] = "call"
_COST_LIMIT: Final[str] = "cost"


@dataclass(frozen=True)
class ModelPrice:
    """What one model costs, per million input and output tokens."""

    input_per_1m: float
    output_per_1m: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """US dollars for a call of this size."""
        return (input_tokens * self.input_per_1m + output_tokens * self.output_per_1m) / TOKENS_PER_PRICE_UNIT


@dataclass(frozen=True)
class PriceTable:
    """Model id -> price, as read from `configs/llm_prices.yaml`.

    An empty table is the shipped state and is not an error: it means no cost can be worked out,
    which is a true statement about a deployment nobody has priced yet (DEC-208).
    """

    prices: Mapping[str, ModelPrice] = field(default_factory=dict)
    as_of: str | None = None
    source: str = ""

    def get(self, model_id: str) -> ModelPrice | None:
        """The price for `model_id`, or `None` when the table has no row for it."""
        return self.prices.get(model_id)

    @property
    def is_empty(self) -> bool:
        """True when nothing can be priced, which is the state this file ships in."""
        return not self.prices


def load_prices(root: Path | None = None) -> PriceTable:
    """The price table of `root`, or an empty one when the file is absent or has no rows."""
    path = config_root(root) / PRICES_FILENAME
    if not path.is_file():
        return PriceTable()
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = document.get("models") or {}
    prices = {
        str(model_id): ModelPrice(
            input_per_1m=float(row.get("input_per_1m", 0.0)),
            output_per_1m=float(row.get("output_per_1m", 0.0)),
        )
        for model_id, row in rows.items()
    }
    as_of = document.get("as_of")
    return PriceTable(
        prices=prices, as_of=None if as_of is None else str(as_of), source=str(document.get("source", ""))
    )


@dataclass
class _Tally:
    """Running totals for one model or one purpose."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    priced: bool = True

    def add(self, input_tokens: int, output_tokens: int, cost: float | None) -> None:
        """Fold one call in. An unpriced call makes the whole tally unpriced, and stays that way."""
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if cost is None:
            self.priced = False
        else:
            self.cost += cost

    @property
    def cost_or_none(self) -> float | None:
        """The cost, or `None` when any call in this tally could not be priced."""
        return round(self.cost, 6) if self.priced else None


class Meter:
    """The one way a generative flow calls a model.

    A `Meter` belongs to a job, not to a process: it carries that job's budget and that job's
    totals, and `usage()` is the artefact it produces. Two jobs never share one, which is what
    makes `llm_usage.json` a record of a single run rather than of whatever else was going on.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        job_id: str,
        llm: LlmConfig,
        budget: BudgetConfig,
        prices: PriceTable | None = None,
        cache: CompletionCache | NullCache | None = None,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._llm = llm
        self._budget = budget
        self._prices = prices if prices is not None else PriceTable()
        self._cache: CompletionCache | NullCache = cache if cache is not None else NullCache()
        if not budget.cache:
            self._cache = NullCache()
        self._calls = 0
        self._cache_hits = 0
        self._cost = 0.0
        self._all_priced = True
        self._by_model: dict[str, _Tally] = {}
        self._by_purpose: dict[GenerativePurpose, _Tally] = {}
        self._warnings: list[str] = []
        self._seen_warnings: set[str] = set()

    # -- what has happened so far -------------------------------------------
    @property
    def calls(self) -> int:
        """Calls actually made, cache hits excluded."""
        return self._calls

    @property
    def cache_hits(self) -> int:
        """Calls answered from the cache, which cost nothing and are not calls."""
        return self._cache_hits

    @property
    def cost_so_far(self) -> float | None:
        """Dollars spent, or `None` when any call could not be priced."""
        return round(self._cost, 6) if self._all_priced else None

    @property
    def budget_usd(self) -> float | None:
        """The cost ceiling in force, or `None` when no price is known so none can be enforced."""
        return None if self._prices.is_empty else self._budget.max_cost_usd_per_run

    def remaining_calls(self) -> int:
        """How many more calls this job may make."""
        return max(0, self._budget.max_calls_per_run - self._calls)

    # -- the calls ----------------------------------------------------------
    def complete(self, rendered: RenderedPrompt, purpose: GenerativePurpose) -> LLMCompletion:
        """Answer `rendered` with the generating model, from the cache when it can.

        Raises `BUDGET_EXCEEDED` before making a call that would take the job past a ceiling, so a
        job that runs out of budget has spent exactly what it was allowed to and not a cent more.
        """
        return self._call(rendered, purpose, self._llm.generation_model)

    def judge(self, rendered: RenderedPrompt, purpose: GenerativePurpose) -> LLMCompletion:
        """The same, against the judging model.

        Its own method rather than an argument because a deployment may judge with a smaller and
        cheaper model than it generates with, and a caller should not have to know which id that is.
        """
        return self._call(rendered, purpose, self._llm.judge_model)

    def _call(self, rendered: RenderedPrompt, purpose: GenerativePurpose, model_id: str) -> LLMCompletion:
        """Cache, then budget, then the model, then the tally - in that order and only that order."""
        key = cache_key(
            content_hash=rendered.content_hash,
            system=rendered.system,
            user=rendered.user,
            model_id=model_id,
            temperature=self._llm.temperature,
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        self._check_budget()
        completion = self._client.complete(
            rendered.user,
            system=rendered.system,
            model_id=model_id,
            max_tokens=self._llm.max_output_tokens,
            temperature=self._llm.temperature,
        )
        self._record(purpose, completion.model_id, completion.input_tokens, completion.output_tokens)
        # Only a completion that came back is stored: caching a failure would make a transient one
        # permanent.
        self._cache.put(key, completion)
        return completion

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed a batch, metered as one call however many texts it carried.

        One request is what a provider bills, so one request is what the budget counts. The
        protocol's `embed` returns vectors and nothing else - no provider reports usage for an
        embedding through it - so the token count here is the shared `APPROX_CHARS_PER_TOKEN`
        approximation, and a `TOKENS_ESTIMATED` warning says so rather than letting an approximation
        pass for a measurement (DEC-215).

        Embeddings are not cached: a chunk is embedded once, when it is indexed, and the index is
        the cache.
        """
        if not texts:
            return ()
        self._check_budget()
        vectors = self._client.embed(texts, model_id=self._llm.embedding_model)
        estimated = sum(-(-len(text) // APPROX_CHARS_PER_TOKEN) for text in texts)
        self._warn(f"{TOKENS_ESTIMATED}:{GenerativePurpose.EMBEDDING.value}")
        self._record(GenerativePurpose.EMBEDDING, self._llm.embedding_model, estimated, 0)
        return vectors

    # -- bookkeeping --------------------------------------------------------
    def _check_budget(self) -> None:
        if self._calls >= self._budget.max_calls_per_run:
            raise generative_error(BUDGET_EXCEEDED, limit=_CALLS_LIMIT, calls=self._calls)
        ceiling = self.budget_usd
        if ceiling is not None and self._all_priced and self._cost >= ceiling:
            raise generative_error(BUDGET_EXCEEDED, limit=_COST_LIMIT, calls=self._calls)

    def _record(
        self, purpose: GenerativePurpose, model_id: str, input_tokens: int, output_tokens: int
    ) -> None:
        price = self._prices.get(model_id)
        cost = None if price is None else price.cost(input_tokens, output_tokens)
        if cost is None:
            self._warn(f"{PRICE_UNKNOWN}:{model_id}")
            self._all_priced = False
        else:
            self._cost += cost
        self._calls += 1
        self._by_model.setdefault(model_id, _Tally()).add(input_tokens, output_tokens, cost)
        self._by_purpose.setdefault(purpose, _Tally()).add(input_tokens, output_tokens, cost)
        _LOGGER.debug(
            "llm.call purpose=%s input_tokens=%d output_tokens=%d calls=%d",
            purpose.value,
            input_tokens,
            output_tokens,
            self._calls,
        )

    def _warn(self, warning: str) -> None:
        """Record a warning once, however many calls raise it."""
        if warning not in self._seen_warnings:
            self._seen_warnings.add(warning)
            self._warnings.append(warning)

    def totals(self) -> LLMUsage:
        """The shared per-run total a `RunManifest` carries (contracts-first surface)."""
        return LLMUsage(
            calls=self._calls,
            input_tokens=sum(tally.input_tokens for tally in self._by_model.values()),
            output_tokens=sum(tally.output_tokens for tally in self._by_model.values()),
            cost_estimate_usd=self.cost_so_far if self._calls else None,
            model_ids=tuple(sorted(self._by_model)),
        )

    def usage(self, *, now: datetime | None = None) -> LlmUsageReport:
        """The `llm_usage.json` this job produced, whether it finished or not."""
        return LlmUsageReport(
            job_id=self._job_id,
            totals=self.totals(),
            cache_hits=self._cache_hits,
            budget_usd=self.budget_usd,
            budget_calls=self._budget.max_calls_per_run,
            by_model=tuple(
                ModelUsage(
                    model_id=model_id,
                    calls=tally.calls,
                    input_tokens=tally.input_tokens,
                    output_tokens=tally.output_tokens,
                    cost_estimate_usd=tally.cost_or_none,
                )
                for model_id, tally in sorted(self._by_model.items())
            ),
            by_purpose=tuple(
                PurposeUsage(
                    purpose=purpose,
                    calls=tally.calls,
                    input_tokens=tally.input_tokens,
                    output_tokens=tally.output_tokens,
                    cost_estimate_usd=tally.cost_or_none,
                )
                for purpose, tally in sorted(self._by_purpose.items(), key=lambda item: item[0].value)
            ),
            warnings=tuple(self._warnings),
            created_at=now if now is not None else utc_now(),
        )
