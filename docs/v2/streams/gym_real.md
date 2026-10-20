# Stream gym_real: a more realistic Gym and an honest pass bar

This stream makes the Gym (`autonomy/gym/`) harder to fool in two ways. The world is more like a
real client's data. Unsubscribes cost money, sleeping dogs leave faint clues, and the history holds
past campaigns. The pass bar is staged and compares a policy with references a real business could
reach. The plan's original bar (`pass_criteria`) is unchanged and still reported.

## What was built

| Module | One line |
| --- | --- |
| `api.py` | `ScenarioSpec` gains realism knobs. All are additions, and a bare spec keeps the old world. The knobs are `lost_contact_value`, `lost_contact_horizon_days`, `lost_contact_sends_per_week`, `complaint_cost`, `sleeping_dog_signal`, `sleeping_dog_unsubscribe_multiplier` and `history_sends_per_week`. |
| `truth.py` | `PlantedTruth` gives each customer a lost value per channel (`lost_value`, `lost_contact_value(cid, channel)`) and gives each sleeping dog a hidden onset (`dog_onset_s`). `expected_net` now subtracts the unsubscribe chance x lost value and the complaint chance x complaint cost. `evaluate_rows` is a batch helper used for past sends. `CHANNEL_INDEX`, `KIND_INDEX` and `THEME_INDEX` are now public. |
| `world.py` | The history now includes a weekly logged campaign. Each SEND carries `logged_probability`, `logging_policy`, `campaign_id`, `offer_id` and `content_id`, and is followed by its DELIVER, CLICK, UNSUBSCRIBE and COMPLAINT events. After their onset, sleeping dogs ignore messages, visit more and unsubscribe more. An UNSUBSCRIBE or COMPLAINT caused by `act` carries `attrs['lost_value']`, which is for scoring only. History events never carry it. |
| `scenarios.py` | Library scenarios set `complaint_cost=5` and `history_sends_per_week=0.4`. A new scenario, `sleeping_dogs_hidden`, sets signal 0, so the dogs leave no clue. |
| `baselines.py` | `BestFixedRule` is the strongest policy that does no learning: WhatsApp if consented, else email, no offer, 14:00 local. `SegmentOracle` is a diagnostic ceiling: the best action per observable segment x reachable channels, judged on the truth's group mean. The helpers `observable_segment`, `frequency_cuts` and `purchase_frequency` support them. |
| `score.py` | Realised net subtracts `lost_value`. `ScoreCard` gains `lost_value`, `segment_oracle_value`, `share_of_segment_oracle`, `best_fixed_rule_value`, `value_vs_best_fixed_rule` and `sleeping_dog_signal`. `staged_criteria` returns a `StagedReport` with Stage 1 Safe, Stage 2 Useful, Stage 3 Smart and a reported stretch. |
| `run.py` | The CLI compares against all five references and prints the staged report, then the plan bar. It adds `--report-json PATH` and `--fixed-hour H`. |
| `docs/v2/GYM_BAR.md` | The staged bar: what each reference can see, each threshold and why, and indicative numbers. |

## How other streams use it

Everything below is exported from `autonomy.gym`.

```python
BestFixedRule(hour: int = 14, *, min_gap_hours: float = 6.0)            # a Policy; never sees the truth
SegmentOracle(truth: GroundTruth, *, segment_of=None, min_gap_hours=6.0,
              history_days: int | None = None)                          # a Policy; uses prepare_day
observable_segment(state, now, cuts=(0.15, 0.35), data_start=None) -> str  # new/lapsed/light/medium/heavy

cmp = compare(spec, {"agent": factory, BEST_FIXED: ..., SEGMENT_ORACLE: ..., ...}, seeds)
report: StagedReport = staged_criteria(cmp.cards, policy="agent")
report.stage_reached -> int        # 0-3; a NOT JUDGED stage ends the climb like a failure
report.verdict() -> dict[str, object]   # JSON-ready, for CI artefacts
report.lines() -> list[str]

PlantedTruth.lost_contact_value(customer_id: str, channel: Channel) -> float
```

On the CLI: `python -m autonomy.gym.run --scenario NAME --policy NAME --seeds N --report-json out.json`.

Rules for the agent stream:

- Never read `attrs['lost_value']` on events. It comes from the planted uplift and exists only for scoring.
- Past SENDs carry `logged_probability`, so they can be used for off-policy learning. Their
  probabilities can be rebuilt from the history alone, because the logging policy does not look ahead.

## Contract additions requested

- `autonomy/runtime/demo.py` (runtime stream): `test_demo.py::test_every_send_traces_to_one_act_decision_and_lands_in_the_gym`
  is expected to fail now, because the `baseline_shop` history holds past-campaign SENDs that no
  decision log contains. There are two fixes. Pass `history_sends_per_week=0.0` to `get_scenario`,
  or count and trace only SENDs at or after the demo's start.
- `score.py` / the agent: strip `attrs['lost_value']` from events before `_fold`, `agent.learn` or
  nightly see them. Decision-period UNSUBSCRIBEs still carry it, and it is 0 for a sleeping dog.
- CI (`.github/workflows/v2.yml`): add `sleeping_dogs_hidden` to the Gym matrix. Add
  `--report-json gym-report/${{ matrix.scenario }}.json` to the scoreboard command.
- `ScoreCard`, `ScenarioSpec` and `BASELINE_NAMES` gained fields and names. These are additions only.

## Dependencies requested

None.

## Known gaps

- mypy was run locally with `--python-version 3.12`, because the installed numpy stubs need 3.12.
  It passes on `autonomy/gym`. CI's Python 3.11 run must still confirm it.
- `tests/v2/gym`, `tests/v2/agent_gym` and `tests/v2/runtime` were not run here. Only
  `tests/v2/gym_real` was run (39 passed).
- The reference numbers in GYM_BAR.md come from one seed with 1,000 customers. They were measured
  before the final changes to the segment oracle's frequency counting. They should be replaced
  with CI's 3,000 x 3 figures.
- `SegmentOracle` ignores the sleeping-dog clues, so Stage 3's dog check compares against send-to-all.
- The best fixed rule already gets 72-91% of the segment oracle, so Stage 3's 80% check mainly has
  teeth in `send_time_matters` and `whatsapp_fatigue`.
- Past sends are simulated at daily resolution. Building the history (reset) is about 3.6x slower,
  about 0.33 s for 1,000 customers. The forward simulation runs at the same speed as before.
- Sleeping dogs now also unsubscribe 3x as often during the decision period, which shifts
  `sleeping_dogs_15` slightly.
- The speed test uses timing allowances and could be flaky on a heavily loaded CI machine.
- `black --check docs/v2/GYM_BAR.md` always errors, because black cannot parse Markdown. Black
  passes on all the Python files.
