# Diagnosis results: what is really wrong

27 Oct 2026. The ablation runs set up in [DIAGNOSE.md](DIAGNOSE.md) ran in CI as run
[40464041864](https://github.com/Sanyamkothari/Marketing.ai-/actions/runs/40464041864) (commit 6c9704f3, which
includes the f-proof and f-codes fixes). They cover 4 Gym scenarios × 20 variants and the Hillstrom and X5 bench × 20
variants. **All 120 jobs are read here. None failed.**

The question was: is it too many rules, strict AI prompts, the ML, or the wrong method?

## Short answer

1. **It is not too many rules.** Every guard relaxed one at a time either changes nothing or loses money, and
   removing the weekly budget or every guard loses 12% to 88% of the value. The one apparent gain on Hillstrom is just
   "send everyone the men's email", which already is the best simple rule.
2. **It is not prompts.** No LLM call sits on the decision path in the Gym, on the bench or in production
   ([ROOT_CAUSE.md](ROOT_CAUSE.md), audit 1). This run changes nothing about that.
3. **v3 lost because of two parts, and a different one in each test bed:**
   - In the Gym it is **journeys**. On its own it costs 25-34%. Removing it from v3 gives back 89-98% of v3's loss in
     three of four scenarios, and about 70% in `whatsapp_fatigue`.
   - On the real bench it is **who**. On its own it stops contact with 98% of customers. Removing it from v3 gives
     back everything on both datasets.
   - Moments and offers do nothing at all: their decisions are identical to the plain agent's.
4. **The plain agent itself is honest but limited.** It thinks each message is worth 1.14-1.52× what it really earns.
   On real data it ties the best simple rule at best (X5) or loses to it slightly (Hillstrom). This is the method
   problem ROOT_CAUSE.md audit 2 predicted: a click-driven per-message bidder on data with little per-customer
   difference to exploit.

## The yardsticks

**Gym** (true value per 3,000 customers, mean of 3 seeds):

| Scenario | Best possible (oracle) | Best fixed rule | Plain agent | Agent vs rule |
|---|---:|---:|---:|---:|
| baseline_shop | 56,846 | 30,338 | 32,460 (Stage 3) | +2,122 |
| whatsapp_fatigue | 55,686 | 28,558 | 39,707 (Stage 3) | +11,148 |
| discount_cannibalisation | 36,917 | 25,885 | 26,818 (Stage 3) | +933 |
| sleeping_dogs_15 | 47,868 | 22,886 | 24,483 (Stage 2) | +1,598 |

**Bench** (doubly-robust profit per customer on the TEST half):

| Dataset | Best simple rule | Plain agent | Agent minus best simple |
|---|---|---:|---:|
| Hillstrom | send all the men's email, 0.6766 | 0.6513, 98.3% contacted | -0.0253 [-0.0476, -0.0030] |
| X5 (RUB) | segment rule, 72.33 | 72.23, 75.8% contacted | -0.10 [-0.55, +0.35] |

## H1. Is one v3 part to blame?

**Yes. It is journeys in the Gym and who on the bench, and no single removal fixes both.**

Gym, value change against the plain agent:

| Variant | baseline_shop | whatsapp_fatigue | discount_cannibalisation | sleeping_dogs_15 |
|---|---:|---:|---:|---:|
| agent_v3 (everything on) | -29.4% | -39.7% | -37.7% | -28.2% |
| v3 without journeys | **-1.4%** | **-11.2%** | **-0.8%** | **-3.2%** |
| v3 without who | -28.8% | -35.2% | -35.1% | -28.1% |
| v3 without look-alike | -28.6% | -36.0% | -36.9% | -28.4% |
| v3 without frequency | -28.4% | -35.0% | -35.2% | -25.7% |
| v3 without moments / offers | -29.4% | -39.7% | -37.7% | -28.2% |

With journeys on, the reason "never reads this channel" (`no_bids:journey_never_read`) accounts for 30-41% of all
"do nothing" decisions. Contacts per customer-week fall from 1.6 to about 0.8-1.0. In `whatsapp_fatigue` the -11.2%
left after removing journeys matches the small losses look-alike (-4.7%), who (-6.0%) and frequency (-6.7%) each show
on their own there.

Bench:

| Variant | Hillstrom | contacted | X5 | contacted |
|---|---:|---:|---:|---:|
| plain agent | 0.6513 | 98.3% | 72.23 | 75.8% |
| agent_v3 | 0.2847 | 1.9% | 70.76 | 1.5% |
| v3 without who | **0.6548** | 98.3% | **72.29** | 76.4% |
| v3 without any other one part | 0.2847 | 1.9% | 70.76 | 1.5% |
| agent + who only | 0.2847 | 1.9% | 70.76 | 1.5% |

The collapse is 99.5-99.97% `no_bids`: the bidders offer nothing at all for those customers. Turning on who also
switches on the bidder's leave-alone step (`autonomy/decide/bidders.py`, around line 1840). That step now runs for
every customer, not only those with a harm record. It drops all options for anyone `WhoBook.leave_alone`
(trait-pooled "people like them are put off") or the harm test marks. Which of the two fires on the bench has not
been pinned down yet. The cheap test is the same bench run with `WhoConfig(trait_harm=False)`.

Journeys does nothing on the bench (identical to the plain agent), because the bench strips SEND events from history,
so "never read" can never fire there. That is why the Gym and the bench blamed different parts.

## H2. Does any new part lose on its own?

- **Journeys alone loses in every Gym scenario:** -28.2%, -26.7%, -33.6%, -24.8%.
- **Who alone loses on the bench** (above). In the Gym it is within 1% of the plain agent, except -6.0% in
  `whatsapp_fatigue`.
- **Look-alike alone** sits within ±1% in three scenarios and loses 4.7% in `whatsapp_fatigue`. **Frequency
  alone** is within ±1% in two scenarios and loses 2.2% in `sleeping_dogs_15` and 6.7% in `whatsapp_fatigue`. On the
  bench they give +0.5% (Hillstrom) and +0.1% (X5), well inside the noise.
- **Moments and offers alone are inert.** They make the same decisions and get the same value as the plain agent
  everywhere they were run. The only trace of moments is a higher *expected* value on X5 (3.05 against 2.89 per
  contact) with no change in what is sent or earned.

## H3. Is the plain agent held back by its own guards ("too many rules")?

**No.** Value change when one guard is relaxed:

| Variant | baseline_shop | whatsapp_fatigue | discount | sleeping_dogs_15 | Hillstrom | X5 |
|---|---:|---:|---:|---:|---:|---:|
| no restraint | 0 | 0 | 0 | 0 | 0 | 0 |
| no exploration | +1.1% | -0.9% | +1.2% | +0.4% | +3.6% | +0.1% |
| no attention price | -0.1% | +1.1% | +1.1% | +0.9% | 0 | 0 |
| loose caps | -17.9% | -0.8% | -20.2% | -29.0% | 0 | 0 |
| no weekly budget | -14.1% | -79.4% | -12.6% | -14.9% | 0 | 0 |
| no guards at all | -14.6% | -88.5% | -11.8% | -15.0% | +3.6% | +0.1% |

- Nothing clears the +2% bar in the Gym.
- When the weekly budget goes, unsubscribes jump to 13-25%, and the agent's overconfidence grows to 4.5× (no weekly
  budget) or 12.5× (no guards) in `whatsapp_fatigue`.
- The Hillstrom +3.6% means contacting 100% instead of 98.3%. That is just the best simple rule (0.6750 against
  0.6766, difference -0.0016 [-0.0120, +0.0088]). It is the 5% exploration floor costing a little on a one-shot test,
  not a guard blocking good sends.
- On the bench the guards barely bind at all: most variants are identical to the plain agent.

The weekly cap is the most common "do nothing" reason (W, 86-90% in the Gym), but relaxing it loses money. So the cap
is doing its job, not holding the agent back.

## H4. Is there an information ceiling?

**Yes, on the real data.** The plain agent, every variant and the T-learner all land within noise of send-to-all or a
coarse segment rule:
- Hillstrom: 0.62-0.68 for all of them except the collapsed ones.
- X5: 72.1-72.6.

Even a policy that never explores (`no_explore`) only matches the best simple rule; it does not beat it. These
datasets do not hold enough per-customer difference in who is *moved* by a message for the agent's method to turn into
profit. In the Gym the agent beats the best fixed rule in all four scenarios, but the Gym's truth was written to
reward the agent's own model (ROOT_CAUSE.md audit 2, section 2). So that win does not answer this question.

## H5. Does the agent over-value its messages?

**Yes, everywhere, by 14-52%.** Expected against realised value per action:

| Setting | Expected | Realised | Ratio |
|---|---:|---:|---:|
| Gym baseline_shop | 0.906 | 0.661 | 1.37× |
| Gym whatsapp_fatigue | 1.306 | 1.147 | 1.14× |
| Gym discount_cannibalisation | 0.713 | 0.468 | 1.52× |
| Gym sleeping_dogs_15 | 0.731 | 0.507 | 1.44× |
| Hillstrom | 0.511 | 0.351 [0.166, 0.536] | 1.46× |
| X5 | 2.885 | 2.168 [0.224, 4.113] | 1.33× |

On the bench the expected value still falls inside the measured range, so this passes the plan's "within 2×" test.
But the bias always points the same way. The plan's 1.5× calibration gate (never built, see STATUS.md) would fail in
`discount_cannibalisation`. For agent_v3 the ratio reaches 2.05× there, and on the bench it is not meaningful (the
measured value is negative on Hillstrom, and 0.26× on 1.5% of X5 customers).

## How this compares with ROOT_CAUSE.md

| ROOT_CAUSE.md said | This run |
|---|---|
| Not too many rules; the weekly cap binds most but learned guards barely bind | **Confirmed.** Relaxing the cap or the guards loses money; the attention price and restraint change nothing measurable |
| Not prompts (no LLM in the decision) | Unchanged; not tested here because there is nothing to test |
| Journeys causes the Gym loss | **Confirmed** at full size: -25% to -34% alone, almost fully recovered without it in discount_cannibalisation |
| The bench collapse is undiagnosed; `never_read` cannot fire there | **Now diagnosed: who.** Journeys is inert on the bench, as predicted |
| The method (click-driven bidder) is the real ceiling; real gains came from the uplift add-on | **Supported.** The plain agent ties or slightly loses to the best simple rule on real data and over-values by 1.3-1.5× |
| Low exploitable difference between customers on these datasets | **Supported** (H4) |
| The Gym is circular and the bench is one-shot | Still true, and it explains why the two blamed different parts. Neither test bed alone would have found both |

## What this means

- **Keep v3 off.** If journeys ever comes back, it needs its "never read" rule replaced by evidence. Who needs its
  leave-alone step removed or gated on the holdout, and then shown on the bench not to stop contact. Moments and
  offers add nothing as built.
- **Keep the guards.** They are the reason the plain agent does not burn the list.
- **The next real gain is a method change, not loosening rules.** The planned test is ROOT_CAUSE.md T1 and T2: an
  `uplift_threshold` policy (cross-fitted uplift on purchases, send only when score × value beats the cost, deploy only
  if it beats the best simple rule on held-out data), and the agent with exploration off and the click model detached.
  Add a calibration gate that shrinks the agent's expected value to what the holdout measures.

## How much of v3's Gym loss is journeys

| Scenario | agent_v3 | v3 without journeys | plain agent | Share of the gap recovered |
|---|---:|---:|---:|---:|
| baseline_shop | 22,905 | 31,991 | 32,460 | 95% |
| sleeping_dogs_15 | 17,588 | 23,699 | 24,483 | 89% |
| discount_cannibalisation | 16,717 | 26,601 | 26,818 | 98% |
| whatsapp_fatigue | 23,936 | 35,266 | 39,707 | 72% |

Journeys is most of the Gym loss everywhere. The 2-11% that is left is the sum of the small losses look-alike, who and
frequency each show on their own. It is largest in `whatsapp_fatigue`, where each of them loses 5-7% alone.

## How the numbers were read

- **Gym value:** the DIAG `true_value_sum` divided by 3 seeds.
- **Oracle and best fixed rule:** taken from each scenario's agent log. All variants share the same seeds.
- **Bench:** paired differences come from the run's own bootstrap.
- **Rows the tables give as 0 or "same as":** the variant's decision counts and values are identical to the row it is
  compared with.
- **Where the logs came from:** they were read through the GitHub API, because the artifact download is blocked from
  this environment.
