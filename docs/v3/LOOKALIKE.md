# Look-alike learning

Plan-v3 foundation B ([`plan-v3.md`](../../plan-v3.md) section 3.B): trust the crowd until you know the person.
Code: [`autonomy/decide/lookalike.py`](../../autonomy/decide/lookalike.py). Opt-in; the default agent is unchanged.

## What it is

`LookalikeResponseModel` implements the `ResponseModel` protocol of `autonomy/decide/models.py`
(`predict` gives the mean and spread of the uplift in purchase chance, `treated_rate`, `update`), so it drops in
where `BetaResponseModel` does. Where the bucket model keeps one record per segment, this one learns purchase
chance from the customer's profile, so a thin per-person record borrows strength from similar customers.
The tools it needs are numpy only; nothing under `autonomy/` imports scikit-learn or lightgbm
(`tests/v3/lookalike/test_agent.py` checks every file).

## Method

1. **Profile.** `profile_of(state)` takes a base set from `CustomerState` fields (recency, tenure, orders, revenue,
   order value, frequency, contacts in 7 and 30 days, offer sensitivity, segment, and per channel: delivered,
   click rate, days since engaged; `FEATURES` lists them) and, when `state.features` is non-empty (the nightly
   profile, foundation A), those features too. A profile is stamped with the moment it was taken: the later of
   `state.as_of` and `state.features_as_of` when it carries nightly features, so features built after a window
   opened make that window's outcome refused. With no first-seen date, tenure and frequency are unknown (read as
   the training average), not a brand-new customer's. A feature present in training and missing when predicting
   also reads as the training average.
2. **The crowd.** One logistic model of "bought within the window", fitted on closed outcomes:
   `logit p(control) = baseline(profile)` and `logit p(treated) = baseline(profile) + effect(profile, channel,
   offer, theme, format)`. Uplift is the difference of the two probabilities. Only customers we did not contact
   (the holdout) tell the baseline from the effect, so with no control there is no uplift estimate. The
   effect's profile slopes carry a heavy ridge penalty: a few hundred outcomes cannot tell a 1-point uplift
   from a 2-point one. The effect has a shared "contacted" term plus one term per channel, offer, theme and
   format level. A level the fit never saw (new content, a new channel) is neutral on average with the prior's
   spread (sd `1/sqrt(level_penalty)` on the logit), so new content is neither penalised nor treated as certain.
3. **What a row is worth.** Contacted rows from the agent's own decisions are weighted by the inverse of their
   logged propensity (`Decision.probability`, clipped at `ipw_clip`, self-normalised so they keep their total).
   Consecutive windows of one customer overlap, so each (control or contacted) is weighted by the share not
   covered by their previous one. And one customer's windows are not independent draws, so in the crowd's fit no
   customer's rows weigh more than `max_customer_weight` (default 4 windows): a few holdout customers decided on
   every day cannot pass for many controls. The person's own shifts (step 6) still use every row.
4. **Spread.** A Laplace approximation to the posterior, drawn as antithetic samples (`draws`, default 64). It
   plays the part of a small bootstrap ensemble, costs one fit instead of many, and shrinks as rows arrive. It is
   the spread Thompson sampling explores with.
5. **Too little data.** Below `min_treated` contacted and `min_control` not-contacted outcomes (weights after
   step 3) the answer is the prior model's, exactly. Above it the crowd's and the prior's estimates are combined
   by precision (inverse variance), so a posterior that is still wide counts for little.
6. **The person.** Two shifts per customer on the logit scale, one on their baseline and one on their response
   to contact. Both start at zero, so a new customer gets the crowd's estimate exactly. Each is one Newton step
   from zero on the customer's own residuals (outcome minus what the crowd predicted for that row), against a
   prior variance estimated from how much customers really differ. A customer with two rows barely moves; one
   with thirty mostly follows their own record. Their posterior variance is part of the spread.
7. **Evidence, never truth.** The model learns from `Outcome`s: the profile at the time of the decision, the
   action (None for the holdout), whether the customer bought in the window after, and the window's bounds.
   - the agent's own sent and holdout decisions, once their window closes (`OutcomeBook`, fed by
     `AgentPolicy.nightly`);
   - past randomised campaigns in the warm-start history (`randomised_arms`, the same test the calibration
     uses), with profiles rebuilt from what the history shows strictly before the campaign started
     (`history_profile`: the campaign's own send is the treatment, not part of the profile). A row converts on
     any purchase in its window, as on the live path; a refund is not one.
   The agent's own "do nothing" for an agent-group customer is not a control (it goes to the people judged least
   likely to respond) and is never kept. A profile taken after its window opened is refused, and `fit(now)` uses
   only windows that had closed by `now`. Nothing reads the Gym's truth.
8. **Cost.** A refit is a few Newton steps on a matrix of at most `max_rows` rows, run when the data has grown by
   `refit_growth` (and `refit_min_new` rows). A prediction is a few small matrix-vector products.

## Switching it on

| Where | Default | On |
|---|---|---|
| `build_agent_policy(..., lookalike=...)` | `False`: `ClickResponseModel`, every existing number unchanged | `True`, or a `LookalikeConfig` to change its numbers; builds `LookalikeClickModel` |
| `RuntimeSettings.lookalike_model` | `False` | `True` reaches the agent only through `build_runtime` (its default `workspace_policy_factory`). `api/v2/engines/decisions.py` builds its agent with `build_agent_policy` directly and ignores the setting |
| Gym CLI `--policy` | `agent` | `agent_lookalike` |

**Limitation of the runtime switch.** The crowd's rows and the `OutcomeBook` live in memory and are filled only by
`decide` in the running process. The runtime rebuilds a goal's policy on a material brief change or a change to
approved content, then replays the decision log through `nightly`; that replay does not reach the book. So after
every rebuild (and every restart) the crowd starts empty and the agent answers with the click model alone until
enough new windows have closed. Past randomised campaigns in the warm-start history are relearned at the first
decision; the agent's own earlier decisions are not.

`LookalikeClickModel` is the click model with the look-alike model on top: the click model's rates, personal
factors, lift per click and fatigue are the prior, so with too little data it answers exactly like the click model
(a test checks this).

## Tests (`tests/v3/lookalike`)

- protocol satisfied; `update` goes to the prior; too little data is exactly the prior; no control means no uplift;
- a new customer gets the crowd's estimate; strong own evidence moves a customer away from it; two messages
  barely move one; spread shrinks with data;
- point in time: a profile taken after its window opened is refused, open windows are not fitted, only purchases
  inside the window count; nightly features built after the window opened are refused;
- evidence (`test_evidence.py`): the warm start from a synthetic randomised campaign rebuilds profiles strictly
  before it (no later purchase, not the campaign's own send), keeps the shared control once, and labels refunds
  and free orders as the live path does; features present in training and absent when predicting; a missing
  first-seen date; one much-decided customer does not count as many controls; propensity weights; an option
  never seen is neutral and uncertain; overlapping sends in the book are weighted;
- the default `build_agent_policy` still builds the plain click model (also for a falsy `lookalike`), the default
  Gym run repeats, the switch builds the look-alike model, the CLI lists `agent_lookalike`. The slow Gym test
  (CI) also checks the crowd's fit came into use and changed a prediction. No test pins the default agent's
  numbers against a build from before this stream; the reviewer checked that by hand (same scorecard sha256 for a
  150-customer, 30-day smoke run on the base commit and on this branch).

## Smoke run

One run, to see that it works end to end (not a comparison): 1 seed, 200 customers, 90 decision days, on this
branch after the review fixes.

```
python -m tests.v3.lookalike.gym_run --scenario baseline_shop --policy agent_lookalike --seeds 1 --customers 200
```

- It finished in 15.0 s (the Gym's own timer; this varies between runs and machines, an earlier run of the same
  command took 19.2 s and the reviewer's 20.9 s) with 0 rule breaks.
- True value 2,517 against 4,283 for the oracle and 3,063 for the segment oracle (best fixed rule 2,306).
- The model had 3,894 rows and refused none, but its fit was not in use (`ready=False`): the 200 customers have
  only 9 holdout customers, whose windows weigh 36.0 after the per-customer cap, below `min_control` (60). The
  contacted rows weigh 618.9. So in this run the agent answered with the click model alone; with too little data
  that is exactly the click model's answer (`test_before_any_data_it_answers_exactly_like_the_click_model`). No
  calibration rows were scored, because rows are scored only once a fit exists.
- The version before the review fixes reported `ready=True` for this same command, on these same few holdout
  customers; the per-customer cap of step 3 is what now keeps the crowd out.
- This says nothing about whether look-alike beats the bucket model: the fit needs more holdout customers than 200
  customers give. No `agent` run was made here.

## The comparison: pending CI

`agent` and `agent_lookalike` on the same seeds. Pending CI: nothing below has been run.

```
for s in baseline_shop sleeping_dogs_15 discount_cannibalisation send_time_matters whatsapp_fatigue null_effect; do
  python -m autonomy.gym.run --scenario $s --policy agent            --seeds 5 --customers 2000 --report-json agent_$s.json
  python -m autonomy.gym.run --scenario $s --policy agent_lookalike  --seeds 5 --customers 2000 --report-json lookalike_$s.json
done
```

Read: true value, share of the segment oracle, contacts to sleeping dogs, and the pass bar of
[`docs/v2/GYM_BAR.md`](../v2/GYM_BAR.md). `null_effect` must still lose at most the stated amount, and
`sleeping_dogs_15` must still stop the harm. The Gym CLI's scenario list is `python -m autonomy.gym.run --list`.
The prediction-scoring scripts `tests/v3/lookalike/gym_run.py` and `bench_run.py` are not collected tests; the
second runs the real-data bench with `lookalike=True` (`python -m tests.v3.lookalike.bench_run --dataset hillstrom
--data-dir DIR --json OUT.json`) and is also pending CI.
