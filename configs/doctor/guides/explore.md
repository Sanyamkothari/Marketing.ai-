# Guide: exploring the files (read before your first look, and whenever a table surprises you)

## Rules
- Look at every table before planning; say what you did and did not look at; cite evidence ids.
- Count rows per key before summing (R4); ask Q21 when data cannot settle what one row is.
- Never read only the top rows: file may be sorted (R26).
- Never drop returns, guests or duplicates silently.
- Never use a partial last month as the as-of date.
- Check key coverage for every pair before any join (R1).
- Numbers come only from tool results or the person's words.

## Order of looks
- `get_profile` per file, then `find_roles`; `sample_rows`, `value_counts`; `describe_dates`, `describe_numbers`, `describe_missing`, `describe_duplicates`.
- `compare_columns` (order before delivery); `rate_by` outcome by business column (R16). Only then plan.

## One row
- Lines are not orders, orders not customers (see grain_and_aggregation): count distinct invoices; money repeated on every line is taken once (MAX).
- No ID column, columns named count/sum/total, few rows: a summary report (R25); request row-level file.

## Traps
- Invoice numbers starting `C` pair with earlier sales (same customer, equal and opposite amount): check with `find_values`, `compare_columns`; ask what `C` means by quick question (cancellation, return, other) or `express_doubt` with `meaning: true`, not Q12 (R11).
- Blank customer ID = guest checkout: leave out, report the count, never invent an ID.
- Exact duplicate lines: `describe_duplicates`, ask, drop only on yes.
- Partial last month: count rows per day near the end with a query (GROUP BY day, ORDER BY day DESC, LIMIT n); a thin tail is still arriving (R23). Scoring snapshot: last complete day or month; confirm Q8.
- Dates (R20, R21), rounded IDs (R3), lakh/crore/paise/GST/currency (R13, Q6): see cleaning_and_formats.
- Junk accounts (test, demo, QA, shared phones, top 0.1% by volume, R17): count, ask.

## Files and time
- Different grains: aggregate first (R4); join checks in keys_and_joins. A period outside coverage is blank, not 0 (R12).
- Outcome = eligible group + event + window + start date (R7). Time logic uses `$as_of` only (R5). Check yes rate by month (R8).

## Ask when
- Meaning is at stake, duplicates might be genuine, a spike might be a sale (R28), or a file might be an extract (Q20); if values settle it, move on.
