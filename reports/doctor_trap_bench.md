# Data Doctor trap bench (v0)

30 planted traps, each run 2 times with no AI (the rules path). A trap passes only if every run passes every check. The result data is graded, never the Doctor's words.

**Passed 6 of 30; failed 24; runs that disagreed with each other: 0.**

3 of the passes are untested: the case has several files but nothing was joined or stacked, so its join traps were never sprung.

Many traps fail today. That is expected: the bench measures, it does not have to pass.

## What was graded

| Check | Graded | Failed |
|---|---|---|
| No silent row loss | 30 | 0 |
| No unapproved change | 34 | 5 |
| Correct join (keys not merged, one row per customer) | 4 | 0 |
| No double counting (totals) | 5 | 4 |
| No leak column kept | 3 | 3 |
| Outcome population correct | 4 | 4 |
| Contact-list exclusions applied | 6 | 6 |
| Asked the right question | 3 | 3 |

## The traps

| Trap | Scenario | Result | What went wrong |
|---|---|---|---|
| c01_selection_not_random | C01 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, OUTCOME_COLUMN, TRAINING_BUDGET) |
| c06_dnd_direction | C06 | FAIL | 27 people who must not be contacted are on the list: C0001, C0004, C0007, C0010 and 23 more; 53 people who may be contacted are left off: C0002, C0003, C0005, C |
| c09_already_hold_product | C09 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / 48 who cannot have the outcome are counted: P0001, P0005, P0006, P0010 and 44 more; the positive rate is |
| crit03_dup_and_drop | crit-03 | pass, untested | nothing was joined or stacked |
| f01_leading_zeros | F01 | FAIL | 120 values changed: customer_id of 000101: '000101' became '101', pin_code of 000101: '091751' became '91751', customer_id of 000102: '000102' became '102' and  |
| f02_excel_e_plus | F02 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) |
| f04_merged_cells_region | F04 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / 56 rows of 'region' still say  |
| f05_pivot_summary | F05 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, DATA_REQUESTED, TRAINING_BUDGET) |
| f21_overlapping_exports | F21 | pass |  |
| k13_household | K13 | FAIL | 20 households have more than one person on the list (by the 'marketing_opt_in' column) |
| k15_failed_payments | K15 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / no column adds up to the true 69,918.00 (amount = 89,978.00) |
| k16_ticket_status_log | K16 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / 30 customers appear more than once: TK001, TK002, TK003, TK004 and 26 more |
| k1_merged_prefixes | K1 | pass, untested | nothing was joined or stacked |
| k3_shared_phone | K3 | pass, untested | nothing was joined or stacked |
| p7_minors | P7 | FAIL | 10 people who must not be contacted are on the list: C0001, C0009, C0017, C0025 and 6 more (by the 'marketing_opt_in' column) |
| p8_deceased_fraud | P8 | FAIL | 16 people who must not be contacted are on the list: C0001, C0006, C0011, C0016 and 12 more (by the 'marketing_opt_in' column) |
| s01_never_active | S01 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / 30 who cannot have the outcome are counted: C0001, C0002, C0003, C0004 and 26 more; the p |
| s05_cancellation_reason | S05 | FAIL | cancellation_reason still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET) |
| t01_mixed_date_order | T01 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, GUIDED_DATE_ORDER, TRAINING_BUDGET) / 80 values changed: signup_date of C0001: '08/09/2025' became '2025-09- |
| t03_placeholder_dates | T03 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / 20 rows of 'last_payment_date' still say 1970-01-01 |
| t06_status_exported_today | T06 | FAIL | is_active still in the table the model would learn from / nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) |
| t07_immature_last_month | T07 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / 30 who cannot have the outcome are counted: R0181, R0182, R0183, R0184 and 26 more |
| t13_outcome_from_column | T13 | FAIL | account_status still in the table the model would learn from / nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) |
| t15_partial_last_month | T15 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / 30 rows of 'month' still say 2026-10 |
| v01_paise_vs_rupees | V01 | FAIL | nothing like it was asked (it asked: KEY_AMBIGUOUS, KEY_AMBIGUOUS, OUTCOME_COLUMN, TRAINING_BUDGET, KEY_AMBIGUOUS and 1 more) / no column adds up to the true 19 |
| v03_refunds_negative | V03 | pass |  |
| v04_blank_vs_zero | V04 | pass |  |
| v06_test_accounts | V06 | FAIL | 8 people who must not be contacted are on the list: TEST001, TEST002, TEST003, TEST004 and 4 more (by the 'marketing_opt_in' column) |
| v13_consent_spellings | V13 | FAIL | 18 people who may be contacted are left off: C0004, C0006, C0014, C0016 and 14 more (by the 'marketing_opt_in' column) |
| v19_order_total_repeated | V19 | FAIL | nothing like it was asked (it asked: TRAINING_BUDGET) / no column adds up to the true 120,964.00 (order_total = 377,802.00) |

## Each trap

### c01_selection_not_random (C01, Campaign history: selection read as cause)

Staff texted only high-balance customers (12% bought against 3% of the rest); there is no random control group.

Expected: The Doctor asks whether anyone was held out at random; without that it never calls the gap an effect of the text.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: asks whether a random group was left uncontacted. nothing like it was asked (it asked: KEY_AMBIGUOUS, OUTCOME_COLUMN, TRAINING_BUDGET)

### c06_dnd_direction (C06, Consent, sensitive data and who may be contacted)

do_not_contact = Y means the customer must NOT be contacted; read as a consent flag, Y means 'may contact'.

Expected: Everyone with do_not_contact = Y is off the contact list; everyone with N is on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: do-not-contact customers are off the list (the flag points the right way). 27 people who must not be contacted are on the list: C0001, C0004, C0007, C0010 and 23 more; 53 people who may be contacted are left off: C0002, C0003, C0005, C0006 and 49 more (by the 'do_not_contact' column)

### c09_already_hold_product (C09, Outcome: who is eligible, and when the clock runs)

Two in five policyholders already hold the vehicle policy being cross-sold, so they can never 'buy' it.

Expected: Existing holders are not counted in the outcome; the true response rate among the others is 23.6%.

- pass: every customer of the main file is still there. all 72 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: customers who already hold the product are not in the outcome. nothing like it was asked (it asked: TRAINING_BUDGET) / 48 who cannot have the outcome are counted: P0001, P0005, P0006, P0010 and 44 more; the positive rate is 14.2%; the true rate is 23.6%

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
- FAIL: columns no approved change touched keep the file's values. 120 values changed: customer_id of 000101: '000101' became '101', pin_code of 000101: '091751' became '91751', customer_id of 000102: '000102' became '102' and 117 more
- FAIL: IDs and PIN codes keep their leading zeros. 120 values changed: customer_id of 000101: '000101' became '101', pin_code of 000101: '091751' became '91751', customer_id of 000102: '000102' became '102' and 117 more

### f02_excel_e_plus (F02, False, merged or missed joins)

Excel saved the 12-digit customer IDs as 1.23457E+11, so 40 customers now share a handful of IDs.

Expected: The Doctor says the IDs were rounded and asks for a re-export as text; all 40 rows are kept and none is joined.

- pass: every customer of the main file is still there. all 13 customers are in the result
- FAIL: says the IDs were rounded by Excel and asks for a re-export. nothing like it was asked (it asked: TRAINING_BUDGET)
- pass: all 40 rows are kept. 40 rows, as in the file

### f04_merged_cells_region (F04, Spreadsheet and export shapes the reader misses)

Region is written once per block (a merged cell) and left blank on the 14 rows below it.

Expected: Region is filled down to every row of its block, or the Doctor asks whether a blank means 'same as above'.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: the 56 blank regions are filled or asked about. nothing like it was asked (it asked: TRAINING_BUDGET) / 56 rows of 'region' still say 

### f05_pivot_summary (F05, Spreadsheet and export shapes the reader misses)

The file is a pivot table pasted as values: counts per region and plan, a Grand Total row and no customer ID.

Expected: The Doctor says this is a summary, not customers, and asks for the row-level export; it invents no rows.

- FAIL: says it is a summary and asks for one row per customer. nothing like it was asked (it asked: KEY_AMBIGUOUS, DATA_REQUESTED, TRAINING_BUDGET)

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
- FAIL: no household gets more than one message. 20 households have more than one person on the list (by the 'marketing_opt_in' column)

### k15_failed_payments (K15, Double counting and wrong grain)

One payment in five is a failed attempt; adding the amount column up counts money that never arrived.

Expected: Only successful payments are counted (69,918), or the Doctor asks which statuses are real money.

- pass: every customer of the main file is still there. all 48 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: spend counts successful payments only. nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / no column adds up to the true 69,918.00 (amount = 89,978.00)

### k16_ticket_status_log (K16, Double counting and wrong grain)

Each complaint ticket has two to four status rows (opened, assigned, reopened, closed); counting rows counts a complaint many times.

Expected: 30 tickets, each once with its latest status, or the Doctor asks how to count them.

- pass: every customer of the main file is still there. all 30 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: 30 tickets counted once each. nothing like it was asked (it asked: TRAINING_BUDGET) / 30 customers appear more than once: TK001, TK002, TK003, TK004 and 26 more

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
- FAIL: customers under 18 are off the contact list. 10 people who must not be contacted are on the list: C0001, C0009, C0017, C0025 and 6 more (by the 'marketing_opt_in' column)

### p8_deceased_fraud (P8, Consent, sensitive data and who may be contacted)

Some customers are deceased, flagged for fraud, under legal hold or closed, yet all show marketing consent.

Expected: Deceased, fraud, legal-hold and closed customers are off the contact list.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: deceased, fraud, legal-hold and closed customers are off the list. 16 people who must not be contacted are on the list: C0001, C0006, C0011, C0016 and 12 more (by the 'marketing_opt_in' column)

### s01_never_active (S01, Outcome: who is eligible, and when the clock runs)

Churn is 'no order in 90 days', so 30 customers who never ordered at all are labelled churners.

Expected: The 30 never-active customers are not counted in the outcome; the true churn rate is 25.7%, not 44%.

- pass: every customer of the main file is still there. all 70 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: never-active customers are not in the outcome population. nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / 30 who cannot have the outcome are counted: C0001, C0002, C0003, C0004 and 26 more; the positive rate is 48.0%; the true rate is 25.7%

### s05_cancellation_reason (S05, Time travel and leaks the checks miss)

cancellation_reason is filled in only for customers who left, so 'is it filled in' gives the answer away.

Expected: cancellation_reason is dropped from the features (or the Doctor asks), even though it is blank for most rows.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: cancellation_reason is out of the features. cancellation_reason still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)

### t01_mixed_date_order (T01, Dates that are read wrong without any error)

signup_date holds day/month in system A rows and month/day in system B rows, so 03/04/2026 can be two dates.

Expected: The Doctor stops and asks the date order for each system, or leaves the dates as written; it never reads them all one way.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: dates are not silently read in one order. nothing like it was asked (it asked: KEY_AMBIGUOUS, GUIDED_DATE_ORDER, TRAINING_BUDGET) / 80 values changed: signup_date of C0001: '08/09/2025' became '2025-09-08 00:00:00', signup_date of C0002: '06/19/2026' became '2026-06-19 00:00:00', signup_date of C0003: '14/02/2026' became '2026-02-14 00:00:00' and 77 more

### t03_placeholder_dates (T03, Dates that are read wrong without any error)

A quarter of the customers have last_payment_date 1970-01-01, the system's stand-in for 'never paid'.

Expected: 1970-01-01 is not kept as a real date: it becomes blank, or the Doctor asks what it means.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: the 1970-01-01 stand-in is not treated as a real date. nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET) / 20 rows of 'last_payment_date' still say 1970-01-01

### t06_status_exported_today (T06, Time travel and leaks the checks miss)

The customer master was exported today, so is_active already says who left: it is 0 for every churner.

Expected: is_active is not left among the columns the model learns from (it is known only after the outcome).

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: is_active (today's status) is out of the features. is_active still in the table the model would learn from / nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET)

### t07_immature_last_month (T07, Outcome: who is eligible, and when the clock runs)

The newest month's outcome window has not finished, so its churn rate is 3% against about 25% before.

Expected: The unfinished month (2026-09) is left out of the outcome, or the Doctor asks; the true rate is 22.8%.

- pass: every customer of the main file is still there. all 180 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: the unfinished last month is not counted as 'did not leave'. nothing like it was asked (it asked: TRAINING_BUDGET) / 30 who cannot have the outcome are counted: R0181, R0182, R0183, R0184 and 26 more

### t13_outcome_from_column (T13, Time travel and leaks the checks miss)

The churn flag was built from account_status, which stays in the file as an ordinary column.

Expected: account_status, the column the outcome came from, is dropped from the features.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: account_status (the outcome's source) is out of the features. account_status still in the table the model would learn from / nothing like it was asked (it asked: KEY_AMBIGUOUS, TRAINING_BUDGET)

### t15_partial_last_month (T15, Outcome: who is eligible, and when the clock runs)

The newest month, 2026-10, covers only 12 days, so its usage looks like a collapse for every customer.

Expected: The partial month is left out, or the Doctor asks; it is never treated as a full month.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: the 12-day month is not used as a full month. nothing like it was asked (it asked: TRAINING_BUDGET) / 30 rows of 'month' still say 2026-10

### v01_paise_vs_rupees (V01, Money and units whose meaning is not in the cells)

The second file's amounts are in paise (100 times larger) and the first file's are in rupees.

Expected: The two files are stacked in one unit and the total is 198,508 rupees, or the Doctor asks which unit each file uses.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: amounts from both files add up in one unit (rupees). nothing like it was asked (it asked: KEY_AMBIGUOUS, KEY_AMBIGUOUS, OUTCOME_COLUMN, TRAINING_BUDGET, KEY_AMBIGUOUS and 1 more) / no column adds up to the true 198,508.00 (amount = 9,179,689.00)

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
- FAIL: test accounts are off the contact list. 8 people who must not be contacted are on the list: TEST001, TEST002, TEST003, TEST004 and 4 more (by the 'marketing_opt_in' column)

### v13_consent_spellings (V13, Consent, sensitive data and who may be contacted)

Consent is written Y, yes, Haan or a tick for agreed, N, nahi or No for refused, and left blank when unknown.

Expected: Y/yes/Haan/tick customers are contactable; N/No/nahi and blank (unknown) customers are not.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: every spelling of yes is on the list; refused and unknown are off it. 18 people who may be contacted are left off: C0004, C0006, C0014, C0016 and 14 more (by the 'marketing_opt_in' column)

### v19_order_total_repeated (V19, Double counting and wrong grain)

The order total is written on every line item of the order, so adding the column up counts each order 2 to 4 times.

Expected: The order total is counted once per order (120,964), or the Doctor asks which level the total is on.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: order totals add up to the true spend, not a repeated one. nothing like it was asked (it asked: TRAINING_BUDGET) / no column adds up to the true 120,964.00 (order_total = 377,802.00)

