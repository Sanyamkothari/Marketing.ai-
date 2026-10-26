# Moments: send when a message is worth most

Plan-v3 decision 2 ([`plan-v3.md`](../../plan-v3.md) section 4). A moment is something a customer did lately
that makes a message land: looking at a product twice, leaving a cart, being due to refill, a price drop on
something they looked at, coming back after a long absence, payday, a festival. Each moment raises the value
of a message and fades. India's calendar comes first (decision V-5 in [`DECISIONS.md`](DECISIONS.md)).

Two parts, with different defaults:

- **The moments hook is off by default.** With it off, every bid is the same as in v2 (tested: `test_hook.py`).
- **The time zone fix is not opt-in.** Every mapping whose `timezone` is not UTC now puts that zone on the
  customer's own events, which changes `state.timezone`, the local hours engagement is counted in, quiet hours
  and send times for every customer of such a file. That includes the shipped ONLINE_RETAIL mappings
  (`autonomy/data/loaders.py`, `ONLINE_RETAIL_TIMEZONE = "Europe/London"`). Real-data numbers measured before it
  were not re-measured here; CI must re-run them (see "What CI must re-measure").

## What is where

| What | File |
|---|---|
| Moment registry, calendar, the opt-in urgency | `autonomy/decide/moments.py` |
| Multipliers and fades, one block per moment | `configs/v3/moments.yaml` |
| Paydays and festival days, one file per country | `configs/v3/calendar_in.yaml` (`calendar_<code>.yaml` for others) |
| New fields on the customer | `autonomy/contracts/customer.py` |
| Folding views, carts, gaps, returns and per-product buying | `autonomy/data/folder.py` |
| Time zone on customer events | `autonomy/data/mapping.py` |
| The hook | `autonomy/decide/bidders.py`: `GoalBidder(moments=book)` |
| The agent's switch and explanations | `autonomy/runtime/agent.py`: `build_agent_policy(moments=book)`, `BidExplanation.moment_reasons` |
| The Gym scenario and its switch | `moments_planted` in `autonomy/gym/scenarios.py`; `--moments` in `autonomy/gym/run.py` |
| CI job for the scenario | `gym-moments` in `.github/workflows/gym.yml` |
| Tests | `tests/v3/moments/` |

## Turning it on

```python
from autonomy.decide.moments import MomentBook
from autonomy.runtime.agent import build_agent_policy

book = MomentBook.load(default_country="IN")   # configs/v3/moments.yaml + every calendar_*.yaml
agent = build_agent_policy(briefs, content, moments=book)   # this shop's agent only
agent.explain_bids(state, ctx)[0].moment_reasons             # plain sentences, strongest first
```

`GoalBidder(brief, content, moments=book)` does the same for one goal, and `bidder.moment_reasons(state, now)`
gives its reasons. `moments=False` keeps a bidder off. `use_moments(book)` in `bidders.py` is still there as a
process-wide default for bidders built without `moments=`. In a server with several shops it would hand one
shop's book to all of them, so pass the book per agent instead.

`default_country` is a per-shop setting: the calendar a customer gets when their file names no country. Leave it
out (None) and such customers get no payday or festival moment, so a shop never pushes India's Diwali to a
customer it cannot place.

In the Gym: `python -m autonomy.gym.run --scenario moments_planted --policy agent --moments ...`. `--moments` loads
`MomentBook.load()` (no default country; Gym customers have none, so no calendar moment fires there). It folds
every policy's states with the production `StateFolder` inside `GymFolder`, because the default fake folder
keeps no carts, views or buying rhythms. With the comparison on, it also runs `agent_no_moments` (the same agent,
`moments=False`, same folder and seeds) and writes `moments.gain_per_seed` and `moments.mean_gain` to the report.

## How a moment counts

At the start of a moment a message is worth `multiplier` times as much. The raise (`multiplier - 1`) then halves
every `half_life_days` and is gone after `window_days`. A moment that is a window (refill due, a festival) holds
its full raise while it lasts. When several are live, the strongest raise counts in full and every other at half
(`rest_weight`), capped at `max_multiplier` (2.5). The result multiplies the goal's `Urgency`, so it multiplies
the whole expected net exactly where cart recovery's fade does (`MomentUrgency`): `net = urgency x (uplift x
value - offer cost - channel cost)`.

| Moment | Signal | Multiplier | Fade | Reason shown |
|---|---|---|---|---|
| `viewed_twice` | a product viewed 2 or more times and not bought since (`viewed_items`) | 1.35 | halves daily, ends after 2 days | "Looked at shoe 3 times, last a day ago" |
| `cart_left` | `last_cart_at`, no purchase since | 1.6 | halves daily, ends after 3 days | "Left a cart 6 hours ago" |
| `refill_due` | the catalogue's `scores.refill_*` (`autonomy/catalog/cycle.py`); else a product bought on 3 or more different days (`bought_items`); else, only when no purchase has a product id, 3 or more purchase days in all (`purchase_gaps`, `purchase_gap_days`) | 1.4 | holds while the window is open (from 3 days before due to 7 after; the scores' own window when they exist) | catalogue: "Refill due in about 2 days"; rhythm: "Usually buys cream again in about 2 days" |
| `price_drop` | a catalogue price fell by 5% or more after the customer's last view of that item, no purchase since | 1.4, scaled up to a 20% drop | halves every 2 days, ends after 3 | "The price of shoe dropped 30% a day ago, after they looked at it" |
| `back_after_absence` | `last_return_at` after a gap of 14 days or more (and 3 times their usual gap) | 1.3 | halves daily, ends after 2 days | "Back just now after 45 days away" |
| `payday` | the 1st and the last day of the month, in the customer's local date | 1.15 | halves every 1.5 days, ends after 3 | "Payday was 2 days ago" |
| `festival` | the country's calendar: from `lead_days` before to `tail_days` after | the festival's own (1.25 to 1.7) | holds through the window | "Diwali is in 3 days" |

Every number in `moments.yaml` is a starting prior of ours, not a measurement. The response model learns what a
message earns at these moments from its own sends and the holdout.

Rules that keep it honest:

- A moment is skipped for a goal that already prices it. Cart recovery has its own fade from the cart. A refill
  goal skips `refill_due` only for a customer it has a product for, because `refill_boost` applies only then; a
  refill goal with no product for the customer prices the window through the moment.
- Buying since ends a cart, a viewed product and a price drop.
- A cart or view time in the future of `now` is no moment.
- A buying rhythm counts purchase days, not order lines: several lines of one order are one purchase. A rhythm
  read from the customer's buying, not from the catalogue, gives a reason that claims no more than that
  ("Usually buys again about now"), not "running low".
- The customer's country picks the calendar, by code or by one of the calendar's `aliases` (`IN`, `India`,
  `IND`, `Bharat`; case and extra spaces do not matter). No country: the shop's `default_country`, if set. A
  country with no calendar file: no payday or festival moment.
- Dates are read in the customer's time zone. A customer who still has the UTC default is read in their
  calendar's zone.

### Adding a country, a moment

A country: copy `configs/v3/calendar_in.yaml` to `calendar_<code>.yaml`, change `country`, `name`, `aliases`,
`timezone` and the days. No code. A festival gives `dates` (one per year) or `fixed: "MM-DD"`.
A moment: write a function `(state, spec, ctx) -> ActiveMoment | None`, `register_moment("name", fn)`, and give
it a block in `moments.yaml`. A moment in the file with no signal fails at load.

## State

`CustomerState` gains these fields. All have defaults, so old states and the mock JSON keep their shape.

| Field | Meaning |
|---|---|
| `last_view_at`, `last_cart_at` | latest view, latest add-to-cart |
| `viewed_items` | item id -> `ItemViews(count, last_at)`; at most 8, the least recently viewed dropped first; a purchase of the item removes it |
| `usual_gap_days` | running average (weight 0.3 on the newest) of gaps of a day or more between days of activity |
| `purchase_gap_days`, `purchase_gaps` | the same between purchases, and how many such gaps went into it |
| `bought_items` | item id -> `ItemPurchases(days, last_at, gap_days)`: distinct purchase days of the product and its own running gap; at most 8, the least recently bought dropped first |
| `last_return_at`, `last_return_gap_days` | the latest visit, view or cart after a gap of at least 14 days and 3 times the usual gap |

`StateFolder` folds them from view, cart, visit and purchase events. A purchase never counts as a return (they
came to buy, not to browse). A late event moves nothing backwards. `ui/v2/mock/customer.json` carries the new
keys with their defaults. The fake `SimpleStateFolder` keeps none of them (`NEW_FIELDS` in
`tests/v2/backbone/test_folder.py`).

## Time zone (REAL_DATA_RESULTS failure 10)

`map_frame` now puts the mapping's `timezone` (when not UTC) on every event the customer made themselves
(signup, visit, view, cart, purchase) as `attrs["timezone"]`, unless the mapping already reads a zone from a
column. The folder already turned that into `state.timezone`, so engagement is counted in the customer's local
hours and `choose_send_time` sends at their local hour inside their quiet hours (tested at Asia/Kolkata,
America/New_York and America/Los_Angeles). The assumption is that the file's clock is the customers' clock. For
a shop with customers in several zones, map a per-row `timezone` attr instead. One existing assertion changed:
`tests/v2/realfix/test_mapping_safe.py` compares an event's attrs exactly under a Europe/London mapping, and now
expects `"timezone": "Europe/London"` there too.

This is unconditional: it is the fix the spec asked for, not part of the opt-in hook.

### What CI must re-measure

Everything built from a mapping with a non-UTC `timezone` can move: send hours, quiet-hour holds, and through
them contacts and value. In this repo that is at least:

- `docs/v2/REAL_DATA_RESULTS.md` (the ONLINE_RETAIL loaders use Europe/London; CDNOW and the telco file, if
  mapped with their zones);
- the real-data checks in `scripts/v2_realbench` and the `bench.*` / `sealed.*` claims in
  `docs/v2/TARGETS.json`, wherever their loaders go through `map_frame` with a non-UTC zone.

None of these was re-run here (the datasets are not on this machine, and the bench is a CI job).

Not done: the cart-recovery template in `autonomy/agents/strategist.py` still stands in "recent visitors" for
"left a cart" (failure 12), and `days_since_cart` in `bidders.py` still reads `last_seen_at`. Both are outside the
files this stream owns; the state field they need (`last_cart_at`) now exists.

## The Gym scenario `moments_planted`

Last in the registry. `ScenarioSpec` gets seven opt-in knobs (`cart_*`, `refill_*`). They are off for every
other scenario, which the `OLD_SPECS` hashes confirm. In the scenario:

- each customer leaves a cart with probability 0.02 a day;
- a third buy a regular item about every 30 days (each customer's own cycle within 15%, each refill within a day);
- a message's lift is multiplied by 3 at a fresh cart, and the extra halves daily;
- a message's lift is multiplied by 3 in the 3 days before a refill purchase. The window ends at the purchase:
  the world emits the refill whatever is sent, and a customer who has just restocked needs no reminder.

The world emits the carts as CART events and the refills as item-tagged purchases (`sku_<category>_refill`), in
the history and while it runs. The truth and the history's past campaigns use the same multiplier. The agent
reads the refills through `bought_items`: the refill product's own rhythm, apart from the customer's other buying.
These are planted beliefs of ours, like the other knobs; nothing is fitted to data. No payday or festival is
planted, and with no default country the agent's book raises none.

`docs/v2/TARGETS.json` holds four claims for it, all run with `--moments` at the CI size (3,000 customers x 3
seeds) and none run yet: Stage 1, 2 and 3 of the bar, and `gym.moments_planted.moments_beat_sweep`
(`moments.mean_gain > 0`: the agent with moments beats the same agent with moments off, plan-v3's Gym pass test).
`PREREGISTERED` in `tests/v2/scientist/test_testing_targets.py` was recomputed for them before any of them was run.

## What was run, and what it showed

Run in this worktree (small on purpose; heavy runs are for CI):

- `python -m pytest tests/v3/moments -p no:cacheprovider -q -n 2 --durations=3`: 83 passed; the slowest test is
  the smoke run at 1.84 s. Covers folding (views, carts, gaps, returns, per-product purchase days), the time
  zone, each moment firing and fading, calendar lookups (aliases, no default country, Eid's tail, the new year,
  29 February), the combination rule, the hook off giving identical bids, `moments=False`, the refill skip only
  with a product, the agent passing its book and explaining with it, the scenario's truth and events, and the
  `--moments` helpers.
- `python -m pytest tests/v2/backbone/test_folder.py tests/v2/realfix/test_mapping_safe.py
  tests/v2/ui/test_live_proof_operator_mock.py tests/v2/scientist/test_testing_gym_calibration.py
  tests/v2/scientist/test_testing_targets.py tests/v2/wire_agent/test_items_formats.py -p no:cacheprovider -q -n 2`:
  all passed.
- ruff and black on the changed files; `mypy --strict` on the changed modules and `tests/v3/moments`.
- A smoke run of the scenario at the smoke maximum:

```
python -m autonomy.gym.run --scenario moments_planted --policy agent --moments --seeds 3 --customers 200 --days 14 --report-json smoke.json
```

(The scenario's own 180 history days; the run took 14.7 s.) `true_value` is the Gym's planted truth for the agent group,
in dollars. Both columns use the same full folder and seeds; only the book differs.

| Seed | Moments off: true value, actions | Moments on: true value, actions |
|---|---|---|
| 0 | 398.65, 595 | 398.65, 595 |
| 1 | 361.68, 567 | 345.03, 568 |
| 2 | 401.75, 578 | 402.67, 578 |

`moments.mean_gain` was -5.24 (gains 0.00, -16.65, +0.92). The same run put the agent at Stage 1 (Safe) of the
bar, with 0 rule breaks. This is a wiring check, not evidence: at 200 customers the seeds are noise, and the
actions are almost the same with moments on and off. The next section gives the likely reason. The earlier table
in this file (moments off 398.33 / 603 on seed 0) was made with the fake folder for the off run and the old
refill window, and no longer applies.

**Not run:** the four `moments_planted` claims (CI size); the RetailRocket check that browse moments predict
purchases better than the daily sweep's ranking (the dataset is not here); anything on real data; the real bench.

## Limits to know

- **The raise multiplies the net, so it cannot turn a losing message into a winning one.** `Urgency` multiplies
  `uplift x value - costs`, so a message that loses money at a quiet time still loses it at a moment, only more.
  The channel cost, the offer cost and the sleeping-dog harm hold (`harm_cost = urgency x loss x value`) all
  grow with the moment too. Moments therefore only re-rank customers and contacts when something is scarce
  (the budget, the weekly cap, the attention price); they do not make the agent send what it would not send.
  The smoke run fits that: the actions barely move. Raising only the uplift-times-value term (a factor kept
  apart from `Urgency`, as `refill_boost` already is) would let a marginal message cross zero at a moment. It
  is not the existing `Urgency` mechanism the spec named. Decide once CI has judged `moments_beat_sweep`.
- **A daily sweep sees a moment up to a day late.** The Gym decides once a day at 10:00. The moment-triggered
  path through the runtime's event handling is where the plan expects the gain; the Gym does not drive it.
- **Festival dates come from third-party lists.** On 2026-10-26 a web search of published Indian holiday lists
  (BankBazaar's central government list, a bank's 2026 PDF, OfficeHolidays, CalendarLabs, a reproduction of the
  DoPT 2027 list) moved Eid al-Fitr 2026 to 21 March. The lists disagree by a day on Holi 2026, Bakrid 2026,
  Holi 2027 and Dussehra 2027, so those festivals and both Eids keep one day of tail. No 2027 date for Raksha
  Bandhan was found. Check against the official notification before the first Indian client goes live.
- **Payday is the first and last of the month for everyone.** The plan's "learned per profile" waits for the
  profile (foundation A).
- **The first long gap counts as a return** when the customer has no usual gap yet (only the 14-day minimum
  applies).
- **The rhythm fallback for shops without product ids** still fires for any repeat buyer near their average gap,
  whatever they buy. Its reason says only "Usually buys again ...".

## Open (for the integrator)

- **Price drops have no producer.** `price_drops` and `MomentBook.with_price_changes` work and are tested, but
  nothing builds `PriceChange`s from catalogue snapshots yet. Until the catalogue sync calls them and refreshes the
  agent's book, `price_drop` cannot fire in the product or the Gym.
- **The runtime has no setting for moments.** `build_agent_policy(moments=...)` is the switch; the server's shop
  settings must load a book (with the shop's `default_country`) and pass it.
- **Explanations carry the reasons, the UI does not show them yet.** `BidExplanation.moment_reasons` is filled by
  `explain_bids`; `autonomy/runtime/demo.py` and the decision view do not read it.
- `Bid.urgency` can now exceed 1 when moments are on (the contract only requires it to be 0 or more). The Gym smoke
  and the unit tests passed with that, but no UI screen was checked.
- `PREREGISTERED` changed with the moments claims; other streams adding claims will have to recompute it when they
  merge.
