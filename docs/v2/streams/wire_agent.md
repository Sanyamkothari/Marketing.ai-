# Stream wire_agent: products, formats and the hero example

This stream wires the agent to the catalogue (products and refills), to the asset library
(images, carousels, video) and to the signed mandate, and runs the plan's hero example end to end
in the demo. No LLM in the per-customer loop; time and seeds are passed in, so runs repeat.

## What was built

| Module | What changed |
|---|---|
| `autonomy/decide/bidders.py` | `GoalBidder` bids name a product (`Action.item_id`) and a format's asset (`Action.asset_id`). Refill goals (repeat purchase, or an audience on `scores.refill_*`) get `refill_boost` while the window is open; offers count `refill_p14` as redemptions. One option per theme x format; rich formats need channel support and an approved asset; a product message never shows another product's image. `quote()` prices without remembering anything. |
| `autonomy/decide/models.py` | `action_key(..., fmt)`: plain text keeps the 3-part key, other formats add a 4th part (`base_key`, `key_format`). Beta priors for a format key fall back to the plain-text key. `PersonalEffects` learns a per-person format factor (text is the reference). |
| `autonomy/decide/__init__.py` | Re-exports the new names (`ItemChoice`, `ItemLookup`, `AssetOf`, `asset_lookup`, `needs_item`, `refill_boost`, `refill_window_open`, `uses_refill_scores`, `base_key`, `key_format`). |
| `autonomy/runtime/agent.py` | `ClickResponseModel` shrinks a format's click rate towards the same words in plain text (`FORMAT_POOL`). `AgentPolicy.explain_bids` ranks every option with what blocks it, with no side effects. `set_items` and new `build_agent_policy` keywords pass products, assets and format costs. |
| `autonomy/learning/` | Learning keys now carry the format (`format_of`; `LoggedSend.format`), so image sends teach the image option through `update_models`, `learn_from_rewards` and the logged-history warm start. |
| `autonomy/runtime/demo.py` | `run_demo(hero=True)` (the default) runs win-back plus a refill goal at APPROVE: a deterministic product layer on Gym purchases, catalogue / cycle / co-purchase models refitted weekly, approved demo images, and one HMAC-signed mandate with a hash-chained ledger checked on every decision (`HeroLayer`, `HeroPolicy`). `hero_story(result)` prints one customer's walk-through. |
| `docs/v2/HERO_EXAMPLE.md` | "Priya" through the hero run (600 customers, 28 days, seed 0) with the real numbers, every option weighed, and the run's limits. |

## How other streams use it

```python
from autonomy.runtime.agent import build_agent_policy, AgentPolicy, BidExplanation, BASELINE_GROUP

build_agent_policy(briefs, content, *, bundle=None, costs=None, exploration_floor=None,
                   baseline_policy=None, kinds=None, learn_horizon_days=...,
                   item_lookup=None, recommender=None, asset_of=None,
                   carousel_template=False, format_costs=None) -> AgentPolicy
AgentPolicy.set_items(refill=None, complements=None) -> None   # e.g. refill=enrich_all(...)[1]
AgentPolicy.explain_bids(state, ctx, *, trigger=None) -> list[BidExplanation]
# BidExplanation: bid, net, blocked_by (rules code | "holdout" | BASELINE_GROUP), considered, item_reason

from autonomy.decide import GoalBidder, asset_lookup, refill_boost, action_key
GoalBidder(brief, content=None, ..., item_lookup=None, recommender=None, refill=None,
           asset_of=None, carousel_template=False, format_costs=None)
  .quote(state, ctx) -> list[Bid]          # same as bids(), remembers nothing
asset_lookup(assets: Iterable[Asset]) -> Callable[[ContentItem], Asset | None]
action_key(channel, offer_id, theme, fmt=None) -> str

from autonomy.runtime.demo import run_demo, hero_story, build_system
run_demo(scenario="baseline_shop", days=30, customers=1500, seed=0, *, ...,
         hero=True, explain=False) -> DemoResult      # DemoResult.hero: HeroLayer | None
hero_story(result, customer_id=None, *, name="Priya") -> str
```

`ItemLookup` is `customer_id -> item id | (item id, reason) | None`; `asset_of` is
`ContentItem -> asset id | Asset | None`. `python -m autonomy.runtime.demo` prints the walk-through.

## Contract additions requested

- `DecisionContext.items: dict[str, str]` and `recommendations: dict[str, tuple[str, str]]`, so
  products travel with the decision instead of through injected callables or `set_items`.
- `ContentItem.asset_id` (or `asset_ids` for a carousel), so `Action.asset_id` need not be
  recovered by matching `asset_url` back to the library.
- A `Decision` field (or contract place) for the full option list and the product reason, so a
  walk-through need not rely on the demo's in-memory `HeroNote`.
- A documented runtime hook for the per-goal mandate check (e.g. `AutonomyRuntime(mandate_for=...)`)
  and for `state_enricher`, so the demo's signed mandate also gates dispatch.

## Dependencies requested

None.

## Known gaps

- `mypy --strict` under Python 3.11 is left to CI (local numpy stubs need 3.12+; only 3.11 syntax used).
- Carousel product check reads only the first card's asset; later cards could show other products.
- The refill boost is a fixed prior from the catalogue backtest, not learned; the response key has
  no window-open flag.
- `explain_bids` skips the first-decision warm start and daily price refresh, and reads
  `AutonomousPolicy._scheduled/_timed/_price` (public read-only accessors would be cleaner).
  `api/v2/engines/decisions.py` should switch to `explain_bids`.
- `AutonomyRuntime` has no `state_enricher` hook yet; the demo uses its fallback (an enriching
  state-store wrapper). The hook path is wired by name but untested against a real signature.
- Speed: the API's served demo (1,200 customers x 56 days) takes about 79 s with the hero vs 55 s
  without, mostly from pricing image and carousel options.
- The Gym knows no products, so the demo shows wiring and explanations, not the uplift of product
  choice. An agent decision failing the final mandate check would be logged as do-nothing with the
  chosen option's probability (never seen in the runs checked).
- `demo.py` is about 2,450 lines; the hero layer could move to `autonomy/runtime/hero.py`.
- Other streams' tests (`tests/v2/runtime/test_demo.py`, `tests/v2/polish/test_api_demo.py`) were
  not run here; their shapes were checked by script.
