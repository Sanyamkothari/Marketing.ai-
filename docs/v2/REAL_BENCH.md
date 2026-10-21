# The real-data scoreboard

21 Oct 2026, branch `v2/realfix`. One command scores Autonomy's agent against simple policies on four
real randomised campaigns. It is the bar for the real-data fixes listed in
[REAL_DATA_RESULTS.md](REAL_DATA_RESULTS.md): every fix in `autonomy/` is measured with it. The
BEFORE numbers below are the code before the fix wave (commit `f55c57b3`); the
[AFTER numbers](#after-the-fix-wave) are the fix wave's commit, on the same command. CI runs it on a
push to `version-2` whose message contains `[real]` (`.github/workflows/realbench.yml`).

## The command

Run from the repo root (the datasets live outside the repo, never in it):

```
python -m scripts.v2_realbench.run --dataset all --data-dir /home/user/datasets --json out.json
python -m scripts.v2_realbench.run --dataset hillstrom --data-dir /home/user/datasets --json hillstrom.json
python -m scripts.v2_realbench.run --dataset {hillstrom,x5,criteo,megafon,all} --data-dir DIR --json FILE
python -m scripts.v2_realbench.run --dataset {hillstrom,x5,criteo,megafon} --policy scientist --data-dir DIR --json FILE
python -m scripts.v2_realbench.run --dataset {hillstrom,x5,criteo,megafon} --policy scientist --ai-answers ANSWERS.json --json FILE
python -m scripts.v2_realbench.run --render out.json        # the markdown tables of a saved report
python -m scripts.v2_realbench.fetch --data-dir DIR          # download the four datasets, checksummed
python -m scripts.v2_realbench.sealed --data-dir DIR         # load check of the two SEALED datasets (see Sealed runs)
python -m scripts.v2_realbench.targets [--base-dir OUT]      # validate / judge docs/v2/TARGETS.json
python -m scripts.v2_realbench.gym_fit --data-dir DIR [--check|--literal]   # the Gym's real-calibrated fits
```

`--policy scientist` scores the policy the Scientist selects on TRAIN from its typed menu instead of the agent
alone, and `--ai-answers` adds the AI roles' loop (recorded Sonnet answers, a prompt without an answer is
saved and the run exits 3); see [SCIENTIST.md](SCIENTIST.md). The agent bench (`--policy agent`, the default)
is the scoreboard of record and is unchanged.

`--dataset lenta` and `--dataset starbucks` are SEALED: the run refuses them unless `--sealed-final` is
passed, they are never part of `--dataset all`, and each sealed run is recorded in
[Sealed runs](#sealed-runs) before the data is read.

Options: `--seeds` (agent decisions per test customer, fixed at 4), `--limit N` (fewer customers, for
a smoke run only: the report then says `"fixed_sizes": false` and is not comparable),
`--with-decision-log` (also hand the agent the past campaign as decisions, see below),
`--no-features` (keep customer features out of the agent's states), `--note TEXT` (kept in the
report), `--quiet`. The JSON is rewritten after each dataset, so a crash keeps what finished.

Full run: about 18-23 minutes and 4.7 GB peak on a 4-CPU, 16 GB box (per-dataset times are in the
report's `timings`). Run one at a time; the machine is shared.

To measure a fix: run the command on your branch and compare the summary table with the BEFORE
table below. `hillstrom` alone takes about 2 minutes and is the quickest real check.

## What it does

Code: `scripts/v2_realbench/` (`run.py` entry point, `common.py` engine, `adapters.py` one adapter
per dataset, `fetch.py` downloads). Tests: `tests/v2/realfix/test_bench.py` (inline fixtures, a
few seconds, including an end-to-end run of the real agent on 600 synthetic customers).

1. **Adapt** the dataset into Autonomy's inputs the way a customer's connector and column mapping
   would (`adapters.py`, the only dataset-specific code): pre-campaign events per customer (signup,
   consent, purchases), the past campaign's SEND events with `content_id` and `logged_probability`
   (the design probability), clicks where the data has a click-like signal, the purchases in the
   campaign window, briefs, approved content and the channel cost.
2. **Split by customer id hash**: BLAKE2b of `autonomy-realbench-v1:<id>`, test when below 0.5.
   Fixed samples are also by hash (X5 clients, MegaFon rows by id; Criteo rows by a SplitMix64 hash
   of the row number).
3. **Build the agent through the product path**: `autonomy.runtime.agent.build_agent_policy(briefs,
   content, costs=...)` with the default bundle (what `autonomy.runtime.settings.agent_policy_factory`
   builds), then the runtime's warm start as the Gym and the runtime call it: `policy.nightly([],
   history, now)` with the TRAIN customers' events only, `policy.prepare_day(train_states, ctx)`,
   then `policy.decide(state, ctx)` for every TEST customer under 4 seeds. States are folded from
   pre-campaign events with the production `StateFolder` (`rebuild_states`). Nothing in the bench
   reaches into the agent, so a fix in `autonomy/` shows up with no change here.
4. **Fit the baselines on TRAIN**: send to none; send to all with each arm; the best segment rule
   (repo `fit_segment_policy`, columns chosen by DR on a 30% validation slice of train, then
   refitted on all of train); a T-learner (one numpy logistic regression of conversion per arm on
   one-hot binned features, times the margin of a conversion, minus the arm's cost; bench only).
5. **Score on TEST** with `autonomy.proof`: `replay.harness.evaluate` (IPS, SNIPS, DR with 95%
   intervals, reward model `per_action_mean_model` cross-fitted) and paired DR differences
   (`ope.mean_interval`) of the agent against every policy.

**Best simple policy.** One of none / send to all (each arm) / segment rule, chosen on TRAIN only:
each blanket policy by its DR on all of train (it is not fitted, so that is out of sample), the
segment rule by its DR on the validation slice after fitting on the rest of train. The report also
gives, as a diagnostic, the best simple policy chosen on TEST (which flatters the baseline).

**Send to all** is the sent arm with the best mean profit on train (Hillstrom: the men's e-mail).

**Calibration.** The agent's mean `Decision.expected_net` over its ACT decisions (its own expected
net value per contact, after the contact cost) against the DR-measured value of contacting the
customers it contacts: its DR profit gain over send-to-none divided by its contact rate. Same
units, both net of cost. The target is a ratio between 0.5 and 2.

**Customer features reach the agent** as `CustomerState.preferences` (column name to the value as
text), exactly what folding one `PREFERENCE` event per column gives (a test checks this). The bench
copies them onto the folded states instead of writing millions of events. Before the fix wave
nothing in the agent read them; since it, the uplift model fitted at the warm start does
(`autonomy/uplift`, see AFTER).

**`--with-decision-log`.** Off by default. It also passes the past campaign to `nightly()` as
`Decision` records (ACT in the agent group for a sent customer, NOTHING in the holdout group for the
control, each with its design probability), which is how the runtime's `_learn` hands a policy the
decisions in its store. Use it only if a fix makes the runtime import a past campaign's control
group that way, and say so with the numbers; the scoreboard of record is the default.

### Per dataset (fixed sizes)

| Dataset | Rows used | Arms (design) | Reward on the scoreboard | Contact cost |
|---|---|---|---|---|
| Hillstrom e-mail (2008) | all 64,000 | none / men's / women's, 1/3 each | 0.4 x spend (USD, 2 weeks) - cost | $0.001 per e-mail (the agent's default) |
| X5 RetailHero SMS | a fixed 100,000 of 200,039 labelled clients (real receipts) | none / SMS, 50/50 | 0.25 x the client's own pre-period mean receipt if they bought - cost | 2 RUB per SMS |
| Criteo uplift v2.1 | a fixed 300,000 of 13,979,592 rows | control / treated, 15/85 | 20 x conversion - cost | break-even on train: 20 x the train effect |
| MegaFon (generated) | a fixed 200,000 of 600,000 rows | control / treated, 50/50 | 20 x conversion - cost | break-even on train: 20 x the train effect |

Adapter choices that are not in the data (each is stated in the report's `notes`): row numbers
become customer ids (Hillstrom, Criteo, MegaFon); every customer gets consent on the campaign's
channel; Hillstrom's men's and women's e-mails carry themes quality / novelty only so the agent can
tell them apart; a treated customer's visit is written as the message's click on Hillstrom and
Criteo (neither logged clicks; X5 and MegaFon have no click-like signal at all); X5 has no campaign
date (2019-03-19 10:00 Moscow is assumed) and no basket value (a campaign-window purchase is booked
at the client's own pre-period mean receipt); MegaFon and Criteo have no amounts, so a conversion is
worth the agent's own default (order value 50 x margin 0.4 = 20) and the cost is set so that
blanket sending breaks even on train, which means only targeting can make money there. X5, Criteo
and MegaFon give no purchase time, so a campaign-window purchase is booked 3.5 days after the send
(`WINDOW_PURCHASE_AFTER`, mid-week as the Hillstrom loader books its window spend; bench version 2).
Version 1 booked it at exactly +7 days, just outside the product's half-open 7-day learning window,
so no learner could see it. Scoring never reads these events, and the BEFORE code gives identical
numbers on both versions (re-run on version 2: X5 -1.7466, Criteo -0.0189, MegaFon -0.1007 against
the best simple policy, as below).

Differences from the family-A runs in [real_data/A-randomised.md](real_data/A-randomised.md), so the
numbers are close but not identical: the split is by id hash instead of a seeded permutation; X5,
Criteo and MegaFon are fixed hash samples, and the agent decides for the whole test half (A used
agent sub-samples on the feature-only sets); `prepare_day` gets the train customers' states only;
the campaign-window purchases are in the train history as events; only the production (click)
agent is scored, not the purchase-bundle variant; 4 seeds everywhere.

## Targets (from the brief)

On each dataset, on the profit reward:

1. **At least as good as the best simple policy** (none / send to all / segment rule) within its
   interval: `verdict.ties_or_beats_best_simple`, i.e. the upper end of "agent minus best simple"
   is at or above 0.
2. **Beat it where customer features carry signal**: where the T-learner beats the best simple
   policy (`feature_signal`), the agent should too (`verdict.beats_best_simple`: the lower end
   above 0).
3. **Never worse than send-to-all by more than the interval**: `verdict.not_worse_than_send_to_all`.
4. **Calibration within 2x** on Hillstrom and Criteo: `calibration.ratio` between 0.5 and 2 (about
   10x today).

The intervals above are 95% normal intervals, one per comparison, NOT corrected for the many
comparisons on the scoreboard: a result that only just excludes zero is suggestive, not proven. The
report now also carries the corrected version (see
[Multiple comparisons](#multiple-comparisons-holm-bonferroni)), and the targets are restated as
pre-registered, machine-checkable claims in [TARGETS.json](TARGETS.json) (below).

### Pre-registered claims (`docs/v2/TARGETS.json`)

`TARGETS.json` is a list of claims, each `{id, claim, command, threshold, passes}` with `passes: null`
until the command has been run and the threshold judged (`python -m scripts.v2_realbench.targets
--base-dir OUT [--write]` does that mechanically; the only claims set by hand are the two whose threshold
is the text "exit status 0", after their command exited 0). It was written before anything was run for it; a
test pins the SHA-256 of every claim's id, claim, command and threshold, so only `passes` can change. On
21 Oct 2026 24 of the 52 were judged (16 pass, 8 fail; the other 28, Gym stage claims at the CI size, were not
run): see [SCIENTIST.md](SCIENTIST.md#targets-met-and-missed). It covers:

* the real bench, per dev dataset: **non-inferior** to the best simple policy (the Holm-Bonferroni-
  corrected lower bound of agent minus best simple policy is above minus 2% of that policy's DR value;
  the 2% is `NON_INFERIORITY_SHARE` in `common.py`, fixed here and not tuned); **beats it where
  features carry signal** (the T-learner beats the best simple policy; MegaFon is that case, so its
  claim is `== true`, the others `!= false` because they are vacuous without signal); **calibration
  within 2x** on all four (the old target asked for it on two);
* the Gym, per scenario and stage (`verdict.stage_reached >= k` at the CI size, 3,000 x 3), with
  the kinds of GYM_BAR.md: Stages 1-3 where messages can make money, Stages 1-2 for `null_effect`
  and `complaint_storm`; `smoke` is a test fixture and has no claim;
* the sealed check: non-inferiority and calibration on `lenta` and `starbucks`, from one run each;
* that the real-calibrated Gym scenarios still hold the numbers fitted on the TRAIN halves
  (`gym_fit --check`), and that this step's tests pass.

## BEFORE: the current code (commit `f55c57b3`)

Command (run from a `git archive f55c57b3 -- autonomy` snapshot with this bench copied in, so the
numbers are the committed code while other builders edit the worktree):

```
python -m scripts.v2_realbench.run --dataset all --data-dir /home/user/datasets --json before_all.json \
    --note "BEFORE: autonomy/ at f55c57b3 (git archive snapshot)"
```

1,050 s wall time in all (Hillstrom 110 s, X5 278 s, Criteo 425 s including the one-off 300k-row
sample read, MegaFon 237 s), peak RSS 4.7 GB (X5). Run twice: every number in the two reports is
identical (the bench is deterministic given its salts and seeds).

| Target | Hillstrom | X5 | Criteo | MegaFon |
|---|---|---|---|---|
| Ties or beats the best simple policy | yes, barely (upper end +0.0004) | **no** | **no** | **no** |
| Beats it where features carry signal | no signal shown (no feature policy beats send-all men's) | signal not shown (feature policies +0.23 / +0.52 RUB over send-to-all, intervals include 0) | **no** (segment rule +0.019 over blanket, agent ~ blanket) | **no** (T-learner +0.58 over blanket, agent = blanket) |
| Not worse than send-to-all beyond the interval | yes (suggestive loss, -0.018) | yes, barely (upper end +0.05 RUB) | yes | yes |
| Calibration within 2x | **no: 11x** ($3.96 expected vs $0.36 measured) | contacts nobody | **no: 48x** ($0.213 vs $0.0044; measured interval [-0.007, +0.016], so at least 13x) | contacts nobody |

| Dataset | Agent value (DR) | Agent contact rate | Best simple policy (chosen on train) | Agent minus best simple [95% CI] | Agent minus send-to-all [95% CI] | Expected vs measured per contact |
|---|---:|---:|---|---:|---:|---:|
| hillstrom | 0.6584 | 97.8% | send to all: Mens E-Mail | -0.0182 [-0.0369, +0.0004] | -0.0182 [-0.0369, +0.0004] | 3.96 vs 0.3597 [0.1721, 0.5472] (x11.0) |
| x5 | 70.59 | 0.0% | segment rule (n_receipts+mean_receipt) | -1.75 [-3.24, -0.25] | -1.52 [-3.08, +0.05] | contacts nobody |
| criteo | 0.0317 | 97.5% | segment rule (f8) | -0.0189 [-0.0221, -0.0158] | +0.0002 [-0.0009, +0.0013] | 0.2131 vs 0.004437 [-0.006901, 0.01578] (x48.0) |
| megafon | 3.6324 | 0.0% | segment rule (X_5) | -0.1007 [-0.1723, -0.0291] | +0.0393 [-0.0609, +0.1396] | contacts nobody |

#### hillstrom: profit per customer on TEST (31,986 of 64,000; profit = 0.4 x spend (USD, 2 weeks) - 0.001 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.3049 | 0.3068 | +0.3066 [+0.2023, +0.4109] | +0.0000 [+0.0000, +0.0000] | +0.3517 [+0.1683, +0.5351] |
| send to all: Mens E-Mail | 100.0% | 0.6798 | 0.6767 | +0.6766 [+0.5182, +0.8349] | +0.3699 [+0.1803, +0.5595] | -0.0182 [-0.0369, +0.0004] |
| send to all: Womens E-Mail | 100.0% | 0.4700 | 0.4694 | +0.4685 [+0.3465, +0.5904] | +0.1618 [+0.0013, +0.3223] | +0.1899 [-0.0056, +0.3854] |
| segment rule (history_segment+mens+womens) | 97.7% | 0.6271 | 0.6259 | +0.6240 [+0.4757, +0.7723] | +0.3174 [+0.1365, +0.4983] | +0.0343 [-0.1035, +0.1721] |
| T-learner (logistic) | 99.8% | 0.6171 | 0.6223 | +0.6213 [+0.4716, +0.7709] | +0.3146 [+0.1322, +0.4970] | +0.0371 [-0.0752, +0.1493] |
| AGENT | 97.8% | 0.6629 | 0.6580 | +0.6584 [+0.5052, +0.8115] | +0.3517 [+0.1683, +0.5351] | +0.0000 [+0.0000, +0.0000] |


#### x5: profit per customer on TEST (50,044 of 100,000; profit = 0.25 x own pre-period mean receipt (RUB) x bought - 2.0 RUB per SMS)

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 70.72 | 70.59 | +70.59 [+69.49, +71.68] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] |
| send to all: SMS | 100.0% | 71.97 | 72.10 | +72.10 [+70.98, +73.22] | +1.52 [-0.05, +3.08] | -1.52 [-3.08, +0.05] |
| segment rule (n_receipts+mean_receipt) | 65.8% | 71.85 | 72.34 | +72.33 [+71.21, +73.45] | +1.75 [+0.25, +3.24] | -1.75 [-3.24, -0.25] |
| T-learner (logistic) | 55.5% | 72.72 | 72.63 | +72.63 [+71.50, +73.75] | +2.04 [+0.70, +3.38] | -2.04 [-3.38, -0.70] |
| AGENT | 0.0% | 70.72 | 70.59 | +70.59 [+69.49, +71.68] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] |


#### criteo: profit per customer on TEST (149,978 of 300,000; profit = 20 x conversion - 0.02937 per contact (break-even cost on train: 20 x train effect +0.00147))

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0276 | 0.0273 | +0.0273 [+0.0176, +0.0370] | +0.0000 [+0.0000, +0.0000] | +0.0043 [-0.0067, +0.0154] |
| send to all: treated | 100.0% | 0.0314 | 0.0314 | +0.0314 [+0.0254, +0.0375] | +0.0041 [-0.0073, +0.0155] | +0.0002 [-0.0009, +0.0013] |
| segment rule (f8) | 24.1% | 0.0511 | 0.0501 | +0.0506 [+0.0443, +0.0569] | +0.0233 [+0.0122, +0.0343] | -0.0189 [-0.0221, -0.0158] |
| T-learner (logistic) | 0.0% | 0.0276 | 0.0273 | +0.0273 [+0.0176, +0.0370] | +0.0000 [+0.0000, +0.0000] | +0.0043 [-0.0067, +0.0154] |
| AGENT | 97.5% | 0.0316 | 0.0317 | +0.0317 [+0.0257, +0.0376] | +0.0043 [-0.0067, +0.0154] | +0.0000 [+0.0000, +0.0000] |


#### megafon: profit per customer on TEST (99,889 of 200,000; profit = 20 x conversion - 1.03962 per contact (break-even cost on train: 20 x train effect +0.05198))

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 3.6100 | 3.6324 | +3.6324 [+3.5649, +3.6998] | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |
| send to all: treated | 100.0% | 3.6152 | 3.5930 | +3.5930 [+3.5188, +3.6673] | -0.0393 [-0.1396, +0.0609] | +0.0393 [-0.0609, +0.1396] |
| segment rule (X_5) | 50.0% | 3.7389 | 3.7328 | +3.7331 [+3.6614, +3.8047] | +0.1007 [+0.0291, +0.1723] | -0.1007 [-0.1723, -0.0291] |
| T-learner (logistic) | 48.9% | 4.1982 | 4.2105 | +4.2088 [+4.1345, +4.2831] | +0.5764 [+0.5060, +0.6469] | -0.5764 [-0.6469, -0.5060] |
| AGENT | 0.0% | 3.6100 | 3.6324 | +3.6324 [+3.5649, +3.6998] | +0.0000 [+0.0000, +0.0000] | +0.0000 [+0.0000, +0.0000] |

### Reading the BEFORE numbers

* **Hillstrom** reproduces family A: the agent e-mails 97.8% (almost all the men's e-mail), about
  as much as send-all men's (-0.018 per customer, 95% CI -0.037 to +0.0004; suggestive only) and
  expects $3.96 per e-mail against $0.36 measured, 11x. Nothing beats send-all men's here: neither
  the segment rule nor the T-learner (features carry no signal that pays at $0.001 per e-mail).
* **X5**: the production agent contacts nobody. The SMS campaign logged no clicks, so its warm start
  learns a click rate of about 0 from 25,199 logged sends ("Left alone: no message is worth more
  than the cost of contacting (best -$2.00 vs $0.00)"). Send-to-all adds +1.52 RUB per client
  (CI -0.05 to +3.08); the segment rule chosen on train (receipts x mean receipt, 65.8% contacted)
  beats the agent by 1.75 RUB (CI 0.25 to 3.24) and the T-learner by 2.04.
* **Criteo** (300k-row sample, break-even cost 0.0294 per contact): the agent contacts 97.5% and
  ties blanket sending (+0.0002), as designed when its value is 48x too high; a one-feature segment
  rule (`f8`, 24.1% contacted) earns +0.019 per customer more (CI 0.016 to 0.022). The T-learner
  contacts nobody: with about 0.3% conversions, its per-arm logistic models on 150k train rows
  find no customer worth the cost, so it is a weak baseline here.
* **MegaFon** (200k-row sample, break-even cost 1.04): the agent contacts nobody (no clicks, as X5),
  which ties send-to-none and send-to-all (both break even by construction); the T-learner earns
  +0.58 per customer over them (CI 0.51 to 0.65) by reading the 50 features, which the agent cannot
  use today (they reach `preferences`; nothing reads them).

Compared with family A (same failures, slightly different numbers for the reasons listed above):
Hillstrom click agent vs send-all men's -0.014 there, -0.018 here; Criteo 97.6% vs 97.5% contacted;
X5 and MegaFon 0% contacted in both.

## AFTER the fix wave

The fix wave (stream notes: [streams/realfix.md](streams/realfix.md)) changed the agent's warm
start in three ways that this scoreboard sees:

* **Calibrated value** (`autonomy/learning/calibration.py`, `AgentPolicy._calibrate`): a past
  campaign that was randomised (constant logged probability per arm, a left-out pool of the size the
  design implies, balanced pre-period buying) is measured in money against its left-out customers,
  and that sets the lift per click, or per message on a channel that logs no clicks (X5's SMS,
  MegaFon).
* **History reaches the agent** (`autonomy/runtime/service.py`): the runtime streams every
  customer's history to a new policy and calls `prepare_day`; the bench already did both.
* **Customer-level uplift** (`autonomy/uplift`, wired in `AgentPolicy._warm_start`): the latest
  closed past campaign, with the customers' features (`CustomerState.preferences`) and behaviour,
  fits a per-customer uplift model, cross-fitted and calibrated out of sample; `GoalBidder` blends
  its score with the response model by precision. No usable campaign, no model: bids are unchanged.

Command (live worktree, the fix wave's code before it was committed; the report records
`git.dirty: true` on top of `f55c57b3`):

```
python -m scripts.v2_realbench.run --dataset all --data-dir /home/user/datasets --json after_final.json --quiet
```

1,247 s wall time in all (Hillstrom 139 s, X5 325 s, Criteo 429 s, MegaFon 354 s), peak RSS 4.97 GB
(X5; BEFORE 4.7 GB), 0.59-0.81 ms per decision (BEFORE 0.39-0.62). Every agent number is identical
to an earlier full run of the same code in which the warm start rebuilt the digest's purchases as
full `Event`s (peak 6.3 GB), and to the uplift builder's runs through a scratch shim. The BEFORE
code was also re-run on this bench version (X5, Criteo, MegaFon; Hillstrom's adapter did not
change): identical to the BEFORE table above.

### Targets

All intervals are 95%, paired, one per comparison and NOT corrected for multiple comparisons:
suggestive.

| Target | Hillstrom | X5 | Criteo | MegaFon |
|---|---|---|---|---|
| Ties or beats the best simple policy | **no**: -0.0253 [-0.0476, -0.0030] vs send-all men's (was -0.0182 [-0.0369, +0.0004]) | **yes** (was no): -0.10 [-0.55, +0.35] vs the segment rule | **yes** (was no): +0.0018 [-0.0012, +0.0048] vs the f8 segment rule | **yes, beats it** (was no): +0.463 [+0.395, +0.531] vs the X_5 segment rule |
| Beats it where features carry signal | no feature signal (segment rule and T-learner below send-all men's) | signal not shown (T-learner minus best simple +0.29 [-0.49, +1.08]) | **no**: ties the f8 rule, does not beat it (signal shown: f8 +0.019 over blanket) | **yes**: +0.463 over the segment rule; ties the T-learner (-0.013 [-0.043, +0.017]) |
| Not worse than send-to-all beyond the interval | **no** (suggestive loss of 0.025 per customer, about 4%) | yes: +0.13 [-0.25, +0.50] | yes, beats it: +0.0209 [+0.0167, +0.0252] | yes, beats it: +0.603 [+0.531, +0.674] |
| Calibration within 2x (target on Hillstrom and Criteo) | **yes**: 1.46x ($0.511 expected vs $0.351 [0.166, 0.535]; was 11x) | 1.33x (2.89 vs 2.17 [0.22, 4.11] RUB; was: contacts nobody) | **no, just outside**: 0.48x (0.0999 expected vs 0.2095 [0.124, 0.295]; was 48x). It now under-predicts the 12% it contacts | 0.84x (1.03 vs 1.22 [1.08, 1.37]; was: contacts nobody) |

Met: X5 and MegaFon on every target; Criteo on ties-or-beats and send-to-all; Hillstrom on
calibration. Not met: Hillstrom ties-or-beats / not-worse-than-send-to-all (the same comparison
there, since the best simple policy is send-all men's), Criteo "beat the f8 rule", Criteo
calibration within 2x.

**Why Hillstrom misses.** The agent sends 95.3% of test customers the men's e-mail, like the best
policy; what it loses is exploration. 1.7% are left alone (the envelope's 5% exploration floor)
and 3.1% get the women's e-mail by Thompson sampling, because the uplift model's honest
arm estimates (men's +0.0062 +/- 0.0012 vs women's +0.0034 +/- 0.0011 conversions per e-mail, z
about 1.7) leave a few percent chance that the women's e-mail is better. A one-shot scoreboard
charges exploration in full and credits none of what it learns. With the brain fixes alone (no
uplift score), the agent tied send-all men's at -0.0182 [-0.0369, +0.0004]. No feature
heterogeneity validated out of sample (slopes -0.21 +/- 0.42 and -0.11 +/- 0.34, so 0 is used).
Lowering exploration to pass this would be tuning to a dataset; it is left as is.

**Why Criteo misses calibration.** The uplift model's calibrated spread (slope 1.19 +/- 0.31 on
train, 1.11 used after shrinkage) understates the top customers it contacts: expected 0.10 per
contact against 0.21 measured. A plausible cause, not tested: the slope is fitted on single-half
out-of-fold scores while scoring averages both halves, which spread less. Criteo has only 37 control
conversions on train, so this slope is the noisiest of the four.

### What the agent learned (from the report's `agent.learned`)

| Dataset | Calibration (past campaign vs its left-out pool) | Uplift model |
|---|---|---|
| Hillstrom | per click: 0.0144 +/- 0.0031 (prior 0.25); men's e-mail +$0.244 margin per recipient (se 0.064, 10,596 vs 10,707 left out), women's +$0.176 (se 0.065) | men's +0.0062 +/- 0.0012, women's +0.0034 +/- 0.0011 conversions per e-mail; no heterogeneity used |
| X5 | per SMS (no clicks): +4.52 RUB margin per recipient (se 0.79, 25,199 vs 24,757), lift 0.038 +/- 0.007 per send | +0.0348 +/- 0.0039 buyers per SMS, slope 0.52 +/- 0.11 (0.50 used) |
| Criteo | per click: +0.0294 per recipient (se 0.0063, 127,683 vs 22,339), lift 0.030 +/- 0.006 per click | +0.0012 +/- 0.0003, slope 1.19 +/- 0.31 (1.11 used) |
| MegaFon | per message (no clicks): +1.04 per recipient (se 0.051, 50,308 vs 49,803), lift 0.052 +/- 0.003 | +0.0512 +/- 0.0025, slope 1.13 +/- 0.04 (1.12 used) |

### AFTER tables

| Dataset | Agent value (DR) | Agent contact rate | Best simple policy (chosen on train) | Agent minus best simple [95% CI] | Agent minus send-to-all [95% CI] | Expected vs measured per contact |
|---|---:|---:|---|---:|---:|---:|
| hillstrom | 0.6513 | 98.3% | send to all: Mens E-Mail | -0.0253 [-0.0476, -0.0030] | -0.0253 [-0.0476, -0.0030] | 0.5113 vs 0.3505 [0.1656, 0.5355] (x1.5) |
| x5 | 72.23 | 75.8% | segment rule (n_receipts+mean_receipt) | -0.10 [-0.55, +0.35] | +0.13 [-0.25, +0.50] | 2.885 vs 2.168 [0.2237, 4.113] (x1.3) |
| criteo | 0.0524 | 12.0% | segment rule (f8) | +0.0018 [-0.0012, +0.0048] | +0.0209 [+0.0167, +0.0252] | 0.09988 vs 0.2095 [0.1236, 0.2954] (x0.5) |
| megafon | 4.1958 | 46.1% | segment rule (X_5) | +0.4627 [+0.3946, +0.5308] | +0.6028 [+0.5312, +0.6743] | 1.026 vs 1.223 [1.075, 1.371] (x0.8) |

#### hillstrom: profit per customer on TEST (31,986 of 64,000; profit = 0.4 x spend (USD, 2 weeks) - 0.001 per e-mail)

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.3049 | 0.3068 | +0.3066 [+0.2023, +0.4109] | +0.0000 [+0.0000, +0.0000] | +0.3446 [+0.1628, +0.5265] |
| send to all: Mens E-Mail | 100.0% | 0.6798 | 0.6767 | +0.6766 [+0.5182, +0.8349] | +0.3699 [+0.1803, +0.5595] | -0.0253 [-0.0476, -0.0030] |
| send to all: Womens E-Mail | 100.0% | 0.4700 | 0.4694 | +0.4685 [+0.3465, +0.5904] | +0.1618 [+0.0013, +0.3223] | +0.1828 [-0.0102, +0.3759] |
| segment rule (history_segment+mens+womens) | 97.7% | 0.6271 | 0.6259 | +0.6240 [+0.4757, +0.7723] | +0.3174 [+0.1365, +0.4983] | +0.0273 [-0.1090, +0.1635] |
| T-learner (logistic) | 99.8% | 0.6171 | 0.6223 | +0.6213 [+0.4716, +0.7709] | +0.3146 [+0.1322, +0.4970] | +0.0300 [-0.0827, +0.1427] |
| AGENT | 98.3% | 0.6554 | 0.6512 | +0.6513 [+0.5000, +0.8026] | +0.3446 [+0.1628, +0.5265] | +0.0000 [+0.0000, +0.0000] |

Agent: decisions {'act': 125805, 'nothing': 2139}, warm start used 21307 logged sends from 32,014 train customers (125,475 events), 0.825 ms per decision, 4 seeds. Verdict: {'ties_or_beats_best_simple': False, 'beats_best_simple': False, 'not_worse_than_send_to_all': False, 'ties_or_beats_best_simple_on_test': False, 'calibration_within_2x': True, 'calibration_expected_inside_measured_ci': True, 'intervals': '95% normal, paired, NOT corrected for multiple comparisons: suggestive'}.

#### x5: profit per customer on TEST (50,044 of 100,000; profit = 0.25 x own pre-period mean receipt (RUB) x bought - 2.0 RUB per SMS)

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 70.72 | 70.59 | +70.59 [+69.49, +71.68] | +0.00 [+0.00, +0.00] | +1.64 [+0.17, +3.12] |
| send to all: SMS | 100.0% | 71.97 | 72.10 | +72.10 [+70.98, +73.22] | +1.52 [-0.05, +3.08] | +0.13 [-0.25, +0.50] |
| segment rule (n_receipts+mean_receipt) | 65.8% | 71.85 | 72.34 | +72.33 [+71.21, +73.45] | +1.75 [+0.25, +3.24] | -0.10 [-0.55, +0.35] |
| T-learner (logistic) | 55.5% | 72.72 | 72.63 | +72.63 [+71.50, +73.75] | +2.04 [+0.70, +3.38] | -0.40 [-1.08, +0.29] |
| AGENT | 75.8% | 71.86 | 72.24 | +72.23 [+71.14, +73.32] | +1.64 [+0.17, +3.12] | +0.00 [+0.00, +0.00] |

Agent: decisions {'act': 151786, 'nothing': 48390}, warm start used 25199 logged sends from 49,956 train customers (1,158,574 events), 0.729 ms per decision, 4 seeds. Verdict: {'ties_or_beats_best_simple': True, 'beats_best_simple': False, 'not_worse_than_send_to_all': True, 'ties_or_beats_best_simple_on_test': True, 'calibration_within_2x': True, 'calibration_expected_inside_measured_ci': True, 'intervals': '95% normal, paired, NOT corrected for multiple comparisons: suggestive'}.

#### criteo: profit per customer on TEST (149,978 of 300,000; profit = 20 x conversion - 0.02937 per contact (break-even cost on train: 20 x train effect +0.00147))

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 0.0276 | 0.0273 | +0.0273 [+0.0176, +0.0370] | +0.0000 [+0.0000, +0.0000] | +0.0250 [+0.0148, +0.0353] |
| send to all: treated | 100.0% | 0.0314 | 0.0314 | +0.0314 [+0.0254, +0.0375] | +0.0041 [-0.0073, +0.0155] | +0.0209 [+0.0167, +0.0252] |
| segment rule (f8) | 24.1% | 0.0511 | 0.0501 | +0.0506 [+0.0443, +0.0569] | +0.0233 [+0.0122, +0.0343] | +0.0018 [-0.0012, +0.0048] |
| T-learner (logistic) | 0.0% | 0.0276 | 0.0273 | +0.0273 [+0.0176, +0.0370] | +0.0000 [+0.0000, +0.0000] | +0.0250 [+0.0148, +0.0353] |
| AGENT | 12.0% | 0.0529 | 0.0519 | +0.0524 [+0.0458, +0.0590] | +0.0250 [+0.0148, +0.0353] | +0.0000 [+0.0000, +0.0000] |

Agent: decisions {'nothing': 528201, 'act': 71711}, warm start used 127683 logged sends from 150,022 train customers (435,161 events), 0.603 ms per decision, 4 seeds. Verdict: {'ties_or_beats_best_simple': True, 'beats_best_simple': False, 'not_worse_than_send_to_all': True, 'ties_or_beats_best_simple_on_test': True, 'calibration_within_2x': False, 'calibration_expected_inside_measured_ci': False, 'intervals': '95% normal, paired, NOT corrected for multiple comparisons: suggestive'}.

#### megafon: profit per customer on TEST (99,889 of 200,000; profit = 20 x conversion - 1.03962 per contact (break-even cost on train: 20 x train effect +0.05198))

| Policy | Contacted | IPS | SNIPS | DR [95% CI] | DR gain vs send-to-none | Agent minus this [95% CI] |
|---|---:|---:|---:|---:|---:|---:|
| send to none | 0.0% | 3.6100 | 3.6324 | +3.6324 [+3.5649, +3.6998] | +0.0000 [+0.0000, +0.0000] | +0.5634 [+0.4953, +0.6316] |
| send to all: treated | 100.0% | 3.6152 | 3.5930 | +3.5930 [+3.5188, +3.6673] | -0.0393 [-0.1396, +0.0609] | +0.6028 [+0.5312, +0.6743] |
| segment rule (X_5) | 50.0% | 3.7389 | 3.7328 | +3.7331 [+3.6614, +3.8047] | +0.1007 [+0.0291, +0.1723] | +0.4627 [+0.3946, +0.5308] |
| T-learner (logistic) | 48.9% | 4.1982 | 4.2105 | +4.2088 [+4.1345, +4.2831] | +0.5764 [+0.5060, +0.6469] | -0.0130 [-0.0429, +0.0169] |
| AGENT | 46.1% | 4.1741 | 4.1993 | +4.1958 [+4.1229, +4.2687] | +0.5634 [+0.4953, +0.6316] | +0.0000 [+0.0000, +0.0000] |

Agent: decisions {'nothing': 215502, 'act': 184054}, warm start used 50308 logged sends from 100,111 train customers (270,975 events), 0.697 ms per decision, 4 seeds. Verdict: {'ties_or_beats_best_simple': True, 'beats_best_simple': True, 'not_worse_than_send_to_all': True, 'ties_or_beats_best_simple_on_test': True, 'calibration_within_2x': True, 'calibration_expected_inside_measured_ci': False, 'intervals': '95% normal, paired, NOT corrected for multiple comparisons: suggestive'}.

## Multiple comparisons (Holm-Bonferroni)

The scoreboard makes many comparisons on one set of customers (the agent against every other policy,
on every dataset), so a 95% interval that only just excludes zero is expected by chance somewhere. The
report (`add_multiplicity` in `common.py`, called by `run.py` after every dataset) now holds, for each
"agent minus policy" paired DR difference, both the **raw** and the **corrected** statistics:

* the raw 95% interval and its two-sided normal p-value (from the interval's own standard error);
* the **Holm** step-down adjusted p-value (sort ascending, multiply the i-th smallest by m - i, take the
  running maximum, cap at 1; it controls the chance of any false rejection at 5% whatever the
  dependence between comparisons, and they share customers, so they are dependent);
* a **Bonferroni-widened interval** at level 1 - 0.05/m, which is simultaneously valid for all m and is
  the conservative companion of Holm's test (Holm rejects at least as often; an exact Holm-consistent
  interval is not used);
* whether the difference is significant raw and after Holm.

Two families are reported. `datasets.<d>.multiplicity` holds a dataset's own comparisons; the report's
`multiplicity` holds every comparison of every dataset in the run, and it is the one the corrected
verdicts use (`datasets.<d>.verdict_corrected`: `non_inferior_to_best_simple`,
`beats_best_simple_where_signal`, `calibration_within_2x`). A single-dataset run therefore has a smaller
family than a full run, so the full command is the one of record. `render` prints both tables.

Applied offline to the saved report of the AFTER run (`after_final.json`, the fix wave's full run on
`f55c57b3` plus the uncommitted fix wave; no new agent run, the family is its 17 comparisons, so the
corrected interval has level 0.9971):

| Comparison | Raw diff [95% CI] | Raw p | Holm p | Corrected interval | Significant (raw / Holm) |
|---|---:|---:|---:|---:|---|
| hillstrom: agent minus send to none | +0.3446 [+0.1628, +0.5265] | 0.0002 | 0.0022 | +0.3446 [+0.0687, +0.6206] | yes / yes |
| hillstrom: agent minus send to all: Mens E-Mail | -0.0253 [-0.0476, -0.0030] | 0.0265 | 0.2650 | -0.0253 [-0.0592, +0.0086] | yes / no |
| hillstrom: agent minus send to all: Womens E-Mail | +0.1828 [-0.0102, +0.3759] | 0.0635 | 0.5077 | +0.1828 [-0.1101, +0.4758] | no / no |
| hillstrom: agent minus segment rule | +0.0273 [-0.1090, +0.1635] | 0.6949 | 1.0000 | +0.0273 [-0.1794, +0.2339] | no / no |
| hillstrom: agent minus T-learner | +0.0300 [-0.0827, +0.1427] | 0.6016 | 1.0000 | +0.0300 [-0.1410, +0.2010] | no / no |
| x5: agent minus send to none | +1.6441 [+0.1696, +3.1185] | 0.0289 | 0.2650 | +1.6441 [-0.5931, +3.8812] | yes / no |
| x5: agent minus send to all: SMS | +0.1284 [-0.2479, +0.5047] | 0.5036 | 1.0000 | +0.1284 [-0.4425, +0.6993] | no / no |
| x5: agent minus segment rule | -0.1026 [-0.5541, +0.3490] | 0.6562 | 1.0000 | -0.1026 [-0.7876, +0.5825] | no / no |
| x5: agent minus T-learner | -0.3962 [-1.0842, +0.2919] | 0.2591 | 1.0000 | -0.3962 [-1.4401, +0.6478] | no / no |
| criteo: agent minus send to none | +0.0250 [+0.0148, +0.0353] | 1.8e-06 | 2.3e-05 | +0.0250 [+0.0095, +0.0406] | yes / yes |
| criteo: agent minus send to all: treated | +0.0209 [+0.0167, +0.0252] | 2.8e-22 | 3.9e-21 | +0.0209 [+0.0145, +0.0274] | yes / yes |
| criteo: agent minus segment rule (f8) | +0.0018 [-0.0012, +0.0048] | 0.2466 | 1.0000 | +0.0018 [-0.0028, +0.0064] | no / no |
| criteo: agent minus T-learner | +0.0250 [+0.0148, +0.0353] | 1.8e-06 | 2.3e-05 | +0.0250 [+0.0095, +0.0406] | yes / yes |
| megafon: agent minus send to none | +0.5634 [+0.4953, +0.6316] | 4.3e-59 | 6.9e-58 | +0.5634 [+0.4601, +0.6668] | yes / yes |
| megafon: agent minus send to all: treated | +0.6028 [+0.5312, +0.6743] | 2.9e-61 | 4.9e-60 | +0.6028 [+0.4942, +0.7113] | yes / yes |
| megafon: agent minus segment rule (X_5) | +0.4627 [+0.3946, +0.5308] | 1.7e-40 | 2.6e-39 | +0.4627 [+0.3594, +0.5660] | yes / yes |
| megafon: agent minus T-learner | -0.0130 [-0.0429, +0.0169] | 0.3942 | 1.0000 | -0.0130 [-0.0583, +0.0323] | no / no |

What the correction changes. Two comparisons that were significant raw stop being so: Hillstrom's
loss to send-all men's (raw p 0.027, Holm 0.27, so "the agent loses to the best simple policy there" is
not proven) and X5's gain over sending nothing (raw p 0.029, Holm 0.27, so "SMS targeting beats silence
on X5" is not proven either). What survives is large: Hillstrom's gain over sending nothing, Criteo's
gains over sending nothing, over sending to all and over the T-learner (which contacts nobody there), and
MegaFon's gains over every policy except the T-learner, which the agent ties. Against the
pre-registered claims the same report gives (tolerance 2% of the best simple policy's DR value):
non-inferior on X5 (corrected low -0.79 against a tolerance of 1.45) and MegaFon (+0.36, and it beats it,
as the signal claim asks), not non-inferior on Hillstrom (-0.059 against 0.0135) or Criteo (-0.0028
against 0.0010); calibration within 2x on Hillstrom, X5 and MegaFon but not Criteo (0.48x). These are the
AFTER numbers re-judged by the new rule, not a new run.

## CI: the scoreboard workflow

### Downloading the data

`python -m scripts.v2_realbench.fetch --data-dir DIR [--dataset NAME]` downloads each file with the
standard library and checks its SHA-256 (a wrong checksum deletes the file and fails). All hosts
are public and reachable from GitHub Actions (two public S3 buckets and a GitHub release); every
URL answered 200 with the recorded size on 21 Oct 2026 from this environment. The official hosts
(`www.minethatdata.com`, `go.criteo.net`, ods.ai) were blocked here, so these are the mirrors
recorded in [real_data/CATALOGUE.md](real_data/CATALOGUE.md) and each dataset's `SOURCE.txt`; the
Hillstrom and Criteo checksums match `autonomy/proof/replay/fetch.py` and libuplift respectively.

| Dataset | File | URL | Bytes | SHA-256 |
|---|---|---|---:|---|
| Hillstrom | `hillstrom/hillstorm_no_indices.csv.gz` | https://hillstorm1.s3.us-east-2.amazonaws.com/hillstorm_no_indices.csv.gz | 443,070 | `bab6578f60db5d792f1c2372c502f029152a5249cf5ea84390f3b7f885d7234f` |
| X5 | `x5_retailhero/uplift_train.csv.gz` | https://sklift.s3.eu-west-2.amazonaws.com/uplift_train.csv.gz | 1,182,423 | `0a218e864a1ec1dfe26e980a082b81d5f46d199c4a6ae32abbaa95265af1c2c7` |
| X5 | `x5_retailhero/clients.csv.gz` | https://sklift.s3.eu-west-2.amazonaws.com/clients.csv.gz | 7,637,755 | `bfca1719b862735ae9816ed9bbba6924931c6a4dff761c481efe9addff458429` |
| X5 | `x5_retailhero/purchases.csv.gz` | https://sklift.s3.eu-west-2.amazonaws.com/purchases.csv.gz | 669,979,268 | `3d57519fa92b553fd1e947c120add475cc42e0c64c323a52cef5aebe8645c8e2` |
| Criteo | `criteo_uplift/criteo-research-uplift-v2.1.csv.gz` | https://github.com/jszymon/uplift_sklearn_data/releases/download/Criteo/criteo-research-uplift-v2.1.csv.gz | 311,422,618 | `2716e1bf0fd157a93b5bf86924d9088419dfbac2022c6cd90030220634f616dc` |
| MegaFon | `megafon/megafon_dataset.csv.gz` | https://sklift.s3.eu-west-2.amazonaws.com/megafon_dataset.csv.gz | 275,944,146 | `cdcb2d052b90f8eefa75937d5540f114d0748ea231d95e2778dd6760478e4a00` |

About 1.27 GB in all; X5's purchases file is half of it. The bench writes two caches in
`<data-dir>/_cache/` (the X5 receipts, about 0.5 GB, and the Criteo sample), never in the repo.

`.github/workflows/realbench.yml` runs on a push to `version-2` whose commit message contains
`[real]`, or by hand (`workflow_dispatch`). Python 3.11 with the light deps only (pydantic, numpy,
pandas, sqlalchemy). One job per dataset (a matrix, so each stays well under its 45-minute limit
and holds one dataset in memory): it restores the raw downloads from `actions/cache` (keyed on
`fetch.py`, which holds the checksums), downloads what is missing with `fetch` (three tries),
runs the scoreboard with `shell: bash` so a crash fails the job, fails on a dataset that recorded
an error (out of memory), and uploads the JSON and markdown. A summary job merges the four reports
into `realbench-report` and writes the tables to the run summary. It is report-only: nothing reads
the verdicts, and it never runs on pull requests, so it gates no merge. The bench's derived caches
are rebuilt every run so they always match the code. Whether every host is reachable from a GitHub
runner was checked from this environment only.

Licences: Hillstrom free for research use (MineThatData 2008); Criteo CC BY-NC-SA 4.0; the X5 and
MegaFon mirrors state no licence (scikit-uplift S3 copies of the ods.ai contest files).

## Sealed runs

Two public randomised campaigns are kept **sealed**: the Lenta uplift dataset (`lenta`) and the Udacity
Starbucks promotion experiment (`starbucks`, folder `starbucks_promo`). Nothing is fitted, tuned or
chosen on them. They are for one final check that what we found on the four dev datasets was not luck.

* **Before the final check** only a load check is allowed (`python -m scripts.v2_realbench.sealed`): row
  counts, column names and the treatment share. It reads the header and the treatment column of each
  file, never an outcome column. On 21 Oct 2026: Lenta 687,029 rows, 195 columns, treatment share 0.7509
  (`group == test`); Starbucks 126,184 rows (84,534 in `training.csv`, 41,650 in `Test.csv`), 10 columns
  each, treatment share 0.5002 (`Promotion == Yes`: 0.5011 and 0.4982 by file).
* **Adapters exist** (`adapters.py`: `lenta`, `starbucks`; tested only on inline fixtures with the real
  files' column names). Lenta: a fixed 200,000-row sample (smallest hash of the row number), treated =
  `group == test`, conversion = `response_att`, `response_sms` and `response_viber` never features, no
  amounts, so a conversion is worth the agent's default 20 and the cost is the break-even cost on train
  (as for MegaFon and Criteo); the treated share is estimated from the data. Starbucks: both files pooled,
  50/50, and the exercise's own economics (a purchase is worth $10, a promotion costs $0.15).
* **The seal is a flag.** `run.py --dataset lenta|starbucks` exits 2 and reads nothing unless
  `--sealed-final` is passed, which also refuses `--limit`, `--seeds` other than 4 and `--no-features`
  (a sealed run is the scoreboard of record, not a smoke run). The run appends a row below to this
  table BEFORE reading the data, so a crashed run still counts as opened. The sealed claims in
  `TARGETS.json` are judged on that one run and are never re-run after a change to get a better number.
* **Not pristine, and why that matters.** Earlier family-A work (before sealing was decided) already ran
  agent code on both files: `docs/v2/real_data/A-randomised.md` has `lenta` and `starbucks` sections, and
  `/home/user/datasets/_results/` holds `a_lenta.*` and `a_starbucks.*`. Their numbers were not used for
  any choice in this step and these files were not opened, but anyone who read them has seen outcomes of
  these data; the final check is a check of the *current* code and settings, fixed here, not a claim of
  never-seen data.

| Date (UTC) | Git sha | Dataset | Command |
|---|---|---|---|
| 2026-10-21 16:32:02 | cecbf4d8af54 (dirty) | lenta | `python -m scripts.v2_realbench.run --dataset lenta --data-dir /home/user/datasets --sealed-final --json /tmp/claude-0/-home-user/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/final/out/sealed_lenta.json --quiet` |
| 2026-10-21 16:40:33 | cecbf4d8af54 (dirty) | lenta | `python -m scripts.v2_realbench.run --dataset lenta --policy scientist --data-dir /home/user/datasets --sealed-final --json /tmp/claude-0/-home-user/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/final/out/sealed_lenta_scientist.json --quiet` |
| 2026-10-21 16:53:48 | cecbf4d8af54 (dirty) | starbucks | `python -m scripts.v2_realbench.run --dataset starbucks --data-dir /home/user/datasets --sealed-final --json /tmp/claude-0/-home-user/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/final/out/sealed_starbucks.json --quiet` |
| 2026-10-21 16:56:53 | cecbf4d8af54 (dirty) | starbucks | `python -m scripts.v2_realbench.run --dataset starbucks --policy scientist --data-dir /home/user/datasets --sealed-final --json /tmp/claude-0/-home-user/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/final/out/sealed_starbucks_scientist.json --quiet` |

The four rows are the four sealed runs of this step: the agent and the Scientist (`--policy scientist`, the fixed
candidate list; the AI roles were not run on sealed data) on each dataset, each opened once and never repeated.
`cecbf4d8af54 (dirty)` is the parent commit plus the uncommitted working tree that was then committed as the commit
that carries these rows (`autonomy/` and `scripts/` were not edited between the first sealed run and that commit).
The reports are `sealed_lenta.json`, `sealed_lenta_scientist.json`, `sealed_starbucks.json` and
`sealed_starbucks_scientist.json` of the run, kept in the session scratchpad, not in the repo.

### Sealed result (21 Oct 2026, one run each, nothing tuned afterwards)

| Dataset | Policy | What it did | Value (DR) | Contacted | Minus best simple [95% CI] | Corrected low vs tolerance | Non-inferior | Calibration within 2x |
|---|---|---|---:|---:|---:|---|---|---|
| lenta | agent | learning agent | 2.0838 | 5.1% | +0.0060 [-0.0781, +0.0901] vs send to all | -0.1012 vs 0.0416 | **no** | **no ratio**: measured gain per contact is not above 0 (expected -0.0400, measured -0.0369 [-0.2462, +0.1723]) |
| lenta | Scientist | kept the safe default, send nothing (segment rule +0.0503, Holm p 0.214; uplift model +0.0001; agent -0.0023) | 2.0857 | 0.0% | +0.0079 [-0.0802, +0.0959] vs send to all | -0.1078 vs 0.0416 | **no** | not applicable (contacts nobody) |
| starbucks | agent | learning agent | 0.0839 | 31.8% | -0.0018 [-0.0081, +0.0045] vs the V4+V5 segment rule | -0.0098 vs 0.0017 | **no** | **yes**, 0.86x (0.0382 expected vs 0.0446 [0.0099, 0.0793]) |
| starbucks | Scientist | kept the safe default, send nothing (segment rule +0.0132, Holm p 0.252; uplift model +0.0083; agent +0.0079) | 0.0697 | 0.0% | -0.0160 [-0.0287, -0.0033] vs the V4+V5 segment rule | -0.0327 vs 0.0017 | **no** | not applicable (contacts nobody) |

Corrected = Bonferroni-widened over the run's own comparisons (4 for the agent, 5 for the Scientist, so the level is
0.9875 and 0.99), the family of a single-dataset run. The four pre-registered sealed claims of
[TARGETS.json](TARGETS.json) are therefore: `sealed.lenta.non_inferior` FAIL, `sealed.lenta.calibration` FAIL (the
rule asks for `within_2x == true`; the report says it cannot be computed because the agent's measured gain per
contact is not above zero), `sealed.starbucks.non_inferior` FAIL, `sealed.starbucks.calibration` PASS.

What this says, in plain words:

* **Lenta has no measurable profit effect at the bench's break-even cost.** Sending to everyone, to nobody, to 5.1% (the
  agent) and to 45% (the T-learner) are all within +/-0.03 of 2.08 per customer, and the best simple policy chosen on
  train (send to all, 2.0574 against 2.0574 for send-to-none: a tie to the fourth decimal) differs from send-to-none by
  -0.0079 [-0.0959, +0.0802]. The pre-registered rule needs the corrected interval of "policy minus best simple" to stay
  above minus 2% of 2.08 = 0.042, and at this sample size that interval's corrected half-width is about 0.11, so a
  policy that merely ties the best simple one cannot pass. Both policies fail the rule there; neither is measurably
  worse (the points are +0.006 and +0.008).
* **Starbucks has signal, and the agent finds most of it.** The agent earns 0.0839 against the best segment rule's
  0.0857 and sending to all loses 0.057 per customer (the exercise's promotion costs more than it brings on average);
  the agent beats send-to-all by +0.0710 [+0.0592, +0.0828] and its calibration is within 2x. It fails the 2% rule
  because the tolerance there is 0.0017 and the corrected interval's half-width is about 0.008.
* **The Scientist's safe default cost it on Starbucks.** Its held-out half of TRAIN had 31,494 customers, the segment
  rule gained +0.0132 there (raw p 0.084, Holm 0.252), so no candidate was proven and it kept sending nothing. On TEST that
  is 0.0160 per customer below the segment rule and 0.0142 below the agent (raw intervals exclude zero). This is the
  rule working as written: it does not act on a gain it cannot prove, and a shop this size cannot prove +0.013 with half
  of its customers. It is the price of "held-out evidence decides" at 60,000 customers.
* **Not pristine.** Earlier family-A work had already run agent code on both files (see above); this is a check of the
  current code and settings, not of never-seen data.
