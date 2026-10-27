# Guide: time and the as-of date (read when a file has dates, before choosing the as-of date, a snapshot, or any "days since" number)

## Rules
- Queries use `$as_of`, never a typed date, now() or CURRENT_DATE (R5; `SQL_FIXED_DATE`). A date the person names is recorded as the as-of (Q8 "I will type a date" or `as_of` on the step).
- Nothing after the as-of reaches a feature; only the outcome reads later rows (`reads_future: true`, `outcome_window_days`).
- Training and scoring are two snapshots: as-of at least the outcome window before data ends (`OUTCOME_WINDOW_PAST_DATA`); train on the earlier one, score on the later one.
- A thin or partial last month is never the as-of (R23); the scoring snapshot is the last complete day or month end, kept as a rule so each file recomputes it.
- Recompute tenure, age, days-since from dates and `$as_of`, not exported columns (R5).
- The plan states as-of, data end and outcome window (receipt numbers only); the person approves the as-of first.

## Last complete day
- `describe_dates` shows data end and longest gap; count rows per day near the end with a query (GROUP BY day, ORDER BY day DESC, LIMIT n) or `rate_by`.
- Rows after the scoring snapshot fill a training window only if that tail is complete; if dropped, count the window check from the last complete day.

## Reading dates
- Find date columns (`find_roles`, `describe_dates`, `find_format_issues`); use the date it became known, not the period date (Q7).
- Day order, time zones and Excel serials (R20, R21, Q18): see cleaning_and_formats. Keep `parse_date` per column; SQL uses the fixed column.

## Files taken today
- (R6) Status, tier, balance, `is_active`, `last_*`, `*_flag`, `days_since_*` hold today's values: for a past as-of drop, recompute, or `request_data`; never keep silently.
- Valid-from / valid-to tables: take the row valid on the as-of.

## Unusual months
- (R8) Last 1-2 months of a 0/1 outcome over 40% below earlier are not final: ask to drop (`rate_by` month).
- (R28) A month over twice the median (Diwali, sale): ask; offer a festival flag, leave it out, or use data after the change.

## Ask when
- Q8: dates disagree (latest date, file name, days-since) (default: last complete month). Q7: several "when known" dates (default: leave out). Q9: current-state columns without history. Q18: dates like 03/04 unprovable, or year start and zone unknown.
