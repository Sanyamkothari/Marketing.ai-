# Calibration and the evidence gate on Autopilot

Stream `v4/m-calibrate`. It builds the two things plan-v3 promised and never shipped: the "1.5x calibration
gate" and ARCHITECTURE_REVIEW section 8's "evidence-gated Autopilot".

## Why

`docs/v3/DIAGNOSE_RESULTS.md` H5: the agent expects more per message than the messages earn, in every setting
measured (figures copied from that document, not re-run here):

| Setting | Expected | Realised | Ratio |
|---|---:|---:|---:|
| Gym baseline_shop | 0.906 | 0.661 | 1.37x |
| Gym whatsapp_fatigue | 1.306 | 1.147 | 1.14x |
| Gym discount_cannibalisation | 0.713 | 0.468 | 1.52x |
| Gym sleeping_dogs_15 | 0.731 | 0.507 | 1.44x |
| Hillstrom | 0.511 | 0.351 | 1.46x |
| X5 | 2.885 | 2.168 | 1.33x |

An auction that over-values every bid sends messages that do not pay, and an Autopilot that is trusted on
those forecasts spends real money on them. Two pieces: a correction (the factor) and a check (the gate).

## 1. The factor

`autonomy/decide/calibration.py`. Over one window:

* **measured** per action = the holdout's incremental profit (`autonomy.proof`: agent group against the people
  we never contact, customers as the unit, Welch 95% range) divided by the number of actions the agent took
  (contact decisions whose message was actually sent: Shadow decisions, rejected or expired batches and failed
  sends are not actions);
* **expected** per action = the mean `expected_net` of those same chosen actions, before the factor.

`factor = measured / expected`, then shrunk and clipped:

* **Prior: Normal(1, 0.25^2).** With no data the agent is taken to be right. A factor of 0.5 (2x over-valuing)
  is two standard deviations from the prior, so it takes real evidence to get there.
* The measured ratio has its own standard error, from the proof's 95% range (`se = (high - low) / (2 x 1.96) /
  expected`). The posterior mean is the precision-weighted mean of prior and measurement, so a small holdout
  (wide range) leaves the factor near 1 and a large one lets the measurement win.
* **Clipped to [0.25, 1.0].** It never inflates a bid, and never takes more than three quarters off one.
* **Exactly 1.0** when it cannot be measured: fewer than 30 customers in the agent group or the holdout (the
  proof's own minimum, `MIN_GROUP`), no actions yet, or an expected net that is not positive.

### How it reaches the auction

`build_agent_policy(calibrate=True)` (default `False`). Every bid's **value part** is shrunk by the factor in
`AutonomousPolicy.decide`, before the attention price is taken off and the send/no-send draw:
`expected_net' = expected_net - (1 - factor) x urgency x uplift x value_if_converted` (`scaled_net`). The channel
and offer costs stay whole, and a bid whose value part is not positive is left as it is, so a bid's net only
ever goes down: a loss-making bid is never moved toward zero. (Shrinking the whole net would have done that: at
0.25 a -0.8 bid became -0.2.) The factor is fitted on net values and applied to the value alone, so for a bid
with costs it takes off a little more than the measured ratio; it errs toward sending less. A journey's
first message, and each later step when it runs, are scaled the same way. A bid's `uncertainty` is not scaled. With the switch off, `value_scale_fn`
is `None`, nothing in the module is called, and every existing number is unchanged
(`tests/v4/calibrate/test_calibration.py::test_default_build_is_unchanged_and_calibration_is_opt_in`).

### What feeds it

The agent is handed events and its own decisions, not the holdout's rewards, so the factor is fitted from the
events (`Calibration.record` on every agent and holdout decision, `Calibration.purchase` on every purchase
the agent sees, `Calibration.refit` at the end of each nightly run, at most once a week):

* the period is the one `autonomy.proof`'s last touch measures: the cutoff is `horizon_days` before the refit,
  and only purchases **before the cutoff** count, for agent and holdout customers alike;
* a customer's profit = the margin of those purchases since the agent started (the shop's rate when a purchase
  has none) less the offer cost of the offers they redeemed, less the channel cost of messages sent at or before
  the cutoff; the actions and their expected nets are those same sends. Purchases after the cutoff, which the
  still-open later sends helped cause, wait for the next refit (counting them against only the closed sends
  would inflate the measured value per action by up to the ratio of the two periods, about the size of the H5
  gap, and leave the factor near 1; `test_the_event_feed_ignores_purchases_from_sends_that_are_still_open`);
* the group is that of the customer's first decision; the factor in force when a decision was made is taken back
  out of its logged expected net (`unscaled_net`), so a calibrated agent does not compare its already-shrunk
  number with the holdout again.

A caller that already has closed rewards for every group (the runtime, say) can use `Calibration.observe`
instead. Not wired into the production runtime here: `calibrate` is a switch on `build_agent_policy` only.

Known limit: the channel cost used is the chosen bid's `channel_cost`; offer cost enters only when a purchase
redeems an offer the brief lists. Purchases whose events carry no `offer_id` are not charged an offer cost.

### Gym and bench

`agent_calibrated` is an ablation variant (`autonomy/runtime/ablation.py`, family `calibration`), so it is in the
Gym's `POLICIES` and the bench's `--policy` choices. It is added to the policy axes of `gym.yml`, `realbench.yml`
and `diagnose.yml`; `realbench.yml`'s summary job also merges its four datasets into
`realbench-agent_calibrated.json`.

* **Gym** (needs at least ~300 customers so the 10% holdout reaches the proof's minimum of 30; the local smoke
  below is smaller, so the factor stays at 1.0 there).
* **Bench**: the bench decides each TEST customer once and never hands the agent closed outcomes, so the
  factor stays 1.0 and `agent_calibrated` is expected to equal `agent` there. It is on the axis so that
  statement is checked, not assumed.

## 2. The calibration gate

`calibration_status(expected, measured, ci) -> ok | over_2x | over_1_5x | not_enough_data`, all per action,
`ci` the 95% range of `measured`. With `t = expected / 2`:

| Condition | Verdict |
|---|---|
| any input missing or not finite, or `expected <= 0` | `not_enough_data` |
| whole range at or above `t` | `over_1_5x` if `expected / measured > 1.5`, else `ok` |
| point below `t` and range no wider than `2 x expected`, or whole range below `t` | `over_2x` |
| anything else (the range cannot rule 2x over in or out) | `not_enough_data` |

On the H5 ratios with a tight range, anything under 1.5x reads `ok` (1.14x, 1.37x in the test) and 1.52x reads
`over_1_5x` (`tests/v4/calibrate/test_gate.py::test_calibration_status`).

**Where it shows.** One new field, `ProofSummary.calibration` (`null` when not worked out), filled by
`StoreReadModels.proof` in `api/v2/deps.py` from the closed agent-group contact decisions whose message was sent
(`autonomy.proof.rewards.sent_decisions` on the customers' events) and the summary's incremental profit
(`proof_calibration`); `AutopilotGuard.evidence` does the same. So `/v2/proof` and `/v2/goals/{id}` carry it.
`ui/v2/screens/proof.js` shows it as one plain sentence under the headline (`calibrationSentence`; nothing for
`null` or `not_enough_data`) and in the shareable summary.

## 3. Evidence-gated Autopilot

`autonomy/runtime/evidence_gate.py`. Autopilot may be switched on only when:

* (a) the holdout's measured incremental profit has a 95% lower bound above zero, and the goal has been acting
  at least `MIN_WINDOW_DAYS` = 14 (the same number as `PromotionRule.min_days`; a test pins them together);
* (b) `calibration` is `ok` or `over_1_5x`. `over_2x` and `not_enough_data` refuse.

Otherwise the request is refused with a plain reason ("... The goal stays on Approve each batch.").

### Where the mode is set, and what changed

| Place | Change |
|---|---|
| `Services.approve` (`api/v2/deps.py`), behind `POST /v2/goals/{id}/approve` | Asking for Autopilot on a goal that is not already on it runs the gate; refused is a 409 with the reason and the goal keeps its level (a draft stays a draft). The window is the days since the plan was last approved (`Brief.approved_at`). Allowed switches go to `Services.audit_log` (and the ledger sink) only after the goal and plan are stored, so a failed save leaves no entry. |
| `POST /v2/onboarding/accept` (`api/v2/onboarding.py`) | Same gate through `Services.approve`; a refusal starts the plan in Approve each batch and puts the reason in `notes`. |
| `AutopilotGuard` (`autonomy/runtime/promotion.py`): the inbox "Turn on Autopilot" click and the mandate's automatic promotion | New `require_evidence` (default `False`, so the class is unchanged for direct users). With it, `promote` raises with the reason, `offers` stops offering, and `run` does not auto-promote. `zero_touch_for_runtime` (`autonomy/inbox/install.py`) and the local inbox guard in `api/v2/inbox.py` turn it on. A Shadow goal can therefore no longer be promoted on its own forecast alone. |

### The email exception (DECISIONS V-1)

V-1 allows email autopilot from day one. It survives only as an explicit choice: `start_without_proof: true` on
the approve and onboarding requests ("start on autopilot without proof"). It is accepted only when

* the plan sends by **email alone** (WhatsApp or mixed plans are never exempt: a burned number cannot be undone),
* the plan is being approved for the **first time**, and
* the owner sent the confirmation.

The confirmation is written as an `AutonomyAuditEntry` (`reason: start_without_proof_confirmed`) to
`Services.audit_log`, and, when the inbox is wired (`local_inbox_services`, `production_inbox_services`), to the
hash-chained autonomy ledger through `AutopilotGuard.record`, so it is tied to the mandate and its ledger head
like every other level change. A switch the gate let through on proof is logged the same way
(`evidence_gate_passed`), with the incremental profit range as evidence.

## Tests

`tests/v4/calibrate/`:

* the factor never exceeds 1.0 or goes below 0.25, over a grid of measured values and ranges;
* it is exactly 1.0 with no holdout data (no data, under 30 customers a group, nothing expected);
* it recovers a planted over-valuation (expected 1.0 per action, holdout shows 0.5: factor within 0.44 to 0.58,
  a little above 0.5 because of the prior), by closed rewards and by events; a small holdout is shrunk harder
  toward 1; an accurate agent is left alone; the factor in force is taken out of the logged expectation; sends
  that go on after the cutoff do not dilute the recovered ratio;
* only the value part is shrunk: a loss-making bid is never moved up, costs stay whole, no bid's net rises;
* the proof's status counts only sent decisions (half the ACTs unsent, exact forecasts: still `ok`);
* a failed save after an allowed Autopilot switch leaves nothing in the audit log or the ledger sink;
* the gate refuses and permits on planted evidence, in `autopilot_gate`, `Services.approve` and
  `AutopilotGuard`; the email exception needs email only, a first approval and the confirmation, and is audited;
* the default `build_agent_policy` is unchanged (`calibrate` defaults to `False`, no scale hook installed; on fixed
  states the default build and a calibrated build at factor 1.0 return identical decisions), and in
  a 200-customer Gym world `agent_calibrated` scores exactly what `agent` does (slow test).

Existing tests changed because they asserted the old ungated behaviour:

* `tests/v2/api/test_api.py::test_approve_then_pause_and_resume` asked a fresh goal for Autopilot and expected it;
  it now expects the 409 and approves at Approve.
* `tests/v3/diagnose/test_variants.py`: the plain agent's switch list gains `calibrate: False`, and the variant
  table gains `agent_calibrated`.
* `tests/v3/preset/test_wiring.py::test_the_bench_workflow_has_a_policy_axis` pins the axis text
  `policy: [agent, agent_v3, agent_calibrated]`.
* `tests/v2/zero_touch/test_goals_limits.py` gained two tests (onboarding with Autopilot requested); none changed.
* `tests/v2/promise/proof.test.mjs` gained one test (the forecast-check sentence); none changed.

## Results

Run here (small, to check the mechanics; not a measurement of the agent):

* `python -m pytest tests/v4/calibrate -q`: all pass (including the 200-customer Gym test below, which asserts
  `agent_calibrated` and `agent` score the same true value and actions while the holdout is too small).
* The smoke below was run on commit bf82df76, before the event feed was cut to the proof's period, the actions to
  sent messages, and the shrink to the value part; it was not re-run. The first sentence still holds by the slow
  test above (the factor stays 1.0); the lowered-minimum numbers would differ now.
* One Gym smoke, `baseline_shop`, 200 customers, 40 days, seed 0: the calibrated agent took 1,537 actions and scored
  the same true value (1,043.5) as `agent`, because the 10% holdout (9 customers) is under the proof's minimum
  so the factor stayed at 1.0. With the minimum lowered to 5 only to see the fit work, the agent's mean expected
  net per closed action was 0.891 (H5 reports 0.906 for baseline_shop) and the measured value per action came out
  1.28 (95% range 0.04 to 2.53) from 9 holdout customers: far too noisy to say anything, and above the
  expectation, so the factor correctly stayed at 1.0 (it never inflates).

Pending CI (cannot be run here):

* Does shrinking help? `agent_calibrated` against `agent` on the Gym at a size where the holdout is measurable:
  `python -m autonomy.gym.run --scenario discount_cannibalisation --policy agent_calibrated --seeds 3 --customers 3000`
  and the same with `--policy agent`, read per seed (workflow `gym.yml`, commit message containing `[gym]`).
* Bench: `python -m scripts.v2_realbench.run --dataset hillstrom --policy agent_calibrated` against `--policy
  agent` (workflow `realbench.yml`, `[real]`). Expected identical, see above.
* The shipped factor on the H5 settings, and how far it moves true value per action, from the DIAG block of the
  Gym run (`mean_expected_net_chosen` against `realised_incremental_per_action`).
