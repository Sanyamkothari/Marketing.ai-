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
