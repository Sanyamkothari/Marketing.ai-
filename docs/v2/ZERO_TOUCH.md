# Zero-touch setup: connect once, then a weekly read

**Why this matters most:** most customers will reach Autonomy through a connector (Shopify +
Klaviyo first). If connecting gives useful, proven results with almost no work, that is the
product. Every item below is judged by one question: does it cut the customer's time or raise the
money they see?

**Target:**
- About 15 minutes from "Connect Shopify" to a running Shadow plan.
- About 5 minutes a week after that.
- No daily approvals.

Starts after connectors wave 1 (`v2/connectors`) is merged.

## Success measures (checked on the demo shop and the real-data sets)

| Measure | Target |
|---|---|
| Time from connect to first proposed goals with a money size | under 10 minutes after backfill |
| Clicks from connect to Shadow running | 5 or fewer |
| Decisions that need a person once running | 0 inside the limits; only the exceptions below |
| Inbox items per week on a healthy shop | 0-2 |
| Every proposed goal, limit and brand rule | traceable to the shop's own data (no invented numbers) |

## What to build, in order of impact

1. **Goal proposals sized in money, from their own history.** After the backfill, scan for the
   opportunities that pay most often:
   - refill due
   - first-to-second purchase
   - win-back
   - VIP at risk
   - browse/cart (when events exist)

   Each comes with audience size, expected monthly value with a range (from the shop's own repeat
   rates and the priors), and one sentence of why. One click accepts it. The best one is
   preselected.
2. **Limits learned from their history, confirmed once.** These come from their own data:
   - **How often they message:** their past send cadence (from Klaviyo).
   - **Discount limits:** their usual discount depth and margins (from Shopify orders and
     discounts).
   - **Quiet hours:** their customers' time zones.
   - **Budget:** from current spend.

   Show "what we found -> what we will use". It becomes the signed mandate in one click.
3. **Brand kit read from their tools.** We read these, never invent them:
   - Tone and phrasing, from their Klaviyo templates and past campaigns.
   - Product names and images, from Shopify.
   - Logo and colours, from the shop theme or their email templates.
   - Discount style.

   The Critic checks every message against it. One approval of a sample of 3 messages covers the
   brand voice.
4. **Exceptions-only inbox.** Only these:
   - safety stop tripped
   - proof shows it is losing
   - reconnect needed
   - something new or bigger than the mandate (offer type, discount depth, channel, budget)
   - a customer complaint that needs a person

   Everything else goes in the weekly briefing, not the inbox. Each item has one recommended action
   and one button.
5. **Shadow -> Autopilot in one click.**
   - After 1-2 weeks of Shadow (it decides, sends nothing), show "it would have made about $X
     (range), and its forecasts matched what happened within Y". Then "Turn on Autopilot?".
   - Option: pre-authorise automatic promotion when the proof bar is met.
   - The automatic version must also stop itself: it drops back to Approve if proof turns
     negative.
6. **Data Doctor as a quiet watchdog.** For connected data it watches for these, and stays silent
   unless the data should not be trusted:
   - missing days
   - row-count drops
   - schema changes
   - consent coverage
   - duplicate customers across Shopify and Klaviyo (link by email or phone)
7. **Guided setup only where unavoidable.** Klaviyo is fully automatic. Other tools get a
   step-by-step check that verifies itself by sending a test event.

## Not in scope

Any approval that the mandate already covers. Daily reports. Asking the customer to map fields.

## Status (21 Oct 2026)

All seven items are built, joined up and rehearsed end to end on a simulated Shopify + Klaviyo shop.
On that shop the setup took **5 clicks**. The goal proposals had money (with ranges) **20 simulated
minutes** after Connect. Over 8 weeks the inbox held **one item**, the reconnect the rehearsal caused
on purpose. **No decision inside the mandate** needed a person. Autopilot was offered on day 23 of
Shadow and taken with one click. What is still not zero-touch, and why, is at the end.

### Built

| Item | Where | What the owner sees |
|---|---|---|
| 1. Goal proposals sized in money | `autonomy/onboarding/` (`scan_opportunities`), `GET /v2/onboarding/proposals` | Refill due, second purchase, win-back, VIP at risk, and cart/browse when those events exist. Each shows how many people qualify, extra profit over 30 days with a range, and one sentence of why. The best one is preselected. Without enough history the proposal shows no money and says what is missing |
| 2. Limits learned and signed once | `autonomy/onboarding/limits.py`, `POST /v2/onboarding/accept` | "What we found -> what we will use" for cadence, discount depth, margin, quiet hours (from each customer's time zone) and budget. One click signs the mandate (the single-owner path) and starts the goal in Shadow |
| 3. Brand kit read from their tools | `autonomy/brand/`, `/v2/onboarding/brand` | Voice rules counted from their Klaviyo templates and sent campaigns, plus logo, colours and product photos from Shopify. Three sample messages, one approval. **New in this step:** the approval also writes every running goal's messages in that voice (`autonomy.brand.samples.goal_library`). Each message passes the Critic's rules and the kit's, so nobody reviews them one by one |
| 4. Exceptions-only inbox | `autonomy/inbox/`, `/v2/inbox` | Seven kinds of item, each with one sentence and one button. Every other alert goes to the weekly briefing, worded "people we never contact", never "holdout" |
| 5. Shadow -> Autopilot in one click | `autonomy/runtime/promotion.py`, `/v2/autopilot` | "Shadow ran N days: it would have made about $X a month ($L to $H)…", then one button. It drops back to Approve on its own when a safety stop trips or proof turns negative. Every level change is in a hash-chained ledger |
| 6. Data Doctor as a quiet watchdog | `autonomy/inbox/watchdog.py`, joined to the connector data gate | Silent unless the data should not be trusted: missing days, row-count drops, schema changes, consent jumps, duplicate customers |
| 7. Guided setup only where unavoidable | `autonomy/connectors/` (Shopify OAuth, Klaviyo OAuth with automatic send setup) | Klaviyo's send flows are created on connect. Sending goes through the shop's own Klaviyo |
| UI | `ui/v2/screens/onboarding.js`, `live.js` | Getting started is the five-click path. Live shows the inbox and the Autopilot offer |

Joined up in this step:

- **The worker runs zero-touch on its own ticks.** `autonomy.runtime.service.worker_for_runtime` is
  what `build_worker` runs. With connectors it installs the inbox, the Autopilot guard and the
  watchdog (`autonomy.inbox.install`). So automatic demotion no longer waits for someone to open the
  app. The API uses the same wiring through `api.v2.app.app_for_runtime`.
- **Refill reminders are never preselected in production.** The production runtime puts no refill
  scores on customers, so a refill plan there would reach no one. The proposal is still sized and
  shown, with a note saying why (`REFILL_UNREACHABLE`). The demo, which has the scores, still
  preselects it.
- **The routes are published.** The 15 onboarding, brand, inbox and Autopilot routes are in
  `docs/v2/openapi.json` and in `tests/v2/api/test_api.py`'s route list.
- **"Holdout" is gone from the proposals.** Their basis now names its source in words: "the default
  share of people we never contact".

### The rehearsal

`python -m scripts.v2_zero_touch_rehearsal --json out.json` (no network, about 30 minutes).

- **Customers.** A Gym world (`baseline_shop`) is the shop's customers. They are spread over New York,
  Chicago, Los Angeles, London and Sydney, with 180 days of history: orders, consent and the shop's
  own weekly campaign. They answer our messages by their hidden planted response.
- **Platforms.** Their events reach Autonomy only as records of a fake Shopify (customers, consent,
  orders, products, brand) and a fake Klaviyo (profiles, consent, campaign deliveries and clicks,
  templates, campaigns). These are the connector tests' fakes, built on the documented API shapes.
- **Autonomy.** It runs as two processes over one SQLite database, the production API app and the
  production worker, with sends handed to the shop's Klaviyo. The rehearsal plays Klaviyo's flow: it
  gives the message to the Gym customer, and the customer's reactions come back as Klaviyo engagement
  with Autonomy's `$flow`, and as Shopify orders.
- **Script.** The run connects both platforms, backfills, proposes, accepts, approves the voice, runs
  Shadow until the offer and turns Autopilot on. It then runs 10 days of Autopilot with three
  incidents: Klaviyo silently drops one hand-off, Shopify answers 503 for 9 hours, and the Shopify
  app is uninstalled and reconnected 5 hours later.

Measured on 5,000 customers (6,318 orders in history), seed 0:

| Measure | Target | Measured | |
|---|---|---|---|
| Time from Connect to proposals with money | under 10 min after backfill (about 15 min to Shadow) | **20 simulated minutes** after Connect. The full backfill took 51 minutes. The proposals at minute 20 were the final ones: the same pick, and no amount moved by more than $0.08 | Met against "after backfill". The 15-minute total is not met here (see below) |
| Clicks from Connect to Shadow running | 5 or fewer | **5**: Continue to Shopify, Connect Klaviyo, Use this goal, Sign and start Shadow, Approve this voice. The margin is typed, not clicked. The platforms' own consent screens are not counted | Met |
| Decisions that need a person once running | 0 inside the limits | **0**: no batch waited for approval, and no approval item appeared | Met |
| Inbox items per week on a healthy shop | 0-2 | **0** every Shadow week. **1** in the Autopilot week: the reconnect the rehearsal caused, which cleared itself when the owner reconnected | Met |
| Every proposed goal, limit and brand rule traces to data | no invented numbers | **4 of 4** proposals have money with a range and a source for every input. Inputs taken from documented priors are marked `assumed` (the per-message lift, message cost, the share of people we never contact, the refill boost) | Met |
| | | **6 of 6** limits come from the shop's data. The margin is the owner's typed answer | |
| | | **12** brand findings, each with its evidence count. "Holdout" appears nowhere | |
| Autopilot offer after 1-2 weeks of Shadow | "it would have made about $X (range), forecasts matched within Y" | **Day 23**: "Shadow ran 23 days: it would have made about $116 a month ($104 to $128); customers spent 189% more than it forecast, so that is cautious." 325 contact decisions (260 closed), 35 purchases checked, and the people we never contact bought like everyone else | Later than 2 weeks (see below) |
| Weekly time after setup | about 5 minutes | One read a week of "Nothing needs you" plus the briefing line ("1 thing needed you this week; 1 cleared on its own"), and 2 clicks in 5 weeks: Turn on Autopilot and Reconnect Shopify | Met in the simulation |

What happened on Autopilot (10 days):

- **Sends.** 349 decisions were handed to Klaviyo and 346 were delivered, as read back from Klaviyo's
  engagement.
- **Silent drop.** The one hand-off Klaviyo dropped became a `not_delivered_by_tool` FAIL six hours
  later, so proof does not count it as sent. No inbox item.
- **9-hour Shopify outage.** Decisions and queued sends were held for 6 hours and released on their
  own when the data caught up. No inbox item: a hold under 6 hours goes to the briefing.
- **Uninstall.** One reconnect item ("Shopify (Acme Goods) needs reconnecting; decisions wait until it
  is."). Decisions were held for 5 hours. One click reconnected, and the item cleared itself.
- **Proof** was too early to judge after 10 days: the range spans zero. No outcome window had closed,
  so the guard did not judge yet either.

Other runs, as cross-checks:

- **3,000 customers** (win-back preselected). Everything above held: 5 clicks, 0 decisions for a
  person, 1 inbox item in 8 weeks. Autopilot was **not** offered within 42 days. The holdup was the
  spend check, 18 of the 30 purchases it needs: lapsed customers buy rarely. 288 contact decisions had
  closed and the comparison was fair.
- **1,500 customers** (second purchase). Not offered within 35 days: 97 of 100 closed decisions, 15 of
  30 purchases, and 23 of the 30 people we never contact it needs.

### Found by the rehearsal and fixed

- **False safety stop when Klaviyo sends** (`autonomy/channels/breakers.py`,
  `autonomy/runtime/settings.py`). The delivery-rate breaker judged sends one hour old. Klaviyo's
  deliveries come back only on the hourly engagement read, so an evening batch still in flight read
  as "82% delivered" (120 of 146). That paused email and dropped Autopilot to Approve, though 145 of
  the 146 were delivered. That run (3,000 customers, before the fix) then showed 3 inbox items in one
  week, and 8 batches expired with nobody approving them. Fix: `Guards.channel_grace`. With
  connectors, a send counts only after Klaviyo's read-back window (6 h email, 18 h SMS). Test:
  `test_an_evening_batch_klaviyo_has_not_read_back_yet_is_not_a_delivery_failure`.
- **A lone silent drop was never recorded** (`autonomy/channels/klaviyo.py`). The delivery watch
  concludes "not delivered" only once some delivery in its 2-day window proves the linking works. A
  small shop that sends every few days had the drop as its only hand-off there, so the watch held
  forever and proof counted a message that never left. Fix: a delivery in the last 14 days also counts
  as proof (`linked_lookback`). Test:
  `test_a_lone_drop_days_after_the_last_delivery_still_becomes_not_delivered`.
- **A running goal had nothing to send after setup.** Nothing in production wrote the goal's message
  library, so Shadow would have decided "no message fits" for everyone. The voice approval now writes
  it, and so does accepting a goal once the voice is approved.

### Still not zero-touch, and why

- **Autopilot takes 3 or more weeks on small shops.** The bar wants 100 closed contact decisions, 30
  purchases to check spend forecasts and 30 measured people we never contact. 5,000 customers met it
  on day 23. 3,000 and 1,500 did not within 6 weeks. Lowering the bar would weaken what "earned"
  means, so whether a small shop should be offered Approve first instead is a product call.
- **The biggest proposal is unavailable in production.** Refill reminders ($164 a month, $80 to $253,
  on the 5,000 shop, against $43 for the goal picked instead) need refill scores on each customer, and
  only the demo's runtime puts them there. Wiring catalogue enrichment into the production runtime is
  runtime work, outside this stream.
- **15 minutes is not met on these fakes.** The backfill walks 730 days of orders in 30-day bulk
  chunks and 10 years of customers in 1-year chunks, one bulk poll each. The proposals only needed
  minute 20, but the walk ran to minute 51. Real Shopify bulk operations take longer per chunk for
  large shops. Not measured against the real APIs.
- **One question remains.** The connector does not read Shopify's product cost, so the margin is
  asked once (typed).
- **The brand voice will be thinner over OAuth.** `campaigns:read` is not in the Klaviyo OAuth
  scopes, so a real OAuth connection reads templates only. The fake does not check scopes, so the
  rehearsal read 16 messages (4 templates and 12 campaigns).
- **Real APIs never exercised.** Shopify OAuth, bulk operations, uninstall and level-2 contact fields,
  Klaviyo OAuth, revoke, flow creation, campaigns and the "Received Email" `$flow` stamp were all
  checked against fakes only. The checklists are in CONNECTORS.md and `autonomy/brand/read.py`.
- **No sign-in for the v2 UI in production.** The mounted API asks for an operator token, so the UI
  cannot call it until v2 sign-in exists.
- **The mandate is checked only through the plan's envelope.** The mandate lists no message
  templates (none existed when it was signed). The production runtime does not call `check_action`.
  The onboarding mandate ledger and keys live in the API process's memory.
- **Simulated figures.** The money on the proposals is the documented prior (one e-mail adds about
  0.45 points), not the shop's own measured lift. On the 5,000 shop that is $26-$164 a month per
  goal, depending on the goal. Proof replaces it once windows close. The rehearsal ticks hourly
  (production: every 5 minutes) and polls Shopify hourly with no webhooks. Both make it slower to
  react than production, never faster.

Checks for this step:

- **Lint:** ruff and black are clean on every file touched.
- **Tests:** the requested suites (zero_touch, connectors, api, ui, promise, runtime, channels) pass
  (579 passed, 15 skipped). A wider sweep of 20 v2 suites passes 1,516 tests. Its only failure is
  `engines/test_decisions_api.py::test_lapsed_contactable_customer_gets_an_action_with_probability`,
  which fails the same way without these changes (decide area).
- **Imports and syntax:** the new modules import under Python 3.11. `node --check` passes on the UI.
- **OpenAPI and screenshots:** `docs/v2/openapi.json` is regenerated, and the screenshots are retaken.
