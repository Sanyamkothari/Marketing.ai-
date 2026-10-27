# Proof that means profit

Foundation fix 3 of [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) (section 4 row 15: A10, E7, F11; section 8
"Fix first" item 3). It changes the plain (v2) agent and the production proof path, not a v3 feature; v3 stays off.

Three things were wrong, and one thing was missing next to every proof number.

## 1. Refunds and voids are netted

`autonomy/proof/rewards.py` used to say "Refunds are not subtracted yet". Now a REFUND reverses a purchase of the
same customer:

- **Which purchase.** The one whose `attrs["order_id"]` the refund names. Shopify refunds and voids both do (the
  connector writes a REFUND per refund and one `void` REFUND for the unpaid total of a voided order). A refund that
  names no order reverses the customer's latest earlier purchase with exactly the refund's amount left, else the
  latest earlier purchase that still has money left. Every purchase of the customer is a candidate, including one no
  decision was credited with (before the first decision, outside every window, or after the last-touch cutoff): its
  refund then finds it and changes nothing, instead of reversing an in-window purchase that was earned (which would
  lower the agent group only, since the holdout has fewer credited purchases to absorb such refunds). An order sent
  as several PURCHASE lines with one `order_id` is reversed across all its lines in proportion. A refund naming an
  order we do not hold (bought before the history) is ignored.
- **Sign and order.** The refund's amount is the size of its `value` whatever its sign (some sources write refunds
  negative, as the profile and the 90-day label already assume). A purchase and a refund at the same instant are
  taken purchase first.
- **How much.** The refunded share of the order's value comes off the revenue, and the same share of its margin
  (recorded or assumed) comes off the margin. Refunds can never take more than the order was worth. An order refunded
  in full is no longer a conversion.
- **When.** Only for decisions whose outcome window holds the refund. A refund after the window has closed is not
  netted: the window was judged on what was known by its end. This is the rule the stream was asked for, and it is
  also what keeps a closed reward from changing after the fact.
- **Cash on delivery and return to origin.** A COD parcel the customer refuses arrives as a voided or cancelled order,
  so it is a REFUND naming the order and is netted like any other. A REFUND with `attrs["rto"]` true (or
  `return_to_origin`), or `attrs["reason"]` of `rto` or `return_to_origin`, reverses the whole order whatever amount it
  carries. These `rto` / `reason` attrs are a convention for a loader or courier feed to set: no connector sets them
  yet (the Shopify REFUND attrs are `order_id`, `refund_id` and `currency`), so today an RTO is netted only through the
  void or refund the shop records. The lost outbound and return shipping of an RTO parcel is **not** charged as a
  separate cost: it belongs in the shop's contribution margin (below).
- An offer's cost is still charged when its order is later refunded (the discount was given).

What moves: any shop with refund events in its store now shows less profit, in the proof, in the nightly negative-lift
breaker and in the Shadow-to-Approve value check, by the margin of what was refunded inside the window. Shops with no
refund events see no change.

## 2. One shop margin

Three different assumptions were in use for a purchase that carries no margin of its own: 0.4 in the decision value
model (`ValueModel.default_margin_rate`), 0.3 in the early read (`EarlySignalConfig.margin_rate`) and 1.0 (revenue) in
the proof rewards (`Costs.margin_rate`) and in the 90-day value label (`slow_models._MarginRate`).

There is now one setting, `RuntimeSettings.margin_rate` (`AUTONOMY_MARGIN_RATE`, a fraction in (0, 1], for example
`0.35`): the shop's **contribution margin after shipping, gateway fees and returns to origin**. When it is set:

| Where | What it does |
|---|---|
| Decision value model | `build_agent_policy(margin_rate=...)` builds the bundle with it as the default margin rate, ahead of the built-in 0.4 and of a pooled prior's `value/margin_rate`. Recorded margins still move the learned rate as before |
| 90-day value label | With `who` on, the label's fallback rate (until some purchase reports a margin) is the setting, not 1.0 (`SlowModelConfig.margin_rate`) |
| Early read | `EarlySignalConfig.margin_rate` is the setting, not 0.3 (`margin_share`); nothing in the runtime builds that config yet, so the caller passes the setting |
| Proof rewards | `Costs.margin_rate`: the nightly negative-lift breaker (`AutonomyRuntime.margin_rate`), the Shadow value check and the Approve real-lift check (`PromotionController`), the API proof (`StoreReadModels`, `Services.margin_rate`) and the ledger summary |

A margin recorded on the purchase always wins over the setting. `Reward.margin_assumed` records how much of a reward's
margin came from the setting.

**When it is unset**, nothing changes numerically (0.4, 0.3 and "full value" stay as they were), but the proof no
longer hides it: `ProofSummary.margin_note` (its own field, not repeated in `notes`) says "No shop margin is set", what share of the
margin counted rests on it, and that profit is overstated; the proof page shows it under the headline and the Analyst's
briefing says so in words. No note is written when every purchase carries its own margin. With the setting on, the note
says which rate was used and for what share of the margin. The proof's profit figures are margin less cost for every
objective (`autonomy/proof/estimate.py`, `profit[...] += r.margin - r.cost`), so the note is written for a revenue or
conversion goal too: their proof page still leads with extra profit.

`build_agent_policy(margin_rate=...)` refuses a `who` given as a ready `WhoBook` whose 90-day label uses another
margin (as it refuses such a `bundle`), so the setting cannot be silently ignored on that path.

What moves when a shop sets it: the proof's profit falls from "full value" to value x rate for purchases with no
margin; the agent's start margin goes from 0.4 to the shop's. The demo's own read model
(`_DemoReadModels.proof`) is simulated data with margins on its events and is left alone.

`autonomy/onboarding/limits.py` and `autonomy/agents/analyst.py` keep their separate 0.4 for onboarding suggestions
(`DEFAULT_MARGIN_RATE`, used only when a segment has no margin and before any goal exists); they are not part of the
proof.

## 3. The minimum detectable effect, next to every number

`autonomy/proof/power.py`. The smallest true difference a comparison can reliably see (95% confidence, 80% power) is
`(1.95996 + 0.84162) x SE = 2.8016 x SE`.

- **From the data** (`ProofSummary.detectable`): `SE` is Welch's standard error of the per-customer profit difference,
  the one the proof's range uses. `detectable.profit` and `.revenue` are in the units of the incremental figures (times
  the agent customers); `.lift_vs_baseline` is per customer; `.relative` is the smallest lift as a share of the
  holdout's profit per customer; `.sentence` says it in words (its own field, not repeated in `notes`): "With 60 agent
  and 40 holdout customers measured over 7 days, this comparison can only see profit per customer more than X% above
  the holdout's...". The proof page shows the sentence under the headline, and the revenue and simple-rule MDEs in the
  descriptions of "Extra revenue" and "Extra profit per customer"; the shareable text carries the sentence. The
  briefing carries the same in `detectable_profit`, `detectable_lift`, `detectable_revenue` and
  `detectable_lift_per_customer`, and the ledger summary has `minimum_detectable_lift_pct` (conversion, at the
  holdout's own rate) and `minimum_detectable_profit`.
- **Before the data** (`minimum_detectable_lift`, `smallest_visible_lift`): for "did the customer buy", relative lift
  `2.8016 x sqrt(p(1-p)(1/n_agent + 1/n_holdout)) / p`, for the runtime's 10% holdout and 80% agent group. Run with
  `python -c` on this commit (`smallest_visible_lift(n, p)`):

| Customers | 5% buy rate (one 2-4 week window) | 20% buy rate (pooled about 6 months) |
|---|---:|---:|
| 8,000 | 45.8% | 21.0% |
| 50,000 | 18.3% | 8.4% |
| 100,000 | 13.0% | 5.9% |
| 300,000 | 7.5% | 3.4% |

These are the review's section 5c figures (46 / 21, 18 / 8, 13 / 6, 7 / 3) to the decimal; the 300,000 row is 7.5%
and 3.4% before rounding. Customers needed for a 10% lift or a 5% lift are in the review.

**Pooling.** The proof already sums every closed window of a customer into one figure (last-touch rewards,
`proof_rewards`), so its standard error, and so the MDE, is for the whole period measured and falls as months are
added. The sentence says so. No new pooling code was needed; the pooled column above is the planning view of the same
effect (a longer period means a higher buy rate per customer).

No MDE is claimed when a group has fewer than `MIN_GROUP` (30) measured customers, the same rule as the incremental
figures, nor when profit per customer does not vary at all in either group (nobody bought yet): a standard error of
0 would claim that any gain could be seen, so a note says there is nothing to say yet instead.

## What was run

- `pytest tests/v2/proof_profit`: 26 passed (refund inside and after the window, partial refund, void, COD/RTO, margin
  single source, MDE for 8k and 300k).
- After review (refund matching, multi-line orders, negative refunds, same-instant order, zero-spread MDE, the shown
  MDEs): `pytest -o addopts="" -n 2` on `tests/v2/proof_profit` (42 tests, the `slow` 300,000-customer ledger run
  included, 16 s), `tests/v2/decide/test_proof.py`, `tests/v2/promise/test_proof.py`,
  `tests/v2/ui_finish/test_briefing.py`, `tests/v2/ui_finish/test_plan_proof.py`,
  `tests/v2/zero_touch/test_wiring_proof.py`, `tests/v2/ui/test_live_proof_operator_js.py`,
  `tests/v3/who/test_slow_models.py`, `tests/v2/engines/test_ledger_doctor_api.py`,
  `tests/v2/agents/test_analyst_conversation.py` and `tests/v2/realfix/test_finish_wiring.py`: 192 passed.
  `node --test tests/v2/promise/proof.test.mjs`: 17 passed. The ledger's shown MDE
  (`minimum_detectable_lift_pct`) is 45.8% / 21.0% for 8,000 customers and 7.5% / 3.4% for 300,000, as in the table.
- Existing tests that cover the touched modules, 255 passed in all (281 with the new file): `tests/v2/decide/test_proof.py`,
  `tests/v2/ui_finish/test_briefing.py`, `tests/v2/ui_finish/test_plan_proof.py`,
  `tests/v2/zero_touch/test_wiring_proof.py`, `tests/v2/engines/test_ledger_doctor_api.py`,
  `tests/v2/agents/test_analyst_conversation.py`, `tests/v3/early`, `tests/v3/who/test_slow_models.py`,
  `tests/v3/preset`, `tests/v2/failure/test_failure_isolation.py`, `tests/v2/failure/test_runtime_wiring.py`,
  `tests/v2/zero_touch/test_inbox_autopilot.py` and `tests/v2/wire_agent/test_hero_demo.py`.
- One Gym smoke run, `python -m autonomy.gym.run --scenario baseline_shop --policy agent --seeds 1 --customers 200`,
  ran to the end in 21 s (it did not crash; its staged-bar verdict at 200 customers is not a result, and it was not
  compared with a run on the base commit).
- Pending CI: the full v2 and v3 suites and the Gym, which this stream did not run. Default behaviour is unchanged
  unless a shop has refund events or sets the margin, so the Gym (no refunds, no setting) should be unchanged:
  `python -m autonomy.gym.run ...` with the same seeds as the last CI run.
- `docs/v2/openapi.json` is not regenerated here: it was already out of date against the code at this base commit
  (the regeneration also pulls in other streams' contract changes), and the route test compares operations, not
  schemas. Regenerate on integration with `python -m api.v2.app --openapi`; the new fields are
  `ProofSummary.detectable`, `.margin_rate`, `.margin_note`, `Reward.margin_assumed`, `CausalMetrics.minimum_detectable_lift_pct`,
  `FinancialImpact.minimum_detectable_profit` and `.margin_note`.

## Not done

- The margin is a deployment setting (`AUTONOMY_MARGIN_RATE`), not yet a question in onboarding. The onboarding
  already asks `margin_pct` for its own suggestions (`api/v2/onboarding.py`); wiring that answer into the runtime
  setting is the follow-up.
- A separate per-order RTO cost (outbound plus return shipping on a refused parcel) is not modelled; it is inside the
  margin rate.
- A partial refund that names no order, and matches no purchase's remaining amount exactly, is matched to the
  customer's latest earlier purchase, which can be the wrong one. Only sources that do not name orders are affected
  (CSV and Online Retail cancellations, `loaders.py`'s synthetic refunds).
