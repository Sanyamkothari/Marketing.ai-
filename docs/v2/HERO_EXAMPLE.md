# The hero example, end to end

The product vision's hero example: *a customer bought a serum, looked at moisturisers, and is
nearing the time she usually runs out. Refill reminder, education, a complementary product, free
shipping, or nothing? Chosen per person, by incremental profit.*

This page walks one simulated customer ("Priya") through it, using the real decision from a demo
run, and says how to reproduce it. The code is `autonomy/runtime/demo.py` (`run_demo`,
`HeroLayer`, `HeroPolicy`, `hero_story`); the tests are `tests/v2/wire_agent/test_hero_demo.py`.

## What the demo runs

`run_demo()` runs a Gym shop through the whole system (agents, runtime, worker, dispatcher, Gym
behind the channel seam). With `hero=True`, the default, it adds:

| Piece | What it does |
|---|---|
| **Two goals** | "Win back customers who have not bought in two months" (autopilot) and "Remind customers to reorder before they run out" (`GoalKind.REPEAT_PURCHASE`, at APPROVE: the demo approves each batch). |
| **A product layer** (`ProductLayer`, demo only) | The Gym simulates purchases in categories, not products. The demo gives every Gym purchase a product from a small skincare-led catalogue (`HERO_PRODUCTS`: Vitamin C serum, daily moisturiser, cleanser, SPF 50, candles, energy bars, ...). It does this deterministically: each customer has a usual product per category (a stable hash of customer, category and seed); 70% of purchases are that product and the rest are one of its complements. Visits become product page views: the last product bought, or one of its complements (the serum buyer who keeps looking at moisturisers). Nothing reads the Gym's truth. |
| **Catalogue and models** | `Catalogue.from_records` (prices and cycles inferred from the labelled purchases), the refill `CycleModel` and the `CoPurchaseModel`, refitted weekly on purchases up to `now`. |
| **Enrichment** | Before each sweep every customer's state gets the refill scores (`enrich_state`: `refill_due_days`, `refill_p14`, `refill_window_open`, ...). This runs through the runtime's `state_enricher` hook when `AutonomyRuntime` accepts one (checked with `inspect.signature`). Otherwise it runs through the state store the runtime scans. |
| **The refill goal's audience** | `scores.refill_due_days` between -28 and 7: due within a week, or up to four weeks overdue. Someone more overdue than that has most likely stopped using the product, which is win-back's job. |
| **Products on actions** | The agent's bidders get `item_lookup` (the refill pick: the replenishable product with the highest 14-day refill chance) and `recommender` (a complement: of the co-purchase model's top three, the one she looked at most). The refill goal's actions are about the refill pick (`Action.item_id`). `{{recommended_product}}` in the copy is filled with its name at send time. |
| **Formats** | The Creative and the Critic get an approved asset library (`hero_assets`): four brand images with alt text, on placeholder URLs under the reserved `assets.demo.invalid` domain, which is never fetched. None of them shows a single product, so a message about one product never shows another. Email gets text, image and carousel variants; WhatsApp gets text and image (`formats_for`). |
| **A signed mandate** | One `Mandate` covers both goals: $8,000 ceiling, $500 a day, **20% margin floor**, quiet 21:00-09:00 local, at most 2 messages in any 7 days, email and WhatsApp, the approved templates and offers, and no contact with anyone who has an open complaint. It is signed by two people (a marketing lead and an approver, HMAC keys) and every step goes into the hash-chained `MandateLedger`. Each brief's envelope is `mandate_to_envelope(mandate, offers, base=strategist's envelope)`. A goal that starts later brings version 2 (both goals, all templates), signed at its start; version 1 is recorded as superseded. |
| **The mandate per decision** | `HeroPolicy` removes what the mandate rules out for the customer *before* the auction: unlisted templates, other channels, and any offer that would take her order below the margin floor. A hard rule applied before sampling keeps the logged probabilities exact. Every decision is then checked with `check_action`. An action that fails is logged as "do nothing" with its codes in `blocked_by`, and is never sent. |

The Gym answers a message by its channel, offer, theme and timing only. It knows no products, so
in the Gym a refill reminder about a serum works exactly as well as a reminder about anything
else. Products here decide *who* is in the refill audience, *what* each message is about and how it
is explained. They do not make a message work better in the simulation.

## Priya

From `python -m autonomy.runtime.demo --customers 600 --days 28 --seed 0` (600 customers, 28 days
from 5 January 2026). The Gym has no names; "Priya" is customer `gym_000518`. Everything below is
printed by `hero_story()` from that run. Only the formatting and the comments in italics are added.

### Monday 12 January 2026, 09:00 UTC (the daily sweep, a week into the run)

**What the agent knew about her**

- Bought Vitamin C serum on 15 Sep 2025 ($97.69).
- Bought Vitamin C serum on 21 Nov 2025 ($52.31).
- Looked at Daily moisturiser 4 times.
- 2 purchases, $75.00 an order on average; segment `active`; group `agent`; 2 messages in the last
  7 days; reachable by email, whatsapp.
- Refill pick: **Vitamin C serum**. Her typical gap for it is about 54 days; the refill is due in
  2.7 days; chance she reorders it within 14 days 15%; refill window open; chance she is still on her
  cycle 53%.
- Complementary product: **Daily moisturiser** ("Often bought with your Vitamin C serum, and they
  looked at it 4 times").
- Mandate v1 (signed by asha.marketing-lead, ravi.approver): offers allowed for her: Free
  shipping, No offer, 10% off.

*Refill due in under three days with the window open puts her in the refill goal's audience. Her
orders average $75, so at the shop's 40% margin both offers keep the 20% floor and nothing was
removed for her. For a customer whose orders average $24.49 (`gym_000043` on 5 January), 10% off
($6 a redemption) would leave 15.5% margin, so the auction never saw it.*

**Every option the agent weighed for "Remind customers to reorder before they run out"**

Priced a moment before deciding (`AgentPolicy.explain_bids`, which changes nothing). Expected net is
the estimated incremental profit after the offer and message costs. Net subtracts the attention
price as well. Only options above doing nothing and near the best are sampled. Every option is
about her Vitamin C serum.

| option | channel | offer | format | expected net | net | status |
|---|---|---|---|---:|---:|---|
| goes-well-with (complementary) **(chosen)** | email | No offer | text_image | $1.45 | $1.43 | sampled |
| goes-well-with (complementary) | email | No offer | text | $1.32 | $1.30 | sampled |
| how-to (education) | email | No offer | text_image | $1.16 | $1.15 | sampled |
| goes-well-with (complementary) | email | No offer | carousel | $0.88 | $0.86 | sampled |
| why people reorder it (quality) | email | No offer | text_image | $0.85 | $0.84 | sampled |
| refill reminder | whatsapp | No offer | text | $0.66 | $0.65 | sampled |
| how-to (education) | whatsapp | No offer | text | $0.66 | $0.65 | sampled |
| why people reorder it (quality) | whatsapp | No offer | text | $0.66 | $0.65 | sampled |
| goes-well-with (complementary) | whatsapp | No offer | text | $0.63 | $0.61 | sampled |
| how-to (education) | whatsapp | No offer | text_image | $0.63 | $0.61 | sampled |
| refill reminder | whatsapp | No offer | text_image | $0.63 | $0.61 | not sampled |
| goes-well-with (complementary) | whatsapp | No offer | text_image | $0.62 | $0.61 | not sampled |
| why people reorder it (quality) | whatsapp | No offer | text_image | $0.61 | $0.59 | not sampled |
| how-to (education) | email | No offer | text | $0.58 | $0.57 | not sampled |
| why people reorder it (quality) | email | No offer | text | $0.55 | $0.54 | not sampled |
| refill reminder | email | No offer | text | $0.52 | $0.50 | not sampled |
| refill reminder | email | No offer | text_image | $0.51 | $0.50 | not sampled |
| why people reorder it (quality) | email | No offer | carousel | $0.48 | $0.47 | not sampled |
| how-to (education) | email | No offer | carousel | $0.37 | $0.36 | not sampled |
| refill reminder | email | No offer | carousel | $0.37 | $0.35 | not sampled |
| savings / convenience (6 options) | email | Free shipping | text, image, carousel | $0.04-$0.05 | $0.03 | not sampled |
| savings / convenience (4 options) | whatsapp | Free shipping | text, image | $0.01 | $0.00 | not sampled |
| savings / urgency (6 options) | email | 10% off | text, image, carousel | -$0.28 | -$0.29 to -$0.30 | not sampled |
| savings / urgency (4 options) | whatsapp | 10% off | text, image | -$0.31 | -$0.32 to -$0.33 | not sampled |
| do nothing | | | | $0.00 | $0.00 | the bar |

*The attention price was $0.01. Nothing was blocked by a hard rule.*

**Why the winner won**

- **Discounts lose money on her.** She orders at full price, so most of a 10% discount would go to
  an order she would have placed anyway: -$0.28 to -$0.31. Free shipping costs less and is close to
  worthless ($0.01-$0.05).
- **The refill reminder was a real option, but not the best one.** The window is open, so the
  agent's bidder raised the refill options' uplift with its refill prior (`refill_boost` in
  `autonomy/decide/bidders.py`). But the agent's click estimates for her segment, learned from the
  first week's sends, put goes-well-with and how-to messages by email well above a plain reminder.
  Its best reminder was worth $0.66 (WhatsApp), against $1.45 for "goes well with your serum" with
  an image.
- **An image beat plain text** for this message by email ($1.45 against $1.32). Format is an
  option the agent learns like channel, offer and theme.
- **Thompson sampling chose among the ten near-best options.** The top one was picked with
  probability 0.28, and that probability is logged so the proof can be measured later.

**The decision**

> Sent a personal note by email with no discount: expected to add $1.45 after costs, more than the
> $0.01 cost of another message; email is cheapest. It is about their Vitamin C serum: refill due in
> about 3 days; 15% chance they reorder it within 14 days.

Chosen with probability 0.28 (logged for proof); sent at 10:00 UTC; format `text_image` with the
approved image `hero_gift_note`; mandate check: passed. The email reads "Hi {{first_name}}, we
picked a few things that go well with your Vitamin C serum." (the product slot is filled at send
time). It went into the refill goal's batch for that morning, which the demo approved.

**What happened next (14 days)**

- 12 Jan 10:00: message sent by email (this decision).
- 13 Jan 10:00: message sent by email for the refill goal, about Vitamin C serum.
- 19 Jan 18:00: message sent by email for the refill goal, about Vitamin C serum.
- 20 Jan 18:00: message sent by email for the refill goal, about Vitamin C serum.

She did not click and did not buy in the two weeks. *That is an honest outcome, not a failure of
the walk-through: a 15% chance of a reorder in 14 days means most such customers do not reorder.
It also shows something to watch. The agent messaged her four times in nine days, the most the
mandate allows (2 in any 7 days), because each message still beat its $0.01 attention price. The
attention price is learned from unsubscribes, and one week in it has seen very few.*

## Across the whole run

The same run, 600 customers over 28 days:

- 2,857 decisions, 862 messages sent, 0 rule breaks in the Gym.
- **Win-back was paused on 14 January and stayed paused.** The complaint-rate circuit breaker saw
  2 complaints in 188 email deliveries over the previous 7 days (1.06%, against a 0.08% limit) and
  paused the goal, which is that breaker's job. Nobody resumes it in the simulation, so from
  14 January on, nearly every message in this run is the refill goal's. (With `hero=False` the same
  seed trips no breaker; at this limit two complaints are enough, so this is largely chance.)
- The refill goal sent 364 agent messages, every one about the customer's refill pick: Reed
  diffuser refill 93, Trail energy bars 78, Daily moisturiser 61, Vitamin C serum 55, Soy candle
  30, ... By message type: goes-well-with 176, how-to 96, quality 57, refill reminder 34, free
  shipping (savings angle) 1. By format: image 176, text 130, carousel 58.
- 512 messages across both goals named a product, and 568 carried an image.
- For 451 of the agent group's 2,308 decision moments, 10% off was ruled out by the margin floor
  before the auction. For 176 of them, every offer was ruled out (no order history to show the
  floor holds, or orders too small even for free shipping). Another 10 moments were customers with
  an open complaint, whom the mandate excludes altogether.
- 17 decisions were held by the mandate, all from the **baseline** group (the fixed comparison rule:
  "email lapsed customers with 10% off"). For those customers 10% off would break the margin floor,
  so the gate had removed its templates and the rule found no approved one to use. The rule's
  message was logged as "do nothing" with `mandate_template_not_approved` and `mandate_margin_floor`
  and was never sent. The baseline rule is deterministic, so that decision's logged probability (1)
  is still exact. No agent decision was held.
- The mandate ledger has 4 entries (created, signed, signed, activated), and its chain verifies
  with the signers' keys.

## Reproduce it

```bash
python -m autonomy.runtime.demo --customers 600 --days 28 --seed 0      # about 17 s on one CPU
python -m autonomy.runtime.demo --customers 600 --days 28 --seed 0 --customer gym_000518   # a chosen customer
python -m pytest tests/v2/wire_agent/test_hero_demo.py -q                # about 5 s
```

The command prints the run summary and the walk-through. With no `--customer` it picks, in order of
preference: a refill-goal message about a serum, sent to someone who looked at its complements at
least twice, made after the first week, with two weeks left to watch, where the chosen option was
the top estimate. Which message won is not part of the choice; that is the agent's call.
`run_demo(..., explain=True)` keeps every option the agent weighed (`HeroNote.options`). It prices
each refill-goal decision twice, so it is off by default.

From code:

```python
from autonomy.runtime.demo import run_demo, hero_story

result = run_demo(days=28, customers=600, seed=0, explain=True)
print(hero_story(result))                       # or hero_story(result, "gym_000518")
hero = result.hero                              # catalogue, models, assets, mandate, checks, notes
note = hero.note(result.decisions[0])           # what the hero layer knew for one decision
```

The numbers above come from one seed and are an illustration, not an evaluation. Nothing was tuned
to this seed.

## Limits

- **The products are a demo layer.** Purchase *timing* is the Gym's (each customer's own rate in a
  category), so refill cycles reflect how often a Gym customer buys in that category, not a real
  product's life. Prices are the Gym's order values.
- **The Gym does not know products.** It cannot reward the right product in the message, so this
  demo shows the wiring and the explanations, not the uplift of product choice.
- **Holding after the fact is exact only for deterministic policies.** The gate removes what the
  mandate rules out before the auction, so the agent's own decisions keep exact probabilities.
  An agent decision that still failed the final check would be logged as "do nothing" with the
  probability of the option it chose. That probability understates the true chance of doing
  nothing. No agent decision failed in these runs.
- **Speed.** The API's served demo (1,200 customers, 56 days, late refill goal) takes about 79 s on
  this machine with the hero, against about 55 s without it (`hero=False`). The extra time is
  mostly the agent pricing image and carousel options beside the text ones, and the refill scores
  (one `CycleModel.estimate` per replenishable product per customer per day; in a profile of a
  600-customer run the whole hero layer was about a sixth of the time).
