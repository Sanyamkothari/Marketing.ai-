# Stream: scientist (the AI proposes and explains, held-out evidence decides)

21 Oct 2026, branch `v2/scientist`. The agent had learned from clicks, a proxy, and nobody had reviewed the design.
This stream puts a reviewed loop in front of it: a Data Doctor reads the data profile, a Proposer suggests up to
three set-ups from a fixed typed menu, each set-up is scored on customers it was not fitted on, and a separate,
sceptical Judge picks by a rule written down beforehand, with "keep the safe default" as a valid answer. The
plain-words write-up, the tables and the targets are in [../SCIENTIST.md](../SCIENTIST.md). The scoreboard and the
sealed runs are in [../REAL_BENCH.md](../REAL_BENCH.md).

## What was built

| Part | Files | What it does |
|---|---|---|
| Menu and evaluator | `autonomy/scientist/{campaign,menu,evaluate}.py` | `Campaign` (one past campaign, one row per customer, read from a bench table or from the history's events); four typed candidate kinds with settings validated against ranges (`candidate_from_spec` is the only way plain data becomes a candidate); fit on one part, score on the other with IPS / SNIPS / DR from `autonomy.proof`, customer split for a randomised campaign, time split when not; a campaign without logged probabilities is `not_enough_data`. |
| Selection rule | `autonomy/scientist/select.py` | Safe default (best blanket arm by train evidence, else send nothing), Holm over the claims of a run, best corrected lower bound wins, flags. The rule is the module docstring. |
| Monitor and hook | `autonomy/scientist/{monitor,chooser}.py`, `AgentPolicy.use_chooser` in `autonomy/runtime/agent.py` | Weekly forecast-versus-actual check (more than 2x either way, or proof negative, means re-run). The selected policy runs in Shadow (logged, changes nothing) or Live (set with a reason, logged, reversible) through the agent's hook, inside the signed envelope and the weekly cap. |
| AI roles | `autonomy/agents/scientist_roles.py`, `autonomy/agents/prompts/{data_doctor,proposer,judge}.md`, `autonomy/scientist/profile.py` | Numbers-only data profile; Data Doctor, Proposer and Judge as pydantic-typed answers checked by code (a rejected answer is replaced by a rule-based one, not patched); the Judge can only agree or veto. |
| Record and replay | `autonomy/agents/llm_exchange.py`, `autonomy/scientist/run.py` | Each prompt is saved with a content-hash id and the run stops; answers written into `{id: answer}` are replayed. No model is called by the bench. |
| Real-data testing | `scripts/v2_realbench/{sealed,targets,gym_fit}.py`, `common.py` (Holm), `docs/v2/TARGETS.json` | Two sealed datasets (Lenta, Starbucks) behind `--sealed-final`; 52 pre-registered claims; Holm-Bonferroni on every scoreboard comparison; the Gym calibrated to the real e-mail and SMS campaigns (`autonomy/gym/calibration.py`, scenarios `real_email_like`, `real_sms_like`). |
| Bench integration | `scripts/v2_realbench/{scientist,ai_loop}.py`, `run.py` | `--policy scientist` scores the Scientist's pick on TEST against the agent and the simple policies; `--ai-answers` adds the AI loop (profile of TRAIN, Doctor, Proposer, evidence, Judge) and scores its pick on the same customers; a prompt without an answer is saved and the run exits 3. |
| Gym spot-check | `scripts/v2_gym_scientist.py` | Registers `scientist` and `scientist_shadow` (the agent with the selection in front of it) for `python -m scripts.v2_gym_scientist`. |

## Results (21 Oct 2026; details in SCIENTIST.md)

* **Dev bench, Scientist against the agent (same TEST customers).** The Scientist is never worse than the agent and
  fixes Hillstrom's exploration loss (the agent -0.0253 [-0.0476, -0.0030] against send-all men's; the Scientist keeps
  the safe default, +0.0000, non-inferior). On MegaFon it picks the uplift model (+0.4642 over the best simple policy) or,
  with Sonnet's list, the agent (+0.4627). On Criteo it is exactly the agent and stays below the 2% bar. The AI layer
  changed no outcome: Sonnet-proposed and fixed-list picks were the same policy on three of four datasets, a tie on the
  fourth.
* **AI checks.** Data Doctor chose purchases over clicks 4 of 4 and never trusted a click; the Proposer stayed inside the
  menu 4 of 4; the Judge's pick equalled the evidence rule's 4 of 4; 4 of 12 answers were rejected at least once (2
  schema failures repaired on the retry, 2 refused by code for a claimed sample-ratio mismatch and a number not in the
  evidence).
* **Sealed (one run each).** The agent is not non-inferior on Lenta or Starbucks under the 2% rule (Lenta has no measurable
  profit effect; Starbucks -0.0018 [-0.0081, +0.0045] against the segment rule); calibration within 2x on Starbucks only.
  The Scientist kept "send nothing" on both, which cost 0.0160 per customer against the best segment rule on Starbucks.
* **Gym.** The Scientist wrapper was identical to the agent in all seven spot-check scenarios: the Gym's histories carry
  no control group with known probabilities, so the Scientist could prove nothing and left the agent in charge.

## Honest limits

* The Judge's rule is implemented twice (`scientist_roles.evidence_rule_pick`, `select.select`); they agree on every
  selection seen here.
* Five of the fifteen recorded AI answers were written by the grading agent (Sonnet 5.5), not by independent sub-agents.
* The Scientist's evidence uses half of TRAIN as the held-out part, so it cannot prove gains of about 1% of the value at
  60,000 customers (Starbucks); that is a property of the written rule, not tuned.
* The AI roles were not run on the sealed data, and the Doctor's recommendations are advisory: nothing feeds them into
  the agent's objective yet.
* Promotion from Shadow to Live and the monitor's re-run are not wired into the runtime; the Gym never exercised a
  promotion.

## How to run

```
python -m scripts.v2_realbench.run --dataset hillstrom --policy scientist --ai-answers answers.json --json out.json
python -m scripts.v2_realbench.run --render out.json
python -m autonomy.scientist.run --dataset-profile profile.json --stage diagnose --record prompts.json   # one stage at a time
python -m scripts.v2_gym_scientist --scenario baseline_shop --policy scientist --seeds 2 --customers 1000 --scientist-log log.json
python -m scripts.v2_realbench.targets --base-dir OUT [--write]
```

Tests: `tests/v2/scientist/` (279 tests, seconds each, inline fixtures): `test_menu_*` (menu, evaluator, rule,
monitor, chooser, bench), `test_roles_*` (profile, roles, exchange), `test_testing_*` (sealed, Holm, Gym calibration,
targets), `test_final_ai_loop.py` (the bench loop with scripted answers) and `test_final_gym_scientist.py` (Shadow
changes nothing).
