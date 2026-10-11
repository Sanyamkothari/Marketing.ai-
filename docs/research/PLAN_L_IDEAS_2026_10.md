# Ideas to unlock more value (Plan L's curious-thinker review, 10 Oct 2026)

**What this is.** A read-only review, done while Plan L (Data Doctor) was being built, of where the product could
create more value for marketing teams without a data team. It cites the files it read. Nothing here is decided:
each idea is a candidate, reviewed by the Plan L integrator after M161 (see the review at the end).

**Three code facts behind several ideas**

1. Data Doctor's readiness looks only for a column that *names* the outcome (`_outcome_named` in
   `engine/doctor/diagnose.py`), though every trainable use case on Home already says how to *build* the outcome
   from events (the `label:` block in `rca.yaml`, `win_back_campaign.yaml`, ...). A telecom file with a recharge
   log but no churn column reads "Missing something".
2. Prepaid is not modelled natively: `configs/roles.yaml` has no recharge table type; the lapse outcome counts
   from the snapshot (plus grace), not from when the customer's pack actually expires; churn is "no activity of
   any kind", so incoming-only secondary SIMs count as alive.
3. Next month's replay stops on a renamed column (`RECIPE_COLUMN_MISSING`, `api/routes/agent_recipes.py`); there
   is no outcome-due reminder (`AlertKind`, `engine/scheduling/alerts.py`); Data Doctor's page forgets on reload.

## A. The first hour

| # | Idea | What to build | Effort |
|---|---|---|---|
| 1 | **"Can be built from your files"**: a status between Ready and Missing | When no file names the outcome but the use case has a `label:` spec and the files include a customer list and the label's event table, say "We can work out *churn_next_60d* as: no activity in the 60 days after the snapshot, from activity.csv", with the definition agreed explicitly before anything is built | S |
| 2 | **Cross-file link-up in Data Doctor** | Reuse pre-flight's `join_coverage` / `check_key_format_mismatch` when several files are present: "92% of bills.csv rows match a customer; tickets.csv IDs are written differently, one fix (remove `CUST-`) makes 98% match" | S |
| 3 | **Data request kit: "send what you have"** | `configs/pilot/data_request.yaml` still asks for UTF-8 CSV, ISO dates and a laptop pre-flight; Data Doctor now reads Excel, titles, totals and `-`. Keep only what still matters (same column names every month, no free-text contact details) | S |
| 4 | **A Data Doctor that remembers** (a per-client "data shelf") | `GET /doctor/shelf`: the client's uploads with their stored diagnosis and hand-offs; the page restores from it; natural home for M159 | S / M |

## B. A useful insight before any model

| # | Idea | What to build | Effort |
|---|---|---|---|
| 5 | **"What your file already says"** | Per file, deterministically (the `rate_by` tool, no AI): the base rate and the outcome rate by the 3-5 most informative columns, or a recency picture when there is no outcome; groups under 10 rows never named; leaky columns left out; labelled "Describes your file. Not a cause and not a prediction." | S-M |
| 6 | **"Too small to predict, big enough to test"** | When a model cannot be trained, offer a randomised test on a person-agreed rule list, sized by the test planner; its outcomes become next month's training data | S on M158 |
| 7 | **A fast draft model, clearly not for use** | A 1-2 minute `fast` run labelled draft, never approvable or scorable | M (only if drop-off is measured high) |

## C. Proving value and trust

| # | Idea | What to build | Effort |
|---|---|---|---|
| 8 | **"Things we checked" + a too-good-to-be-true alarm** | On the results page, from existing artefacts (leak probe, future-data check, label-rate stability, split, baseline, fairness); `RESULT_SUSPICIOUSLY_GOOD` in `engine/model_gates.py` above a per-use-case threshold, naming the strongest feature | S |
| 9 | **"Your rule vs the model"** | The marketer names their current rule; compare its top-decile capture with the model's on the same test split, honestly either way | M |
| 10 | **Outcome-definition preview** | Before agreeing a built outcome: rate per snapshot, censored snapshots, excluded customers, three masked example timelines | S after M157 |

## D. India telecom / B2C

| # | Idea | What to build | Effort |
|---|---|---|---|
| 11 | **Validity-aware prepaid lapse + a `recharges` table type** | Role in `roles.yaml`; features `days_to_validity_end`, `recharge_count_90d`, `mean_recharge_gap`, `denomination_downgrade`; label option `window_start: validity_end` (point-in-time safe); a config-only "Prepaid recharge lapse" use case with MNP port-outs excluded | M |
| 12 | **When to contact**, tested | `contact_by` from `days_to_validity_end`; timings offered as M100 treatment levels so the best one is measured | S-M after 11 |
| 13 | **DLT template library as the SMS boundary** | Import the DLT export; the AI only chooses a registered template and fills its variables; a rule checks the rendered message | M (overlaps Plan K K3) |
| 14 | **WhatsApp-aware channel costs** | `channel_costs` per channel and message category; dated India defaults | S |
| 15 | **Offer catalogue editable by a marketer** | Settings → Offers over the same pydantic model, versioned, Approver sign-off, audited | M |

## E. Retention

| # | Idea | What to build | Effort |
|---|---|---|---|
| 16 | **Next month's file changed: propose the fix** | On `RECIPE_COLUMN_MISSING`, match the missing column's training fingerprint against the new file's unexplained columns and propose a pending `rename_column`; list unseen spellings with a suggested `map_values` | M |
| 17 | **Outcome-day reminders** | `AlertKind.OUTCOMES_DUE` when a campaign's analysis date has passed without a final report; a Results card; a daily digest, identifiers only | S |
| 18 | **The monthly one-page brief** | From existing artefacts (M105 cards, band shifts, drift with noted events, challenger waiting, upcoming outcome days), traced with `check_figures`, as a PDF | S-M |

## F. The AI, safely more useful

| # | Idea | What to build | Effort |
|---|---|---|---|
| 19 | **Ask your data, earlier and on scores** | Mount the read-only explore session per file in Data Doctor (S); later, aggregated questions on a scoring run's bands and reasons (M, revisits DEC-1257) | S, then M |

## The thinker's top 7 (value per effort)

1. #1 "Can be built from your files" (S). 2. #2 Cross-file link-up (S). 3. #5 First look with no AI (S-M).
4. #8 Checks panel and alarm (S). 5. #17 Outcome-day reminders (S). 6. #16 Propose the rename on replay (M).
7. #11 Validity-aware prepaid lapse (M). Next: #9, #3, #6, #14.

## Ideas that conflict with the product's principles (do not build as stated)

- AI fixes that start ticked when "very confident" (Plan L §5.2.1; §12 says never).
- A campaign's effect without a control, shown as the effect (Plan J §3.1.3; Plan K §1.3).
- Filling empty cells with the mean or median in the recipe (stateless rule, Plan L §5.2.3).
- Fuzzy matching people across files (a CDP's job; DATA_DOCTOR §5).
- AI-written queries for Ask your data (DEC-1256).
- Messages sent to customers from the product (K1, J1, DEC-1100); #17 reminds the marketer only.
- Partial tension: #7 (only if a draft can never be approved, scored or exported) and #5/#6 (only while labelled
  "describes your file" / "not a model").

## The review after M161 (11 Oct 2026)

Every idea was weighed for value to a person without a data team, effort, and fit with the product's rules.

| # | Idea | Verdict | Where |
|---|---|---|---|
| 1 | "Can be built from your files" | Built as M157: the outcome from an event log, agreed as a sentence | DEC-1570 … 1579 |
| 2 | Cross-file link-up | Built as M155: each log against the customer list, the fix in plain words | DEC-1550 … 1559 |
| 3 | Data request kit: "send what you have" | **Built** | DEC-1598 |
| 4 | A Data Doctor that remembers | Partly, as M159 (the same treatment for the same shape); the page itself still forgets on reload | DEC-1590 … 1595 |
| 5 | "What your file already says" | **Built**, on the diagnosis, no AI | DEC-1596 |
| 8 | "Things we checked" + the too-good alarm | **Built**: an advisory check and a panel from the run's own reports | DEC-1599 |
| 16 | Next month's file changed: propose the fix | **Built**: the rename proposed, a renamed copy on the person's yes | DEC-1597 |
| 6 | Too small to predict, big enough to test | Partly, as M158's "collect with the next campaign" option | DEC-1582 |
| 10 | Outcome-definition preview | Partly, as M157's counted choices and censoring sentence | DEC-1572, 1574 |
| 17 | Outcome-day reminders | Next: touches the alerts other plans own (`engine/scheduling/alerts.py`); worth a small change there | — |
| 11, 12 | Validity-aware prepaid lapse; when to contact | Next, for an India telecom pilot: config-heavy, needs a `recharges` role | — |
| 9, 18, 19 | Your rule vs the model; monthly brief; Ask your data earlier | Later: medium effort, outside Data Doctor | — |
| 7, 13, 14, 15 | Draft model; DLT templates; WhatsApp costs; offer editor | Not now: overlap Plan K or need product decisions | — |

## Sources (web)

- Pecan, Obviously AI and Akkio reviews on G2 (black box; users needed vendor help to clean and merge data).
- Klaviyo's guide to predictive analytics (explicit data minimums; churn risk explained by recency and frequency).
- Microsoft Fabric community threads on renamed columns breaking monthly refreshes.
- WhatsApp prices, DLT and TRAI facts as cited in `MARKET_RESEARCH_2026_10.md` §5 (not re-checked).
