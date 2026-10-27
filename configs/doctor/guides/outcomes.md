# Guide: outcomes (read before defining, mapping or checking what the model should predict)

## Rules
- Outcome = population + event + window + start date (R7), one plain sentence the person approves. Nothing applies without explicit yes.
- Meaning is never yours (R10, Q12, Q24): ask via `ask_question` or `express_doubt` with `meaning: true`. Q24 is only an analyst note, never "applied".
- Eligible population first (churn needs earlier activity; win-back lapsed only; cross-sell excludes holders; renewal expiring policies); show how many are out (Q10, Q23).
- A window must finish before the data ends; unfinished ones are left out, not "no": train earlier, score later (time_and_as_of).
- Remove source column X and look-alikes (same prefix or source date, strong match) from features; list for confirmation. Several outcome-like columns: ask which is the target, no pre-ticking.

## Window and groups
- Window from rhythm (R9): query gaps between purchases, take median and 90th percentile, offer windows near them (not from `describe_dates`); `rate_by` or receipt gives each yes rate.
- `rate_by` each unit, brand, region, channel (R16); over 5 times apart, ask: one model, separate rules, or one model per group.
- Warn when yes rate is above 70% or below 3%; minimum rows and positives stay. Rate by month (R8). "Within N days of first order" uses each customer's own start date.

## Yes/no column
- `describe_outcome`, `value_counts`, `draft_outcome_labels`, `inspect_column`. Blank = unknown, not "no" (R12). Quote only tool yes rates.
- Unclear values (Pending, Unknown, Test): Q12, yes/no/leave out; default leave out. An unplaced value stops the build (`OUTCOME_VALUE_NOT_MAPPED`): ask, never guess.
- Over 20 distinct values: not yes/no (`OUTCOME_TOO_MANY_VALUES`): an amount (AMOUNT_TARGET, not in this version) or a threshold outcome in words.

## Logs and campaigns
- One row per customer first (sql_steps). Guests (blank ID): neither yes nor no. A return (`C` invoice) is not a purchase: count distinct non-cancellation orders after `$as_of`.
- Campaigns (R19) without a random holdout (Q13): "who responds among those contacted", never extra sales. "Delivered" is no response; "opened" is never an outcome. Count windows from each send date; ask if same-day purchases count and how overlapping campaigns share credit.

## Ask when
- the window; several candidate outcome columns, files or dates;
- too few positives: say TOO_FEW_POSITIVES, offer a looser definition or more data; never reword to hit the minimum.
