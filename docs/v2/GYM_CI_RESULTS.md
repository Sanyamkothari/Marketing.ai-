# Gym results from CI: the realistic Gym at full size

- **Run:** GitHub Actions `gym` run 39409140711, commit `03fc0816`.
- **Setup:** 10 scenarios × 3 seeds × 3,000 customers × 90 days, judged by the staged bar in
  `GYM_BAR.md`.
- **Agent version:** the per-person "agent brain" (wave 3b), *before* the refill and format wiring.
- **Abbreviation:** BFR is the best fixed rule.

| Scenario | Stage reached | Safe | Useful (beats send-to-all, the marketer rule and the BFR) | Smart (share of the segment oracle) | Full oracle |
|---|---|---|---|---|---|
| baseline_shop | **3 Smart** | ✅ | ✅ 3/3 | ✅ 81% (BFR 80%) | 55% |
| small_business | **3 Smart** | ✅ | ✅ 3/3 | ✅ 82% (BFR 80%) | 55% |
| send_time_matters | **3 Smart** | ✅ | ✅ 3/3 | ✅ **96%** (BFR 72%) | 50% |
| drift_midway | 2 Useful | ✅ | ✅ 3/3 | ❌ 79.99% (BFR 76%) | 55% |
| sleeping_dogs_15 | 2 Useful | ✅ | ✅ 3/3 | value ✅ 82%; ❌ contacts 93% of sleeping dogs (send-to-all: 93%) | 49% |
| null_effect | 2 Useful | ✅ | ✅ loses $0.20 per customer (allowed $0.25) | not judged | - |
| complaint_storm | 2 Useful | ✅ stopped after 1 batch | ✅ lost value $4.2k vs $104.5k for send-to-all | not judged | - |
| sleeping_dogs_hidden | 1 Safe | ✅ | ❌ 2/3 (one seed $550 below the BFR) | 81% | 49% |
| discount_cannibalisation | 1 Safe | ✅ | ❌ 0/3 | 86% (BFR **92%**) | 66% |
| whatsapp_fatigue | 1 Safe | ✅ | ❌ 0/3 | ❌ **18%** (BFR 71%), unsubscribe rate 27% | 13% |

**Safe everywhere:** zero rule breaks across all 30 runs, the breaker stops harm after one batch,
and the intervals cover the truth.

**The pattern.** The agent beats send-to-all and the marketer rule everywhere. Its edge over the
best fixed rule is thin, though: about $270–$800 on the plain scenarios, which is within one seed's
noise.

It loses whenever the right move is to **contact less**: fatigue, discount cannibalisation and
sleeping dogs. It runs at about 1.8 contacts per customer per week in nearly every scenario,
including null_effect (1.6), and its unsubscribe rate and lost future value are consistently above
the best fixed rule's.

Its clear win is timing (send_time_matters, 96% of the segment oracle). There it learns something a
fixed rule cannot.

**Next fixes, in order:**
1. Make the attention price and fatigue cost bite: learn the unsubscribe hazard faster and charge
   the lost future value.
2. Turn on the pacer that is already built.
3. When uplift is unproven, start from "contact less" rather than "contact up to the cap".
4. Offer discounts only when per-person offer sensitivity is shown.

## After contact-less (run 39437303830, commit 203fc041)

- **Run:** GitHub Actions `gym` run 39437303830, commit `203fc041`, same setup as above (10
  scenarios × 3 seeds × 3,000 customers × 90 days).
- **Agent version:** the agent after the "contact less" work in `CONTACT_LESS.md` (fatigue hazard,
  restraint rule, weekly budget, offer discipline, contact-harm model).
- **Before** is the run above. It printed contacts a week only as "about 1.8 nearly everywhere" and
  an unsubscribe rate only for `whatsapp_fatigue`, so the other before cells say "~1.8" or "-".
  The after unsubscribe rate has the BFR's in brackets.

| Scenario | Stage | Share of the segment oracle | Contacts a customer a week | Unsubscribed (BFR) | Beats the BFR |
|---|---|---|---|---|---|
| baseline_shop | 3 -> **3** | 81% -> **91%** (BFR 80%) | ~1.8 -> 1.61 | - -> 8.1% (7.8%) | 3/3, by $4.4k |
| small_business | 3 -> **3** | 82% -> **92%** (BFR 80%) | ~1.8 -> 1.62 | - -> 8.0% (7.8%) | 3/3, by $4.7k |
| send_time_matters | 3 -> **3** | 96% -> **106%** (BFR 72%) | ~1.8 -> 1.56 | - -> 7.7% (9.2%) | 3/3, by $9.7k |
| drift_midway | 2 -> **3** | 79.99% -> **89%** (BFR 76%) | ~1.8 -> 1.62 | - -> 7.9% (8.2%) | 3/3, by $4.8k |
| sleeping_dogs_15 | 2 -> 2 | 82% -> 93% (BFR 80%); ❌ still contacts 93% of sleeping dogs (send-to-all 93%) | ~1.8 -> 1.59 | - -> 10.0% (10.3%) | 3/3, by $3.9k |
| null_effect | 2 -> 2 | not judged; loses $0.20 -> **$0.10** a customer (allowed $0.25) | 1.6 -> 1.29 | - -> 6.6% (7.8%) | 3/3, by $0.7k |
| complaint_storm | 2 -> 2 | not judged; stopped after 1 batch -> **paused before day 0**, lost value $4.2k -> $0 | ~0 -> 0.00 | - -> 0% (44%) | 3/3, by $1.9k |
| sleeping_dogs_hidden | 1 -> **3** | 81% -> **92%** (BFR 80%); contacts 94% of sleeping dogs (not judged: no signal) | ~1.8 -> 1.61 | - -> 10.3% (10.2%) | 2/3 -> **3/3**, by $3.6k |
| discount_cannibalisation | 1 -> **3** | 86% -> **98%** (BFR 92%) | ~1.8 -> 1.59 | - -> 7.5% (8.1%) | 0/3 -> **3/3**, by $1.8k |
| whatsapp_fatigue | 1 -> **3** | 18% -> **101%** (BFR 71%) | ~1.8 -> 1.01 | 27% -> **3.9%** (15.5%) | 0/3 -> **3/3**, by $12.2k |

"By" is the mean true value above the BFR over the three seeds. Still zero rule breaks in all 30
runs. Nine scenarios reach their highest stage; only `sleeping_dogs_15` is held back.

**What this says:**

* **Contacting less fixed all three "contact less" losses.** `whatsapp_fatigue`,
  `discount_cannibalisation` and `sleeping_dogs_hidden` went from Stage 1 to Stage 3. In
  `whatsapp_fatigue` the agent now sends about one message a week instead of 1.8, and 3.9% of
  customers unsubscribe instead of 27%. It beats the segment oracle there (101%), because that
  ceiling does not pace.
* **The edge over the best fixed rule is now real.** It was $270-$800 in the plain scenarios,
  within one seed's noise; it is now $4.4k-$4.8k, beating the rule on every seed. `drift_midway`
  climbs to Stage 3 (89%).
* **Less waste where nothing works.** `null_effect` loses $0.10 a customer instead of $0.20, at
  1.3 contacts a week. In `complaint_storm` the agent never sends at all: no harm, but it also
  leaves the $10.8k the segment oracle finds in the few customers who still respond.
* **Sleeping dogs are still not found.** The agent contacts 93% of them in `sleeping_dogs_15`, the
  same as send-to-all, so Stage 3 fails there. Likely reason: restraint works at the level of a
  channel or segment, and nothing yet reads the per-person traces (ignored messages, direct
  visits, channel unsubscribes) to leave one customer alone.
* **Unsubscribes are now at or below the fixed rule's**, except in the plain shops (8.0-8.1%
  against 7.8%), where the agent sends a little less but still loses slightly more customers.
* **One interval missed.** In `whatsapp_fatigue` seed 0 the 95% range ($15.8k-$37.2k) missed the
  true value ($39.7k). Coverage still passes (2 of 3, Wilson range 21%-94%), but it is worth
  watching. The full oracle is still out of reach: 55-75%.

## After per-person harm and the click model (runs 39481636725 and 39507038294)

- **Runs:** GitHub Actions `gym` run 39481636725 (commit `cf4d3492`) and run 39507038294 (commit
  `72b5fc31`). Same setup as above: 10 scenarios × 3 seeds × 3,000 customers × 90 days. All 20 jobs
  finished; none crashed.
- **Per-person harm** (run 39481636725): contact harm worked out for each customer, plus the new
  decision reasons (merge of `v2/dogs-storm`, plus a Python 3.11 fix).
- **Click model** (run 39507038294): adds the purchase-weighted click blend from `v2/click-harm`
  (`ClickResponseModel.uplift_from_clicks = True` in `autonomy/runtime/agent.py`).
- Each cell reads contact-less -> per-person harm -> click model. "Unsubscribed" has the BFR's rate
  in brackets. "Beats the BFR" gives the seeds won and the mean true value above the BFR. The
  reference policies (send-to-all, marketer rule, BFR, segment oracle) are the same in both runs,
  because they share seeds and do not use the agent's code.

**baseline_shop**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 80%) | 91% | 91% | 92% |
| Contacts a customer a week | 1.61 | 1.61 | 1.61 |
| Unsubscribed (BFR 7.8%) | 8.1% | 8.1% | 8.0% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $4.4k | 3/3, by $4.4k | 3/3, by $4.7k |

**small_business**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 80%) | 92% | 92% | 92% |
| Contacts a customer a week | 1.62 | 1.62 | 1.62 |
| Unsubscribed (BFR 7.8%) | 8.0% | 8.0% | 7.8% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $4.7k | 3/3, by $4.7k | 3/3, by $4.7k |

**send_time_matters**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 72%) | 106% | 106% | 105% |
| Contacts a customer a week | 1.56 | 1.56 | 1.56 |
| Unsubscribed (BFR 9.2%) | 7.7% | 7.7% | 7.6% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $9.7k | 3/3, by $9.6k | 3/3, by $9.5k |

**drift_midway**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 76%) | 89% | 89% | 89% |
| Contacts a customer a week | 1.62 | 1.62 | 1.62 |
| Unsubscribed (BFR 8.2%) | 7.9% | 7.9% | 7.9% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $4.8k | 3/3, by $4.8k | 3/3, by $4.8k |

**sleeping_dogs_15**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 2 | 2 | 2 |
| Share of the segment oracle (BFR 80%) | 93% | 93% | 93% |
| Contacts a customer a week | 1.59 | 1.59 | 1.59 |
| Unsubscribed (BFR 10.3%) | 10.0% | 10.0% | 10.0% |
| Sleeping dogs contacted (send-to-all 93%) | 93% ❌ | 93% ❌ (1,036 of 1,113) | 93% ❌ (1,036 of 1,113) |
| Beats the BFR | 3/3, by $3.9k | 3/3, by $3.7k | 3/3, by $3.9k |

**null_effect**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 2 | 2 | 2 |
| Share of the segment oracle | not judged | not judged | not judged |
| Loss a customer (allowed $0.25) | $0.10 | **$0.00** | $0.00 |
| Contacts a customer a week | 1.29 | **0.02** | 0.02 |
| Unsubscribed (BFR 7.8%) | 6.6% | **0.1%** | 0.1% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $0.7k | 3/3, by $0.9k | 3/3, by $0.9k |

**complaint_storm**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 2 | 2 | 2 |
| Share of the segment oracle | not judged (0%) | not judged (0%) | not judged (0%) |
| Contacts a customer a week | 0.00 (paused before day 0) | 0.00 | 0.00 |
| Unsubscribed (BFR 44%) | 0% | 0% | 0% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $1.9k | 3/3, by $1.9k | 3/3, by $1.9k |

**sleeping_dogs_hidden**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 80%) | 92% | 92% | 92% |
| Contacts a customer a week | 1.61 | 1.61 | 1.60 |
| Unsubscribed (BFR 10.2%) | 10.3% | 10.3% | 10.2% |
| Sleeping dogs contacted (not judged: no signal) | 94% | 94% (1,040 of 1,109) | 94% (1,040 of 1,109) |
| Beats the BFR | 3/3, by $3.6k | 3/3, by $3.6k | 3/3, by $3.5k |

**discount_cannibalisation**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 92%) | 98% | 98% | 98% |
| Contacts a customer a week | 1.59 | 1.59 | 1.59 |
| Unsubscribed (BFR 8.1%) | 7.5% | 7.6% | 7.5% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $1.8k | 3/3, by $1.7k | 3/3, by $1.7k |

**whatsapp_fatigue**

| | contact-less | per-person harm | click model |
|---|---|---|---|
| Stage | 3 | 3 | 3 |
| Share of the segment oracle (BFR 71%) | 101% | 101% | 100% |
| Contacts a customer a week | 1.01 | 1.01 | 1.01 |
| Unsubscribed (BFR 15.5%) | 3.9% | 3.9% | 4.1% |
| Sleeping dogs contacted | - | - | - |
| Beats the BFR | 3/3, by $12.2k | 3/3, by $12.2k | 3/3, by $11.7k |

Still zero rule breaks in all 60 runs. The stages are the same as after contact-less: nine
scenarios at their highest stage, `sleeping_dogs_15` held at Stage 2.

**What this says:**

* **Per-person harm fixed one thing: `null_effect`.** Where messages do nothing, the agent now
  almost stops: 0.02 contacts a week instead of 1.29, 0.1% unsubscribed instead of 6.6%, and a loss
  of about $0.00 a customer instead of $0.10. Every other scenario is unchanged to within a few
  hundred dollars.
* **Sleeping dogs are still not found.** Both runs contact exactly the same 1,036 of 1,113
  sleeping dogs in `sleeping_dogs_15` (93%, the same as send-to-all). Neither per-person harm nor
  the click model moved a single one, so Stage 3 still fails there.
* **The click model changed nothing beyond seed noise.** Against per-person harm, its edge over the
  BFR moved by +$210 (baseline_shop), +$115 (sleeping_dogs_15), +$11, -$17, -$92, -$114, -$148 and
  -$587 (whatsapp_fatigue). Summed over the eight money-making scenarios that is about -$600. The
  seeds alone spread the edge by about $800 in the plain shops and about $6.7k in
  `whatsapp_fatigue`. No stage was lost, but none was gained either, and no sleeping dog was spared.
* **Recommendation: switch the purchase-weighted click blend off** (`uplift_from_clicks = False`).
  It was meant to help with sleeping dogs. It did not, and it did not add profit beyond noise. It
  only adds complexity. Keep the code behind the flag in case a later per-person signal gives it
  something to work with.
* **One interval still misses.** `whatsapp_fatigue` seed 0 misses in both runs, as before (95% range
  $15.8k-$37.2k against a true $39.6k-$39.7k). Coverage still passes at 2 of 3. The full oracle is
  still out of reach: 55-75%.
* **These are simulator results only.** `REAL_DATA_RESULTS.md` shows that on real randomised data
  (Hillstrom, X5) the agent does not yet beat simple policies, so these Gym gains have not been shown
  outside the simulator.
