# The margin question and the proof in the shop's money

Two honesty fixes left over from [ARCHITECTURE_REVIEW.md](../v3/ARCHITECTURE_REVIEW.md) section 8, after
[CURRENCY.md](../v3/CURRENCY.md) (channel prices) and [PROOF_PROFIT.md](../v3/PROOF_PROFIT.md) (one shop margin).
It changes the plain (v2) product path, not the agent; v3 stays off.

## 1. The proof prices sends at the shop's prices

`api/v2/deps.py` `SEND_COST` was a dollar-sized table (email 0.001, WhatsApp 0.04, SMS 0.0075). It priced the proof
(`StoreReadModels.proof`), the measurement ledger (`GET /measurement/goals/{id}/summary`) and the "spent so far" of
`/v2/today` when the runtime has no ledger (`api/v2/insights.py` `goal_spend`). A rupee shop saw dollar costs.

| Where | Before | Now |
|---|---|---|
| `StoreReadModels.proof` | `SEND_COST` | the runtime's `ShopPrices.costs()` (settings, or the mandate catalogue's dearest provider per channel) |
| Measurement ledger summary | `SEND_COST` | `Services.send_prices()`: the same prices |
| `goal_spend` (log fallback) | `SEND_COST` | `Services.send_prices()`; `None` when a send was on a channel with no price |
| The fake demo's proof | `SEND_COST` | `DEMO_SEND_COST`, an explicit demo fixture, set on the demo `Services.channel_costs` |

`SEND_COST` no longer exists. `DEMO_SEND_COST` is used only by `build_demo_services`. The simulated demo keeps the
Gym's own costs (`Services.channel_costs` from its `Costs`).

**An unknown price is said, not defaulted.** `api/v2/honest_proof.py` looks at the decisions whose message was really
sent (`sent_decisions`) on a paid channel (email, WhatsApp, SMS) with no price. If there is one, the proof and the
ledger summary drop every profit figure (extra profit, profit against the simple rule, ROI, the smallest visible
profit) and their note says: "No price is set for whatsapp, so what those messages cost is not counted and profit is
not shown. Set the price of one message per channel (AUTONOMY_CHANNEL_COSTS, in the shop's currency)." Revenue, the
group sizes and the spend that can be counted stay. Push, web and webhook are free unless priced.

## 2. Onboarding asks the margin, and nothing is assumed until it is answered

The question, in these words (`autonomy/onboarding/limits.py` `MARGIN_QUESTION`):

> Out of every 100 of sales, how much is left after product cost, shipping and payment fees?

Plain explanation (API `explanation`, and the hover help in the limits step): this is the shop's contribution margin;
if it sells 100 and 60 goes on what the products cost, shipping and payment fees, the answer is 40; refused
cash-on-delivery parcels and returns count too; it turns extra sales into extra profit, and until it is answered the
proof shows revenue only.

| Route | |
|---|---|
| `GET /v2/onboarding/margin` | `MarginStatus`: the question, the explanation, `answered`, `margin_pct`, `margin_rate`, `source` (`your answer`, `settings` or `not set`), the bounds and warn limits, `warning`, `note` |
| `POST /v2/onboarding/margin` `{margin_pct}` | answers it; returns `MarginStatus` |
| `POST /v2/onboarding/answers` `{margin_pct}` | the older route, same effect, returns the proposals sized by it |
| `POST /v2/onboarding/accept` | answers 409 until the margin is answered (in the body, earlier, or by `AUTONOMY_MARGIN_RATE`); nothing is written before that, and a margin in the body is kept only once every refusal check has passed |

**Validation.** The answer is a percent, above 0 and below 100 (so the stored rate satisfies 0 < m < 1); anything
else is 422 and nothing is kept. Below 5% or above 80% is kept and warned about ("very low": probably a fraction typed
as a percent, 0.4 for 40%; "very high": the proof would show more profit than there is). The limits are
`MARGIN_WARN_BELOW_PCT` and `MARGIN_WARN_ABOVE_PCT` in `api/v2/onboarding.py`.

**Where it is stored.** The answer is stored as a rate (0-1) on `Services.margin_rate` (what the proof and the
measurement ledger read, live, on the next request) and on the running runtime's `margin_rate` (what its nightly
negative-lift check reads), through `Services.set_margin_rate`, and on the Autopilot guard when the inbox services are
attached (`AutopilotGuard.set_margin_rate`, which also drops the guard's cached judgements, since they are keyed by goal
and level and not by margin). It is the same value as `RuntimeSettings.margin_rate` (`AUTONOMY_MARGIN_RATE`), and a
value from the setting counts as answered (the limits row then says "from your settings (AUTONOMY_MARGIN_RATE)", not
"from your answer"). See "Not done here" for what a restart does.

**The orders' own margins come first for sizing and the offer floor.** When at least half of the shop's orders carry
a margin (`margin_reading` finds one), those margins still size the proposals and set the mandate's margin
(`terms.margin_rate`) and offer floor (margin less the deepest offer), as before this change. The answer is then used
only by the proof, for purchases that carry no margin of their own, and the limits row shows both ("30% gross margin
across N orders that carry one. 70% from your answer counts only the purchases with no margin of their own, in the
proof; sizing and the offer floor use your orders' margin."). A typed answer that is too high can therefore never
loosen the offer floor.

**After a mandate is signed, the margin can only be lowered.** `POST /v2/onboarding/margin`, `/answers` and a later
`accept` with a higher `margin_pct` answer 409 and change nothing once a mandate has been signed here: a higher margin
shows more profit and would loosen the nightly demotion check without a new signature. A lower one only makes the proof
and the checks more cautious, so it is kept. The mandate ledger has no entry kind for a margin change, so none is
written.

**No hidden default.** Until it is answered:

* `accept` is refused (above);
* the limits step shows the margin limit with the question, also when the shop's orders carry margins (the proof
  counts every other purchase at the shop's own margin), and the UI will not sign without an answer;
* the proof shows revenue only. It reuses what f-proof did: `ProofSummary.margin_note` is written when some of the
  margin counted rests on purchases with no margin of their own. That used to say profit was overstated and still show
  it; now `incremental_profit`, `lift_vs_baseline`, `roi` and `detectable` are removed and the note says "Profit needs
  your margin, and it is not set, so this page shows revenue only." plus the question. A shop whose every purchase
  carries its own margin has no such note and keeps its profit (nothing rests on a margin it did not give).

The 40% that onboarding sizes *proposals* with before an answer (`DEFAULT_MARGIN_RATE`, flagged "Safe default" and
now described as only that) is unchanged: it never reaches the proof, the mandate or the agent.

## What moves

* A production shop with no `AUTONOMY_MARGIN_RATE` and orders without margins: the proof page stops showing profit
  (and ROI) until the question is answered. Before it showed profit at the full order value, flagged.
* A shop that sells on a paid channel with no price in its settings: profit is not shown until the price is set.
  Before, WhatsApp was costed at 0.04 whatever the currency.
* `POST /v2/onboarding/accept` needs `margin_pct` the first time (or the setting). Callers that did not send it
  (tests, `scripts/v2_zero_touch_rehearsal.py`) now do.
* `docs/v2/openapi.json` is regenerated (`python -m api.v2.app --openapi`): the two new routes and `MarginStatus`;
  the file also picked up other drift from the generator at this commit.
* `ui/v2/mock/onboarding_limits.json`: the sample shop's margin row now asks the question (and `questions` lists it),
  as the real API does for a shop whose orders carry margins and that has not answered; rewritten through
  `LearnedLimits` (validate, dump). `ui/v2/mock/onboarding_margin.json` is new: the unanswered `MarginStatus`, dumped
  from the model. The mock API keeps an answer like the server (`POST /onboarding/margin` and `/answers`, 422 outside
  0-100, the same warnings), refuses `accept` without one (409), and drops the margin question from the limits once
  answered. `tests/v2/zero_touch/flow.test.mjs` types the margin before signing.

## Evidence

All run in this worktree.

* `pytest tests/v4/onboard`: 29 tests, 29 passed. They cover: the question text, unanswered and answered states, the
  stored rate on the services, 422 for 0, -5, 100, 140 and 1e-9, warnings below 5 and above 80 (none at exactly 5 and
  80), the older `answers` route, the setting counting as answered, `accept` refused with nothing written and then
  accepted, the limits question disappearing once answered, the proof with no margin (revenue kept, profit `None`),
  the profit returning after `set_margin_rate(0.35)` with no restart (30 x 35.0), an unpriced WhatsApp send named
  instead of costed, the same send costed at the INR price (profit 30 x (35.0 - 0.85), spend 30 x 0.85), a free channel
  needing no price, `SEND_COST` gone, the ledger summary in both cases, the demo's named fixture, and `goal_spend`
  returning `None` or the priced total.
* Changed existing tests (assertions kept except where the behaviour changed on purpose):
  `tests/v2/proof_profit/test_proof_profit.py::test_the_api_proof_prices_a_purchase_with_no_margin_at_the_setting`
  (with no setting the profit is now withheld), the `accept` calls in `tests/v2/zero_touch/test_goals_limits.py`,
  `tests/v2/zero_touch/test_flow.py`, `tests/v2/connectors/test_shopify_offer_codes.py` and
  `tests/v3/currency/test_currency.py` (they send `margin_pct: 40`), and the route list in `tests/v2/api/test_api.py`.
* The modules' existing tests, run together with the new ones: `pytest -n 2 tests/v4/onboard tests/v2/api/test_api.py
  tests/v2/engines/test_ledger_doctor_api.py tests/v2/polish/test_api_demo.py
  tests/v2/proof_profit/test_proof_profit.py tests/v2/zero_touch/test_goals_limits.py
  tests/v2/zero_touch/test_flow.py tests/v2/connectors/test_shopify_offer_codes.py tests/v3/currency
  tests/v2/api/test_api_runtime.py tests/v2/polish/test_runtime_gaps.py tests/v2/ui_finish/test_briefing.py
  tests/v2/zero_touch/test_wiring_production.py`: 247 passed. `node --test tests/v2/zero_touch/flow.test.mjs`: 7
  passed.
* `ruff check` and `black` on the changed Python files: clean. `mypy --strict` on `api/v2/deps.py`,
  `api/v2/honest_proof.py`, `api/v2/onboarding.py`, `api/v2/insights.py`, `api/v2/engines/measurement.py` and
  `autonomy/onboarding/limits.py`: no issues.
* Pending CI: the full v2 suite (`pytest tests/v2`) and the UI test suites, which were not run here.

## Review fixes

After review (verdict fix_then_ship), in this order:

| Finding | Fix |
|---|---|
| A required answer replaced the orders' margins for sizing and the mandate floor | the orders' margins win when they exist (`api/v2/onboarding.py` `_sizing_rate`); the answer is shown on the row and used by the proof only |
| After signing, the margin could be changed with no signature | raising is refused (409) once a mandate is signed; lowering is kept and also reaches the Autopilot guard |
| An unpriced channel of goal B withheld goal A's profit | the unpriced check reads only the goal's own decisions (`honest_proof.goal_decisions`) in the proof and the ledger |
| A refused `accept` still stored its margin | the margin is checked first and stored only after every refusal check passed |
| The demo's proof used a fixed margin and its own prices, the ledger the live ones | the proof replaces `costs`' margin and prices with the live margin and `channel_prices()` (the same reading as `Services.send_prices`); a demo with no setting starts at its own `costs.margin_rate` |
| Mock out of step | see "What moves" |
| A withheld ledger still said "net incremental profit is below zero" | `withhold_measurement_profit` drops that note (`PROFIT_LOSS_NOTE`) |
| The setting was labelled "your answer" on the limits row | the row names the setting |

Evidence (run in this worktree after the fixes):

* `pytest --co -q tests/v4/onboard`: 42 tests (29 in `test_onboard_margin.py`, 13 in `test_onboard_review_fixes.py`).
* `pytest -n 2 tests/v4/onboard tests/v2/api/test_api.py tests/v2/engines/test_ledger_doctor_api.py
  tests/v2/polish/test_api_demo.py tests/v2/proof_profit/test_proof_profit.py tests/v2/zero_touch/test_goals_limits.py
  tests/v2/zero_touch/test_flow.py tests/v2/connectors/test_shopify_offer_codes.py tests/v3/currency
  tests/v2/api/test_api_runtime.py tests/v2/polish/test_runtime_gaps.py tests/v2/ui_finish/test_briefing.py
  tests/v2/zero_touch/test_wiring_production.py --junitxml=...`: the JUnit file reports tests="260", failures="0",
  errors="0", skipped="0".
* `node --test tests/v2/zero_touch/flow.test.mjs tests/v2/promise/shell_onboarding.test.mjs`: 16 passed, 0 failed.
* `ruff check`, `black` and `mypy --strict` on the changed Python files: clean. `python -m api.v2.app --openapi`
  changed two route descriptions in `docs/v2/openapi.json`.
* Pending CI: the full v2 suite (`pytest tests/v2`) and the UI test suites.

## Not done here

* The answer lives in memory (the services and the onboarding desk). A restart loses it unless
  `AUTONOMY_MARGIN_RATE` is set; the answer says so. There is no settings store to write `RuntimeSettings` to yet.
* The agent's value model and the Shadow/Approve value check take the margin when the policy and the controller are
  built (at start, from the setting). An answer given in onboarding reaches the proof, the measurement ledger, the
  runtime's nightly negative-lift check and the Autopilot guard at once, and the agent's own start margin at the next
  restart with the setting.
* Lowering the margin after signing is not written to the mandate ledger (it has no entry kind for it); raising it
  needs a new deployment setting and restart, not a route.
* The measurement ledger's `dispatch_costs` and the proof's `spend` still count an unpriced channel as 0 (the note
  says so); only the profit figures are withheld.
* The UI was changed (margin field wording, required, warning toast, no signing without an answer) and checked only
  by the existing Node test; no browser was opened.
