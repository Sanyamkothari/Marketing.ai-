# Data Doctor trap bench (v0)

30 planted traps, each run 2 times with no AI (the rules path). A trap passes only if every run passes every check. The result data is graded, never the Doctor's words.

**Passed 30 of 30; failed 0; runs that disagreed with each other: 0.**

4 of the passes are untested: the case has several files but nothing was joined or stacked, so its join traps were never sprung.

Many traps fail today. That is expected: the bench measures, it does not have to pass.

## What was graded

| Check | Graded | Failed |
|---|---|---|
| No silent row loss | 30 | 0 |
| No unapproved change | 34 | 0 |
| Correct join (keys not merged, one row per customer) | 4 | 0 |
| No double counting (totals) | 5 | 0 |
| No leak column kept | 3 | 0 |
| Outcome population correct | 4 | 0 |
| Contact-list exclusions applied | 6 | 0 |
| Asked the right question | 3 | 0 |

## The traps

| Trap | Scenario | Result | What went wrong |
|---|---|---|---|
| c01_selection_not_random | C01 | pass |  |
| c06_dnd_direction | C06 | pass |  |
| c09_already_hold_product | C09 | pass |  |
| crit03_dup_and_drop | crit-03 | pass, untested | nothing was joined or stacked |
| f01_leading_zeros | F01 | pass, untested | nothing was joined or stacked |
| f02_excel_e_plus | F02 | pass |  |
| f04_merged_cells_region | F04 | pass |  |
| f05_pivot_summary | F05 | pass |  |
| f21_overlapping_exports | F21 | pass |  |
| k13_household | K13 | pass |  |
| k15_failed_payments | K15 | pass |  |
| k16_ticket_status_log | K16 | pass |  |
| k1_merged_prefixes | K1 | pass, untested | nothing was joined or stacked |
| k3_shared_phone | K3 | pass, untested | nothing was joined or stacked |
| p7_minors | P7 | pass |  |
| p8_deceased_fraud | P8 | pass |  |
| s01_never_active | S01 | pass |  |
| s05_cancellation_reason | S05 | pass |  |
| t01_mixed_date_order | T01 | pass |  |
| t03_placeholder_dates | T03 | pass |  |
| t06_status_exported_today | T06 | pass |  |
| t07_immature_last_month | T07 | pass |  |
| t13_outcome_from_column | T13 | pass |  |
| t15_partial_last_month | T15 | pass |  |
| v01_paise_vs_rupees | V01 | pass |  |
| v03_refunds_negative | V03 | pass |  |
| v04_blank_vs_zero | V04 | pass |  |
| v06_test_accounts | V06 | pass |  |
| v13_consent_spellings | V13 | pass |  |
| v19_order_total_repeated | V19 | pass |  |

## Each trap

### c01_selection_not_random (C01, Campaign history: selection read as cause)

Staff texted only high-balance customers (12% bought against 3% of the rest); there is no random control group.

Expected: The Doctor asks whether anyone was held out at random; without that it never calls the gap an effect of the text.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: asks whether a random group was left uncontacted. asked: Q13 HOLDOUT In the past campaign recorded in “contacted”, was a random group deliberately 

### c06_dnd_direction (C06, Consent, sensitive data and who may be contacted)

do_not_contact = Y means the customer must NOT be contacted; read as a consent flag, Y means 'may contact'.

Expected: Everyone with do_not_contact = Y is off the contact list; everyone with N is on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: do-not-contact customers are off the list (the flag points the right way). the list leaves out everyone it must (by the contact-list gate alone (no consent rule))

### c09_already_hold_product (C09, Outcome: who is eligible, and when the clock runs)

Two in five policyholders already hold the vehicle policy being cross-sold, so they can never 'buy' it.

Expected: Existing holders are not counted in the outcome; the true response rate among the others is 23.6%.

- pass: every customer of the main file is still there. all 72 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: customers who already hold the product are not in the outcome. asked: Q23 WHO CAN RESPOND 48 customers in policyholders.csv already hold the product. 0 of them 

### crit03_dup_and_drop (crit-03, SQL steps slip past the safety limits)

Twenty customers have two tag rows and twenty have none, so a join adds 20 and drops 20 and the row count stays 100.

Expected: Exactly the 100 customers, each once: neither the duplicates nor the dropped customers may cancel out.

- pass: every customer of the main file is still there. all 100 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 100 customers once each (duplicates and drops are not netted). 100 rows, one per customer

### f01_leading_zeros (F01, False, merged or missed joins)

Customer IDs like 000101 and PIN codes like 012341 start with zeros that a number reader drops.

Expected: IDs and PIN codes come out exactly as written, with their leading zeros.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: IDs and PIN codes keep their leading zeros. the values are as the file had them

### f02_excel_e_plus (F02, False, merged or missed joins)

Excel saved the 12-digit customer IDs as 1.23457E+11, so 40 customers now share a handful of IDs.

Expected: The Doctor says the IDs were rounded and asks for a re-export as text; all 40 rows are kept and none is joined.

- pass: every customer of the main file is still there. all 13 customers are in the result
- pass: says the IDs were rounded by Excel and asks for a re-export. asked: DIFFERENT FILE REQUESTED This file cannot be used as it is. Ask whoever exported the file 
- pass: all 40 rows are kept. 40 rows, as in the file

### f04_merged_cells_region (F04, Spreadsheet and export shapes the reader misses)

Region is written once per block (a merged cell) and left blank on the 14 rows below it.

Expected: Region is filled down to every row of its block, or the Doctor asks whether a blank means 'same as above'.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 56 blank regions are filled or asked about. asked: BLOCK BLANKS 'region' is filled on 4 rows and blank on the 56 rows below them, the way mer

### f05_pivot_summary (F05, Spreadsheet and export shapes the reader misses)

The file is a pivot table pasted as values: counts per region and plan, a Grand Total row and no customer ID.

Expected: The Doctor says this is a summary, not customers, and asks for the row-level export; it invents no rows.

- pass: says it is a summary and asks for one row per customer. asked: DIFFERENT FILE REQUESTED This file cannot be used as it is. Ask whoever exported the file 

### f21_overlapping_exports (F21, Double counting and wrong grain)

The February export repeats the last 25 calls of January, so stacking the two files counts them twice.

Expected: January and February are stacked into one table of 150 calls, each call once.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 150 calls once each after stacking the two exports. 150 rows, one per customer

### k13_household (K13, Who one row is, and one model or several)

Twenty households have three members each with consent; texting everyone sends three messages to one household.

Expected: At most one person per household is on the contact list.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: no household gets more than one message. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)

### k15_failed_payments (K15, Double counting and wrong grain)

One payment in five is a failed attempt; adding the amount column up counts money that never arrived.

Expected: Only successful payments are counted (69,918), or the Doctor asks which statuses are real money.

- pass: every customer of the main file is still there. all 48 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: spend counts successful payments only. asked: Q22 REAL MONEY Which values of “status” in payments.csv are real money? 12 of 60 rows say 

### k16_ticket_status_log (K16, Double counting and wrong grain)

Each complaint ticket has two to four status rows (opened, assigned, reopened, closed); counting rows counts a complaint many times.

Expected: 30 tickets, each once with its latest status, or the Doctor asks how to count them.

- pass: every customer of the main file is still there. all 30 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 30 tickets counted once each. asked: Q21 ONE ROW Each “ticket_id” in tickets_log.csv has several rows. The status changes from 

### k1_merged_prefixes (K1, False, merged or missed joins)

Customer IDs C-101 and S-101 are two different people; the orders file writes both as 101.

Expected: 60 customers stay 60: a rule that strips the prefix must not merge C-101 and S-101 into one row.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 60 customers stay 60 rows, none merged. 60 rows, one per customer
- pass: C-101 and S-101 each keep their own segment. the named customers have their own values

### k3_shared_phone (K3, False, merged or missed joins)

Ten customers share the dummy phone 9999999999 and eight share a family phone; the calls file is keyed by phone.

Expected: 50 customers stay 50 rows: calls must not be handed to every customer who shares a number.

- pass: every customer of the main file is still there. all 50 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 50 customers stay 50 rows when calls are keyed by a shared phone. 50 rows, one per customer

### p7_minors (P7, Consent, sensitive data and who may be contacted)

Ten customers are under 18 on the as-of date, and every customer has ticked marketing consent.

Expected: Minors are off the contact list even though they have consent; adults with consent are on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: customers under 18 are off the contact list. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)

### p8_deceased_fraud (P8, Consent, sensitive data and who may be contacted)

Some customers are deceased, flagged for fraud, under legal hold or closed, yet all show marketing consent.

Expected: Deceased, fraud, legal-hold and closed customers are off the contact list.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: deceased, fraud, legal-hold and closed customers are off the list. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)

### s01_never_active (S01, Outcome: who is eligible, and when the clock runs)

Churn is 'no order in 90 days', so 30 customers who never ordered at all are labelled churners.

Expected: The 30 never-active customers are not counted in the outcome; the true churn rate is 25.7%, not 44%.

- pass: every customer of the main file is still there. all 70 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: never-active customers are not in the outcome population. asked: Q23 WHO CAN RESPOND 30 customers in customers.csv were never active. 30 of them are marked

### s05_cancellation_reason (S05, Time travel and leaks the checks miss)

cancellation_reason is filled in only for customers who left, so 'is it filled in' gives the answer away.

Expected: cancellation_reason is dropped from the features (or the Doctor asks), even though it is blank for most rows.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: cancellation_reason is out of the features. the leak columns are out

### t01_mixed_date_order (T01, Dates that are read wrong without any error)

signup_date holds day/month in system A rows and month/day in system B rows, so 03/04/2026 can be two dates.

Expected: The Doctor stops and asks the date order for each system, or leaves the dates as written; it never reads them all one way.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: dates are not silently read in one order. the values are as the file had them

### t03_placeholder_dates (T03, Dates that are read wrong without any error)

A quarter of the customers have last_payment_date 1970-01-01, the system's stand-in for 'never paid'.

Expected: 1970-01-01 is not kept as a real date: it becomes blank, or the Doctor asks what it means.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 1970-01-01 stand-in is not treated as a real date. asked: DATE PLACEHOLDER 20 rows (25%) of 'last_payment_date' are 1970-01-01, which is usually a b

### t06_status_exported_today (T06, Time travel and leaks the checks miss)

The customer master was exported today, so is_active already says who left: it is 0 for every churner.

Expected: is_active is not left among the columns the model learns from (it is known only after the outcome).

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: is_active (today's status) is out of the features. the leak columns are out

### t07_immature_last_month (T07, Outcome: who is eligible, and when the clock runs)

The newest month's outcome window has not finished, so its churn rate is 3% against about 25% before.

Expected: The unfinished month (2026-09) is left out of the outcome, or the Doctor asks; the true rate is 22.8%.

- pass: every customer of the main file is still there. all 180 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the unfinished last month is not counted as 'did not leave'. asked: THIN LAST PERIOD The newest rows of 'snapshot_month' look unfinished. The newest month is 

### t13_outcome_from_column (T13, Time travel and leaks the checks miss)

The churn flag was built from account_status, which stays in the file as an ordinary column.

Expected: account_status, the column the outcome came from, is dropped from the features.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: account_status (the outcome's source) is out of the features. the leak columns are out

### t15_partial_last_month (T15, Outcome: who is eligible, and when the clock runs)

The newest month, 2026-10, covers only 12 days, so its usage looks like a collapse for every customer.

Expected: The partial month is left out, or the Doctor asks; it is never treated as a full month.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 12-day month is not used as a full month. asked: THIN LAST PERIOD The newest rows of 'month' look unfinished. The newest month is only part

### v01_paise_vs_rupees (V01, Money and units whose meaning is not in the cells)

The second file's amounts are in paise (100 times larger) and the first file's are in rupees.

Expected: The two files are stacked in one unit and the total is 198,508 rupees, or the Doctor asks which unit each file uses.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: amounts from both files add up in one unit (rupees). asked: Q06 UNIT SCALE What unit is amount in? Its values look like two units: they are about 73 t

### v03_refunds_negative (V03, Money and units whose meaning is not in the cells)

One transaction in six is negative: a refund, which is a strong signal that must not be clipped to zero.

Expected: The negative amounts stay as they are (refunds), or the Doctor asks about them.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: negative amounts (refunds) are not clipped or dropped. the values are as the file had them

### v04_blank_vs_zero (V04, Money and units whose meaning is not in the cells)

last_purchase_amount is blank for customers who never bought; filling it with 0 makes them look like small buyers.

Expected: Blank stays blank (never bought), or the Doctor asks what blank means.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: blank 'never bought' is not turned into 0. the values are as the file had them

### v06_test_accounts (V06, Test, staff and extreme accounts)

Eight QA accounts (TEST001..., qa+...@company.example) sit in the customer list with marketing consent.

Expected: The test accounts are off the contact list; the real customers are on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: test accounts are off the contact list. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)

### v13_consent_spellings (V13, Consent, sensitive data and who may be contacted)

Consent is written Y, yes, Haan or a tick for agreed, N, nahi or No for refused, and left blank when unknown.

Expected: Y/yes/Haan/tick customers are contactable; N/No/nahi and blank (unknown) customers are not.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: every spelling of yes is on the list; refused and unknown are off it. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)

### v19_order_total_repeated (V19, Double counting and wrong grain)

The order total is written on every line item of the order, so adding the column up counts each order 2 to 4 times.

Expected: The order total is counted once per order (120,964), or the Doctor asks which level the total is on.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: order totals add up to the true spend, not a repeated one. asked: Q21 ONE ROW Each “order_id” in order_lines.csv has several rows. The order total is the sa

