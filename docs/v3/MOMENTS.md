# Moments: send when a message is worth most

Plan-v3 decision 2 ([`plan-v3.md`](../../plan-v3.md) section 4). A moment is something a customer did lately
that makes a message land: looking at a product twice, leaving a cart, being due to refill, a price drop on
something they looked at, coming back after a long absence, payday, a festival. Each moment raises the value
of a message and fades. India's calendar comes first (decision V-5 in [`DECISIONS.md`](DECISIONS.md)).

It is **off by default**. With it off, every v2 number is the same.

## What is where

| What | File |
|---|---|
| Moment registry, calendar, the opt-in urgency | `autonomy/decide/moments.py` |
| Multipliers and fades, one block per moment | `configs/v3/moments.yaml` |
| Paydays and festival days, one file per country | `configs/v3/calendar_in.yaml` (`calendar_<code>.yaml` for others) |
| New fields on the customer | `autonomy/contracts/customer.py` |
| Folding views, carts, gaps and returns | `autonomy/data/folder.py` |
| Time zone on customer events | `autonomy/data/mapping.py` |
| The hook | `autonomy/decide/bidders.py`: `GoalBidder(moments=book)` and `use_moments(book)` |
| The Gym scenario | `moments_planted` in `autonomy/gym/scenarios.py` |
| Tests | `tests/v3/moments/` |

## Turning it on

```python
from autonomy.decide.bidders import GoalBidder, use_moments
from autonomy.decide.moments import MomentBook

book = MomentBook.load()                    # configs/v3/moments.yaml + every calendar_*.yaml
bidder = GoalBidder(brief, content, moments=book)   # one goal
use_moments(book)                           # or: every bidder built from now on, e.g. the agent's
bidder.moment_reasons(state, now)           # plain sentences for the explanation
```

`use_moments(None)` switches it off again. Nothing in `autonomy/runtime/` reads a setting for it yet; wiring it
to a switch in the agent's settings is left to the integration (see "Open").

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
| `refill_due` | the catalogue's `scores.refill_*` (`autonomy/catalog/cycle.py`), else the customer's own rhythm (3 or more purchases, `purchase_gap_days`) | 1.4 | holds while the window is open (from 3 days before due to 7 after; the scores' own window when they exist) | "Refill due in about 2 days" |
| `price_drop` | a catalogue price fell by 5% or more after the customer's last view of that item, no purchase since | 1.4, scaled up to a 20% drop | halves every 2 days, ends after 3 | "The price of shoe dropped 30% a day ago, after they looked at it" |
| `back_after_absence` | `last_return_at` after a gap of 14 days or more (and 3 times their usual gap) | 1.3 | halves daily, ends after 2 days | "Back just now after 45 days away" |
| `payday` | the 1st and the last day of the month, in the customer's local date | 1.15 | halves every 1.5 days, ends after 3 | "Payday was 2 days ago" |
| `festival` | the country's calendar: from `lead_days` before to `tail_days` after | the festival's own (1.25 to 1.7) | holds through the window | "Diwali is in 3 days" |

Every number in `moments.yaml` is a starting prior of ours, not a measurement. The response model learns what a
message earns at these moments from its own sends and the holdout.

Rules that keep it honest:

- A moment is skipped for a goal that already prices it: a refill goal has `refill_boost`, cart recovery has its
  own fade from the cart (`skipped_for`).
- Buying since ends a cart, a viewed product and a price drop.
- A cart or view time in the future of `now` is no moment.
- The customer's country picks the calendar. No country: `default_country` (IN). A country with no calendar file:
  no payday or festival moment.
- Dates are read in the customer's time zone; for a customer in the calendar's country who still has the UTC
  default, the calendar's zone.

### Adding a country, a moment

A country: copy `configs/v3/calendar_in.yaml` to `calendar_<code>.yaml`, change `country`, `name`, `timezone` and
the days. No code. A festival gives `dates` (one per year) or `fixed: "MM-DD"`.
A moment: write a function `(state, spec, ctx) -> ActiveMoment | None`, `register_moment("name", fn)`, and give
it a block in `moments.yaml`. A moment in the file with no signal fails at load.

## State

`CustomerState` gains (all defaulted, so old states and the mock JSON keep their shape):

| Field | Meaning |
|---|---|
| `last_view_at`, `last_cart_at` | latest view, latest add-to-cart |
| `viewed_items` | item id -> `ItemViews(count, last_at)`; at most 8, the least recently viewed dropped first; a purchase of the item removes it |
| `usual_gap_days` | running average (weight 0.3 on the newest) of gaps of a day or more between days of activity |
| `purchase_gap_days` | the same between purchases |
| `last_return_at`, `last_return_gap_days` | the latest visit, view or cart after a gap of at least 14 days and 3 times the usual gap |

`StateFolder` folds them from view, cart, visit and purchase events. A purchase never counts as a return (they
came to buy, not to browse). A late event moves nothing backwards. `ui/v2/mock/customer.json` carries the new
keys with their defaults.

## Time zone (REAL_DATA_RESULTS failure 10)

`map_frame` now puts the mapping's `timezone` (when not UTC) on every event the customer made themselves
(signup, visit, view, cart, purchase) as `attrs["timezone"]`, unless the mapping already reads a zone from a
column. The folder already turned that into `state.timezone`, so engagement is counted in the customer's local
hours and `choose_send_time` sends at their local hour inside their quiet hours (tested at Asia/Kolkata,
America/New_York and America/Los_Angeles). The assumption: the file's clock is the customers' clock. For a shop
with customers in several zones, map a per-row `timezone` attr instead. One existing assertion changed:
`tests/v2/realfix/test_mapping_safe.py` compares an event's attrs exactly under a Europe/London mapping, and now
expects `"timezone": "Europe/London"` there too.

Not done: the cart-recovery template in `autonomy/agents/strategist.py` still stands in "recent visitors" for
"left a cart" (failure 12), and `days_since_cart` in `bidders.py` still reads `last_seen_at`. Both are outside the
files this stream owns; the state field they need (`last_cart_at`) now exists.

## The Gym scenario `moments_planted`

Last in the registry. `ScenarioSpec` gets eight opt-in knobs (`cart_*`, `refill_*`; all off for every other
scenario, which the `OLD_SPECS` hashes confirm). In the scenario: each customer leaves a cart with probability
0.02 a day; a third buy a regular item about every 30 days; a message's lift is multiplied by 3 at a fresh cart
(halving daily) and by 3 while a refill is due (2 days before to 4 after). The world emits the carts as CART
events and the refills as item-tagged purchases, in the history and while it runs; the truth and the history's
past campaigns use the same multiplier. These are planted beliefs of ours, like the other knobs; nothing is
fitted to data. The Stage 1 to 3 claims for it are in `docs/v2/TARGETS.json` (not yet run).

## What was run, and what it showed

Run in this worktree (small on purpose; heavy runs are for CI):

- `python -m pytest tests/v3/moments -n 2`: 66 passed in 3.96 s. Covers folding, the time zone, each moment
  firing and fading, calendar lookups (including the new year and 29 February), the combination rule, the hook
  off giving identical bids, the hook on, and the scenario.
- Existing tests next to the changed code, also green: `tests/v2/backbone/test_folder.py`,
  `tests/v2/backbone/test_loaders.py`, `tests/v2/realfix/test_mapping_safe.py`,
  `tests/v2/realfix/test_mapping_doctor.py`, `tests/v2/connectors/test_shopify_mapping.py`,
  `tests/v2/decide/test_bidders.py`, `tests/v2/ui/test_live_proof_operator_mock.py`,
  `tests/v2/gym/test_world.py`, `tests/v2/gym/test_score.py`, `tests/v2/gym_real/test_realism.py`,
  `tests/v2/scientist/test_testing_targets.py`, `tests/v2/scientist/test_testing_gym_calibration.py`,
  `tests/v2/promise/test_promise_api.py`, `tests/v2/test_fakes_and_loop.py`, `tests/v2/test_contracts.py`.
- Four existing v2 test files needed a one-line follow-up and were changed: `test_mapping_safe.py` (the event's
  attrs now hold the zone), `backbone/test_folder.py` (the production folder keeps seven more fields than the
  fake, listed in `NEW_FIELDS`), `test_testing_gym_calibration.py` (the old scenarios' hashes ignore the new Gym
  knobs, and the registry has one more name) and `test_testing_targets.py` (the pre-registered hash covers the
  three new claims).
- ruff and black on `autonomy/` and the changed tests; `mypy --strict` on the modules changed.
- A smoke run of the scenario (200 customers, 60 history days, 14 decision days, the agent with and without
  moments; `true_value` is the Gym's planted truth for the agent group, in dollars):

| Seed | Moments off: true value, actions | Moments on: true value, actions |
|---|---|---|
| 0 | 398.33, 603 | 401.80, 603 |
| 1 | 349.13, 576 | 349.31, 576 |
| 2 | 346.63, 557 | 343.20, 559 |

That is a wiring check, not evidence: the sizes are the smoke maximum, the differences are inside what three
seeds of 200 customers can show, and the agent's action counts are nearly the same. The reason is in the next
section.

**Not run:** the Stage 1 to 3 scoreboard for `moments_planted` (CI size is 3,000 customers x 3 seeds); the plan's
test that moment-triggered sends beat the same messages in the daily sweep (needs the runtime's event path,
which this stream does not touch); the RetailRocket check that browse moments predict purchases better than the
daily sweep's ranking (the dataset is not available here); anything on real data.

## Limits to know

- **The raise multiplies the net, so it cannot turn a losing message into a winning one.** `Urgency` multiplies
  `uplift x value - costs`, so a message that loses money at a quiet time still loses it at a moment, only more.
  Moments therefore re-rank customers and contacts when something is scarce (the budget, the weekly cap, the
  attention price); they do not make the agent send what it would not send. Raising the uplift alone (so costs
  stay put and a marginal message can cross zero at a moment) would act more like the plan's example, but it is
  not the existing `Urgency` mechanism the spec named. A follow-up if CI shows the daily sweep gains nothing.
- **A daily sweep sees a moment up to a day late.** The Gym decides once a day at 10:00; the moment-triggered
  path through the runtime's event handling is where the plan expects the gain.
- **Festival dates are unverified.** The lunar dates in `calendar_in.yaml` (Holi, Eid, Raksha Bandhan, Dussehra,
  Diwali, for 2026 and 2027) were written from general knowledge; no command or source was run to check them,
  and Eid can fall a day later. Check them before the first Indian client goes live. The fixed days (Republic
  Day, Independence Day, Christmas, the year-end sale) do not depend on that.
- **Payday is the first and last of the month for everyone.** The plan's "learned per profile" waits for the
  profile (foundation A).
- **The first long gap counts as a return** when the customer has no usual gap yet (only the 14-day minimum
  applies).

## Open

- Nothing in the runtime switches `use_moments` on; the agent's settings need a flag.
- `moments_planted` is not in the matrix of `.github/workflows/gym.yml`; its claims in `TARGETS.json` have no CI
  job behind them yet. `PREREGISTERED` in `tests/v2/scientist/test_testing_targets.py` changed with the new
  claims; other streams adding claims will have to recompute it when they merge.
- `Bid.urgency` can now exceed 1 when moments are on (the contract only requires it to be 0 or more); the Gym smoke and the unit tests passed with that, but no UI screen was checked.
