# Data Doctor: the order-log path (Phase B) — design proposal

Status: **proposal for the owner to approve.** No code has been written. Branch `version-2`. Line numbers are
for the committed HEAD. `engine/agent/advisor.py` has an uncommitted edit in the working tree that moves its lines
by about 27. All real-file numbers come from the UCI file (541,909 lines), measured with
`library/online-retail/fetch.py`'s rules.

## 1. Problem and goals

A raw order log such as UCI Online Retail (`InvoiceNo, StockCode, Description, Quantity, InvoiceDate, UnitPrice,
CustomerID, Country`) is refused everywhere today. Two facts explain it.

- **The outcome is looked up by column name only.** Guided setup stops at `advisor.py:486-492` ("No column says
  whether…"). The old Doctor answers `NO_OUTCOME` (`engine/doctor/diagnose.py:235-247`). The visit records a doubt
  and a data request (`engine/doctor/visit.py:1795-1812`) and then loops between TREAT and VERIFY with
  `VISIT_NO_OUTCOME` (`visit.py:1960`).
- **Every tool that could build the outcome needs a separate customer list.** The same refusal appears four times:
  `engine/onboarding/validate.py:256` (`NO_ENTITY_SOURCE`), `engine/doctor/build_driver.py:355-359`
  (`BUILD_PLAN_INCOMPLETE`), `engine/doctor/outcome_definition.py:957-961` (`OUTCOME_CANNOT_MEASURE`) and
  `engine/doctor/outcomes.py:604` (`offer_outcome` returns None).

Almost everything else already exists:

- point-in-time features (`engine/onboarding/features.py`);
- month-end snapshots (`engine/onboarding/snapshots.py:66-98`);
- future-window labels with censoring (`engine/onboarding/labels.py:464` `build_labels`);
- the leak probe (`engine/onboarding/build.py`, `FUTURE_EVENTS_LEAKED`);
- the exact-sentence outcome definition (`propose_event_outcome`, `outcome_definition.py:925`; `label_spec_of`, `:595`);
- the build executor (`drive_builder`, `build_driver.py:892`).

Nothing connects these pieces for a person. `drive_builder` is only called from tests.

**Goals.**

1. A single order log reaches **ready** in the Data Doctor visit for any use case whose outcome can be built from
   events.
2. Every meaning-bearing choice is asked, counted and approved: which column is the customer, which rows are left
   out, and what the outcome is.
3. No safety rule is loosened: minimum rows and positives, consent, leakage, exact keys only, and a person
   approving the outcome.
4. Nothing changes while the new switch is off (M249, §7).

**Not goals.** Turning on `reshape` for every use case. Changing `advise()`'s stops or `STOP_CODES`. Letting the
AI pick anything (that is Phase C).

## 2. The flow, state by state

The path adds **no new visit state**. It is a new rule module, `engine/doctor/order_log.py`, called from the
visit's existing places. Its questions and its one plan item travel through the states the visit already has
(`visit.py:197-201`).

| State | What happens | Reused | New |
|---|---|---|---|
| UNDERSTAND | `diagnose_files` as today. Also `order_log_reading(file)`: an ID-like column that repeats (more than 1.05 rows per value; blanks allowed), a readable date column, and no column that is the goal's outcome. It may also find a second repeating ID-like column (the order number). | `diagnose_file` (`diagnose.py:87`), `key_candidates` (`sources.py:135`) | The detector. It ranks by ID-like name first and blank share second, for this path only, so `CustomerID` is not buried by `sources.py:164`. Names are matched with camelCase split (`CustomerID` → `customer_id`). |
| TREAT (goal rules) | In `_goal_rules`' no-outcome branch (`visit.py:1795`), if the file reads as an order log **and** the goal has an outcome recipe, the path opens Q1 and Q2 below instead of only recording the doubt. The data request stays as the alternative. | `_goal_rules`, `CaseQuestion`, `_add_request` | `order_log.rules(...)` |
| ASK | Q1 (customer column), then Q2 (blank-customer rows), both blocking. | ASK as is | — |
| TREAT (second pass) | With Q1 and Q2 answered, the outcome is measured with `propose_event_outcome`. The customer list is derived from the log (§4). The build plan comes from `plan_build`. Both go into one pending, never-ticked plan item of a new kind, `order_log_build`. A window or audience choice that changes the meaning is asked (Q3, Q4). | `propose_event_outcome`, `label_spec_of`, `plan_build` (`build_driver.py:326`), guards (`engine/doctor/guards.py:676`) | The derived customer list, the order roll-up (§4) and the `order_log_build` item |
| PREVIEW | The plan is tried on the first 1,000 customers' lines (not the first 1,000 lines, so orders stay whole). Counts are shown per snapshot. | PREVIEW/`check_plan` | A sampler that picks by customer |
| APPROVE | Two exact sentences: the outcome (an Analyst, `data.define_outcome`, `configs/agentic/policy.yaml:311-315`) and the build (`data.build_dataset`, `policy.yaml:161-166`, plus the over-5% loss acknowledgement). A changed sentence gives `APPROVAL_SENTENCE_CHANGED`. | `check_outcome_approval`, `BuildApproval` | — |
| PRESCRIBE | `drive_builder(plan, frames, port, approval)` runs. Its result's `dataset_id` is recorded on the case as a built table whose `original_upload_id` is the log. The decided outcome column is set on that table. | `drive_builder`, `_combine` (`visit.py:1328`) | An in-process `BuilderPort` that calls the onboarding routes' functions directly (today only `ApiBuilderPort`, over HTTP, exists in `engine/doctor/builder_api.py:63`). Add `order_log_build` to the kinds `current_upload` follows (`engine/doctor/combine_run.py:163`). |
| VERIFY | `readiness_verify` (`visit.py:1936`) on the built dataset. `prescribed_training` already takes a `dataset_id` (`api/routes/case_visit.py:375-384`). The full readiness check runs: min rows, min positives, leak probe, consent. Not ready sends the visit back to TREAT with the reason, as today. | All as is | — |
| HAND_OFF / FOLLOW_UP | As today. Next month the same log shape is recognised by its fingerprint. The approved definition is re-applied by hash: counts may change, the meaning may not. | RECALL, carry | — |

The order of the questions is deliberate. The customer column is settled before anything is counted, so no
sentence ever counts invoice lines as customers. That is the bug in today's `_label_draft` output, which says
"432,638 of 541,909 already quiet".

## 3. What the person sees (proposed wording, numbers from the real file)

**Q1 — blocking, no recommended answer (it is meaning-bearing).**
> "Each row of online-retail.csv is one line of an order, not one shopper. Which column names the shopper?
> • 'CustomerID': 4,372 different values, 93 lines each on average, blank on 135,080 lines (24.9%).
> • 'InvoiceNo': 25,900 different values, about 21 lines each, never blank. This looks like the order number."

Options: each candidate column, plus "None of these: this file is not one row per order line". "None" ends the path
and goes back to today's data request.

**Q2 — blocking. The over-5% loss approval is folded into it.**
> "135,080 of 541,909 lines (24.9%) have no 'CustomerID' — 3,710 orders, likely guest checkouts. Nobody can be
> counted for them. Leave them out?"

Options: "Leave them out" or "Stop: I will get the shopper for these orders". Choosing "Leave them out" is the
person's acknowledgement for `PLAN_LOSES_TOO_MUCH`.

**Q3 — window. Asked only when the use case's definition does not fix it.** For retail-win-back the definition
says 90 days, so the window is filled in from it and stated in the sentence. The other template windows are offered
as alternatives, none of them ticked.

**Q4 — audience. Asked only when the use case declares one (§4).**
> "Retail Win-back is for shoppers who have stopped buying. Count only shoppers with no order in the 90 days before
> each snapshot? (5,132 of 16,957 shopper-months)"

**Outcome sentence (approved word for word by an Analyst):**
> "A shopper has come back when there is an order within 90 days after the snapshot. Measured on 6 month-ends,
> 31 Mar 2011 to 31 Aug 2011, counting only shoppers with no order in the 90 days before: 1,724 of 5,132 rows are
> yes (33.6%). 30 Sep 2011 is left out because its 90 days are not over. An order is a line with a quantity above
> zero."

**Build sentence (approved word for word):**
> "Build one row per shopper per month-end from online-retail.csv: 6 month-ends, 5,132 rows, using only what
> happened on or before each month-end. 135,080 lines with no CustomerID are left out. Lines with a quantity below
> zero (10,624, including the 3,836 cancelled orders whose number starts with 'C') are counted as returns, not
> orders. 'Description' is not used."

## 4. Data rules

- **Blank customer IDs.** These rows are left out, never filled in. They are counted per line and per order. They
  are approved through Q2, which feeds the existing guard (`guards.py:73`, `LOSS_STOP_PCT = 5.0`). If one order has
  some lines with an ID and some without, the path stops with a question; it never assumes. (In UCI no order is
  mixed: checked, 0 of 3,710.)
- **The customer list is derived, never fuzzy.** It holds the distinct non-blank values of the confirmed column,
  written the way the log writes them. A UCI ID like `12346.0` becomes `12346` through the existing exact
  `normalise_key` rules only. Each customer's **first order date** goes into `signup_date`. The existing spine
  filter then keeps a shopper out of every month-end before their first order (`snapshots.py:135-141`; the column
  name is `build.py:111`). That date is used only as an "existed by T" filter, never as a feature.
- **Cancellations, returns and negative quantities.** In UCI every `C…` line has a negative quantity (9,288 of
  them). Another 1,336 negative lines are not `C…` (stock adjustments). The rule works on quantity, not on the
  invoice prefix, because the prefix is specific to this dataset:
  - an **order** is `Quantity gt 0`;
  - a **return** is `Quantity lt 0`.

  The `C` prefix is only shown as evidence in the sentence. Lines with `UnitPrice <= 0` (2,517) add nothing to
  spend. The cleaning detector must **never propose `parse_number`** on a column chosen as the customer or the order
  number. Today it proposes "Turn 'InvoiceNo' into numbers", ticked as sure, which blanks every `C…` number
  (`advisor.py:813-968`).
- **Two levels: line → order → shopper.** The plan builds two sources from the log, deterministically, with the
  pandas groupby recorded in the plan hash:
  - `orders`: one row per order, with the shopper, the earliest timestamp (31 UCI orders carry two timestamps), the
    value (sum of quantity × price over its lines with a quantity above zero) and the item count;
  - `returns`: one row per order whose quantity is below zero.

  The lines stay as `order_lines` for product-level counts. If one order carries two shoppers, the path stops
  (UCI: 0). This reproduces `order_value_mean/max` without a new aggregate function, which `features.py` lacks.
  The line value can instead be a mapping `derive` (`Quantity * UnitPrice`; `ast.Mult` is in
  `engine/onboarding/transforms.py`'s whitelist). Whether `derive` reads two columns of one table still has to be
  checked.
- **Snapshots and censoring.** The defaults stay: monthly, at least 90 days of history, at most 12 snapshots
  (`engine/config.py:3595-3606`). On UCI that gives month-ends 31 Mar to 31 Aug 2011. `build_labels` drops 30 Sep
  as censored and says so (`LABEL_HORIZON_CENSORED`). `SnapshotDefinition` is `agent_editable: False` and stays so.
  The library used a single snapshot instead (2011-09-10, `library/online-retail/fetch.py:63`) (open question 2).
- **Lapsed audience.** This filter is new. Neither `LabelDefinition` (`config.py:3485-3511`) nor
  `SnapshotDefinition` can restrict the spine. Proposal: an optional `audience` on the use case, for example
  `{role: orders, no_event_days: 90}`. It is applied to the spine after `build_snapshot_frame` at both train and
  score time, and it is written into the plan hash and both sentences. It is point-in-time by construction, because
  it looks only at events on or before T.
- **Repeated shoppers across snapshots.** One shopper appears in several month-ends. The built dataset must split
  by `snapshot_date` (time-based) so no shopper-month in testing has a later twin in training.
- **Columns left out.** `Description` is free text and may carry personal data, so it is never mapped
  (`fetch.py:88-90` does the same). `Country` becomes a "latest" feature.

## 5. Where it plugs in

- **Unified visit (main path).** `_goal_rules`' no-outcome branch (`visit.py:1795`) calls `order_log.rules`. The
  `order_log_build` item rides PREVIEW → APPROVE → PRESCRIBE. The TREAT ↔ VERIFY loop (`visit.py:1446`)
  becomes the safety net: if the built dataset fails readiness (for example too few positives), the person sees the
  check's own message and can pick another window. Nothing is lowered.
- **`what_can_this_power` / no goal chosen** (`visit.py:1688-1708` → `readiness`). A use case with a recipe is
  shown as "can be built from this log" instead of `NO_OUTCOME`, with its question ("When has a lapsed customer come
  back?").
- **Legacy path (switch off).** `attach_outcome_offers` (`outcomes.py:718`) is allowed to offer for a single log
  when the new flag is on (`_entity_file` falls back to the derived list). Its `next_steps` point to the Data Doctor
  visit; the legacy screen gets no build of its own.
- **Guided setup `advise()` is unchanged.** Its stops stay as they are (open question 6).

## 6. Config changes

- **`configs/outcomes.yaml` templates.** Add `retail-win-back`: question "When has a lapsed shopper come back?",
  the same sentence as win-back-campaign (`:69-75`), `label_type: event_presence`,
  `event_kinds: [orders, bills, other_event]`, `days: [30, 60, 90]`, `default_days: 90`. Add `order_log: true` to
  each template that should offer the path. That keeps it explicit rather than "every template" (open question 1).
- **`configs/use_cases/retail_win_back.yaml`** has no `label` and no `suggested_features` today. Add:
  - `label: {name: reactivated_90d, type: event_presence, role: orders, horizon_days: 90, where: {column: quantity, op: gt, value: 0}}`
  - `audience: {role: orders, no_event_days: 90}`
  - `suggested_features` mapping the template's columns (`:52-67`) onto the three sources:
    - `orders`: `days_since_last` (recency), `days_since_first` (tenure), `count`, `sum`/`mean`/`max` of value;
    - `order_lines`: `nunique` of the product code, `sum` of quantity;
    - `returns`: `count`, `sum` of quantity;
    - `active_months`: `nunique` of a derived year-month;
    - `days_between_orders_mean`: a `derive` feature over the others.
  - `split: {type: time_based, time_column: snapshot_date}` for built datasets.
  - The `template` and the library path stay, so the already-combined `sample.csv` keeps working.
- **`configs/engine.yaml`** (Plan O block, `:381-388`): add `data_doctor.order_log: false` (bool). Its comment
  says it needs `unified` and stays off until this plan's checklist passes.

## 7. M249 and the unified switch

`data_doctor.unified` is off by default (`configs/engine.yaml:382`). It stays off until the parity checklist in
`docs/plans/MARKETING_AI_PLAN_O_DATA_DOCTOR.md:145-151` holds: the goldens are byte-identical, the M160 benchmark
gets 30 of 30 to Approve with at most 0.33 questions per file, the readiness invariant holds, and so on. "Thresholds
are never lowered to pass" (`:153`). To stay compatible:

1. **All new behaviour sits behind the new flag `data_doctor.order_log`, default off.** With it off, every
   existing output is byte-identical, whatever `unified` is set to. Each of the four lifted checks keeps its current
   text and only gains a branch for an explicitly derived list (passed as a parameter, never inferred).
2. **The detector must not fire on any parity file.** A test runs it over Plan G's 22, the 27 Hinglish files and
   the 30 M160 files, and expects none of them to be read as an order log. That way turning `order_log` on cannot
   move the parity numbers or add questions.
3. **No new `VisitState`.** The case store schema only gains a new item kind and a field on the built table. The
   case store's tests and the M247 journeys are unaffected.
4. **No change to `STOP_CODES`, `min_rows` (1,000), `min_positive` (200) or `LOSS_STOP_PCT`.**

## 8. Guardrails (all in code)

- Minimum rows and positives are checked by readiness on the **built** dataset, never on the lines.
- The leak probe runs on every build. `build_labels` refuses SQL that lacks its own window clause (`labels.py:375`).
- Consent: retail-win-back has no consent column (`retail_win_back.yaml:36-37`). Use cases that need one still
  stop on `CONSENT_COLUMN_MISSING`.
- Exact keys only: `normalise_key`'s five rules (`policy.yaml:164-165`), no near matches.
- The outcome and the customer column are only ever chosen by a person. Both plan items are `never_ticked`, and
  the outcome sentence needs an Analyst.
- The plan hash covers the roll-up, the audience, the left-out counts and the snapshot plan. Any change after
  approval invalidates the approval.

## 9. Tests

- **Committed fixture `tests/fixtures/online_retail/`.** It holds a seeded sample of the real file, sampled **by
  customer** (each sampled shopper keeps all their lines), plus guest-checkout orders in proportion. It is gzipped
  CSV, with a CC BY 4.0 attribution file (`library/online-retail/LICENSE.txt`) and the generator script. Probe:
  800 customers give 98k lines and 1.4 MB gzipped, but only 967 lapsed rows and 310 positives, under the 1,000-row
  minimum. About **1,000 customers (~1.7 MB gzipped)** are needed to clear both minimums honestly. A second slice of
  about 3,000 lines tests that the honest verdict there is "too small" (`ROWS_TOO_FEW`).
- **Unit tests:**
  - the detector says yes on the fixture and no on every parity file;
  - camelCase name matching;
  - the derived list, including `12346.0` → `12346` and the first-order date;
  - the roll-up (two shoppers on one order stops; two timestamps take the earliest);
  - blank-row counts;
  - quantity-based returns;
  - no `parse_number` on key columns;
  - the audience filter is point-in-time (a future order never changes a row);
  - censoring;
  - golden texts for Q1–Q4 and both sentences.
- **Library cross-check.** At T = 2011-08-31, the built `recency_days`, `orders_total`, `spend_total` and
  `order_value_mean` equal `fetch.py`'s formulas recomputed at that T, for every shopper in the fixture.
- **End to end:** `tests/integration/test_order_log_visit.py`. With `unified` and `order_log` on: upload the
  fixture, choose retail-win-back, answer Q1 and Q2, approve both sentences as an Analyst, PRESCRIBE builds the
  dataset, and VERIFY returns **`ready`** (or `ready_with_warnings`). A `/runs` dry run on the dataset passes. The
  same file with `order_log` off gives exactly today's `NO_OUTCOME` output.
- **Parity:** the goldens and the M160 benchmark run with `order_log` on and give no diff.

## 10. Milestones (in order; S ≈ ½–1 day, M ≈ 2–3 days, L ≈ 4–5 days)

1. **B1 (S).** Flag; `outcomes.yaml` template; retail-win-back `label`, `audience`, `suggested_features`, `split`.
   Config tests.
2. **B2 (M).** `order_log_reading` detector with camelCase matching, Q1, and the parity-file "never fires" test.
3. **B3 (M).** Derived customer list. Lift `outcome_definition.py:957` and `outcomes.py:604` behind an explicit
   parameter. Q2 through the loss guard. No `parse_number` on key columns.
4. **B4 (M).** `plan_build` with a derived entity and the order/return roll-up. Lift `build_driver.py:355`.
   Materialise the list as a real entity source, so `validate.py:256` passes unchanged, plus a new check that the
   list equals the log's distinct IDs.
5. **B5 (M).** The audience filter on the spine (train and score), with point-in-time tests.
6. **B6 (L).** The `order_log_build` item; PREVIEW sampled by customer; the in-process `BuilderPort`; PRESCRIBE
   runs `drive_builder`; `current_upload` follows the built table; VERIFY on the dataset.
7. **B7 (M).** The fixture and its generator, the end-to-end test to `ready`, the library cross-check, and the
   parity runs.
8. **B8 (S–M).** The no-goal "can be built from this log" wording, the legacy offer's next step, and showing the
   new questions in `ui/modules/datadoctor/` with jsdom tests.

Total: roughly 3–4 weeks of focused work.

**Phase C (later).** Give the Doctor AI these tools (detect, propose a customer column, propose a window) under
code-enforced rules. Each proposal must cite a preview made from this file in this case. Proposals are never
ticked. The person still answers Q1–Q4 and approves both sentences. A new `policy.yaml` rule set, refused by
default.

## 11. Open questions for the owner

1. **Which use cases offer the path?** win-back-campaign and rca (churn as "no order for N days") fit. Does
   targeted-advertisement ("converted within 30 days") make sense on a log where everyone has bought? Proposal:
   only templates marked `order_log: true`, starting with retail-win-back and win-back-campaign.
2. **Snapshots:** monthly (6 month-ends, 5,132 rows, 1,724 positives on UCI) or the library's single 2011-09-10
   snapshot? Monthly gives more rows and needs the time-based split. Single matches the shipped `sample.csv`.
3. Is a ~1.7 MB gzipped real-data fixture acceptable in the repo, or should it be fetched in CI?
4. Should the 1,336 negative non-`C` lines (stock adjustments) count as returns, as proposed, or be ignored, as the
   library does?
5. A separate `order_log` flag (proposed), or ride on `unified` alone?
6. Should Guided setup's "No column holds a different value…" stop (`advisor.py:432-438`) gain one sentence
   pointing to the Data Doctor when the file reads as an order log? It is outside the parity scope only if it sits
   behind the flag.
7. On a single-user install, who is the "Analyst" who approves the outcome sentence?
8. Scoring next month: is the person expected to upload the whole log again, or only new lines stacked on the old
   ones?
