# Guide: cleaning and formats (read when a column is text that should be a number, date, yes/no or ID, or a value looks like Indian or foreign formatting)

## Rules
- Clean the column the model reads, never the file; each fix is a step the person approves. Look first (`find_format_issues`, `value_counts`, `find_values`, `describe_numbers`, `describe_dates`) at the whole file or a random sample.
- One step, one column, one reason; kinds (`parse_number`, `parse_date`, `map_boolean`, `normalise_text`, `map_values`, `convert_units`, `remove_rows`) before `propose_sql`; cite `evidence_ids`.
- Unreadable values stay blank, counted. A fix changing over 30% of a column warns; over 60% is refused: ask.
- IDs, phones, PIN codes, account numbers are text: never average, scale or round; pad zeros only after Q3 gives the length.
- A blank is unknown (Q5), never 0/"No". `na` is never mapped: ask (Q12).
- Queries read the file after accepted fixes: never repeat a fix in SQL.

## Numbers
- Strip symbols/separators; `decimal` `,` only if proven.
- Lakh/crore (`₹2.5 L`, `1.2 Cr`, `12K`): `parse_number` with `indian_scale_words`; `3,50,000` = 350000. Bare `L`/`K`/`M` with no money sign: ask.
- Units (Q6): read headers; compare medians across joining files (ratio ~100: paise; ~1.18: GST). Confirm Q6 before `convert_units`.
- Never add currencies; a mixed Rs/$ column stops: ask which column names the currency. Conversion needs the person's dated, sourced rate; else report each alone.
- Placeholders (`NA`, `9999`, `TBD`): `describe_placeholder_values` first. A negative may be a refund (R11): ask before `valid_range`.

## Labels
- haan / nahi / हाँ / नहीं / ✓: `map_boolean` only when both sides appear.
- Merge look-alike spellings (`Mumbai / Bombay`) via `map_values` only as confirmed; unmatched codes: "Unmapped", not "Other".

## Dates
- DD/MM vs MM/DD: a part above 12 in one slot proves the day; if none, both fit: check a related date, then ask Q18. Mixed in one column: stop (R21), one rule per source file.
- Excel serials (`45123`) are dates. `Z`/offset on some values only: ask the plain ones' zone (R20).
- Recompute tenure, age, days-since at `$as_of`; no typed dates or `now()`.

## Phones and IDs
- Mobile: 10 digits from 6-9; strip `+91`, `0091`, `91` (on 12 digits), leading `0`; same form in both files; never fuzzy-match.
- Strip leading `'` and `="..."`. Excel-rounded IDs (`1.23457E+11`): ask re-export as Text. Mixed lengths: Q3.
