# Guide: returns, duplicates and guest checkouts (read when an order log has negative quantities, repeated rows, or blank customer IDs)

## Rules
- Usually business facts, not errors: each is a question of meaning, never a quiet delete.
- Negatives or `C` invoices: count them, confirm they pair with earlier sales, then ask what they mean.
- Never apply a minimum-zero rule (`valid_range`) to a column with returns.
- Exact duplicates: ask first; safe choice keeps them and says totals may be overcounted.
- Blank customer ID rows belong to no customer: leave them out of the customer table and say how many lines and how much spend.
- A refund is known only after the sale: for snapshot D use only returns dated on or before D.
- Never merge duplicate customers (same phone or name) on similarity; ask (Q2).

## Returns
- (R11) Look for the pairing sale (same customer, same item, equal and opposite amount): `describe_numbers` for the count, `sample_rows` with `where_column` and `equals` for examples.
- Ask what `C` or negatives mean: quick question (cancellation, return, other) in chat, or `express_doubt` with `meaning: true` in review. Q12 is only yes/no/leave out; Q22 is for payment statuses.
- Once confirmed: keep gross spend, return amount and return count as own columns and net against spend. Return rate = returned items / items; blank when items are 0.

## Duplicates
- `describe_duplicates` on the whole row; drop only on "double scans", reporting the count.
- A duplicate differing in any column is a conflict (Q11 which copy wins), not a duplicate; Q11 is not for exact duplicate lines.

## Guests
- Two blanks are never one customer; not scored, contacted or counted.
- Check `describe_missing`, `value_counts` and `describe_placeholder_values` for placeholders (0, -1, "guest").
- Keep them for shop-wide totals if the person wants.

## Traps
- Netting a return against the wrong customer or period; removing duplicates before asking; counting a cancelled order as an order; guest lines deleted silently; a refund feature when the outcome is churn after the refund (a leak).

## Ask when
- Negatives or `C` might be returns, exact repeats might be real purchases, or blank IDs might be errors or not yet known (Q5).
- Excluded customers would change the yes rate or number scored: show counts.
