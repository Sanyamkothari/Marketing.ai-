# Repeated-campaign benchmark: first results

27 Oct 2026. CI run [40521935481](https://github.com/Sanyamkothari/Marketing.ai-/actions/runs/40521935481) on commit
6324065f. All 152 jobs succeeded.

**The setup.**
- 20,000 real X5 customers. The cutoff is 2018-12-25, with 33 days of history before it and 306,053 real receipts
  replayed after it.
- 84 decision days, 3 seeds per world.
- The message effects are synthetic. The benchmark's design is in [WAVES.md](WAVES.md).

**How the numbers were read.**
- Every value below is the true incremental net margin in RUB, the mean of the three seeds.
- The figures come from each job's own log. The summary job's paired intervals went to the step summary and an
  artifact, and neither can be downloaded from this environment.
- Differences below are between means only. Read them against the seed-to-seed spread, shown per world.

## Short answer

**No. On this benchmark the agent does not beat a simple rule, even when customers differ and the campaign repeats
for 12 weeks.**

| World | Agent (share of oracle) | Best simple policy | Agent minus best simple |
|---|---:|---|---:|
| null (no effect) | -51,621 | send nothing, 0 | -51,621 (the cost of its sends) |
| homogeneous_small | 556,435 (50%) | best fixed rule, 757,810 (68%) | -201,375 |
| heterogeneous | 1,406,340 (67%) | best fixed rule, 1,723,317 (82%) | -316,977 |
| sleeping_dogs | 499,759 (20%) | SMS once a week, 551,104 (22%) | -51,345 (within the seed spread) |
| fatigue_heavy | 528,754 (84%) | SMS once a week, 685,453 (109%) | -156,699 |

- **The fairness check passes.**
  - In the null world the oracle sends nothing and earns 0, and every other policy loses exactly what it spent.
  - The agent sends little there: 0.16-0.57 messages per customer-week, against 1.8 for send-to-all.
  - Its loss of 51,621 RUB is the cost of exploring a world in which messages do nothing. Send-all SMS loses 836,259.
- **Where messages help, the agent sends too little.**
  - In homogeneous_small and heterogeneous it sends 0.8-1.8 messages per customer-week. The best fixed rule sends 1.8,
    and the oracle about 1.4 but to the right people.
  - The agent captures 50-67% of the oracle. The fixed rule captures 68-82%.
- **Where restraint matters, the agent is right to hold back, but a weekly SMS does it better.**
  - The agent beats send-to-all by a wide margin in sleeping_dogs and fatigue_heavy.
  - One SMS a week still earns more: 685,453 against 528,754 in fatigue_heavy.
- **The variants change nothing.** `agent_greedy` (no exploration) and `agent_uplift_only` (no click model) are
  within 1% of the agent in every world. So the gap is not exploration and not the click model.
- **The uplift method never got past its gate.**
  - `uplift_waves` refitted 108 times (9 refits × 4 worlds × 3 seeds), and the gate chose a simple rule every time.
  - The lower bound of the paired difference was at most 0 in all of them.
  - It lands near the agent: 52%, 65%, 16% and 63% of the oracle. It is below the agent in sleeping_dogs and
    fatigue_heavy.

## Per world, all policies

Mean true value in RUB, with share of the oracle in brackets.

| Policy | homogeneous_small | heterogeneous | sleeping_dogs | fatigue_heavy |
|---|---:|---:|---:|---:|
| oracle | 1,107,676 | 2,095,518 | 2,508,982 | 628,093 |
| agent | 556,435 (50%) | 1,406,340 (67%) | 499,759 (20%) | 528,754 (84%) |
| agent_greedy | 555,780 (50%) | 1,411,731 (67%) | 530,191 (21%) | 523,569 (83%) |
| agent_uplift_only | 555,077 (50%) | 1,410,148 (67%) | 507,528 (20%) | 530,148 (84%) |
| uplift_waves | 576,157 (52%) | 1,352,517 (65%) | 396,659 (16%) | 395,298 (63%) |
| best_fixed | **757,810 (68%)** | **1,723,317 (82%)** | 105,092 (4%) | 6,384 (1%) |
| send_all_sms | 745,411 (67%) | 1,702,584 (81%) | 118,072 (5%) | -3,853 |
| send_all_email | 562,237 (51%) | 962,750 (46%) | 277,483 (11%) | 192,805 (31%) |
| weekly_sms | 582,988 (53%) | 1,321,084 (63%) | **551,104 (22%)** | **685,453 (109%)** |

Contacts per customer-week and unsubscribe rate:

| Policy | homogeneous_small | heterogeneous | sleeping_dogs | fatigue_heavy |
|---|---|---|---|---|
| agent | 0.8-1.2, 2-4% | 1.0-1.8, 3-6% | 0.9-1.3, 3-5% | 0.64, 5-7% |
| best_fixed | 1.81, 5% | 1.81, 5% | 1.81, 6% | 1.74, 20% |
| weekly_sms | 0.91, 2% | 0.91, 2% | 0.91, 2.5% | 0.89, 6.3% |

## Seed-to-seed spread

The agent's value in each seed (0, 1, 2):

| World | Seed 0 | Seed 1 | Seed 2 |
|---|---:|---:|---:|
| homogeneous_small | 681,277 | 489,842 | 498,185 |
| heterogeneous | 1,500,252 | 992,617 | 1,726,150 |
| sleeping_dogs | 847,537 | -149,888 | 801,629 |
| fatigue_heavy | 519,819 | 544,869 | 521,574 |

- The best fixed rule beat the agent in every seed of homogeneous_small and heterogeneous.
- Weekly SMS beat the agent in every seed of fatigue_heavy.
- In sleeping_dogs every policy except send-nothing loses money in seed 1, so that world cannot separate them.

## Limits of this benchmark

These are stated so the result is not over-read:

1. **The truth leans toward frequent, broad sending.** The effect in money grows with a customer's base buying rate
   (WAVES.md). Customers who buy often are both the most valuable to message and the ones a blanket rule reaches
   anyway.
2. **The oracle is not a ceiling under fatigue.** It is greedy per contact and ignores the fatigue a send causes
   later, which is why weekly SMS reaches 109% in fatigue_heavy. Shares there compare policies, not distance to the
   best possible.
3. **The effect sizes are our choice** (3-10% relative lift), with one effect window (14 days) and one pull-forward
   share (40%).
4. **Precision is limited.** There are three seeds and no paired intervals were read. The measured holdout estimate
   (an intention-to-treat comparison) has a half-width of about ±1.4M RUB. A shop could not see any of these
   differences from its own holdout over 12 weeks at this size.

## What this means

This test was built to settle whether the one-shot datasets were simply the wrong test. They were not the whole
story. **Given repeated campaigns, real buying and customers who differ, the agent's method still does not find the
customers a message moves.**

- It reads clicks, which here say who is active, not who is moved.
- Its holdout reading is far too noisy to correct that within a quarter.
- So it settles on a cautious, middling contact rate.
- A fixed rule that sends broadly (where messages help) or about weekly (where fatigue bites) does better.

The problem is partly ours (the method) and partly physics: per-customer effects of a few percent cannot be learned
from a 10% holdout in 12 weeks.

### Next steps

These are decisions for the owner, not built yet:

1. **Make the product's default a simple, well-tuned cadence rule.** Send to everyone with consent up to N a week,
   with N set by the measured unsubscribe and complaint rates, and pull back on fatigue signals. Keep the agent as a
   challenger that must beat that rule on the holdout before it takes over. This is what the evidence supports today.
2. **If the agent is kept as a challenger, change what it learns from.** That means purchases against a randomised
   slice, pooled across shops through the cross-client priors, not clicks. It also means a gate sized to what 12
   weeks can show.
3. **Re-run this benchmark with a second truth** in which the effect does not grow with the base rate (limit 1), so
   a blanket rule is not favoured by construction. The agent should get that chance before the decision.

## Other results from the same commits

- **The WhatsApp revert worked.** Gym run 40506722177 (commit 86626bf0) shows the agent back at 39,707 in
  `whatsapp_fatigue` and 32,460 in `baseline_shop`, Stage 3 in both. These match the values before the change.
- **The v2 suite failed only on two of this benchmark's tests**, which used pandas' Spearman correlation and so need
  scipy, which the v2 job does not install. Fixed: the tests rank and correlate with pandas alone.
