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
