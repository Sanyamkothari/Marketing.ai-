# v4 results: does the new method beat the agent?

27 Oct 2026. Real bench: CI run [40482951813](https://github.com/Sanyamkothari/Marketing.ai-/actions/runs/40482951813).
Gym: CI run [40482951672](https://github.com/Sanyamkothari/Marketing.ai-/actions/runs/40482951672). Both ran on commit
e0be04b7, before the WhatsApp merge. All jobs succeeded.

The bench figure is doubly-robust profit per customer on the TEST half, with its 95% interval. "Best simple" is the
best of send-to-none, send-to-all per arm and the segment rule, chosen on TRAIN.

## Short answer

1. **The new uplift method does not beat the agent.** It ties it within noise on all four datasets.
   - It beats the best simple rule only on MegaFon, where the agent beats it too.
   - Its safety gate had a bug: on Hillstrom it fell back to a weaker rule. That is fixed in this commit.
   - With the fix, its job is a guarantee, not a gain: it is never worse than the best simple rule.
2. **Exploration costs money on a one-shot test.** The agent with no exploration (`agent_greedy`) is at least as
   good as the plain agent on every dataset. On Hillstrom it matches the best simple rule.
3. **The click model adds nothing measurable.** `agent_uplift_only` gives the same result as the agent.
4. **The trait leave-alone inside "who" is what stops nearly all messages.** Turning it off (`agent_who_notrait`)
   restores normal contact. Removing the other suspect (`agent_who_noprune`) does not.
5. **Calibration does not help.**
   - It changes nothing on the bench, because a one-shot bench gives it no holdout to learn from.
   - In the Gym it loses 1-3% in 8 of 10 scenarios and drops a stage in 3. It stays off.
6. **The data has little more to give.** The stronger learned baselines land close to the agent too.

## Real bench

| Dataset | Best simple | Agent | Uplift, gated | Uplift, ungated | agent_greedy | agent_uplift_only |
|---|---|---|---|---|---|---|
| Hillstrom | Mens 0.6766 [0.518, 0.835] | 0.6513, 98.3% | 0.4685 (Womens, **bug**) | 0.6766, 100% | **0.6766**, 100% | 0.6505 |
| X5 (RUB) | segment 72.33 [71.21, 73.45] | 72.23, 75.8% | 72.10 (send all SMS) | 72.09, 65.0% | 72.25 | 72.23 |
| Criteo | segment f8 0.0506 [0.0443, 0.0569] | 0.0524, 12.0% | 0.0506 (segment f8) | 0.0526, 12.8% | 0.0527 | 0.0524 |
| MegaFon | segment X_5 3.7331 [3.661, 3.805] | 4.1958, 46.1% | **4.2239** (model), 47.6% | 4.2239 | 4.2093 | 4.1958 |

### Agent against the best simple rule

These are paired differences:

| Dataset | Agent minus best simple |
|---|---|
| Hillstrom | -0.0253 [-0.048, -0.003] (a small loss) |
| X5 | -0.10 [-0.55, +0.35] |
| Criteo | +0.0018 [-0.001, +0.005] |
| MegaFon | **+0.46 [+0.39, +0.53]** |

### Where the uplift method sits

- **Gated, against the best simple rule:**
  - On MegaFon it is +0.49 [+0.42, +0.56].
  - On MegaFon it is +0.028 [-0.002, +0.058] against the DR-learner, a tie.
  - The point difference against the agent on MegaFon is +0.028. No paired interval for it was printed.
- **Ungated, against the gated policy:** on Criteo and X5 the two tie.

**The gate bug.** The gate picked the rule it compares against, and falls back to, on 70% of TRAIN. On Hillstrom
that slice named "send all Womens" (0.4685 on TEST), while all of TRAIN names Mens. It also missed the model by
0.0007 at the lower bound. Since this commit the fallback is the rule chosen on all of TRAIN, as the bench's
`choose_simple` chooses it. The test `test_the_rule_it_falls_back_to_is_the_best_simple_rule_on_all_of_train`
fails on the old code and passes on the new.

After the fix (CI run 40496337176, commit 9db6ad13) the gated policy falls back to the rule all of TRAIN names:
- Hillstrom: the segment rule (history_segment), 0.6090 [0.4657, 0.7522], minus best simple -0.0676 [-0.2207, +0.0854].
  That is no longer significantly worse, but it is still not Mens: this module's rule choice on TRAIN and the
  bench's `choose_simple` disagree when the rules are within noise of each other.
- X5: still send-to-all SMS, 72.10.
- MegaFon: the model again, 4.2239.

### Learned baselines

These are chosen on TEST, so they flatter themselves:

| Dataset | Best learned baseline | Agent minus it |
|---|---|---|
| Hillstrom | depth-2 tree 0.7272 [0.564, 0.890] | -0.076 [-0.181, +0.029] |
| X5 | tree 72.40 | -0.17 [-0.77, +0.44] |
| Criteo | tree 0.0512 | +0.0012 [-0.002, +0.004] |
| MegaFon | DR-learner 4.1962 | -0.0004 [-0.033, +0.032] |

### Which part of "who" collapses the bench

DR profit per customer, with the share contacted:

| Dataset | agent | agent_who | agent_who_notrait | agent_who_noprune |
|---|---|---|---|---|
| Hillstrom | 0.6513 (98.3%) | 0.2847 (1.9%) | **0.6548 (98.3%)** | 0.2847 (1.9%) |
| X5 | 72.23 (75.8%) | 70.76 (1.5%) | **72.29 (76.4%)** | 70.76 (1.5%) |
| Criteo, MegaFon | unchanged | unchanged | unchanged | unchanged |

The cause is `WhoConfig.trait_harm`: "people like them are put off by messages". On the bench it leaves almost
everyone alone. If "who" ever comes back, it comes back with `trait_harm` off.

### Expected against measured value per contact

| Dataset | Agent | Uplift policy |
|---|---|---|
| Hillstrom | 1.46× | 0.94× |
| X5 | 1.33× | 1.46× |
| Criteo | 0.48× | 0.58× |
| MegaFon | 0.84× | 0.93× |

- The agent over-values on Hillstrom and X5 and *under*-values on Criteo and MegaFon.
- A calibration that can only shrink cannot fix the second case.

## Gym: calibration

True value, mean of 3 seeds:

| Scenario | agent | agent_calibrated | Difference | Stage (agent / calibrated) |
|---|---:|---:|---:|---|
| baseline_shop | 32,460 | 31,613 | -847 | 3 / 1 |
| sleeping_dogs_15 | 24,483 | 23,726 | -757 | 2 / 1 |
| sleeping_dogs_hidden | 23,956 | 23,064 | -892 | 1 / 1 |
| whatsapp_fatigue | 39,707 | 39,018 | -688 | 3 / 3 |
| drift_midway | 31,739 | 31,287 | -451 | 1 / 1 |
| small_business | 31,753 | 31,204 | -549 | 1 / 1 |
| discount_cannibalisation | 26,818 | 26,581 | -237 | 3 / 1 |
| send_time_matters | 28,062 | 27,140 | -923 | 3 / 3 |
| null_effect, complaint_storm | same | same | 0 | same |

- Calibration shrank the agent's expected value by 14-31%.
- It sent about 1% fewer messages and cut unsubscribes slightly.
- It lost value everywhere money is made.

The agent's over-valuation is a fault in what it *reports*, not in what it *chooses*. The fix belongs in the proof and
briefing numbers (the `calibration` status the API now returns), not in the decision.

## What this means

- **Keep the plain agent as the decision-maker.** Calibration and v3 stay off.
- **Turn the uplift policy into the safety net.** Before a campaign, compare the agent with the best simple rule on
  held-out data, and run the rule when the agent is not clearly better.
- **Reduce exploration for one-off campaigns.** Exploration only pays for itself when the same customers are
  messaged again.
- **Any further real gain needs repeated campaigns and real shops.** The planned semi-synthetic multi-wave benchmark
  and a pilot (ROOT_CAUSE.md T5 and T6) are the next tests, not more one-shot tuning.
- **WhatsApp purchase-only valuation is reverted to an option.** The merge after these runs valued WhatsApp from
  purchases only, never clicks. Gym run 40496337323 (commit 9db6ad13) measured the cost:
  - `whatsapp_fatigue`: the agent fell from 39,707 to 24,112 (-39%) and dropped below Stage 1.
  - `baseline_shop`: it fell from 32,460 to 27,272 (-16%).
  - A 200-customer smoke on `whatsapp_fatigue` gave 1,284 with the switch on and 2,425 with it off.

  The agent already finds a channel that logs no clicks from its own sends (200 deliveries without a click). So the
  default is back to that, and `build_agent_policy(never_click_channels=UNTRACKED_WHATSAPP)` keeps the
  purchase-only mode for shops whose WhatsApp links are not click-tracked.
