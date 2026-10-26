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
   profile, foundation A), those features too. A profile is stamped with the moment it was taken.
2. **The crowd.** One logistic model of "bought within the window", fitted on closed outcomes:
   `logit p(control) = baseline(profile)` and `logit p(treated) = baseline(profile) + effect(profile, channel,
   offer, theme, format)`. Uplift is the difference of the two probabilities. Only customers we did not contact
   (the holdout) tell the baseline from the effect, so with no control there is no uplift estimate. The
   effect's profile slopes carry a heavy ridge penalty: a few hundred outcomes cannot tell a 1-point uplift
   from a 2-point one.
3. **Spread.** A Laplace approximation to the posterior, drawn as antithetic samples (`draws`, default 64). It
   plays the part of a small bootstrap ensemble, costs one fit instead of many, and shrinks as rows arrive. It is
   the spread Thompson sampling explores with.
4. **Too little data.** Below `min_treated` contacted and `min_control` not-contacted outcomes the answer is the
   prior model's, exactly. Above it the crowd's and the prior's estimates are combined by precision (inverse
   variance), so a posterior that is still wide counts for little.
5. **The person.** Two shifts per customer on the logit scale, one on their baseline and one on their response
   to contact. Both start at zero, so a new customer gets the crowd's estimate exactly. Each is one Newton step
   from zero on the customer's own residuals (outcome minus what the crowd predicted for that row), against a
   prior variance estimated from how much customers really differ. A customer with two rows barely moves; one
   with thirty mostly follows their own record. Their posterior variance is part of the spread.
6. **Evidence, never truth.** The model learns from `Outcome`s: the profile at the time of the decision, the
   action (None for the holdout), whether the customer bought in the window after, and the window's bounds.
   - the agent's own sent and holdout decisions, once their window closes (`OutcomeBook`, fed by
     `AgentPolicy.nightly`);
   - past randomised campaigns in the warm-start history (`randomised_arms`, the same test the calibration
     uses), with profiles rebuilt from what the history shows before the campaign started (`history_profile`).
   The agent's own "do nothing" for an agent-group customer is not a control (it goes to the people judged least
   likely to respond) and is never kept. A profile taken after its window opened is refused, and `fit(now)` uses
   only windows that had closed by `now`. Nothing reads the Gym's truth.
7. **Cost.** A refit is a few Newton steps on a matrix of at most `max_rows` rows, run when the data has grown by
   `refit_growth` (and `refit_min_new` rows). A prediction is a few small matrix-vector products.

## Switching it on

| Where | Default | On |
|---|---|---|
| `build_agent_policy(..., lookalike=...)` | `False`: `ClickResponseModel`, every existing number unchanged | `True`, or a `LookalikeConfig` to change its numbers; builds `LookalikeClickModel` |
| `RuntimeSettings.lookalike_model` | `False` | `True` reaches the agent through `agent_policy_factory` |
| Gym CLI `--policy` | `agent` | `agent_lookalike` |

`LookalikeClickModel` is the click model with the look-alike model on top: the click model's rates, personal
factors, lift per click and fatigue are the prior, so with too little data it answers exactly like the click model
(a test checks this).

## Tests (`tests/v3/lookalike`)

- protocol satisfied; `update` goes to the prior; too little data is exactly the prior; no control means no uplift;
- a new customer gets the crowd's estimate; strong own evidence moves a customer away from it; two messages
  barely move one; spread shrinks with data;
- point in time: a profile taken after its window opened is refused, open windows are not fitted, only purchases
  inside the window count;
- the default `build_agent_policy` still builds the plain click model, the default Gym run repeats, the switch
  builds the look-alike model, the CLI lists `agent_lookalike`.

## Smoke run

One run, to see that it works end to end (not a comparison): 1 seed, 200 customers, 90 decision days, on this
branch.

```
python -m tests.v3.lookalike.gym_run --scenario baseline_shop --policy agent_lookalike --seeds 1 --customers 200
```

- It finished in 19.2 s (the Gym's own timer) with 0 rule breaks.
- True value 2,472 against 4,283 for the oracle and 3,063 for the segment oracle (best fixed rule 2,306).
- The model had 3,893 rows, was in use (`ready=True`) and refused none. Scored before it learned, its treated
  rows predicted 0.1389 and realised 0.1318 (n = 1,464); its control rows predicted 0.0151 and realised 0.0000
  (n = 50).
- At 200 customers and one seed the 95% range missed the truth and there are 50 control rows, so this says nothing
  about whether look-alike beats the bucket model. No `agent` run was made here.

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
