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

**What it predicts.** For each customer, the margin they bring in the 90 days after a week, from
what they did in that week: a second visit, a second order, the size of the basket (average order
value, and the week's margin), and whether they replied. Four facts from before the week are added:
had the customer been seen before, their margin in the 90 days before it, days since they were last
seen and days since they were first seen. Ten features (`FEATURE_NAMES`), numpy only, in two ridge
regressions: one for starts with no history (a new customer's first week) and one for customers
seen before (`SplitModel`).

**Which week.** An early read of a test has to look at the week *after the customer entered the
test*, or nothing the agent did can show up in it. Most tests (win-back, repeat) are about existing
customers whose own first week was long before the agent ever wrote to them, so:

- At forecast time the caller passes each customer's start: `early_read(signal, events, entered,
  group_of)`, with `entered` the time each customer entered the test (their first decision, or when
  they were put in the holdout). The week is `[entered, entered + 7 days]`; only the four history
  facts read anything earlier. Customers whose week in the test is not over yet are not counted, and
  the read says how many.
- For training, the starts are each customer's first own event (signup, visit or purchase; messages
  we sent do not start the clock) and also, every `cutoff_every_days` (30) from the start of the
  log, a past cut-off for every customer whose first week was over by then. So the model has learned
  what a week after an arbitrary start says for a customer who already has a history: the same
  definition as at forecast time. With `cutoff_every_days=0` it trains on first weeks only, and then
  it refuses to forecast customers who have a history (`forecasts(..., anchors=)` leaves them out).

**One boundary.** The cut-off `T` is the start plus 7 days. Features read events in `[start, T]`.
The label reads `(T, T + 90 days]`. It is the same boundary rule `engine/onboarding/labels.py`
writes in `future_window_clause`: an event at exactly `T` is in the features and never in the label.
`engine/` is not edited and not imported (it is DuckDB; the `autonomy/` CI job has no DuckDB), so the
rule is re-implemented in numpy. Events repeated with the same `event_id` are counted once.

**Censoring.** The end of the data is the latest event in the whole log (or the `as_of` the caller
passes), not each customer's own last event, for the reason `labels.py` gives: a customer who left
has an old last event, and a per-customer end would define the answer in terms of itself. A row
whose label window runs past the end gets `nan`, not zero, and is never trained on or scored. A
start whose week is not over is not an example at all (`Examples.open_week` counts them).

**The backtest.** Rows are grouped by the month of their start (new customers and the cut-off rows
of known customers together). For each past month the model is fitted with only the rows whose label
windows had closed by the time that month's weeks were over, then its forecast of the month's mean is
compared with the month's actual mean. Nothing from after the forecast date is used, including in
the error floor. A month is **within the band** when `|forecast - actual| / max(|actual|, floor)` is
at most `band` (floor: 10% of the mean absolute label of that month's training rows, so a month near
zero does not divide by zero). Months that cannot be scored are listed with the reason (windows not
over yet, too few rows, too little earlier history). The model is used only if

1. at least 3 months were scored,
2. at least 80% of them are within the band (the 80% is the pass test of plan-v3 decision 8), and
3. the customer-level forecasts over the scored months correlate with what happened by at least
   `min_correlation` (0.2). Right averages alone are not enough: the early read compares an agent
   group with a holdout, and a model that cannot tell one customer from another would show no
   difference whatever the agent did.

Otherwise `fit` returns a model with `usable=False`, the reason in words, and `forecasts()` returns
`{}`.

**Using it.** `fit(events, config)` returns an `EarlySignal`. `early_read(signal, events, entered,
group_of)` compares the agent group with the holdout (Welch interval from `autonomy.proof.estimate`)
and returns an `EarlyRead`, or `None` when the model is not usable or a group has under 30 forecast
customers. Pass it to `Analyst.briefing(..., early=read)` and the briefing gets an "Early read"
section: the forecast difference "in the 90 days after" the customers' "first 7 days in the test",
and one sentence with the model's own backtest (for example "its forecasts were within 25.0% of
what happened for 8 of 9 months"), every number in the section's numbers. Its words say it is a
forecast and that the measured results will confirm or correct it; its interval covers the spread
between customers only, and the model's own error is what the backtest band reports. Experiments and
the Analyst should read this first and confirm with the real 30/60/90-day numbers.

### What was run, and what was not

Only synthetic shops could be run here, and the numbers below say nothing about real data. The
plan's pass test names Online Retail II and CDNOW; neither is in this repository at the base commit
(`library/online-retail` holds a customer-level sample, not a transaction log, and there is no CDNOW
file), and the order-log fixture of the Data Doctor stream is not in this branch. **The 80% pass test
on those two datasets has therefore not been run; it is still open.** The code reads
`autonomy.contracts.Event`s, so once an order log is turned into events the same `fit` call runs on
it.

The shops are the helpers in `tests/v3/early/test_early_signal.py`. `_shop`: 14 monthly arrivals of
40 customers, 560 in all; 40% come back within three days and go on to spend about five times more
in the next 90 days, then stop. "Shift" is the same shop with the spending level tripled from month
7. `_flat`: the same arrivals, but every customer buys at one steady random rate from arrival to the
end, so nothing tells customers apart. All three were run with `CFG`
(`EarlySignalConfig(enabled=True, min_train=60, min_cohort_size=10, min_cohorts=3)`, defaults
otherwise: band 25%, cut-offs every 30 days, minimum correlation 0.2) by this command from the
repository root, which took well under a second per shop:

```
python -c "
from tests.v3.early.test_early_signal import CFG, _flat, _shop
from autonomy.learning.early_signal import fit
for name, ev in [('steady', _shop()), ('shift', _shop(shift_after=7)), ('flat', _flat())]:
    s = fit(ev, CFG); b = s.backtest
    print(name, len(b.cohorts), b.within, [round(100 * r.error, 1) for r in b.cohorts], [r.n for r in b.cohorts], round(b.correlation, 3), s.usable, len(b.skipped), '|', s.reason)
"
```

| Shop | Months scored | Within band | Rows per month | Customer-level correlation | Usable |
|---|---|---|---|---|---|
| Steady | 9 | 9 of 9 (errors 0.9% to 25.0%) | 196 to 553 | 0.682 | yes |
| Spending level triples from month 7 | 9 | 3 of 9 (errors up to 69.5%) | 196 to 554 | 0.757 | no (needs 80%) |
| Flat: nothing tells customers apart | 7 | 7 of 7 (errors 0.9% to 8.3%) | 276 to 473 | -0.035 | no (correlation under 0.2) |

The second row is the point of the band: the week still ranks customers well (correlation 0.757)
but the level is wrong because the shop changed. The third row is the point of the correlation
check: every month's average is right, but the forecasts carry no customer-level signal, so an
early read could never show what the agent did. Each shop has 7 months skipped: the first four
(no or too little finished history to learn from) and the newest three (windows still open). The
steady shop's first scored month is only just inside the band: its error printed unrounded
(`b.cohorts[0].error`) is 0.2497.

The tests also check the early read on the steady shop: existing customers who arrived in the first
nine months enter a test on day 450; with nothing changed, the agent-minus-holdout range contains
zero, and when the agent half buys twice in its first week in the test the range is above zero.

The band (25%), the 3-month minimum, the 0.2 correlation minimum, the 30-day cut-off spacing, the
ridge strength, the 10% floor and the assumed margin share for purchases that carry no margin (30%)
are placeholders in `EarlySignalConfig`, not findings. The plan says the band is "agreed"; it needs
the founder's number. One sign the model is fragile: with the ridge strength lowered from 1.0 to 0.1,
the steady shop fell to 1 of 9 months within the band (a scratch run of the same command with
`dataclasses.replace(CFG, ridge=0.1)`). Building training rows at every cut-off costs one row per
known customer per month of history (the steady shop gives 5,819 rows from 560 customers); on a
large shop this is the cost to watch.

## 2. Auto-budget: money moves to what works

**Evidence.** Per goal, proven extra profit per unit spent with a 95% interval, taken from the
goal's `ProofSummary` (`goal_returns`): the proof's own return on spend (`ProofSummary.roi`, so a
bootstrap proof's range is the same one the proof and the briefing show), or incremental profit
against the holdout divided by spend when the proof has none. A proof filed under another goal's id
is refused.
The unit is spend, not allowance, because money not spent earns nothing. A goal with no interval
(too few customers, or another goal acted on the same customers so one holdout cannot separate them)
takes no part.

**The rule** (decision V-4), in `plan_move`:

- The strongest goal (highest point return) must have a range entirely above the weakest goal's,
  and entirely above zero. Overlapping ranges, or touching ones, move nothing.
- At most 20% of a goal's budget a week. `BudgetConfig.max_weekly_share` can only be lowered
  (validated `<= 0.20`). The limit applies to the giver and to the receiver, against each one's
  budget a week ago, over a rolling 7 days, and counts money in and out together: a goal that gave
  20% this week cannot also take money back the same week.
- The giver keeps what it has spent and at least half of what it started with
  (`floor_share`, a default of mine), so it keeps being measured.
- The receiver never goes above its own envelope ceiling (`GoalBudget.ceiling`), across weeks too.
  The programme total is conserved (moves only shift money between goals) and the starting budgets
  are checked against the approved `envelope_total`.
- Every move is a logged `BudgetMove` (who, how much, before and after, both ranges, when).
- `BudgetLedger.undo(move_id, now)` reverses it. Amounts are whole cents, so budgets come back
  equal to the cent, not approximately. `undo_last` undoes the latest one. An undo is refused if the
  receiver has already spent the money. After an undo the same move is not repeated for 28 days.

**Briefing.** `move_entry` writes one plain line with every number it uses, for example (from a test run with
two goals of 1,000 each): "Moved $200 of budget from Win-back to Repeat purchase: Repeat purchase earned
$1.50 to $2.50 extra profit for each unit spent and Win-back -$0.10 to $0.50, ranges that do not
overlap. No goal moves more than 20.0% of its budget a week, and one click undoes it." The
percentage is the configured limit (`cap`).
`inbox.briefing.budget_week(moves, since, until, names, cap=config.max_weekly_share)`
turns the week's applied moves into a "Budget moves" section, or returns `None` when there are none
(so the briefing is unchanged while auto-budget is off). Goal names with digits are replaced with
"the stronger goal" / "the weaker goal", since the briefing's number check flags every digit.

**Not built here.** The Gym scenario "two goals of planted different value" (plan-v3 section 8) is
Gym-stream work. The ledger is in memory: it holds budgets and a log of Pydantic models that can be
stored, but persisting the `BudgetMove` rows, wiring it to the goal store, the "one click" button in
the UI and the weekly job that calls `rebalance` belong to the integrator. To restore a ledger, pass
the stored moves with `moves=` and the goals' *current* budgets (which already include every
applied move; the ledger does not replay the log). A log naming a goal the ledger does not hold, or
the same move twice, is refused. How fresh a proof must be (`ProofSummary.as_of`) is not checked
here; the weekly job should pass this week's proofs.

## Tests

`tests/v3/early/` (50 tests; `pytest tests/v3/early` reported 2.90s here):

- censoring: no label from an unfinished window, the window closing exactly at the end, a week not
  over, the end of data is the whole log;
- point-in-time: nothing after `T` in the features, an event at `T` is a feature and one just after
  it is the label, a start given by the caller reads only what came after it, a month's forecast is
  unchanged when later customers change and when earlier customers whose windows were still open do
  something after the forecast date, a repeated event is counted once;
- the backtest gate: passes on the steady shop, fails when the shop changes, fails when nothing tells
  customers apart, needs enough months, off by default;
- the early read: sees what the agent group did after entering the test, leaves out customers whose
  week in the test is not over, a first-weeks-only model does not forecast known customers, and the
  Analyst section carries the backtest with every number backed;
- budget: moves only when ranges separate, never above 20% over a rolling week (6.9 days: no; 7: yes)
  counting money in and out, week-by-week amounts over a month, the receiver's ceiling over several
  weeks, the log checked when a ledger is restored, undo restores exactly, the briefing states the
  configured limit.
