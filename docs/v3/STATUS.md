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

The real bench result is above. The Gym run (CI 40325406058) will be added when it finishes.
