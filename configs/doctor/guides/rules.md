# Guide: the data rules R1 to R29 (read the section for the job before a join, a total, a date, an outcome, consent or a campaign)

## Rules
- Each rule has an id (R1 ...) that the plan, the questions and the other guides cite. Read the section for the job in hand; a rule applies whenever its words match the files.
- Sections: Keys and IDs, Lookups and groups, Grain and totals, Blanks, Dates and the as-of date, Date formats, Rates over time, Outcome, Outcome by group, Values and units, Files and sheets, Samples and long text, Consent and sensitive data, Campaign history, Junk accounts.

## Keys and IDs
- R1. Before any join, check key coverage and quality for every pair of tables and show matched and unmatched shares in the plan. Write down what each key names. Never join because two column names match.
- R2. With several ID systems, build one crosswalk table from the master (customer ID, ID type, cleaned value) and join every event table through it. Ask Q1 for the main ID. Report the share matched per ID type.
- R3. Read IDs, phones, PIN codes and account numbers as text. Strip a leading `'` and `="..."` wrappers. Pad with zeros only after Q3 gave the length. Keep the case of case-sensitive IDs; match Salesforce IDs on their first 15 characters. Strip the country code (+91, or 91 on 12-digit numbers) from phones.

## Lookups and groups
- R22. Before a lookup join, check the lookup key is unique. Count codes not found, label them "Unmapped" (never quietly "Other") and ask.
- R29. With household or account IDs, keep each group together in the train and test split, and remove duplicates from the contact list by household.

## Grain and totals
- R4. Check the grain before summing: count rows per key in each table. A money value repeated on every line of an order is taken once (MAX), not summed. Reduce status logs to the latest row per ticket, then count. For payments, ask which statuses are real money; net refunds against the original. Tables at different grains: aggregate first, then join.
- R11. Before a minimum-zero rule on a money column, count the negatives and check whether they pair with earlier sales (same customer, same amount). If so, ask about refunds and derive gross spend, refund amount and refund count.

## Blanks
- R12. After every join or aggregation, decide per group of columns what a blank means: zero, unknown, not applicable or not yet (Q5). A period outside a table's date coverage is blank, not 0.
- R15. Fill a blank with the value above only after asking whether it means the same as the row above.

## Dates and the as-of date
- R5. Never use now(), CURRENT_DATE or a typed-in date in SQL; every time calculation uses the case's `$as_of`. Recompute tenure, age and days-since from the underlying dates, not exported columns.
- R6. Type each attribute file: dated history, snapshot as of a date, or true only today. In a today-only file, mark state-like columns (status, is_active, tier, plan, balance, credit limit, `last_*`, `*_flag`, `days_since_*`) and propose to drop them, recompute them as of the date, or request history. Use the date the information became known, not the period date. For valid-from/valid-to tables, take the row valid on the as-of date; check each customer has one.
- R23. Set the as-of date to the last complete day or month. Leave out a thin, still-arriving tail and a partial last month.

## Date formats
- R20. If some dates carry a time-zone offset or `Z` and others do not, ask which zone the plain ones use, and convert before cutting to a date.
- R21. If one column mixes day-first and month-first dates, stop. Test each order against related dates (order date before delivery date), check each source file separately, and use one SQL CASE per source.

## Rates over time
- R8. For a ready-made 0/1 outcome with a date, compute the yes rate by month. If the last 1-2 months are over 40% below the earlier level, ask whether to drop them. For "within 60 days of the first order" outcomes, use each customer's own start date and drop customers whose window has not finished.
- R28. If a month's rate or volume is over 2 times the median, ask whether it was a sale, a festival or a change in recording. Offer a festival flag, leaving the period out, or data after the change.

## Outcome
- R7. Every outcome is an eligible population plus an event plus a window plus a start date. Churn needs earlier activity. Cross-sell excludes current holders. Win-back covers lapsed customers only. Renewal covers policies expiring in the window. For start and end tables, keep rows with start on or before the as-of date and end after it. Always show customers excluded as not eligible.
- R9. Propose a churn window from the customers' own buying rhythm (median and 90th-percentile gap between purchases). Show the yes rate for each option. Warn above 70% or below 3%.
- R10. When the outcome is built from column X, drop X and similar columns (same prefix, same source date, strong match) and list them for confirmation. With several outcome-like columns, ask which is the target and propose dropping the others, not pre-ticked. Write the label as a plain sentence.

## Outcome by group
- R16. After the outcome is set, compute its rate by every business unit, brand, product, region or channel column. If rates differ more than 5 times, ask: one model, separate outcome rules per group, or one model per group.

## Values and units
- R13. Check the money scale. Read headers and title rows (lakh, crore, Cr, Mn, '000, paise, cents, USD). Compare medians across files that join or stack: a ratio near 100 means paise, near 1.18 means GST. For percentages, check the share of values at or below 1. Confirm with Q6 before converting.

## Files and sheets
- R14. List every sheet of a workbook in the plan. Sheets with the same columns are stacked, keeping the sheet name. A two-column code and meaning sheet becomes a value map the person confirms. Skip a sheet only on an explicit "skip". Dates in file or sheet names become a snapshot date, confirmed with Q8.
- R25. A file with no ID column, columns named count, sum or total, and few rows is a summary report. Say so and request a row-level file. Never invent rows.

## Samples and long text
- R26. Ask whether the file holds all customers or an extract (Q20). Assume the file may be sorted. Decide formats and value lists from a random sample.
- R27. Long text columns are hidden from you. Make flags such as "intends to cancel" only by SQL keyword matching (ILIKE) from a word list the person approves, with Hinglish words such as band, port, refund.

## Consent and sensitive data
- R18. Always ask which way a DND or consent column points (Q14). A blank means unknown, never yes. Ask about DND-Partial. Build consent from a separate log as the latest event on or before the as-of date. Suppression files and do-not-contact statuses (deceased, fraud, legal, minors, "Do not call") go to the contact list; they stay in training unless told otherwise.
- R24. For sensitive columns and values (religion, caste, health, disability, pharmacy), ask how they may be used (Q15); the default is not used for targeting. Mark PIN code and surname as possible stand-ins when a sensitive column exists. Never guess gender from names or titles.

## Campaign history
- R19. Ask Q13 about a random holdout. Without one, call the result "who responds among those contacted"; never claim extra sales from the campaign. Count each campaign's window from its send date. "Delivered" is not a response; "opened" is never an outcome. Ask whether a purchase on the send day counts, and how to credit overlapping campaigns.

## Junk accounts
- R17. Run the junk-account query: test, dummy, demo or QA strings; company email domains; phones shared by many customers, one repeated digit or in sequence; round-number birth dates; the top 0.1% of customers by volume. Ask whether to exclude each group from training, the contact list, or both.
