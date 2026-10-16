# Data Doctor trap bench (v1)

30 planted traps, each run 2 times with no AI (the rules path). A trap passes only if every run passes every check. The result data is graded, never the Doctor's words.

**Person-like (the number to track): passed 26 of 30.**
Tick-all (the old driver, flatters the product): passed 30 of 30. Person who skips the questions that do not block: passed 23 of 30.

* **Person-like** acts like a careful person: it accepts what the product pre-ticks, leaves every unticked suggestion unticked (unless the case's answer key says a person who knows their business would tick it), answers every question that has a recommended option with that option, answers the questions only the person can (which column is the outcome, consent direction, units, which statuses are real money, household) from the case's answer key, says "I don't know" where the key says the person would not know, and says yes to a safety stop only where the key says the loss is what the person meant.
* **Tick-all** (`--tick-all`) answers blocking questions only and ticks every pending suggestion, so a leak column the product leaves unticked is dropped anyway. It is the old behaviour, kept for comparison.
* **Skips optional questions** is the person-like driver that leaves every question that does not block unanswered (a person who clicks through). A trap that passes person-like but fails here only passes if the person answers a question the product could have answered for them.

Runs that disagreed with each other (person-like): 0. 5 of the passes are untested: the case has several files but nothing was joined or stacked, so its join traps were never sprung. 2 pass (asked only): the right question was asked but the data was not fixed (the check accepts either).

**Asked the right question** (person-like run; the right question or flag was on the screen the person saw, ticked or not, whatever the data did): 18 of 18 traps that have one. A flag that changes nothing does not protect the data, so this is counted apart from the pass.

Blocking questions with neither a recommended option nor an answer in the key, answered with the first option offered (a guess): 0. Answers where the key overrode the product's recommended option: 0.

Traps where the person said yes to a safety stop because the key says the loss is what they meant (5): c09_already_hold_product, k16_ticket_status_log, s01_never_active, t07_immature_last_month, t15_partial_last_month. Without that yes the visit cannot be approved, so these passes rest on the key's judgement.

Many traps fail today. That is expected: the bench measures, it does not have to pass.

## What was graded (person-like)

| Check | Graded | Failed |
|---|---|---|
| No silent row loss | 30 | 0 |
| No unapproved change | 34 | 0 |
| Correct join (keys not merged, one row per customer) | 4 | 0 |
| No double counting (totals) | 5 | 1 |
| No leak column kept | 3 | 3 |
| Outcome population correct | 4 | 0 |
| Contact-list exclusions applied | 6 | 0 |
| Asked the right question | 3 | 0 |

## The traps

| Trap | Scenario | Person-like | Tick-all | Skips optional questions | Showed the right question | What went wrong |
|---|---|---|---|---|---|---|
| c01_selection_not_random | C01 | pass | pass | pass | yes |  |
| c06_dnd_direction | C06 | pass | pass | pass | - |  |
| c09_already_hold_product | C09 | pass | pass (asked only) | FAIL | yes |  |
| crit03_dup_and_drop | crit-03 | pass, untested | pass, untested | pass, untested | - | nothing was joined or stacked |
| f01_leading_zeros | F01 | pass, untested | pass, untested | pass, untested | - | nothing was joined or stacked |
| f02_excel_e_plus | F02 | pass | pass | pass | yes |  |
| f04_merged_cells_region | F04 | pass (asked only) | pass (asked only) | pass (asked only) | yes |  |
| f05_pivot_summary | F05 | pass | pass | pass | yes |  |
| f21_overlapping_exports | F21 | FAIL | pass | FAIL | - | the result has 80 rows; the customers are 150 |
| k13_household | K13 | pass | pass | pass | - |  |
| k15_failed_payments | K15 | pass | pass (asked only) | pass (asked only) | yes |  |
| k16_ticket_status_log | K16 | pass | pass (asked only) | pass (asked only) | yes |  |
| k1_merged_prefixes | K1 | pass, untested | pass, untested | pass, untested | - | nothing was joined or stacked |
| k3_shared_phone | K3 | pass, untested | pass, untested | pass, untested | - | nothing was joined or stacked |
| p7_minors | P7 | pass | pass | pass | - |  |
| p8_deceased_fraud | P8 | pass | pass | pass | - |  |
| s01_never_active | S01 | pass | pass (asked only) | FAIL | yes |  |
| s05_cancellation_reason | S05 | FAIL | pass | FAIL | yes | cancellation_reason still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET) |
| t01_mixed_date_order | T01 | pass | pass | pass | yes |  |
| t03_placeholder_dates | T03 | pass | pass | pass (asked only) | yes |  |
| t06_status_exported_today | T06 | FAIL | pass | FAIL | yes | is_active still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET) |
| t07_immature_last_month | T07 | pass | pass (asked only) | FAIL | yes |  |
| t13_outcome_from_column | T13 | FAIL | pass | FAIL | yes | account_status still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET) |
| t15_partial_last_month | T15 | pass | pass (asked only) | pass (asked only) | yes |  |
| v01_paise_vs_rupees | V01 | pass, untested | pass (asked only) | pass, untested | yes | nothing was joined or stacked |
| v03_refunds_negative | V03 | pass | pass | pass | - |  |
| v04_blank_vs_zero | V04 | pass | pass | pass | - |  |
| v06_test_accounts | V06 | pass | pass | pass | yes |  |
| v13_consent_spellings | V13 | pass | pass | pass | - |  |
| v19_order_total_repeated | V19 | pass | pass (asked only) | pass (asked only) | yes |  |

Pass with tick-all, fail person-like (4): f21_overlapping_exports, s05_cancellation_reason, t06_status_exported_today, t13_outcome_from_column.

Pass person-like, fail when the person skips the optional questions (3): c09_already_hold_product, s01_never_active, t07_immature_last_month. For these the recommended answer is what makes the data right, yet the product leaves it unanswered: it should apply the recommendation as a pre-answered, changeable choice (or make the question block).

## Why each trap fails for the person-like driver

For each failure: the failing check, what was left unticked on the plan or unanswered when the person approved, and (written by hand in the case file) what the person would have to do and whether the product should pre-tick it or ask a question.

### f21_overlapping_exports (F21)

- Failed check: 150 calls once each after stacking the two exports. the result has 80 rows; the customers are 150
- Left unticked: [stack_files] Stack calls_feb.csv and calls_jan.csv into one table: 175 rows in the files, 25 rows that two exports both hold are counted once, 150 rows in the table.
- Left unticked: [data_check] 25 rows appear in both calls_jan.csv and calls_feb.csv (31% of the smaller one): the two exports overlap, and anything added up across both would count them twice.
- Also 2 routine 'every value is different' flag(s) (information only).
- Left unanswered (not blocking): DATA_REQUESTED
- Why and what to do: The person would have to tick the plan's suggestion "Stack <file> and <file> into one table". It is shown but not pre-ticked, so each file is used alone and the other one's customers are not counted. The product should PRE-TICK it, not ask: the files have the same columns, it has already measured the repeated rows, and the suggestion states the rows before and after; the approval still names it and one click unticks it.

### s05_cancellation_reason (S05)

- Failed check: cancellation_reason is out of the features. cancellation_reason still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Left unticked: [recipe_step] Remove 'cancellation_reason': it looks like it gives away the outcome (it tells yes from no almost by itself).
- Also 1 routine 'every value is different' flag(s) (information only).
- Why and what to do: The person would have to tick the plan's suggestion "Remove '<column>': it looks like it gives away the outcome". It is shown but not pre-ticked, and the column stays among the features, so the model learns from something known only after the outcome. The product should PRE-TICK it, not ask: the leakage check found it and the suggestion already explains it, the person's list and totals do not depend on a feature column, and the plan screen lists it, so the approval still names it and one click unticks it. A leak the person had to find and tick is the failure the owner described: an excellent test score, then a near-random live campaign.

### t06_status_exported_today (T06)

- Failed check: is_active (today's status) is out of the features. is_active still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Left unticked: [recipe_step] Remove 'is_active': it looks like it gives away the outcome (it tells yes from no almost by itself).
- Also 1 routine 'every value is different' flag(s) (information only).
- Why and what to do: The person would have to tick the plan's suggestion "Remove '<column>': it looks like it gives away the outcome". It is shown but not pre-ticked, and the column stays among the features, so the model learns from something known only after the outcome. The product should PRE-TICK it, not ask: the leakage check found it and the suggestion already explains it, the person's list and totals do not depend on a feature column, and the plan screen lists it, so the approval still names it and one click unticks it. A leak the person had to find and tick is the failure the owner described: an excellent test score, then a near-random live campaign.

### t13_outcome_from_column (T13)

- Failed check: account_status (the outcome's source) is out of the features. account_status still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Left unticked: [recipe_step] Remove 'account_status': it looks like it gives away the outcome (it tells yes from no almost by itself).
- Also 1 routine 'every value is different' flag(s) (information only).
- Why and what to do: The person would have to tick the plan's suggestion "Remove '<column>': it looks like it gives away the outcome". It is shown but not pre-ticked, and the column stays among the features, so the model learns from something known only after the outcome. The product should PRE-TICK it, not ask: the leakage check found it and the suggestion already explains it, the person's list and totals do not depend on a feature column, and the plan screen lists it, so the approval still names it and one click unticks it. A leak the person had to find and tick is the failure the owner described: an excellent test score, then a near-random live campaign.

### v01_paise_vs_rupees (V01) (passes, but untested)

- Left unticked: [stack_files] Stack sales_a.csv and sales_b.csv into one table: 80 rows in the files, no row is in two exports, 80 rows in the table.
- Also 2 routine 'every value is different' flag(s) (information only).
- Why and what to do: The person would have to tick the plan's suggestion "Stack <file> and <file> into one table". It is shown but not pre-ticked, so each file is used alone and the other one's customers are not counted. The product should PRE-TICK it, not ask: the files have the same columns, it has already measured the repeated rows, and the suggestion states the rows before and after; the approval still names it and one click unticks it. Until it is, the paise-against-rupees trap is never sprung (untested).


## Each trap (person-like)

### c01_selection_not_random (C01, Campaign history: selection read as cause)

Staff texted only high-balance customers (12% bought against 3% of the rest); there is no random control group.

Expected: The Doctor asks whether anyone was held out at random; without that it never calls the gap an effect of the text.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: asks whether a random group was left uncontacted. asked: Q13 HOLDOUT In the past campaign recorded in “contacted”, was a random group deliberately 
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: Q13_HOLDOUT: No (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### c06_dnd_direction (C06, Consent, sensitive data and who may be contacted)

do_not_contact = Y means the customer must NOT be contacted; read as a consent flag, Y means 'may contact'.

Expected: Everyone with do_not_contact = Y is off the contact list; everyone with N is on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: do-not-contact customers are off the list (the flag points the right way). the list leaves out everyone it must (by the contact-list gate alone (no consent rule))
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### c09_already_hold_product (C09, Outcome: who is eligible, and when the clock runs)

Two in five policyholders already hold the vehicle policy being cross-sold, so they can never 'buy' it.

Expected: Existing holders are not counted in the outcome; the true response rate among the others is 23.6%.

- pass: every customer of the main file is still there. all 72 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: customers who already hold the product are not in the outcome. asked: Q23 WHO CAN RESPOND 48 customers in policyholders.csv already hold the product. 0 of them 
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person answered: Q23_WHO_CAN_RESPOND: Yes, leave them out of the outcome (recommended)
- Person said yes to the stop: PLAN_LOSES_TOO_MUCH: Together the steps lose 48 of 120 customers (40.0%), more than 5%. Most from step 2. By outcome, 0: 48. (answer key)
- Person said yes to the stop: PLAN_MOVES_OUTCOME_RATE: The outcome rate moves from 14.2% to 23.6% (+9.4 points), more than 1. Biggest moves: step 2. (answer key)

### crit03_dup_and_drop (crit-03, SQL steps slip past the safety limits)

Twenty customers have two tag rows and twenty have none, so a join adds 20 and drops 20 and the row count stays 100.

Expected: Exactly the 100 customers, each once: neither the duplicates nor the dropped customers may cancel out.

- pass: every customer of the main file is still there. all 100 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 100 customers once each (duplicates and drops are not netted). 100 rows, one per customer
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### f01_leading_zeros (F01, False, merged or missed joins)

Customer IDs like 000101 and PIN codes like 012341 start with zeros that a number reader drops.

Expected: IDs and PIN codes come out exactly as written, with their leading zeros.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: IDs and PIN codes keep their leading zeros. the values are as the file had them
- Person answered: KEY_AMBIGUOUS: order_id (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### f02_excel_e_plus (F02, False, merged or missed joins)

Excel saved the 12-digit customer IDs as 1.23457E+11, so 40 customers now share a handful of IDs.

Expected: The Doctor says the IDs were rounded and asks for a re-export as text; all 40 rows are kept and none is joined.

- pass: every customer of the main file is still there. all 13 customers are in the result
- pass: says the IDs were rounded by Excel and asks for a re-export. asked: DIFFERENT FILE REQUESTED This file cannot be used as it is. Ask whoever exported the file 
- pass: all 40 rows are kept. 40 rows, as in the file
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### f04_merged_cells_region (F04, Spreadsheet and export shapes the reader misses)

Region is written once per block (a merged cell) and left blank on the 14 rows below it.

Expected: Region is filled down to every row of its block, or the Doctor asks whether a blank means 'same as above'.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 56 blank regions are filled or asked about. asked: BLOCK BLANKS 'region' is filled on 4 rows and blank on the 56 rows below them, the way mer
- Person answered: BLOCK_BLANKS: Yes: I will send the file with it filled in on every row (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### f05_pivot_summary (F05, Spreadsheet and export shapes the reader misses)

The file is a pivot table pasted as values: counts per region and plan, a Grand Total row and no customer ID.

Expected: The Doctor says this is a summary, not customers, and asks for the row-level export; it invents no rows.

- pass: says it is a summary and asks for one row per customer. asked: DIFFERENT FILE REQUESTED This file cannot be used as it is. Ask whoever exported the file 
- Person answered: KEY_AMBIGUOUS: I don't know (dont_know)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### f21_overlapping_exports (F21, Double counting and wrong grain)

The February export repeats the last 25 calls of January, so stacking the two files counts them twice.

Expected: January and February are stacked into one table of 150 calls, each call once.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: 150 calls once each after stacking the two exports. the result has 80 rows; the customers are 150
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### k13_household (K13, Who one row is, and one model or several)

Twenty households have three members each with consent; texting everyone sends three messages to one household.

Expected: At most one person per household is on the contact list.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: no household gets more than one message. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person ticked: “marketing_opt_in” says who agreed to be contacted. (answer key)

### k15_failed_payments (K15, Double counting and wrong grain)

One payment in five is a failed attempt; adding the amount column up counts money that never arrived.

Expected: Only successful payments are counted (69,918), or the Doctor asks which statuses are real money.

- pass: every customer of the main file is still there. all 48 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: spend counts successful payments only. asked: Q22 REAL MONEY Which values of “status” in payments.csv are real money? 12 of 60 rows say 
- Person answered: Q22_REAL_MONEY: Only the other rows are real money: leave out the “failed” rows (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### k16_ticket_status_log (K16, Double counting and wrong grain)

Each complaint ticket has two to four status rows (opened, assigned, reopened, closed); counting rows counts a complaint many times.

Expected: 30 tickets, each once with its latest status, or the Doctor asks how to count them.

- pass: every customer of the main file is still there. all 30 customers are in the result
- pass: columns no approved change touched keep the file's values. the ID column was changed by an approved change; rows cannot be lined up
- pass: 30 tickets counted once each. asked: Q21 ONE ROW Each “ticket_id” in tickets_log.csv has several rows. The status changes from 
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person answered: Q21_ONE_ROW: Keep the latest status per ticket id (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person said yes to the stop: PLAN_LOSES_TOO_MUCH: Together the steps lose 65 of 95 customers (68.4%), more than 5%. Most from step 2, step 1. By outcome, 0: 65. (answer key)

### k1_merged_prefixes (K1, False, merged or missed joins)

Customer IDs C-101 and S-101 are two different people; the orders file writes both as 101.

Expected: 60 customers stay 60: a rule that strips the prefix must not merge C-101 and S-101 into one row.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 60 customers stay 60 rows, none merged. 60 rows, one per customer
- pass: C-101 and S-101 each keep their own segment. the named customers have their own values
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### k3_shared_phone (K3, False, merged or missed joins)

Ten customers share the dummy phone 9999999999 and eight share a family phone; the calls file is keyed by phone.

Expected: 50 customers stay 50 rows: calls must not be handed to every customer who shares a number.

- pass: every customer of the main file is still there. all 50 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: 50 customers stay 50 rows when calls are keyed by a shared phone. 50 rows, one per customer
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### p7_minors (P7, Consent, sensitive data and who may be contacted)

Ten customers are under 18 on the as-of date, and every customer has ticked marketing consent.

Expected: Minors are off the contact list even though they have consent; adults with consent are on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: customers under 18 are off the contact list. the list leaves out everyone it must (by the contact-list gate alone (no consent rule))
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### p8_deceased_fraud (P8, Consent, sensitive data and who may be contacted)

Some customers are deceased, flagged for fraud, under legal hold or closed, yet all show marketing consent.

Expected: Deceased, fraud, legal-hold and closed customers are off the contact list.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: deceased, fraud, legal-hold and closed customers are off the list. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person ticked: “marketing_opt_in” says who agreed to be contacted. (answer key)

### s01_never_active (S01, Outcome: who is eligible, and when the clock runs)

Churn is 'no order in 90 days', so 30 customers who never ordered at all are labelled churners.

Expected: The 30 never-active customers are not counted in the outcome; the true churn rate is 25.7%, not 44%.

- pass: every customer of the main file is still there. all 70 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: never-active customers are not in the outcome population. asked: Q23 WHO CAN RESPOND 30 customers in customers.csv were never active. 30 of them are marked
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person answered: Q23_WHO_CAN_RESPOND: Yes, leave them out of the outcome (recommended)
- Person said yes to the stop: PLAN_LOSES_TOO_MUCH: Together the steps lose 30 of 100 customers (30.0%), more than 5%. Most from step 1. By outcome, 1: 30. (answer key)
- Person said yes to the stop: PLAN_MOVES_OUTCOME_RATE: The outcome rate moves from 48.0% to 25.7% (-22.3 points), more than 1. Biggest moves: step 1. (answer key)

### s05_cancellation_reason (S05, Time travel and leaks the checks miss)

cancellation_reason is filled in only for customers who left, so 'is it filled in' gives the answer away.

Expected: cancellation_reason is dropped from the features (or the Doctor asks), even though it is blank for most rows.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: cancellation_reason is out of the features. cancellation_reason still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### t01_mixed_date_order (T01, Dates that are read wrong without any error)

signup_date holds day/month in system A rows and month/day in system B rows, so 03/04/2026 can be two dates.

Expected: The Doctor stops and asks the date order for each system, or leaves the dates as written; it never reads them all one way.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: dates are not silently read in one order. the values are as the file had them
- Person answered: GUIDED_FORMAT: I will export it again with the year first (answer key)
- Person answered: DATE_ORDER_BOTH: I will send the dates written one way (2024-03-25) (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### t03_placeholder_dates (T03, Dates that are read wrong without any error)

A quarter of the customers have last_payment_date 1970-01-01, the system's stand-in for 'never paid'.

Expected: 1970-01-01 is not kept as a real date: it becomes blank, or the Doctor asks what it means.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 1970-01-01 stand-in is not treated as a real date. asked: DATE PLACEHOLDER 20 rows (25%) of 'last_payment_date' are 1970-01-01, which is usually a b
- Person answered: DATE_PLACEHOLDER: Treat it as blank (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### t06_status_exported_today (T06, Time travel and leaks the checks miss)

The customer master was exported today, so is_active already says who left: it is 0 for every churner.

Expected: is_active is not left among the columns the model learns from (it is known only after the outcome).

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: is_active (today's status) is out of the features. is_active still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### t07_immature_last_month (T07, Outcome: who is eligible, and when the clock runs)

The newest month's outcome window has not finished, so its churn rate is 3% against about 25% before.

Expected: The unfinished month (2026-09) is left out of the outcome, or the Doctor asks; the true rate is 22.8%.

- pass: every customer of the main file is still there. all 180 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the unfinished last month is not counted as 'did not leave'. asked: THIN LAST PERIOD The newest rows of 'snapshot_month' look unfinished. The newest month is 
- Person answered: TRAINING_BUDGET: Balanced (about 3 min, recommended) (recommended)
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person said yes to the stop: PLAN_LOSES_TOO_MUCH: Together the steps lose 30 of 210 customers (14.3%), more than 5%. Most from step 1. By outcome, 0: 29, 1: 1. (answer key)
- Person said yes to the stop: PLAN_MOVES_OUTCOME_RATE: The outcome rate moves from 20.0% to 22.8% (+2.8 points), more than 1. Biggest moves: step 1. (answer key)

### t13_outcome_from_column (T13, Time travel and leaks the checks miss)

The churn flag was built from account_status, which stays in the file as an ordinary column.

Expected: account_status, the column the outcome came from, is dropped from the features.

- pass: every customer of the main file is still there. all 120 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- FAIL: account_status (the outcome's source) is out of the features. account_status still in the table the model would learn from / nothing like it was asked (it asked: TRAINING_BUDGET)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### t15_partial_last_month (T15, Outcome: who is eligible, and when the clock runs)

The newest month, 2026-10, covers only 12 days, so its usage looks like a collapse for every customer.

Expected: The partial month is left out, or the Doctor asks; it is never treated as a full month.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: the 12-day month is not used as a full month. asked: THIN LAST PERIOD The newest rows of 'month' look unfinished. The newest month is only part
- Person answered: THIN_LAST_PERIOD: Leave them out (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person said yes to the stop: PLAN_LOSES_TOO_MUCH: Together the steps lose 30 of 120 customers (25.0%), more than 5%. Most from step 1. By outcome, 0: 24, 1: 6. (answer key)

### v01_paise_vs_rupees (V01, Money and units whose meaning is not in the cells)

The second file's amounts are in paise (100 times larger) and the first file's are in rupees.

Expected: The two files are stacked in one unit and the total is 198,508 rupees, or the Doctor asks which unit each file uses.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: amounts from both files add up in one unit (rupees). asked: Q06 UNIT SCALE What unit is amount in? Its values look like two units: they are about 73 t
- Person answered: Q06_UNIT_SCALE: The files use different units: I will send them in one unit (recommended)
- Person answered: OUTCOME_COLUMN: churn (sales_a.csv) (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### v03_refunds_negative (V03, Money and units whose meaning is not in the cells)

One transaction in six is negative: a refund, which is a strong signal that must not be clipped to zero.

Expected: The negative amounts stay as they are (refunds), or the Doctor asks about them.

- pass: every customer of the main file is still there. all 60 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: negative amounts (refunds) are not clipped or dropped. the values are as the file had them
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### v04_blank_vs_zero (V04, Money and units whose meaning is not in the cells)

last_purchase_amount is blank for customers who never bought; filling it with 0 makes them look like small buyers.

Expected: Blank stays blank (never bought), or the Doctor asks what blank means.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: blank 'never bought' is not turned into 0. the values are as the file had them
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

### v06_test_accounts (V06, Test, staff and extreme accounts)

Eight QA accounts (TEST001..., qa+...@company.example) sit in the customer list with marketing consent.

Expected: The test accounts are off the contact list; the real customers are on it.

- pass: every customer of the main file is still there. all 80 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: test accounts are off the contact list. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person ticked: “marketing_opt_in” says who agreed to be contacted. (answer key)

### v13_consent_spellings (V13, Consent, sensitive data and who may be contacted)

Consent is written Y, yes, Haan or a tick for agreed, N, nahi or No for refused, and left blank when unknown.

Expected: Y/yes/Haan/tick customers are contactable; N/No/nahi and blank (unknown) customers are not.

- pass: every customer of the main file is still there. all 90 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: every spelling of yes is on the list; refused and unknown are off it. the list leaves out everyone it must (by the 'marketing_opt_in' column and the contact-list gate)
- Person answered: OUTCOME_COLUMN: converted (answer key)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)
- Person ticked: “marketing_opt_in” says who agreed to be contacted. (answer key)

### v19_order_total_repeated (V19, Double counting and wrong grain)

The order total is written on every line item of the order, so adding the column up counts each order 2 to 4 times.

Expected: The order total is counted once per order (120,964), or the Doctor asks which level the total is on.

- pass: every customer of the main file is still there. all 40 customers are in the result
- pass: columns no approved change touched keep the file's values. the values are as the file had them
- pass: order totals add up to the true spend, not a repeated one. asked: Q21 ONE ROW Each “order_id” in order_lines.csv has several rows. The order total is the sa
- Person answered: Q21_ONE_ROW: Take the order total once per order id: the later rows of each order id are left blank (recommended)
- Person answered: TRAINING_BUDGET: Balanced (about 2 min, recommended) (recommended)

