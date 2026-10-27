# v3 status

26 Oct 2026, branch `claude/beautiful-keller-kacuxh`. The plan is [`plan-v3.md`](../../plan-v3.md); the calls on its open
questions are in [DECISIONS.md](DECISIONS.md).

## Status: parked research

**v3 is parked research. It stays off.** `AUTONOMY_V3` is off for every pilot, nothing in v3 is proven on real data,
and the plain (v2) agent is what the product runs and what is sold ([ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md)
section 8, "Park" and "Stop now"). No new v3 features are being built. A part comes back on only with the evidence
the review lists for it (section 8, "Evidence that would justify turning each v3 part back on").

## Built, wired, proven

Every stream was built by one agent, reviewed by a second (adversarial) agent and fixed by a third, then merged.
Every v3 piece is **off by default**; `build_agent_policy(v3=True)` or `AUTONOMY_V3=1` turns them all on together
([PRESET.md](PRESET.md)).

The three columns mean different things, and only the first was true when this file was first written:

- **Built**: the code and its tests exist.
- **Wired into production**: it changes what the running product does when `AUTONOMY_V3=1`.
- **Proven on real data**: it showed value on real data it could actually see.

The verdicts are copied from [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) section 7. Not one v3 component is proven.

| Plan item | Doc | Built | Wired into production | Proven on real data |
|---|---|---|---|---|
| Phase 0: the Data Doctor's order-log path (B1-B5, B7, the reusable part of B6; B8 superseded by the AI-first design, DECISIONS B-9) | [ORDER_LOG.md](ORDER_LOG.md) | Yes | No: no link to the agent's event store (D5) | Works for the v1 AutoML table only |
| Foundation A: the customer profile (52 features, nightly) | [PROFILE.md](PROFILE.md) | Yes | Yes | No; invisible on 2 of 4 bench sets (F1) |
| Foundation B: look-alike learning | [LOOKALIKE.md](LOOKALIKE.md) | Yes | Yes, but wiped on every rebuild | No; unidentifiable live (B1) |
| Foundation C: live experiments | [EXPERIMENTS.md](EXPERIMENTS.md) | Yes (library) | No: empty book; only the Gym starts one | No; the bar was never run |
| Decision 1: who. Leaving risk and 90-day value models | [WHO.md](WHO.md) | Yes | Yes | Synthetic only |
| Decision 1: who. Sleeping dogs by traits | [WHO.md](WHO.md) | Yes | Yes | No; confounded (B5, E12) |
| Decision 2: moments and the India calendar | [MOMENTS.md](MOMENTS.md) | Yes | Yes, as fixed multipliers | No; the smoke gain was negative (-5.24) |
| Decision 3: offers and the whole-budget planner | [OFFERS.md](OFFERS.md) | Yes | Yes | No; broken on Shopify discount codes (D1) |
| Decision 4: journeys | [JOURNEYS.md](JOURNEYS.md) | Yes | Yes | No; no journey-level credit |
| Decision 5: generated images (fake provider, checker, batch approval) | [IMAGES.md](IMAGES.md) | Yes (fakes) | No | No |
| Decision 6: copy per customer type, scoring model, live elimination | [COPY.md](COPY.md) | Yes (library) | No | No; impossible at 8k customers |
| Decision 7: frequency (look-alike tolerance, 3-week plans) | [FREQUENCY.md](FREQUENCY.md) | Yes | Yes | No; a candidate cause of the bench collapse |
| Decision 8: early signals | [EARLY_SIGNALS.md](EARLY_SIGNALS.md) | Yes (library) | No | No; pull-forward bias (A1) |
| Decision 8: auto-budget | [EARLY_SIGNALS.md](EARLY_SIGNALS.md) | Yes (library) | No | No; the two-goal scenario was never built |
| Decision 9: personalisation | through the profile; cross-client priors already in `autonomy/priors/` | Yes | Yes | n/a (needs at least 5 clients) |
| Scale: the sweep in batches | [SCALE.md](SCALE.md) | Yes | Yes | 300k completed |
| CI failures that were red on version-2 before v3 | [CI_GREEN.md](CI_GREEN.md) | Yes | Yes | Yes (CI) |
| The preset (`AUTONOMY_V3`) | [PRESET.md](PRESET.md) | Yes | Yes, all-or-nothing | n/a |
| Plan's 1.5x calibration gate | plan-v3.md | No | No | No |

## Read this first: the review and the first real-data result

**"Built" means the code and its tests exist. It does not mean wired into the running product or proven; the table
above keeps the three apart.** An adversarial review (six red-team lenses, an independent skeptic per finding, a senior architect and a senior
CMO) is in [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md); its section 7 is the source of the table above.
In short: experiments, copy per type, images, early signals and auto-budget have no caller in the runtime; the
wired parts lose what they learn on every rebuild; and no v3 part is proven.

**First real-data bench (CI run 40325405923, commit 3af057a7), doubly-robust profit per customer on the TEST half:**

| Dataset | agent (plain) | agent_v3 | v3 contacted |
|---|---:|---:|---:|
| Criteo | 0.0524 | 0.0524 (identical) | 11.9% |
| MegaFon | 4.196 | 4.196 (identical) | 46.1% |
| Hillstrom | 0.651 [0.50, 0.80] | **0.285** [0.19, 0.38] | 1.9% |
| X5 | 72.23 [71.14, 73.32] | **70.76** [69.66, 71.86] | 1.5% |

v3 changed nothing where its parts cannot see the customers' features (Criteo, MegaFon), and nearly stopped
contacting anyone and lost where they can (Hillstrom, X5). The cause of that collapse is not yet diagnosed (the
bench records `blocked_by` per decision; reading it is the next step). **Decision: v3 stays off; see the review's
section 8 for the fix order.** Full numbers: [redteam/bench_results.md](redteam/bench_results.md).

## The proof runs

Nothing here shows yet that v3 beats the plain agent or the best simple rule. That is the job of the CI runs this
commit starts (`[gym]`, `[real]`):

- **Gym** (`.github/workflows/gym.yml`): `agent`, `agent_lookalike`, `agent_who`, `agent_frequency`, `agent_v3` on
  every scenario, 3,000 customers, 3 seeds, judged by the staged bar ([GYM_BAR.md](../v2/GYM_BAR.md)).
- **Real bench** (`.github/workflows/realbench.yml`): `agent` and `agent_v3` on Hillstrom, X5, Criteo and MegaFon,
  judged by the targets in [TARGETS.json](../v2/TARGETS.json).

The real bench result is above.

**Gym (CI run 40325406058, commit 3af057a7):** 10 scenarios (plus `moments_planted`), 3,000 customers, 3 seeds,
90 decision days, all 52 jobs completed, zero rule breaks anywhere. True value in dollars, mean of 3 seeds; stage on
the staged bar ([GYM_BAR.md](../v2/GYM_BAR.md)).

| Scenario | agent | agent_lookalike | agent_who | agent_frequency | agent_v3 | best fixed rule |
|---|---:|---:|---:|---:|---:|---:|
| baseline_shop | 32,460 (S3) | 32,498 (S3) | 32,368 (S3) | 32,657 (S3) | **22,905 (S1)** | 30,338 |
| sleeping_dogs_hidden | 23,956 (S1) | 24,057 (S1) | 23,759 (S1) | 23,309 (S1) | **17,293 (S1)** | 23,063 |
| sleeping_dogs_15 | 24,483 (S2) | 24,618 (S2) | 24,513 (S2) | 23,951 (S2) | **17,588 (S1)** | 22,886 |
| whatsapp_fatigue | 39,707 (S3) | 37,827 (S3) | 37,333 (S3) | 37,038 (S3) | **23,936 (S1)** | 28,558 |
| send_time_matters | 28,062 (S3) | 28,003 (S3) | 27,559 (S3) | 28,450 (S3) | **18,011 (S1)** | 20,475 |
| discount_cannibalisation | 26,818 (S3) | 26,961 (S1) | 27,020 (S1) | 26,643 (S1) | **16,717 (S1)** | 25,885 |
| drift_midway | 31,739 (S1) | 32,796 (S3) | 31,909 (S1) | 32,911 (S3) | **22,611 (S1)** | 29,784 |
| small_business | 31,753 (S1) | 32,476 (S1) | 31,673 (S1) | 32,639 (S1) | **22,859 (S1)** | 30,273 |
| moments_planted | 35,354 (S1) | not run | not run | not run | **26,156 (S1)** | 34,363 |
| null_effect | -8 | -9 | -9 | -9 | -11 | -953 |
| complaint_storm | 0 (stopped at once) | 0 | 0 | 0 | 0 | -1,920 |

Reading, with a tie band of 2% of the plain agent's value:
- **agent_v3 loses to the plain agent in all 9 scenarios where messages make money**, by 12% to 36% of value, and
  falls below the best fixed rule in every one of them (0 of 3 seeds beat it). It contacts 30-45% fewer customers a
  week (0.8-1.1 against about 1.6). It does unsubscribe fewer people (about 4-5% against 7-10%) and contacts fewer
  planted sleeping dogs (68% against 93%), but at a large cost in value.
- **The single parts add nothing measurable on their own:** agent_lookalike and agent_who are within about 1% of the
  plain agent almost everywhere (each loses 5-6% in `whatsapp_fatigue`); agent_frequency ties or gains up to 4% in
  some scenarios and loses 2-7% in others. So the collapse comes from the parts combined in the preset (journeys,
  moments, offer pricing, or their interaction), not from look-alike, who or frequency alone. The bench's
  `blocked_by` counts are the way to find which.
- **Moments:** in `moments_planted` (a scenario the moments stream wrote), moments add +$56 a seed for the plain
  agent and -$33 a seed for agent_v3: no measurable value even on its own planted truth.
- **The plain agent** beats the best fixed rule in every money-making scenario on the pooled value, but reaches
  Stage 2 or better in only some of them, because one seed in three falls below the rule in several.

Together with the real bench, this confirms the review: **v3 stays off.**
