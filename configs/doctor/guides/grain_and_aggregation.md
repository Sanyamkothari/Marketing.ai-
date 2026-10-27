# Guide: grain and aggregation (read before summing, counting, or turning events into one row per customer)

## Rules
- Count rows per key first (R4, Q21): `describe_repeats` on the ID, `describe_duplicates` on the key. One row per ID is customer grain; many are events, lines or snapshots: say which.
- Aggregate before you join (each table to customer grain); never join lines to customers then sum.
- An order total repeated on every line is taken once per order (MAX), not summed.
- Count orders, not lines: frequency = distinct orders; items = sum of quantity; line total = quantity x unit price. Never mix.
- Status logs: latest row per ticket first, then count; time is `$as_of` only.
- Payments: ask which statuses are real money (Q22); net refunds against the original.
- Blanks after aggregation need a meaning (R12, Q5): no orders in a window is 0; a window outside the table's date coverage is blank. Keep look-back within shortest coverage.

## Query
- One SELECT. State `expect` before running: `rows_out_equals`, `keys_unique`, `totals_kept`, `max_keys_lost_pct`. Never rewrite it after failure.
- Queries read the file after accepted per-column fixes: use the fixed columns, not raw text.
- Outcome from rows after the snapshot: set the outcome window and `reads_future`; as-of at least the window before data end.

## Order-log pattern
- Lines -> invoices (sum quantity x price) -> customers (orders, spend, first/last date, recency). Expect rows_out equal to customer count, keys unique.
- Combining rows is not losing customers: read customers lost and doubled in the receipt.
- A window that cannot finish before the data ends cannot be observed: train on an earlier snapshot, score on the later. A partial last month is never the as-of; count the window check from the last complete day.

## Traps
- Summing a repeated order total; counting lines as orders; missing windows as 0 (a new customer has no 365-day history: blank); averaging per-line when per order or customer is meant.
- Percentages stored 0-100 in one place and 0-1 in another (R13, Q6): compare medians first.

## Ask when
- What one row is (Q21); who counts as a customer on the as-of date (Q10); the file may be an extract (Q20), so do not compare totals with the whole business.
