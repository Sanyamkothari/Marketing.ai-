# The six engines

Autonomy is built from six engines. Each one does one job well, has its own public API and its own
contract, and can be used without the other five. They are packages in one app, not separate
services: they share the contracts in `autonomy/contracts/` and run in one process, but nothing in
one engine's API needs another engine to have run first.

| Engine | Job | API module | Prefix |
|---|---|---|---|
| **P1 Data Doctor** | Turn a business's raw data into clean, linked events and customer state | `api/v2/engines/doctor.py` | `/v2/engines/doctor` |
| **P2 Mandate & Catalogue** | Turn a goal into an approved mandate (brief + envelope) and keep the offer and product catalogue | `api/v2/engines/mandates.py` | `/v2/engines/mandates` |
| **P3 Decision Hub & Clearinghouse** | Decide, for one customer at one moment, whether to act and with what | `api/v2/engines/decisions.py` | `/v2/engines/decisions` |
| **P4 Creative & Compliance Studio** | Write labelled content and check it before it can be used | `api/v2/engines/studio.py` | `/v2/engines/studio` |
| **P5 Dispatch Gateway** | Send a decision on a channel and bring its events back | `api/v2/engines/dispatch.py` | `/v2/engines/dispatch` |
| **P6 Causal Incrementality Ledger** | Prove what the marketing caused, against a holdout | `api/v2/engines/measurement.py` | `/v2/engines/measurement` |

## How they are mounted

`api/v2/engines/router.py` has one `APIRouter` with the prefix `/v2/engines`, and the integrator
mounts it next to the `/v2` routes. Each engine module exposes an `APIRouter` named `router` with
its own sub-prefix (`/decisions`, `/doctor`, ...). The aggregator imports each engine module and
includes its router; a module that does not exist yet is skipped, so an engine appears as soon as
its stream merges, with no edit here. A module that exists but fails to import is a bug, and the
error is not hidden.

`GET /v2/engines` lists the six engines and whether this build serves each:

```json
{"engines": [
  {"id": "P1", "name": "Data Doctor", "module": "api.v2.engines.doctor", "mounted": true},
  {"id": "P3", "name": "Decision Hub & Clearinghouse", "module": "api.v2.engines.decisions", "mounted": true}
]}
```

**Rules every engine follows.** Inputs come in the request wherever possible (stateless), so a
client can use one engine with its own tools for the rest. Time is passed in (`now`); no engine
reads the clock. Answers are deterministic given a `seed`. No LLM runs per customer. When the app
has `Services` attached (`api.v2.deps.attach_services`), an engine may read the running programme's
workspace or stores to save the caller sending them, but it never needs them.

---

## P1 Data Doctor

**Value.** Most marketing tools assume clean data. The Data Doctor makes it clean: it connects
files or a database, maps the columns to Autonomy's event types (purchase, visit, send, open,
click, reply, unsubscribe), links records for the same person by email or phone, and raises an
alarm when the data drifts. Its output is the `Event` and `CustomerState` contracts, which any
decision tool can read.

**Endpoints** (from `api/v2/engines/doctor.py`, where the full request and response shapes live).
Both are stateless: the file is read, answered on and forgotten. Uploads are capped at 20 MB (413
beyond that), and multipart is parsed with the standard library, so no extra package is needed.

| Method | Path | What it does |
|---|---|---|
| POST | `/v2/engines/doctor/profile` | A CSV in (multipart field `file`, or a raw `text/csv` body with the options as query params); a readiness score out, with per-column profiles, personal-data findings (email, phone, Luhn-valid cards, Verhoeff-valid Aadhaar, PAN; names, addresses and dates of birth by header) and what to do with each (hash, mask, drop), primary-key duplicates and blanks, unreadable or future-dated dates, and columns that might leak the target |
| POST | `/v2/engines/doctor/map-events` | A CSV plus an `EventMapping` (form field `mapping`, JSON) in; the `Event`s, the rows that could not become one and a count per problem out |

Form fields for `profile`: `snapshot` (ISO time with a zone; a date after it is "future-dated"),
`primary_key`, `target`, `date_columns` (comma-separated), `delimiter`, `use_v1`. The readiness
score is 100, less 20 per error and 7 per warning. No cell value is ever returned. When the v1
engine is importable, v1's Indian identifier checks (GSTIN, voter ID, PIN, mobile) are folded in
under `v1_enrichment`.

```bash
curl -F file=@customers.csv -F snapshot=2026-10-01T00:00:00+05:30 -F primary_key=customer_id \
     -F target=churned http://localhost:8000/v2/engines/doctor/profile
```

Column mapping proposals, identity linking and drift alerts are not in this API yet.

**Standalone use.** A business that already has a sending tool can use the Doctor alone to check an
export before trusting it (personal data, key, dates, leaks) and to turn it into clean events, then
feed those to P3 or to its own models.

---

## P2 Mandate & Catalogue

**Value.** A marketer says what they want in plain words ("win back customers who haven't bought
in 60 days, $5,000 this month"). The Mandate engine turns that into a `Goal` and a `Brief`: the
audience, the objective, the outcome window, and the **envelope** the agent must stay inside
(channels, approved offers, contact caps, quiet hours, exploration floor, split shares, stop
rules). Nothing reaches a customer until a person approves the envelope. The Catalogue keeps the
offers (`OfferOption`: kind, amount, cost per redemption, whether each needs approval) and the
products the agent may talk about.

**Endpoints.** See the engine's own router (`api/v2/engines/mandates.py`); its stream fills this
list. The expected shape: create a goal from text and get a proposed brief; approve or edit an
envelope; read and update the offer and product catalogue.

**Standalone use.** A team that writes its own campaigns can use P2 to turn goals into explicit,
approved guardrails and a clean offer catalogue, and hand the brief to any decision engine (P3
takes it as-is).

---

## P3 Decision Hub & Clearinghouse

**Value.** The inner loop as a service. For one customer at one moment it decides whether to make
contact at all, and if so on which channel, with which offer and content, and when. Every goal
bids for the customer's attention; one more message has a price (the learned chance of an
unsubscribe times the value of the messages we could no longer send); hard rules remove what may
never go out (consent, unsubscribes, weekly caps, quiet hours, budget, offers outside the
envelope); Thompson sampling with an exploration floor chooses. Every answer, "do nothing"
included, carries the probability the policy had of choosing it, which is what makes it possible
to evaluate a new policy on old data and to prove value later. Pure maths: no LLM, no clock.

The **clearinghouse** view makes the auction readable: every option that did not go out is listed
with its reason, and the response shows the runner-up bid and a second-price clearing price. That
view is informative only; it never changes what the policy chose.

**Endpoints.**

| Method | Path | What it does |
|---|---|---|
| POST | `/v2/engines/decisions/evaluate` | One customer: an action or nothing, with its auction |
| POST | `/v2/engines/decisions/evaluate-batch` | Up to 1,000 customers sharing one plan, library and moment |

**Request** (`evaluate`):

| Field | Type | Notes |
|---|---|---|
| `customer_state` | `CustomerState` | The contract; one row per customer |
| `briefs` | `[Brief]` | Approved briefs, one per goal. Or name them in `brief_ids` |
| `brief_ids` | `[str]` | Read from the attached workspace (latest version per goal); 400 without one, 404 if unknown |
| `content` | `[ContentItem]` or omitted | The approved library; omitted reads the workspace's, else none |
| `now` | aware datetime | The moment to decide for |
| `seed` | int, default 0 | Fixes the draw: the same request gives the same answer |
| `budget_left` | `{goal_id: money}` | Optional; a goal left out is not budget-checked |
| `goal_kinds` | `{goal_id: GoalKind}` | Optional (cart recovery fades fast); read from the workspace when attached |
| `exploration_floor` | 0..1 | Optional override of the envelopes' floor |

`evaluate-batch` takes the same fields with `customer_states: [CustomerState]` (1 to 1,000; more is
a 422) instead of `customer_state`.

```json
{
  "customer_state": {"customer_id": "c_lapsed", "as_of": "2026-03-02T09:00:00Z", "purchases": 3,
    "revenue": 150.0, "margin": 60.0, "first_seen_at": "2025-01-26T09:00:00Z",
    "last_purchase_at": "2025-11-22T09:00:00Z",
    "channels": {"email": {"address_known": true, "consent": true},
                 "whatsapp": {"address_known": true, "consent": false}}},
  "briefs": [{"brief_id": "brief_goal_winback_1", "goal_id": "goal_winback", "...": "..."}],
  "content": [{"content_id": "ci_0", "channel": "email", "theme": "reminder", "...": "..."}],
  "now": "2026-03-02T09:00:00Z",
  "seed": 1
}
```

**Response** (`evaluate`; `evaluate-batch` returns `{results: [...], n_customers, n_take_action,
n_do_nothing, policy_version}`):

```json
{
  "customer_id": "c_lapsed",
  "decision_id": "dec_b50723dad14cb08d8fff",
  "decision": "TAKE_ACTION",
  "group": "agent",
  "probability": 0.456625,
  "reason": "Sent a reminder by email with no discount: expected to add $0.25 after costs, more than the $0.01 cost of another message; an offer would not add enough to pay for itself.",
  "winning_action": {
    "goal_id": "goal_winback", "offer_id": "none", "channel": "email", "content_id": "ci_0",
    "format": "text", "item_id": null, "asset_id": null, "follow_up_step": 0,
    "scheduled_time": "2026-03-02T10:00:00Z", "expected_net_value": 0.249,
    "assigned_probability": 0.456625, "reason": "Sent a reminder by email ..."
  },
  "suppressed_actions": [
    {"goal_id": "goal_winback", "channel": "whatsapp", "offer_id": null, "content_id": null,
     "reason_code": "no_consent", "plain_reason": "No consent to message the customer on this channel.", "bid": null},
    {"goal_id": "goal_winback", "channel": "email", "offer_id": "none", "content_id": "ci_2",
     "reason_code": "outbid", "plain_reason": "Expected to add $0.25, but lost the auction to the chosen action.", "bid": 0.249},
    {"goal_id": "goal_winback", "channel": "email", "offer_id": "pct10", "content_id": "ci_1",
     "reason_code": "below_attention_price", "plain_reason": "Expected to add -$0.11, not more than the $0.01 cost of another message.", "bid": -0.115}
  ],
  "blocked_by": [],
  "clearing": {
    "attention_price": 0.0065, "winning_bid": 0.249,
    "runner_up": {"goal_id": "goal_winback", "channel": "email", "offer_id": "none", "content_id": "ci_2", "bid": 0.249},
    "clearing_price": 0.249, "winner_surplus": 0.0
  },
  "policy_version": "autonomy-agent-v1"
}
```

* `decision` is `TAKE_ACTION` or `DO_NOTHING`; `probability` is always set (a holdout customer is
  left alone with probability 1).
* `expected_net_value` is the bid: uplift x value of a conversion, minus the expected offer cost
  and the channel cost, times urgency. The attention price is separate, in `clearing`.
* `blocked_by` is the decision's own list of the hard-rule codes that removed options.
* `suppressed_actions` lists every option that did not go out. Codes: the hard rules'
  (`autonomy.decide.rules.ALL_CODES`: `holdout`, `no_consent`, `unsubscribed`, `no_address`,
  `weekly_cap`, `too_soon`, `quiet_hours`, `no_budget`, `offer_needs_approval`, `no_discounts`,
  ...), plus `not_in_audience`, `baseline_group`, `below_attention_price` (the bid does not beat
  the price of one more message), `outbid` (another action won) and `lost_draw` (the bid beats the
  price on average, but doing nothing won this draw while the agent is still testing it).
* `clearing.runner_up` is the best allowed bid that did not win; `clearing_price` is
  `max(attention_price, runner_up.bid)`, what the winner had to beat in a second-price auction;
  `winner_surplus` is `winning_bid - clearing_price`. With no runner-up the clearing price is the
  attention price.

**How it runs.** Each call builds a fresh agent policy (`autonomy.runtime.agent.build_agent_policy`)
from the request's briefs and content, so it is stateless and repeatable; a batch builds one per
customer, so a batch answer equals the single-call answer. The live runtime's policy is never
borrowed: deciding writes to its memory (sends scheduled but not yet out count towards the weekly
cap), so a "what would you do" call would change what the live loop does next. A fresh policy has
not learned anything yet, so it decides on the default priors; it is the same policy the runtime
starts from.

**Standalone use.** A business with its own sending tool (Klaviyo, Braze, HubSpot) posts its
customers' states and its approved briefs and gets back, per customer, what to send and when, or
to leave them alone, with the probability to log next to each send. Logging that probability is
what lets P6 measure the result later.

---

## P4 Creative & Compliance Studio

**Value.** The agent can only choose among messages it has. The Studio writes them and checks
them: a Creative agent writes `ContentItem`s labelled by theme, tone, offer, format and channel,
with `{{slot}}` bodies; a Critic checks every item before it enters the library (brand voice,
claims, links, channel rules, tone), rules first, then an LLM judge from a different model family.
Only approved items are used by P3. LLMs run here, in the outer loop, never per customer.

**Endpoints.** See the engine's own router (`api/v2/engines/studio.py`); its stream fills this
list. The expected shape: post a brief and get a labelled draft library; post an item and get a
pass or fail with the rule or claim that failed; approve or reject an item.

**Standalone use.** A content team can use the Studio alone to draft channel-ready variants and to
check copy against its brand and compliance rules before anything ships, whatever sends it.

---

## P5 Dispatch Gateway

**Value.** One contract for sending, several adapters: email through Amazon SES, WhatsApp through
the Cloud API (approved templates with slots), and a webhook or CSV hand-off to the client's own
tool. Every send carries the `decision_id` that caused it. Inbound webhooks are checked for
signatures, de-duplicated, mapped back to their `decision_id` and written as events. Circuit
breakers are on by default: a goal pauses when complaints, unsubscribes, delivery or spend go
outside their limits.

**Endpoints.** See the engine's own router (`api/v2/engines/dispatch.py`); its stream fills this
list. The expected shape: post a decision with its rendered message and get a `SendReceipt`; post
a provider's webhook and get the events it produced; read the breakers' state.

**Standalone use.** A team with its own decisioning can use the Gateway alone for compliant,
traceable sending: one API for several channels, every event tied back to the decision that caused
it.

---

## P6 Causal Incrementality Ledger

**Value.** It proves what the marketing *caused*, not what happened after it. Customers are split
into agent, simple baseline and no-contact holdout; the Ledger compares them in money, with 95%
intervals, and evaluates other policies on the logged decisions (off-policy evaluation), which is
possible because every decision carries its probability.

**Endpoints** (from `api/v2/engines/measurement.py`, where the full request and response shapes
live):

| Method | Path | What it does |
|---|---|---|
| POST | `/v2/engines/measurement/summary` | Decisions + events in; treated vs holdout conversion, lift with interval and p-value, incremental revenue, costs, net profit and ROI out |
| GET | `/v2/engines/measurement/goals/{goal_id}/summary` | The same summary for one goal of the running programme (needs attached `Services`) |
| POST | `/v2/engines/measurement/ope` | IPS, SNIPS and doubly robust estimates of how another policy would have done on a decision log |
| POST | `/v2/engines/measurement/holdout/assign` | Puts customer ids into agent / baseline / holdout with the same salted hash we use |

**Standalone use.** A business running any sending tool can hold out customers with
`holdout/assign`, send as usual, then post its decisions and events to `summary` to get an honest
incrementality readout, or post a log with probabilities (from P3 or elsewhere) to `ope` to see
whether a new policy would have done better before switching to it.
