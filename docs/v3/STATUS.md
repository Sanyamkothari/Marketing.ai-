# v3 status

26 Oct 2026, branch `claude/beautiful-keller-kacuxh`. The plan is [`plan-v3.md`](../../plan-v3.md); the calls on its open
questions are in [DECISIONS.md](DECISIONS.md).

## Built

Every stream was built by one agent, reviewed by a second (adversarial) agent and fixed by a third, then merged.
Every v3 piece is **off by default**; `build_agent_policy(v3=True)` or `AUTONOMY_V3=1` turns them all on together
([PRESET.md](PRESET.md)).

| Plan item | Doc |
|---|---|
| Phase 0: the Data Doctor's order-log path (B1-B5, B7, the reusable part of B6; B8 superseded by the AI-first design, DECISIONS B-9) | [ORDER_LOG.md](ORDER_LOG.md) |
| Foundation A: the customer profile | [PROFILE.md](PROFILE.md) |
| Foundation B: look-alike learning | [LOOKALIKE.md](LOOKALIKE.md) |
| Foundation C: live experiments | [EXPERIMENTS.md](EXPERIMENTS.md) |
| Decision 1: who (leaving risk, 90-day value, sleeping dogs by traits) | [WHO.md](WHO.md) |
| Decision 2: moments and the India calendar | [MOMENTS.md](MOMENTS.md) |
| Decision 3: offers and the whole-budget planner | [OFFERS.md](OFFERS.md) |
| Decision 4: journeys | [JOURNEYS.md](JOURNEYS.md) |
| Decision 5: generated images (fake provider, checker, batch approval) | [IMAGES.md](IMAGES.md) |
| Decision 6: copy per customer type, scoring model, live elimination | [COPY.md](COPY.md) |
| Decision 7: frequency (look-alike tolerance, 3-week plans) | [FREQUENCY.md](FREQUENCY.md) |
| Decision 8: early signals and auto-budget | [EARLY_SIGNALS.md](EARLY_SIGNALS.md) |
| Decision 9: personalisation | through the profile; cross-client priors already in `autonomy/priors/` |
| Scale: the sweep in batches | [SCALE.md](SCALE.md) |
| CI failures that were red on version-2 before v3 | [CI_GREEN.md](CI_GREEN.md) |
| The preset | [PRESET.md](PRESET.md) |

## Read this first: the review and the first real-data result

**"Built" above means the code and its tests exist. It does not mean wired into the running product or proven.**
An adversarial review (six red-team lenses, an independent skeptic per finding, a senior architect and a senior
CMO) is in [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md); its section 7 has the built / wired / proven table.
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
