# Data Doctor: real-life situations it cannot yet handle, and what to add

Reference for the guards (G), rules (R), questions (Q), data requests (D) and screens (U) named in the code and tests. The data requests are in `configs/pilot/data_request_messages.yaml` and the out-of-scope sentences of section 4 in `configs/doctor/out_of_scope.yaml`.

Date: 15 Oct 2026. Written in plain English.

Source: 8 sweeps of real-life situations (file formats, several tables, time, outcome, values, business, data quality, privacy and operations) plus a critic pass. **195 distinct situations** are covered here.

Spot-checks in the code (read only): `engine/doctor/change_share.py:95,148` (an SQL step has no single column, so its change share counts as 0 and passes), `engine/stages/validate.py:1681-1687` (a column filled only for churners gets no leak score), `engine/stages/ingest.py:445` (`keep_leading_zeros` is off by default). All three findings are confirmed.

**What "Batch B" and "Batch C" mean here.** Batch A of v1 is merged on `main`. Batch B is the next batch: the branches `v1b-sql-step` and `v1b-start-ui` (the SQL step, plus Start and the Campaign Run). Batch C is the batch after it: the real AI agent (the AI review, the real-AI test, wider checks) and the replay of next month's file. Rule used below: anything that **must exist before the SQL step can be safely switched on** goes in B. Anything the AI agent or the monthly replay needs goes in C.

---

## 1. Verdict

Assume the planned agent is built: SQL steps, typed steps, questions and one approval. Of the 195 situations, it handles about **1 in 8 completely (24)**, **7 in 10 only partly (139)** and **1 in 6 not at all (32)**. Almost all of them (**188 of 195, 96%**) could be solved with the data the person already has, or by asking one question. Only 7 need more data or a different product.

"Partly" nearly always means the SQL step *could* do the fix, but nothing makes the agent think of it, and nothing in code catches the mistake if it does not. The dangerous failures are therefore **silent**: the plan looks clean, the rows add up, the model trains, and the contact list is wrong.

The main classes it still cannot handle:
- **(1) The SQL step slips past the safety limits.** The 30%/60% rewrite limit and the row accounting do not measure SQL. Small losses also add up across steps.
- **(2) False or merged joins.** Coverage is high but the customers are wrong.
- **(3) Double counting.** Overlapping exports, order totals repeated on every line, status logs counted as events.
- **(4) Time travel.** Columns that were only known after the as-of date: today-only status, `now()` in SQL, tenure counted to the wrong day.
- **(5) Wrong outcome population.** Never-active customers labelled as churners, outcome windows not yet complete, existing holders counted as cross-sell buyers.
- **(6) Leaks the checks miss.** For example, a `cancellation_reason` that is filled in only for churners.
- **(7) Consent, minors and suppression lists.** Mistakes here put people on the contact list who must not be there.
- **(8) Campaign history read as cause.** A past campaign where staff chose who to contact gets treated as if it measured the campaign's effect.

With the **Must-for-v1 additions** below (about 25 prompt rules, 12 question types and 16 code guards, mostly small), I estimate:
- about **80%** of situations handled end to end;
- about **15%** handled with a clear question or warning;
- about **5%** properly refused, with a ready data request.

The single most important point: **SQL is code, and v1's own principle is "the AI never writes code".** The SQL step is acceptable only if every receipt (rows in and out, customers lost or duplicated, totals kept) is computed by our sandbox and never by the AI. Batch B must ship those receipts together with the SQL step, not after it.

---

## 2. What it will not handle (or only partly), grouped into 12 themes

All of these can be solved from the given data or by asking the person.

| # | Theme | Example | How common | Risk if wrong | Smallest addition | Priority |
|---|---|---|---|---|---|---|
| 1 | **The SQL step slips past the safety limits** (crit-01 to crit-04, crit-06) | The AI writes `CREATE ... LEFT JOIN ... WHERE` that changes 100% of a column. It is measured as 0% changed. A join adds 4,000 duplicates and a filter drops 4,000 rows, so the receipt says "no rows lost". 25 small steps each drop under 2% and together remove 18% of customers, mostly churners. | Very common, because every SQL step is affected | The main safety promise is broken without anyone seeing it, and the training data is biased against exactly the people being predicted | Receipts computed by the sandbox for every SQL step (G1); a measure that fits each step type (G2); a running total of loss for the whole plan (G3); profiling on a random sample instead of the first rows (G13) | **Must** |
| 2 | **False, merged or missed joins** (K1, K2, K3, K4, K5, K6, K11, K12, K13, K17, K20, F01, F02, F23, V21, DQ12, C24, P19, crit-05) | Removing the "C-" prefix merges `C-123` and `S-123` into one customer. Two systems both number customers from 1, so 98% "match" but they are different people. A family shares one phone number. Excel shows the ID as `1.23457E+11`. | Very common | Customers get someone else's purchases; spend is doubled for some customers while others disappear; good-looking coverage hides the problem | Collision check on key rules (G4); key-quality report covering repeated values, placeholders, E+ IDs and leading zeros (G5); IDs read as text by default (G19); prompt rules R1 to R3; questions Q1 and Q3; cross-checking a join against other shared details (G6) | **Must** (G6 Should) |
| 3 | **Double counting and wrong grain** (V19, K7, K9, K15, K16, F21, DQ07, T17, F16, F13, DQ20) | The order total is repeated on all 3 line items, so summing gives 3 times the real spend. The Jan and Feb exports overlap. A ticket's 4 status rows are counted as 4 complaints. Failed and successful payment attempts both count as purchases. | Very common | Spend, frequency and complaints inflate 2 to 10 times for the most active customers; revenue never matches finance | Rule "check the grain before summing" (R4); overlap and duplicate detector (G12); totals kept before and after, inside G1 | **Must** |
| 4 | **Time travel and leaks the checks miss** (T06, T11, T12, T13, T14, K9, K10, C22, S03, P13, S05, S12, S21, T24, V15, F06) | The customer master was exported today, so `is_active`, `tier` and `balance` already show the churn. `cancellation_reason` is filled only for churners. A SQL step uses `CURRENT_DATE`. `tenure_days` was frozen when the spreadsheet was last saved. | Very common | Excellent test score, then near-random results in the live campaign, because the model learned the outcome | Rules R5, R6 and R10; SQL check that bans `now()` and requires the as-of value, plus a leak test on tables built by SQL (G10); a leak check based on which rows are filled in (G7); questions Q8 and Q9 | **Must** |
| 5 | **Outcome: who is eligible, and when the clock runs** (S01, S04, S09, S10, S14, S15, S16, S17, S22, C09, T07, T08, T09, T10, S06, S11) | "Churn = no order in 90 days" labels people who never bought anything as churners. The newest month counts as "did not buy" because its window is not over. 40% of a cross-sell file already holds the product. | Very common | The positive rate is wrong (60% instead of 18%); the model learns "new customer = churn"; the contact list includes people who already bought | Outcome templates written as "eligible population + event + window + start date" (R7, R8, R9); a check that recent periods are mature (G11); positive label stated explicitly everywhere (G8); questions Q7, Q10 and Q11 | **Must** |
| 6 | **Dates that are read wrong without any error** (T01, T02, T03, T04, T05, T15, T16, T19, F20, V18, DQ05, DQ19, K18, K21) | One column has both DD/MM and MM/DD (the code then quietly reads everything as month first). `Mar-24` becomes 24 March of this year. `1970-01-01` sits in for blanks. A UTC timestamp at 20:30 is actually the next day in India. The last 12 days of October are counted as a full month. | Very common | Recency, tenure and outcome windows shift by days or months; "spend dropped" becomes the top churn signal | Date sanity checks for placeholder dates, order between related dates, date range and stopping when both orders appear (G9); last-period and thin-tail detector (G11); rules R21, R23 and R20; Q18 locale settings asked once per client (time zone, fiscal-year start) | **Must** (time zone and fiscal year: Should) |
| 7 | **Money and units whose meaning is not in the cells** (V01, V02, V03, V04, V05, V08, V17, V22, F14, F24, K15, DQ16, V16) | Amounts in paise next to amounts in rupees. `discount` stored as 0.15 in one file and 15 in another. Negative rows are refunds. A blank `last_purchase_amount` means "never bought". `Revenue (₹ in Lakhs)` appears only in a title row. Income bands like `5L-10L`. | Very common | Values off by 100 times or 100,000 times; refunds deleted (losing a strong churn signal); never-bought customers look like the hottest leads | Rules R11, R12 and R13; Q5 "what does blank mean"; Q6 unit confirmation; the text of title rows passed to the agent | **Must** for refunds, blank-vs-zero and paise; Should for the rest |
| 8 | **Who one row is, and one model or several** (K13, S10, C12, C13, S07, C08, DQ13) | Telecom family plans: the outcome is per account but contacts are per person. Prepaid and postpaid in one file, with churn meaning different things in each. Eligibility rules (age 18+, metro PIN codes) that exist only in the owner's head. | Common | Family members on both sides of the train/test split (leak); 3 people in one household get the same SMS; the model just learns "business unit" | Q1 "what is one row"; R16 compare outcome rates by segment; R29 group the household in the split and in contact de-duplication; Q17 business rules | **Must** (Q1, R29); Should (others) |
| 9 | **Consent, sensitive data and who may be contacted** (C06, C07, C10, C20, P3, P4, P5, P6, P7, P8, P20, P22, P25, V13, V14, V23, DQ17, S19, P1, P15, P21) | `DND = Y` read as "may contact". Consent kept in a separate log where the latest row wins. Minors in the file. Deceased or fraud customers. A `religion` column. Pharmacy purchases. Aadhaar used as the join key. | Very common | Messages to people who opted out, to children, or to deceased customers; targeting by religion or health; legal exposure under DPDP and TRAI | Contact-list gate in code (G14); sensitive-column detector (G15); long text hidden from the AI by default (G16); PII keys hashed (G24); R18 and R24; Q14 and Q15 | **Must** |
| 10 | **Spreadsheet and export shapes the reader misses** (F03, F04, F05, F06, F07, F08, F09, F13, F14, F15, F18, F21, F22, F25, DQ14, C16, T18) | A region shown once in a merged cell and blank below it. Jan/Feb/Mar sheets in one workbook plus a "Legend" sheet. A pivot table pasted as values. Hindi text turned into `à¤¹`. A file cut off at Excel's 1,048,576-row limit. | Very common | Most rows lose their region or email; only one month is used; subtotals counted as customers; text columns turned to garbage | R14 handle multi-sheet files; R15 fill-down only after asking; R25 detect summary files; G17 shape detectors (garbled text, cut-off files, formulas with no saved value, hidden rows, two tables on one sheet); Q4 block picker | **Must** (R14, R15, R25); Should (G17, Q4) |
| 11 | **Test, staff and extreme accounts** (V06, K19, DQ24, V07, K3) | `TEST001`, `qa+1@company.com`, the phone `9999999999` shared by 300 customers, staff with 100% discounts, one corporate account holding 38% of revenue. | Common | Test numbers get real messages; the dummy phone links unrelated customers; one large account decides the uplift result | R17 standard query for junk and very large accounts, then ask; G5 flags placeholder keys | **Should** (the contact-list exclusion inside G14 is **Must**) |
| 12 | **Campaign history: selection read as cause** (C01, C02, C03, C05, C17, C19, K14, S13, C18, T20, S20) | Staff texted only high-balance customers; texted customers convert at 12% and others at 3%. No control group. Two campaigns overlap in time. The training month was Diwali. | Very common | "Who responds" sold as "who was persuaded"; ROI overstated; the forecast for a normal month is badly off | R19 (ask about a random group left out; without one, never claim lift); R28 festival and seasonality question; Q13; G21 balance check | **Must** (the R19 wording and Q13); Should (G21, R28) |

The next-month replay situations (DQ01-DQ11, DQ18, DQ20, DQ22, P18, K22, T23) are a 13th group. The v1 vision says re-checking next month's file is **not in v1**: the person brings next month's data and the Doctor reuses their answers. So those guards are listed under G18 as **Should, Batch C**. One part is Must: time settings must be saved as rules, not fixed dates (G10, T23). Otherwise the first reuse quietly filters next month's data to last month's date.

---

## 3. Additions, de-duplicated and prioritised

Size: S = days, M = 1-2 weeks, L = more. Batch: B = ships with the SQL step; C = the AI agent and replay batch.

### (a) Rules for the agent's system prompt

These are rules for the AI. Anything that must never fail is also listed as a code guard in (c).

| ID | Rule (concrete) | Situations | Size | Batch | Priority |
|---|---|---|---|---|---|
| R1 | Before any join, check key coverage and key quality for **every pair of tables**, and show "matched / unmatched %" in the plan. Write down which entity each key column identifies (customer, order, account). **Never join on a column only because the names match**, and never on name + city. | K1, K2, crit-05, DQ12 | S | B | Must |
| R2 | When customers have several ID systems, build one crosswalk table from the master (customer ID, ID type, cleaned value) and join every event table through it. Ask Q1 for the main ID. Report the share of rows matched through each ID type. | K2, C24, K14, K20 | S | B | Must |
| R3 | Read IDs, phones, PIN codes and account numbers as text. Strip a leading `'` and `="..."` wrappers. Pad with zeros (`lpad`) only after Q3 has given the length. Never change the letter case of IDs that are case-sensitive; for Salesforce IDs, match on the first 15 characters. Strip the `91` and `+91` country code before matching phones. | F01, F15, F17, F23, V21 | S | B | Must |
| R4 | **Check the grain before summing.** Count rows per key in each table. A money value that is the same on every line of an order is taken once (MAX), not summed. Status logs are first reduced to the latest row per ticket, then counted. For payments, list the status values and ask which ones are real money; refunds are netted against the original. Joining tables at different grains: aggregate first, then join. | V19, K15, K16, F16, K9, F13 | S | B | Must |
| R5 | **Never use `now()`, `CURRENT_DATE` or a typed-in date in SQL.** Every time-based calculation uses the case's `:as_of` value. Recompute tenure, age and days-since from the underlying dates. Do not trust columns that were computed in the export. | T06, T14, F06, V15, T23 | S | B | Must |
| R6 | Put every attribute file in one of three types: **dated history**, **snapshot as of a date**, or **true only today**. For a "today only" file, mark state-like columns (status, is_active, tier, plan, balance, credit limit, `last_*`, `*_flag`, `days_since_*`) and propose drop, recompute as of the date, or a data request. Use the date the information became known (posting date, invoice date), not the period date. For history tables with valid-from and valid-to dates, take the row valid on the as-of date, then check each customer has exactly one match. | T11, T12, K9, K10, C22, S03, P13 | M | B | Must |
| R7 | **Every outcome = eligible population + event + window + start date.** Use templates: churn requires earlier activity; cross-sell excludes current holders; win-back covers lapsed customers only; renewal covers policies expiring in the window; for start/end tables, include only rows with start ≤ as-of < end. Always show the number of customers excluded as "not eligible". | S01, S17, C09, T10, S15, S09 | S | B | Must |
| R8 | When a file has a ready-made 0/1 outcome and a date column, compute the positive rate by month. If the last 1-2 months fall more than 40% below the earlier level, ask whether to leave them out. For outcomes like "within 60 days of the first order", use a separate start date per customer, and drop customers whose window has not finished. | T07, T08, S14 | S | B | Must |
| R9 | Propose a churn window from the customers' own buying rhythm (the median and the 90th-percentile gap between purchases). Show the positive rate for each option. Warn if it is above 70% or below 3%. | T09, C14 | S | B | Should |
| R10 | When the outcome is built from column X, drop X and similar columns (same prefix, same source date, strong match) and list them for the person to confirm. When there are several outcome-like columns, ask which one is the target and propose dropping the others (not pre-ticked). Write the positive label as a sentence: "We will predict: customers who **leave**". | T13, S12, S22, S08, S21 | S | B | Must |
| R11 | Before any minimum-zero rule on a money column, count the negative values and check whether they pair with earlier sales (same customer, same absolute amount). If they do, ask about refunds and derive gross spend, refund amount and refund count. | V03 | S | B | Must |
| R12 | After every join or aggregation, decide per group of columns: **blank means zero, unknown, not applicable or not yet**. Any period outside a table's date coverage is blank (NULL), not 0. Limit look-back windows to the shortest coverage and say so. | V04, K21 | S | B | Must |
| R13 | **Check the money scale.** Read the words in headers and title rows (lakh, crore, Cr, Mn, `'000`, paise, cents, USD). Compare medians across files that join or stack (a ratio near 100 means paise; near 1.18 means GST). For percentage columns, check the share of values ≤ 1. Confirm with Q6 before converting. | V01, V02, F24, V16, V22, F16 | S | B | Must |
| R14 | For a workbook with several sheets: **always list every sheet in the plan**. Sheets with the same columns are stacked, keeping the sheet name. A two-column code/meaning sheet becomes a value map that the person confirms. A sheet is skipped only by an explicit "skip". Dates in file or sheet names become a snapshot date, confirmed with a question. | F09, T18, K20 | S | B | Must |
| R15 | Fill blanks with the value above ("fill-down") only after asking "does a blank here mean the same as the row above?". Keep the original row order (`_row_id`). | F04, F13, F15, F05 | S | B | Must |
| R16 | After the outcome is chosen, compute the outcome rate by every column that looks like business unit, brand, product, region or channel. If rates differ more than 5 times, ask: one model, separate outcome rules per group, or one model per group. | S07, C12, C13 | S | C | Should |
| R17 | Run a standard junk-account query: test, dummy, demo or QA strings; company email domains; phone numbers shared by more than N customers; phones with all the same digit or in sequence; round-number birth dates; the top 0.1% of customers by volume. Ask whether to exclude each group from training, from the contact list, or both. | V06, K19, DQ24, V07 | S | C | Should |
| R18 | **Consent:** always ask which way a DND or consent column points. Blank means *unknown*, not yes. Ask about DND-Partial. Build consent from a separate log as "the latest event on or before the as-of date". Suppression files and do-not-contact statuses (deceased, fraud, legal, minors, sales "Do not call") are **carried to the contact list** and stay in training unless the person says otherwise. | C06, V13, P5, P6, P8, C10, C20, P7, S19 | S | B | Must |
| R19 | **Campaign history:** ask Q13. If there was no random holdout, call the result "who responds among those you contacted" and never claim extra sales caused by the campaign. Count each campaign's window from its own send date. "Delivered" is not a response. "Opened" is never an outcome. Ask whether a purchase on the send day counts. When campaigns overlap, ask how to credit them. | C01, C02, C03, C04, C17, S13, T24, K14 | S | B | Must |
| R20 | If some date columns have time-zone offsets or `Z` and others do not, ask which zone the plain ones are in, and convert before cutting to a date. Show an hour-of-day chart (a peak at 18:30 UTC means the system runs on midnight IST). | T05, K18, V18 | S | C | Should |
| R21 | When one column has evidence of both day-first and month-first, **stop**. Test each order against related dates (order date ≤ delivery date), check each source file separately, and handle a mix with one SQL `CASE` per source. | T01, V18, DQ05 | S | B | Must |
| R22 | Lookup joins: check the lookup key is unique before joining. Count codes not found and label them "Unmapped" (never quietly "Other"). Ask about them. | K11, V09, C15, V10 | S | B | Should |
| R23 | Set the as-of date to the **last complete day or month**. Use daily counts at the end of the file to find a thin, still-arriving tail, and leave out a partial last month. | T15, T16, DQ19 | S | B | Must |
| R24 | For sensitive columns and category values (religion, caste, health, disability, pharmacy), ask how they may be used; the default is "not used for targeting". Mark PIN code and surname as possible stand-ins (proxies) when a sensitive column exists. Never guess gender from names or titles. | P3, P4, P20, V23, V14 | S | B | Must |
| R25 | If a file has no ID column, has columns named count, sum or total, and has few rows, it is a **summary report**. Say so plainly and send the data request for a row-level file. Never invent rows. | F05, DQ14, C16 | S | B | Must |
| R26 | Ask whether the file holds all customers or an extract (Q20). Assume the file may be sorted. Make decisions about formats and value lists from the random sample (G13), not from the first rows. | DQ10, crit-06 | S | B | Must |
| R27 | Long text columns are hidden from the AI. Flags such as "intends to cancel" are made only with SQL keyword matching (`ILIKE`) from a word list the person approves (including Hinglish words such as band, port, refund). | P1, V11 | S | C | Should |
| R28 | If one month's rate or volume is more than 2 times the median, ask whether there was a sale or festival, or whether the way the data is recorded changed. Offer: a festival flag, leaving the period out, or using only data after the change. | T20, C18, S20, DQ22 | S | C | Should |
| R29 | When household or account IDs exist, keep the group together in the train/test split and remove duplicates from the contact list by household. | K13, S10 | S | B | Must |

### (b) New question types

Every question has options, a recommended answer and "I don't know". Answers are remembered per client.

| ID | Question | Options (recommended in bold) | Size | Batch | Priority |
|---|---|---|---|---|---|
| Q1 | "What do you score and contact: a person, an account or a household? Which ID is the main customer ID?" | Detected ID columns; **the ID the campaign contacts** | S | B | Must |
| Q2 | "These N customers look like the same person. Merge them?" | Merge by phone / **phone + date of birth** / keep separate / I don't know | M | C | Should |
| Q3 | "How many digits should this ID have? Are upper and lower case different?" | Detected lengths | S | B | Must |
| Q4 | Block or sheet picker, showing the header row of each | Each block or sheet | S | C | Should |
| Q5 | "What does a blank mean in these columns?" | None (use 0) / **not known** / not applicable / not yet | S | B | Must |
| Q6 | Unit or scale confirmation, with the detected hint pre-selected | Rupees / paise / lakhs; 0-1 or 0-100; 5-point or 10-point | S | B | Must |
| Q7 | "Which date says when you knew this, or when each customer's clock starts?" | The detected date columns | S | B | Must |
| Q8 | "What date is this data true as of?" | Latest date in the file / date in the file name / back-calculated from `days_since` / type a date | S | B | Must |
| Q9 | "Was this file taken today, or on the as-of date?" | Today / as-of / a history table exists | S | B | Must |
| Q10 | "Who counts as a customer on the as-of date?", with the number of rows and the positive rate shown for each option | **Bought in the last 12 months** / any order ever / everyone in the master | S | B | Must |
| Q11 | "Two copies disagree: which one wins?" (copies, source of truth, conflicting outcomes) | Later wins / earlier wins / any yes / latest by a date / leave out | S | B | Must |
| Q12 | For each value, three ways: **positive / negative / leave these out**. The same question shape is used for "which statuses mean never contact" | One row per value with its count | S | B | Must |
| Q13 | "Was a random group deliberately NOT contacted?" | Yes, there is a flag column / yes, a separate list / **no** / I don't know | S | B | Must |
| Q14 | "In this column, what does Y mean? What was the consent given for, and how old may it be?" | Agreed / asked NOT to be contacted; marketing or service only | S | B | Must |
| Q15 | "This looks like religion, health or caste. Use it?" | **Fairness report only** / remove / use | S | B | Must |
| Q16 | "I don't know" leads to "Who would know?" (CRM admin, DBA, compliance, finance, campaign manager). It gives a message ready to copy and paste, parks the step as "waiting for X", and states the safe default it will use meanwhile. | Roles | M | B | Must |
| Q17 | "What business rules hold?" (eligibility, product rules), showing how many rows each rule would remove | Typed rules | S | C | Should |
| Q18 | Locale settings, asked once per client: time zone, month the fiscal year starts, usual day/month order | **Asia/Kolkata, April, day-first** | S | B | Should |
| Q19 | "Rows are colour-coded: what do the colours mean?" / "Include hidden rows?" | Legend options | M | C | Later |
| Q20 | "Is this all your customers, or an extract?" | All / a sample / one segment / I don't know | S | B | Must |

### (c) New code checks: hard guards that must not depend on the prompt

| ID | Guard | Situations | Size | Batch | Priority |
|---|---|---|---|---|---|
| G1 | **Receipt for every SQL step, computed by the sandbox:** rows in for each source; rows out; distinct keys in and out; keys lost and keys duplicated, **counted separately and never netted off**; totals and counts of every money or count column before and after. If the base table was one row per customer and an SQL step duplicates a key, the plan **stops**. | crit-01, crit-03, K12, K9, V19 | M | B | Must |
| G2 | **Change-share measure that fits each step type.** SQL step: measure each output column whose name matches an input column, by joining on the key. Reshaping steps (combine, unpivot): check that totals and distinct keys are kept, instead of counting the change in rows. Parsing steps: count only values whose meaning changed (failed, or not the obvious reading). The 30%/60% limit stays for AI rewrites of values. A step that cannot be measured is treated as "needs a yes", never as 0%. | crit-01, crit-02 | M | B | Must |
| G3 | **Running total for the whole plan:** rows and customers after each step, total share lost, and positive rate before and after. If the total loss is over 5%, or the positive rate moves more than 1 point, the plan needs an explicit yes, naming the steps that contributed most. | crit-04 | S | B | Must |
| G4 | **Collision check on key rules:** count distinct IDs before and after the tidy-up rule, on each side. A rule that merges different IDs (prefix strip, zero strip, case change) is refused, or shown as "IDs merged by this rule: N" for a yes. | K1, F23, DQ12 | S | B | Must |
| G5 | **Key-quality report** for every join key: share unique, the 5 most frequent values, placeholder shapes (9999999999, test@, NA), the `E+` pattern (**stop** and send a data request), and mixed lengths that point to lost leading zeros. | F01, F02, K3, K17, V06 | S | B | Must |
| G6 | **Join cross-check:** on matched pairs, how often other shared details agree (phone hash, email hash, city, date of birth), and whether dates are in a sensible order (no orders before signup). If agreement is below about 70%, ask "are these the same customer numbers?". | crit-05 | M | C | Should |
| G7 | **Leak check based on filled rows:** for each column, a score for whether it is blank or not, and the positive rate among filled vs blank rows. Add these words to the name pattern: reason, cancel, closure, refund, termination, exit, attrition, retention, risk, propensity, likelihood. | S05, S21, S12 | S | B | Must |
| G8 | **Positive label stated explicitly** and passed to every check, readiness step and reason. Remove the "pick the rarer value" fallback from the agent's path. If churn, default or fraud is above 50% positive, ask "is this the right way round?". | S22, S08 | S | B | Must |
| G9 | **Date sanity:** placeholder-date spikes (1900-01-01, 1970-01-01, 1899-12-30); `9999-12-31` / `2099-12-31` in end or valid-to columns read as "still open"; dates after the as-of date; order between related dates (signup ≤ first purchase ≤ last purchase); a range check after Excel serial conversion (the 1904-system shift). **Fix the bug where `date_order` returns None and the code silently reads month-first**: that case must stop and ask. | T03, F20, T01, T04 | M | B | Must |
| G10 | **Time safety for SQL:** reject SQL text that contains `now()`, `current_date`, `today()` or a typed-in date that is not the `:as_of` value. Save time settings in the recipe as rules ("latest date in the file", "last complete month-end"), each with the date it resolved to. Run the existing leak test on tables built by SQL (move future rows; the features must not change). | T23, T05-T24, T14 | M | B | Must |
| G11 | **Maturity and tail check:** outcome rate per period (recent periods immature?); last period covering less than 90% of its days; final days below 60% of the 4-week median for the same weekday. Each proposes an as-of date and leaving the period out. | T07, T15, T16, DQ19, S14 | S | B | Must |
| G12 | **Overlap and duplicate detector:** the same ID in more than one file; files whose date ranges overlap; rows that are complete duplicates (reported, **never dropped silently**); a header row repeated in the middle of a file. | F21, K7, DQ07, T17 | S | B | Must |
| G13 | **Profile on a random or stratified sample of the whole file**, not the first rows. Read the full value lists for key, consent and outcome columns. | crit-06, DQ10 | S | B | Must |
| G14 | **Contact-list gate:** unknown consent is not consent; minors are excluded (age on the as-of date; unknown age is flagged); suppression lists are removed by matching on cleaned keys, with the match rate shown; deceased, fraud and legal statuses are removed; a warning if the DND scrub is older than N days. Each exclusion is counted on the list. | P7, P8, C07, C10, C20, V06 | M | B | Must |
| G15 | **Sensitive-column detector** (word lists for column names and values, including product and merchant categories). Such columns are left out of features and out of what the AI sees, unless Q15 says otherwise. | P3, P20, V23 | S | B | Must |
| G16 | **Long text hidden from the AI by default:** columns whose median length is over 40 characters, or named notes, remarks or comments, show only their shape to the AI. | P1, P2 | S | B | Must |
| G17 | **Shape detectors:** garbled characters from wrong encoding (`Ã`, `à¤`, `�`, `?` inside words); a file cut off (row count near 1,048,576 or 65,536, or a short last line); formulas with no saved value; counts of hidden rows and columns; several table blocks on one sheet; Tally or SAP exports recognised (Dr/Cr, a trailing minus, pipe-character separator lines). | F18, F22, F06, F08, F03, F13, F14 | M | C | Should |
| G18 | **Next-month guards:** date coverage vs the as-of date; ID overlap with last month (expect 80-95%); row count within ±30%; a category that has vanished; rows per ID; outcome-rate stability; key coverage falling more than 10 points; a consent or flag share moving more than 20 points (re-confirm its meaning); a schema fingerprint. A failure pauses the run with a question; it does not run on. | DQ02, DQ04, DQ06, DQ08, DQ09, DQ17, DQ18, DQ20, K22, P18 | M | C | Should |
| G19 | **IDs read as text by default:** `keep_leading_zeros=True` on the first read (columns typed later). | F01, K17 | S | B | Must |
| G20 | **Client isolation test:** two clients, and the memory and AI context of one never shows in the other. The client ID is mandatory when a case is created. | P23 | S | B | Must |
| G21 | **Treatment balance check:** compare treated vs untreated customers on details from before the campaign (standardised difference, plus a small model that predicts the treatment flag). | C01, C19 | M | C | Should |
| G23 | Every run removes customers on the erasure register (matched by hashed key) as a counted step; scratch copies are deleted N days after approval. | P21 | S | C | Should |
| G24 | When the key itself is PII (phone, PAN, Aadhaar): the working key is a salted hash. The real value is kept only for the contact-list export. Aadhaar is never stored (ask for another key). | P25, P19 | S | B | Must |

### (d) Data requests: new templates for the data request kit

All of them are Size S. All are Batch B except D10, which is Batch C.

| ID | Request | Triggered by |
|---|---|---|
| D1 | "Re-export with the ID or phone column formatted as **Text**, or send the .xlsx. The digits have already been rounded off." | F02, K17 (G5) |
| D2 | "Send the row-level export, not the summary or pivot." | F05, DQ14, C16 |
| D3 | "Save as .xlsx or CSV UTF-8 (steps shown). We never ask for a password." Also: "a PDF is a picture of a table", and "download the Google Sheet and tell us the date". | F10, F11, F12, F18 |
| D4 | "Plan, tier or status **history with effective dates**", or "the date each customer left". | T11, K10, S03 |
| D5 | "Confirm this churn list covers ALL leavers from [date] to [date]", or send the missing channels or regions. | S04, S18, DQ09 |
| D6 | "The old-to-new ID mapping from the CRM migration", or "the merge log". | K20, K5 |
| D7 | "A fresh DND scrub", "the consent source", "the suppression / erasure list". | C07, P5, P8, P21 |
| D8 | "Product master with categories and a discontinued flag", "code dictionaries (plan, branch, state)". | V09, V10, C15 |
| D9 | "12+ months of history (last year's same season too)." | T21, T20, S06 |
| D10 | "Keep a random 5-10% holdout in your next campaign" (with the size worked out by the power calculation). | C01, C02, S13 |
| D11 | "An exchange-rate table (date, currency, rate)." | V08, DQ16, C21 |
| D12 | "The usual delay of this feed", and "a full extract, not a partial one". | T16, DQ09, P11 |

### (e) What the screens need

| ID | Need | Size | Batch | Priority |
|---|---|---|---|---|
| U1 | **A long plan without fatigue:** group the steps into 5-7 sections (Read · Fix values · Combine · Outcome · Exclusions · Privacy). One plain sentence and one count per section. Sure fixes are folded away and can still be unticked. Only items needing a yes are opened. Approve section by section, then one final yes. | M | B | Must |
| U2 | A **funnel bar at the top** of the plan: rows and customers in, then out, and the positive rate before and after (from G3), with "biggest losses" clickable. | S | B | Must |
| U3 | **"3 things to check"** at the top, ranked by risk (merged IDs, columns dropped as "known only after the outcome", exclusions from the contact list). | S | B | Must |
| U4 | Each SQL step shows the sandbox receipt (G1) and before/after sample rows with personal data hidden. The plain-English description is checked against the receipt, so every number comes from our code. | S | B | Must |
| U5 | A question queue with at most 3 blocking questions. The rest are deferred, with the default stated. "I don't know" opens the who-would-know message (Q16). Parked steps can be resumed later. | S | B | Must |
| U6 | Exclusions shown on the contact list (consent unknown, minors, DND, suppressed, deceased), each with a count. | S | B | Must |
| U7 | An exportable **data handling report**: steps, approvers, rows removed for each reason, what the AI could see, source and pull window. | S | C | Should |
| U8 | A "what changed since last month" screen for the replay (G18 results), with one-click confirmation of the new as-of date. | M | C | Should |
| U9 | Steps tagged as privacy, consent, sensitive or new-join need a second approver (once sign-in is on). | M | C | Later |

**The Must-for-v1 set:** R1-R8, R10-R15, R18, R19, R21, R23-R26, R29 · Q1, Q3, Q5-Q16, Q20 · G1-G5, G7-G16, G19, G20, G24 · D1-D9, D11, D12 · U1-U6. Almost all of it is size S. The M-sized pieces (G1, G2, G9, G10, G14, R6, Q16, U1) are the core of Batch B.

---

## 4. Out of scope for v1, and what the Doctor should say

| Situation | Why it is out of scope | What the Doctor says |
|---|---|---|
| PDF, scans, photos of registers (F11) | Reading text from images (OCR) gets digits wrong without anyone noticing | "A PDF is a picture of a table. Please ask for the Excel or CSV export of this report." (D3) |
| IDs already rounded by Excel (`1.23E+11`) (F02, K17) | The digits are gone | "These IDs were rounded when the file was saved, so different customers now look the same. We cannot join on them. Please re-export with the column as Text." (D1) |
| Live Google Sheets or SaaS connectors (F12) | Needs a new connector | "Download it as .xlsx (File > Download). Which date is this copy from?" |
| Chat-log .txt exports (F17) | Not a table | "This is a chat log, not a customer table. Send the delivery report export instead." |
| Predicting amounts (CLV, spend next 90 days) (S11) | The product only predicts yes/no outcomes | "I can predict *who will spend more than ₹X*. Predicting the exact amount is not in this version." |
| "Next best product" across many products (C15) | Needs one model per product | "Pick one product to predict. We can repeat this for others." |
| Uplift from a past campaign that was not random (C01, S13) | Cause cannot be separated from who staff chose | "This shows who responds among people you chose. It cannot show who was persuaded. Keep a random group out of the next campaign." (D10) |
| Automatic currency conversion (V08, C21) | v1 decision: no conversion | "Amounts are in ₹ and $. Model each market separately, or give a dated rate table." |
| Fuzzy matching of people by name similarity (K4) | Matching is exact by design | Offer merge options based on exact phone, email or date of birth only (Q2). Otherwise: "These may be duplicates; we kept them separate." |
| Consent rules outside India (C21) | Only Indian rules are encoded | "Only Indian consent rules are checked. Customers in other countries are excluded from the list until you confirm their consent." |
| Incremental database pulls, very large tables without filters (P9, P10, P11) | Needs connector work | "This table is too large to copy whole. Please ask IT for a view with the last 18 months." (D12) |
| Seasonality with less than 13 months of history (T20) | Season and trend cannot be told apart | "Your data covers one festive season only. Scores may be too high in a normal month." (D9) |
| Too few positives (fewer than about 50) (S06, C23) | Not enough examples to learn from | "There are only 40 examples of customers who [did X]. We need about 200. A looser definition gives [N]; or wait until [date]." |
| Next-month automatic re-checks (G18) | Not in the v1 scope | "Bring next month's file and we will reuse your answers. Check the changes we list." |

---

## 5. Appendix: the full situation table (195 rows)

**Columns.** Handled: yes, partly or no, for the planned agent before the additions above. Resolvable by: G = from the given data; A = by asking; GA = both; X = needs more data or out of scope. Additions refer to section 3.

| ID | Cat | Situation | Handled | Resolvable | Addition |
|---|---|---|---|---|---|
| F01 | formats | Leading zeros lost in IDs, PIN codes, phones; the CRM keeps them | partly | GA | G19, G5, R3, Q3 |
| F02 | formats | Excel-saved CSV shows IDs as `1.23457E+11` (digits rounded) | no | X | G5 stops, D1 |
| F03 | formats | Two tables on one sheet (summary block above, lookup beside) | partly | GA | G17 block finder, Q4 |
| F04 | formats | Merged cells or blanks meaning "same as above" (Region, Tally date, Shopify email) | partly | GA | R15 fill-down after asking |
| F05 | formats | Pivot table pasted as values, with subtotals and Grand Total | partly | GA | R25 summary detector, D2 |
| F06 | formats | Formulas never calculated; TODAY()-based tenure frozen at save time | partly | GA | G17 formula report, R5 recompute |
| F07 | formats | Meaning carried by cell colour (red = churned) | no | A | Q19 colour legend (Later), ask for a Status column |
| F08 | formats | Hidden or filtered rows and hidden columns | no | A | G17 hidden counts, Q19 |
| F09 | formats | Related sheets: Jan/Feb/Mar, Customers/Orders, a Legend sheet | partly | GA | R14 list all sheets |
| F10 | formats | .xls, .xlsb, password-protected or ODS files | partly | A | D3 template |
| F11 | formats | PDF reports, scanned registers | no | X | Out of scope, D3 |
| F12 | formats | Google Sheets link pasted | no | A | Steps + as-of question, D3 |
| F13 | formats | Tally day book: Dr/Cr suffixes, continuation rows | partly | GA | R15, R4, G17 Tally signature |
| F14 | formats | SAP: `31.03.2024`, `1.250,00`, trailing minus `250-`, pipe-character separator lines | partly | GA | G17, trailing-minus SQL pattern |
| F15 | formats | Shopify: one row per line item, phone with leading `'`, tags, consent column | partly | G | R3, R15, R18 |
| F16 | formats | Razorpay: paise vs rupees, JSON notes, attempts as rows | partly | A | Q6, R4, R13 |
| F17 | formats | WhatsApp delivery reports and chat .txt exports | partly | GA | R3 phone, R20 12-hour time, chat = out of scope |
| F18 | formats | Encoding damage to Hindi text (cp1252 / mixed / UTF-16 / Kruti Dev) | partly | GA | G17 garbled-text detector, D3 |
| F19 | formats | Line breaks inside cells, delimiter collisions, a `sep=;` line | yes | G | Test for `sep=;` and a stray quote |
| F20 | formats | Excel serial dates, 1904 system | yes | GA | G9 range check after conversion |
| F21 | formats | Files split into parts, overlapping monthly exports, zips of parts | partly | GA | G12, stack with source file |
| F22 | formats | Huge files, Excel row-limit cut-off, half-downloaded CSV | partly | GA | G17 cut-off detector |
| F23 | formats | Salesforce 15 vs 18-character IDs, IDs where case matters | partly | GA | R3, G4, Q3 |
| F24 | formats | Units only in headers or titles ("₹ in Lakhs") | partly | GA | R13, Q6 |
| F25 | formats | Nested JSON (line items arrays) | partly | GA | SQL unnest pattern, empty-array count |
| K1 | multitable | ID spelling differs; the tidy-up rule merges distinct IDs (C-123 / S-123) | partly | GA | G4 collision check, R1 |
| K2 | multitable | Several ID systems, no single crosswalk | partly | GA | R2, Q1 |
| K3 | multitable | One phone or email shared by many customers, placeholders | partly | GA | G5, Q11 household |
| K4 | multitable | Same person under two IDs | no | GA | Q2 (Should), exact-match clusters |
| K5 | multitable | Merged or deleted customers still in transactions | partly | GA | Recursive merge-chain SQL, G23, D6 |
| K6 | multitable | Orders for customers not in the master (guests, test, newer) | yes | GA | Profile unmatched keys, Q11 |
| K7 | multitable | Overlapping monthly exports double counted | yes | GA | G12, Q11 later file wins |
| K8 | multitable | Monthly full snapshots; churn = missing from the next one | partly | G | R7 panel pattern, Q9 partial-export check |
| K9 | multitable | Tables at different grains; invoice known only after period end | partly | GA | R4, R6 availability date, G1 fan-out stop |
| K10 | multitable | Plan or tier holds today's value only | partly | GA | R6, D4 |
| K11 | multitable | Lookup tables with mismatched or duplicate codes | partly | GA | R22 |
| K12 | multitable | Joint accounts: balance counted per holder | partly | A | G1 totals check, Q11 allocation |
| K13 | multitable | Household vs individual vs account | partly | GA | Q1, R29 |
| K14 | multitable | Campaign table keyed on phone, control group missing or overlapping | partly | GA | R2, R19, Q13 |
| K15 | multitable | Payments with failures, reversals, retries | partly | GA | R4 status question |
| K16 | multitable | Complaint status log, reopened tickets | yes | G | R4 latest per ticket first |
| K17 | multitable | Key columns damaged by Excel (E+, .0, lost zeros) | partly | X | G5, G19, D1 |
| K18 | multitable | Same event dated differently across systems (UTC/IST, date orders) | partly | GA | R20, R21, Q18 |
| K19 | multitable | Test, staff, bulk corporate accounts | no | GA | R17 |
| K20 | multitable | ID migration; mapping on another sheet | partly | X | R14, D6 |
| K21 | multitable | Tables cover different time windows | partly | GA | R12 |
| K22 | multitable | Next month's IDs or codes change shape | partly | GA | G18 coverage drop |
| T01 | time | One date column with DD/MM and MM/DD from two systems | partly | GA | G9 stop when both orders appear, R21 |
| T02 | time | Month-year values (`Mar-24`, `FY24 Q1`, no year) | partly | GA | Month-precision option, never take the year from today |
| T03 | time | Placeholder or impossible dates (1970, 1900, 9999, future, order violations) | partly | GA | G9 |
| T04 | time | Dates stored as numbers (serial, epoch seconds or milliseconds, yyyymmdd) | partly | G | Wire `find_excel_serial_dates`, epoch detection |
| T05 | time | UTC and IST mixed | partly | A | R20, Q18 |
| T06 | time | As-of date unknown or stale for a flat file | partly | GA | Q8, R5, back-calculate from `days_since` |
| T07 | time | Recent outcomes not yet mature in a flat file | partly | GA | R8, G11 |
| T08 | time | Outcome window starts at each customer's own date | no | GA | R8, Q7 |
| T09 | time | Choosing the inactivity window for churn | partly | GA | R9 |
| T10 | time | Subscription start/end intervals; population at the as-of date | partly | GA | R7 |
| T11 | time | Today's snapshot mistaken for history | partly | GA | R6, Q9, D4 |
| T12 | time | History table with valid-from/valid-to; 9999 open ends | partly | G | R6, G9 open-ended |
| T13 | time | Outcome built from a column that is also a feature | partly | GA | R10 |
| T14 | time | Tenure measured to the wrong date | partly | G | R5, G10 |
| T15 | time | Partial current month in aggregated tables | partly | G | G11, R23 |
| T16 | time | Late-arriving data, thin tail | no | G | G11, R23, D12 |
| T17 | time | Overlapping exports and back-dated corrections | partly | GA | G12, the version known at the time |
| T18 | time | Snapshot date only in the file name | partly | GA | R14, Q8 |
| T19 | time | Indian fiscal-year labels (FY24-25, Q3 FY25) | no | A | Q18 fiscal start, period parsing |
| T20 | time | Festival, sale and pay-day seasonality | partly | GA | R28, D9 |
| T21 | time | History too short for the window | partly | X | Time span in the readiness check, D9 |
| T22 | time | Same customer in several snapshots; the split leaks | partly | G | Group split by ID, gap between train and test |
| T23 | time | Next month's replay with fixed as-of literals | partly | GA | G10 time settings saved as rules |
| T24 | time | Same-day boundary at the as-of date or send date | partly | GA | R19, G10 leak test |
| S01 | outcome | Churn built from orders; never-active and new customers labelled churned | partly | GA | R7, Q10 |
| S02 | outcome | Status with several values (Suspended, Dormant) | yes | A | Q12 three-way, warn if more than 20% excluded |
| S03 | outcome | Status and features exported after churn | partly | GA | R6, Q9, D4 |
| S04 | outcome | Outcome in a separate list; is the list complete? | partly | GA | D5, match-rate stop |
| S05 | outcome | Columns filled only for churners (cancellation_reason) | no | G | G7 |
| S06 | outcome | Very rare outcome | partly | GA | Alternatives with counts, D9 |
| S07 | outcome | Outcome means different things per business unit | no | GA | R16 |
| S08 | outcome | Positive label spelled many ways | yes | G | G8 explicit label |
| S09 | outcome | Outcome given by a date column being filled | partly | GA | R7, Q5 |
| S10 | outcome | Outcome per account, contact per person | no | GA | Q1, R29 |
| S11 | outcome | Amount target, but only yes/no outcomes are supported | partly | GA | Threshold options; amounts out of scope |
| S12 | outcome | Several outcome columns in one file | partly | A | R10 |
| S13 | outcome | Past campaign response, chosen by staff, no holdout | partly | GA | R19, Q13 |
| S14 | outcome | Delayed outcomes; newest month still maturing | yes | GA | Use the earliest end date across tables |
| S15 | outcome | Status history that flips back and forth | partly | G | R7 status on the as-of date |
| S16 | outcome | Conflicting outcomes for one customer | partly | GA | Q11 |
| S17 | outcome | Eligible population is part of the outcome | partly | GA | R7 templates, check whether already true on the as-of date |
| S18 | outcome | Offline conversions not tracked; low match rate | partly | GA | Match-rate stop, D5 |
| S19 | outcome | Closure reasons mixed (migrated, deceased, fraud) | yes | A | Q12 groups, G14 |
| S20 | outcome | Outcome definition changed partway through the history | no | GA | R28 |
| S21 | outcome | Scores from other systems in the file | partly | A | G7, "before or after?" question |
| S22 | outcome | Outcome the wrong way round (retained = 1) | partly | A | G8 |
| V01 | values | Paise vs rupees | no | GA | R13, Q6 |
| V02 | values | Percentages stored as 0-1 and 0-100 | partly | G | R13, Q6 |
| V03 | values | Negative values are refunds | partly | GA | R11 |
| V04 | values | Zero vs blank | partly | GA | R12, Q5 |
| V05 | values | Capped values (999999 meaning unlimited) | partly | GA | Spike at the maximum + question |
| V06 | values | Test, dummy, employee accounts | no | GA | R17, G14 |
| V07 | values | Real extremes (corporate accounts) | partly | GA | R17, customer-type flag |
| V08 | values | Mixed currency with a currency column, or per file | partly | A | Record the unit of each file before stacking, D11 |
| V09 | values | Coded values needing a lookup | partly | GA | R22, India state table, D8 |
| V10 | values | Category hierarchies, 4,000 SKUs | partly | GA | Roll up to the parent level, D8 |
| V11 | values | Free text that matters | partly | GA | R27 |
| V12 | values | Hinglish and regional-script values | partly | GA | Grow the word lists, ask the language |
| V13 | values | Yes/no in many forms; which way DND points | partly | GA | Add ✓/✗ glyphs, Q14 |
| V14 | values | Gender and titles inconsistent | partly | GA | R24, never infer gender |
| V15 | values | Rounding artefacts; stored age vs date of birth | partly | GA | R5, scale question |
| V16 | values | Units mixed across rows or files | yes | GA | Units read from headers |
| V17 | values | Indian number bands (`5L-10L`) | partly | GA | Ordered category or range parsing |
| V18 | values | Date and time-zone meaning | partly | GA | R20, R21, Q18 |
| V19 | values | Order total repeated on every line item | no | G | R4, G1 |
| V20 | values | Status values that need a business decision | yes | A | Q12 three-way |
| V21 | values | Formatted keys, lost zeros, E+ | partly | GA | R3, G5 |
| V22 | values | Same measure, different definitions between files | no | A | R13 ratio check + question |
| V23 | values | Sensitive attributes | partly | A | G15, Q15 |
| C01 | business | Treatment not random | no | GA | R19, G21 |
| C02 | business | No control group at all | partly | G | R19 wording |
| C03 | business | Messy or multi-arm treatment values | partly | GA | Q12, DND/NA = not eligible |
| C04 | business | Contact log features | yes | G | R19 delivered and opened are not responses |
| C05 | business | Contact fatigue | partly | GA | Ask the contact cap, features on earlier contacts |
| C06 | business | Consent per channel, many spellings | yes | GA | R18 DND-Partial, blank = unknown |
| C07 | business | DND scrub stale or missing | partly | A | G14 age warning, D7 |
| C08 | business | Eligibility rules not written down | partly | GA | Q17 |
| C09 | business | Customers who already hold the product | partly | GA | R7 |
| C10 | business | Exclusion lists in other key formats | partly | GA | R18, G14 match rate |
| C11 | business | Budget and capacity | yes | A | Ask only when the goal is a contact list |
| C12 | business | Several brands or business units | partly | A | R16 |
| C13 | business | B2B and B2C mixed | partly | A | Q1, R16 |
| C14 | business | Goal unclear | yes | GA | First question: "what will you do with the score?" |
| C15 | business | Product catalogue for cross-sell | partly | GA | R22, choose one product |
| C16 | business | Campaign-level summaries only | partly | X | R25, D2 |
| C17 | business | Attribution windows and overlapping campaigns | partly | GA | R19 |
| C18 | business | Festival distorted the training period | no | A | R28 |
| C19 | business | Discount levels chosen by sales reps | partly | GA | G21, ask the levels |
| C20 | business | Leads already being worked by sales | partly | A | Q12 do-not-contact statuses |
| C21 | business | Several countries and currencies | partly | GA | Out-of-scope message on consent, D11 |
| C22 | business | Cohort snapshots with a current-status column | partly | GA | R6 |
| C23 | business | Tiny outcome | yes | G | Name the number needed and the date it could be reached |
| C24 | business | Recipient identity differs from customer identity | partly | GA | R2, G5 |
| DQ01 | quality | Columns renamed next month | yes | GA | Cover the key and outcome columns too |
| DQ02 | quality | Columns added or dropped next month | partly | GA | G18 feature check |
| DQ03 | quality | New category values | yes | GA | "Same as X?" question |
| DQ04 | quality | Column changes meaning, keeps its name | partly | A | G18 |
| DQ05 | quality | Source system migrated partway through the history | no | GA | R21 per period, break detector |
| DQ06 | quality | One month with a large share missing | partly | A | Blank share by month, never fill |
| DQ07 | quality | Same load delivered twice | partly | GA | G12 |
| DQ08 | quality | Wrong (last year's) file uploaded | no | GA | G18 freshness |
| DQ09 | quality | Partial extract (one region) | no | GA | G18 vanished category, D12 |
| DQ10 | quality | A sorted or biased sample taken for the full file | partly | GA | Q20, G13 |
| DQ11 | quality | Different customer mix in the scoring file | partly | GA | Explain the drift by segment (C) |
| DQ12 | quality | IDs inconsistent across months and tables | yes | GA | G4 collisions |
| DQ13 | quality | Contradictions across columns | partly | GA | G9 date order, Q17 |
| DQ14 | quality | Summary data instead of rows | partly | GA | R25 |
| DQ15 | quality | Number format changes between months | yes | G | Report columns with mixed formats |
| DQ16 | quality | Currency mix appears in a later month | yes | GA | D11 |
| DQ17 | quality | Category re-coded; consent meaning flipped | partly | A | G18 flag-share check, Q14 |
| DQ18 | quality | Outcome definition changed | partly | A | Show the outcome definition at replay |
| DQ19 | quality | Late-arriving data, open window | partly | GA | G11, R23 |
| DQ20 | quality | Grain changes next month | partly | GA | G18 rows per ID |
| DQ21 | quality | Placeholder values change | yes | GA | G9 placeholder-date spikes |
| DQ22 | quality | Seasonality mistaken for drift | partly | A | Remember "this is seasonal" |
| DQ23 | quality | Conflicting attributes across sources | partly | A | Q11 source of truth |
| DQ24 | quality | Test and internal records | no | GA | R17 |
| P1 | privacy | PII inside free text, names in prose | yes | G | G16 |
| P2 | privacy | PII in the wrong column | partly | GA | Scan all text columns, ask |
| P3 | privacy | Sensitive attributes present | no | GA | G15, Q15 |
| P4 | privacy | Stand-ins for sensitive attributes (PIN code, surname) | no | GA | R24 |
| P5 | privacy | Consent in a separate log file | partly | GA | R18 latest as of the date |
| P6 | privacy | Unclear or stale consent values | partly | A | Q14 |
| P7 | privacy | Minors | no | GA | G14 |
| P8 | privacy | Opted-out, deceased, fraud, legal hold | partly | GA | R18, G14, D7 |
| P9 | privacy | Very large database table | partly | GA | Pull spec (later), D12 |
| P10 | privacy | Incremental monthly refresh | partly | GA | "Full history or changes only?" question |
| P11 | privacy | Slow or unreliable connection | partly | A | Pull in date chunks, D12 |
| P12 | privacy | Live schema differs from the documentation | partly | GA | Compare with the dictionary, G18 |
| P13 | privacy | Views that show only the current state | partly | A | Q9, R6 |
| P14 | privacy | Read-only credential limits | yes | A | Data-request line for missing grants |
| P15 | privacy | Data must stay in India | partly | A | Onboarding question about where AI data may go |
| P16 | privacy | Person does not know the answer | partly | A | Q16 |
| P17 | privacy | Several approvers needed | partly | GA | U9 (Later) |
| P18 | privacy | Changed file in the monthly refresh | partly | GA | G18 |
| P19 | privacy | Hashed or different PII across systems | partly | GA | G24, ask how the values were hashed |
| P20 | privacy | Sensitive purchase categories | no | GA | G15 values |
| P21 | privacy | Erasure requests | partly | GA | G23 |
| P22 | privacy | Purchased or partner list, lawful basis unknown | no | A | Question about where the data came from; list blocked until answered |
| P23 | privacy | Agency with several clients in one workspace | partly | X | G20 |
| P24 | privacy | Audit trail asked for later | partly | G | U7 |
| P25 | privacy | Aadhaar, PAN or phone as the key | partly | GA | G24 |
| crit-01 | critic | SQL step bypasses the 30%/60% limit (measured as 0%) | no | G | G1, G2 |
| crit-02 | critic | Core reshaping or parsing steps refused by the 60% limit | partly | G | G2 |
| crit-03 | critic | Duplicates and drops cancel out in the net row count | partly | G | G1 |
| crit-04 | critic | Many small drops add up to bias | no | G | G3, U2 |
| crit-05 | critic | Join that matches by coincidence (same numbering) | no | GA | G6, R1 |
| crit-06 | critic | Sorted file, only the first rows profiled | partly | GA | G13, R26 |

Counts: 195 situations. Handled yes 24, partly 139, no 32. Resolvable from the data or by asking: 188. Needs more data or out of scope: 7 (F02, F11, K17, K20, T21, C16, P23).
