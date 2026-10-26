# Early signals and auto-budget (plan-v3 decision 8)

Two additions, both opt-in and both off by default. With them off, every v2 number is unchanged: the
early-signal model is not usable and returns no forecasts, auto-budget moves no money, and the
briefing hooks add nothing when they get nothing.

| Piece | Where | Switch |
|---|---|---|
| Early-signal model | `autonomy/learning/early_signal.py` | `EarlySignalConfig(enabled=True)` |
| Auto-budget | `autonomy/budget/` | `BudgetConfig(enabled=True)` |
| Briefing hooks | `Analyst.briefing(..., early=)` in `autonomy/agents/analyst.py`; `budget_week()` in `autonomy/inbox/briefing.py` | pass an `EarlyRead` / the week's moves |

## 1. Early-signal model: a 90-day answer from the first week

**What it predicts.** For each customer, the margin they bring in the 90 days after their first
week, from what they did in that first week: a second visit, a second order, the size of the basket
(average order value, and first-week margin), and whether they replied. Six features, a ridge
regression, numpy only (`FEATURE_NAMES`).

**One cut-off per customer.** The anchor is the customer's first own event (signup, visit or
purchase; messages we sent do not start the clock). The cut-off `T` is the anchor plus 7 days.
Features read events in `[anchor, T]`. The label reads `(T, T + 90 days]`. It is the same boundary
rule `engine/onboarding/labels.py` writes in `future_window_clause`: an event at exactly `T` is in
the features and never in the label. `engine/` is not edited and not imported (it is DuckDB; the
`autonomy/` CI job has no DuckDB), so the rule is re-implemented in numpy.

**Censoring.** The end of the data is the latest event in the whole log (or the `as_of` the caller
passes), not each customer's own last event, for the reason `labels.py` gives: a customer who left
has an old last event, and a per-customer end would define the answer in terms of itself. A customer
whose label window runs past the end gets `nan`, not zero, and is never trained on or scored. A
customer whose first week is not over is not an example at all (`Examples.open_week` counts them).

**The backtest.** Customers are grouped by the month of their anchor. For each past month the model
is fitted with only the customers whose label windows had closed by the time that month's first
weeks were over, then its forecast of the month's mean is compared with the month's actual mean.
Nothing from after the forecast date is used. A month is **within the band** when
`|forecast - actual| / max(|actual|, floor)` is at most `band` (floor: 10% of the mean absolute
label, so a month near zero does not divide by zero). Months that cannot be scored are listed with
the reason (windows not over yet, too few customers, too little earlier history). The model is used
only if at least 3 months were scored and at least 80% of them are within the band (the 80% is the
pass test of plan-v3 decision 8). Otherwise `fit` returns a model with `usable=False`, the reason in
words, and `forecasts()` returns `{}`.

**Using it.** `fit(events, config)` returns an `EarlySignal`. `signal.forecasts(events)` gives the
forecast per customer whose first week is over. `early_read(signal, forecasts, group_of)` compares
the agent group with the holdout (Welch interval from `autonomy.proof.estimate`) and returns an
`EarlyRead`, or `None` when the model is not usable or a group has under 30 forecast customers. Pass
it to `Analyst.briefing(..., early=read)` and the briefing gets an "Early read" section. Its words
say it is a forecast and that the measured results will confirm or correct it; its interval covers
the spread between customers only, and the model's own error is what the backtest band reports.
Experiments and the Analyst should read this first and confirm with the real 30/60/90-day numbers.

### What was run, and what was not

Only a synthetic shop could be run here, and the numbers below say nothing about real data. The
plan's pass test names Online Retail II and CDNOW; neither is in this repository at the base commit
(`library/online-retail` holds a customer-level sample, not a transaction log, and there is no CDNOW
file), and the order-log fixture of the Data Doctor stream is not in this branch. **The 80% pass test
on those two datasets has therefore not been run.** The code reads `autonomy.contracts.Event`s, so
once an order log is turned into events the same `fit` call runs on it.

The synthetic shop (`_shop` in `tests/v3/early/test_early_signal.py`: 14 monthly arrivals of 40
customers, 560 in all; 40% come back within three days and go on to spend about five times more in
the next 90 days), run with `EarlySignalConfig(enabled=True, min_train=60, min_cohort_size=10)` and
the default band of 25%:

| Shop | Months scored | Within band | Customer-level correlation | Usable |
|---|---|---|---|---|
| Steady | 9 | 9 of 9 (errors 0.8% to 9.4%) | 0.716 | yes |
| Spending level triples from month 7 | 9 | 3 of 9 | 0.815 | no (needs 80%) |

The second row is the point of the gate: the first-week signal still ranks customers well
(correlation 0.815) but the level is wrong because the shop changed, so the forecasts are not used.
Of the 14 months, four early months and the newest one are skipped (no finished windows yet to learn
from, or windows still open).

The band (25%), the 3-month minimum, the 10% floor and the assumed margin share for purchases that
carry no margin (30%) are placeholders in `EarlySignalConfig`, not findings. The plan says the band
is "agreed"; it needs the founder's number.

## 2. Auto-budget: money moves to what works

**Evidence.** Per goal, proven extra profit per unit spent with a 95% interval, taken from the
goal's `ProofSummary` (`goal_returns`): incremental profit against the holdout divided by spend.
The unit is spend, not allowance, because money not spent earns nothing. A goal with no interval
(too few customers, or another goal acted on the same customers so one holdout cannot separate them)
takes no part.

**The rule** (decision V-4), in `plan_move`:

- The strongest goal (highest point return) must have a range entirely above the weakest goal's,
  and entirely above zero. Overlapping ranges, or touching ones, move nothing.
- At most 20% of a goal's budget a week. `BudgetConfig.max_weekly_share` can only be lowered
  (validated `<= 0.20`). The limit applies to the giver and to the receiver, against each one's
  budget a week ago, and counts what already moved in the last 7 days.
- The giver keeps what it has spent and at least half of what it started with
  (`floor_share`, a default of mine), so it keeps being measured.
- The receiver never goes above its own envelope ceiling (`GoalBudget.ceiling`). The programme total
  is conserved and checked against the approved `envelope_total`.
- Every move is a logged `BudgetMove` (who, how much, before and after, both ranges, when).
- `BudgetLedger.undo(move_id, now)` reverses it. Amounts are whole cents, so budgets come back
  equal to the cent, not approximately. `undo_last` undoes the latest one. An undo is refused if the
  receiver has already spent the money. After an undo the same move is not repeated for 28 days.

**Briefing.** `move_entry` writes one plain line with every number it uses, for example (from a test run with
two goals of 1,000 each): "Moved $200 of budget from Win-back to Repeat purchase: Repeat purchase earned
$1.50 to $2.50 extra profit for each unit spent and Win-back -$0.10 to $0.50, ranges that do not
overlap. No goal moves more than 20.0% of its budget a week, and one click undoes it."
`inbox.briefing.budget_week(moves, since, until, names)`
turns the week's applied moves into a "Budget moves" section, or returns `None` when there are none
(so the briefing is unchanged while auto-budget is off). Goal names with digits are replaced with
"the stronger goal" / "the weaker goal", since the briefing's number check flags every digit.

**Not built here.** The Gym scenario "two goals of planted different value" (plan-v3 section 8) is
Gym-stream work. The ledger is in memory: it holds budgets and a log of Pydantic models that can be
stored, but wiring it to the goal store, the "one click" button in the UI and the weekly job that
calls `rebalance` belong to the integrator.

## Tests

`tests/v3/early/` (38 tests, about a second): censoring (no label from an unfinished window, the
window closing exactly at the end, first week not over, end of data is the whole log), point-in-time
(nothing after `T` in the features, an event at `T` is a feature and one just after it is the label,
a month's forecast is unchanged when later customers change), the backtest gate (passes on a steady
shop, fails when the shop changes, needs enough months, off by default), and budget (moves only when
ranges separate, never above 20% a week, never outside the envelope, undo restores exactly).
