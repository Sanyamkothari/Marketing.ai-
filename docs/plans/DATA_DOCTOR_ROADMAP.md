# Data Doctor roadmap: no switches, every limitation and how to solve it

Status: proposal for the owner (2026-10-27). Principle from the owner: **what we build is live and visible; no on/off
switches now or later.** Safety is enforced by rules in code and by tests, never by hiding features behind flags.

## 0. Principle: replace switches with gates that are tests

| Today | Instead |
|---|---|
| `data_doctor.unified: false` hides the whole new Data Doctor (only `make run` / docker turn it on) | **Delete the switch and the old path.** One Data Doctor, always on. The "M249 parity checklist" becomes the **benchmark** below: it must pass in CI before merge, not a flag in production. |
| `agentic.enabled: false` hides the AI's header reading and outcome drafts in Guided setup | **Delete the switch.** Guided setup becomes the Data Doctor (one product, §2.1); with no AI connected the rules still run, with the AI they help. |
| `configs/doctor/suggestions.yaml` `skip: [telco-churn]` | **Delete the skip list.** Offer every goal the data can power, **ranked** (best fit first, with the reason); duplicates of a Home goal are shown as "also fits". |
| Task types `uplift`, `forecasting`, `clustering` disabled | Uplift: **enable in the Data Doctor** with its hard rule (random control group). Forecasting / clustering: **build as goals** (§3), not switches. |
| New behaviour that could surprise a stored case | Lazy migration plus "asked again" (already the pattern); never a flag. |

## 1. What the Data Doctor is responsible for

Finds, measures, explains and **proposes**; the person **confirms** meaning (customer ID, outcome, dates, consent, every
change). Does by itself: reading formats, spotting problems, measuring, all safety checks. Not its job: model training,
algorithm choice, encoding/scaling (AutoML), the campaign decision.

## 2. Limitations today and how to solve each

### 2.1 Product structure
| Limitation | Solution | Size |
|---|---|---|
| Two products do the same job: Guided setup (old rules-first advisor) and the Data Doctor | One Data Doctor; Guided setup's page becomes its screen; the advisor stays only as rule tools | L |
| Two prompt versions (v1 and v2) | Delete v1; tests read v2 | S |
| Old replay paths (prescription replay outside `materialize`) | Route through `materialize`; delete the old path | M |

### 2.2 Data shapes not handled yet
| Limitation | Solution | Size |
|---|---|---|
| **File with no ID** (UCI Bank Marketing: one row per call) | Propose a row ID (`row_number`) as a decision the person confirms; warn that predictions cannot be joined back to a CRM without one | S |
| **Uplift / campaign tests** (Hillstrom: treated vs control) | Offer uplift goals; hard rule: treatment must be randomly assigned (check balance across groups); otherwise out of scope with the existing sentence | M |
| **Amount as the outcome** (predict spend) | Offer regression goals (engine supports regression); outcome definition "spend in the N days after the snapshot" via `propose_features`-style point-in-time SQL | M |
| **Next best product** (which product) | Multi-class goal: outcome = category of the next purchase in a window; needs class-count minimums | L |
| **Several files** (customer list + orders + returns) | Joins exist; make the AI-first path use them end to end: query and feature tools read built tables (T1..), coverage shown per join | M |
| **Wide monthly sheets** (spend_jan..spend_dec) | Unpivot exists in Guided setup; expose as an AI tool with the year question | S |
| **Forecasting / clustering** | Later goals; clustering = segments from features (no outcome), forecasting = time series per entity | L each |
| **Live Google Sheets, chat logs, PDFs** | Sheets: a connector that snapshots to a file (connections exist); PDF/scans/chat logs stay out of scope (fixed sentence) | M |

### 2.3 Scale and speed
| Limitation | Solution | Size |
|---|---|---|
| Full-file check before approval runs inline; untested at 500k+ rows | Run it as a background job with progress; cache by plan digest (exists); measure on the 541,909-row Online Retail file | M |
| Every tick re-runs rules and review (slower since step 3) | Re-run only what the change touches (by subject); cache rule results by table digest | M |
| "Large table" is out of scope | Read in chunks / DuckDB on the file instead of pandas for >2M rows | L |

### 2.4 AI quality and cost
| Limitation | Solution | Size |
|---|---|---|
| ~200k input tokens and 30 calls per case (clean run 1) | Prompt caching (stable prefix done), compact state, guides on demand (done), cheap look results; target < 100k tokens | M |
| The AI repeats mistakes across cases | **Lessons memory**: after a case, the AI proposes lessons; the owner approves; approved lessons join the guides | M |
| Query tool cannot read tables the AI built (clean run 2) | Query and feature tools read current tables (T1..) like the look tools | S |
| Model choice fixed per deployment | Route cheap steps (looks, summaries) to a small model, decisions to a strong one | M |

### 2.5 What the person sees
| Limitation | Solution | Size |
|---|---|---|
| Plan, assumptions and approvals spread over several panels | **One screen**: the plan in plain words, each assumption with yes/change, problems with yes/no, one Approve | M |
| Typed answers exist in the API, partly in the UI | Every question that needs a value shows the right input | S |
| Swaps (AI replaces your accepted step) not drawn in the UI | Show old vs new side by side | S |
| Hard stops look like normal stops in places | Distinct style, the reason, and what would fix it | S |

### 2.6 After the first run
| Limitation | Solution | Size |
|---|---|---|
| Next month's file (re-run the approved recipe) | Replay through `materialize` with drift checks; ask only what changed | M |
| Currency conversion, foreign consent rules | Currency: rates file + approval; consent: per-country rule table the owner maintains | M each |

## 3. Testing that replaces switches

1. **Benchmark (CI gate):** the six library datasets (Online Retail 541,909 rows; Health Insurance Cross-Sell 381,109;
   UCI Bank Marketing 41,188; Hillstrom 64,000; UCI Credit Default 30,000; Telco Churn 7,043) plus synthetic shapes
   (no ID, wide months, two files, Indian formats). Each runs the full visit with a scripted person and a recorded or
   live model; records reached-ready, reason, time, memory, AI calls, tokens. A change merges only if no dataset gets
   worse.
2. **Class tests already in place:** trial = run, one decisions reader, no dead loop, contract tests, guide tests.
3. **Golden replays** of real cases kept as fixtures (the Online Retail run).

## 4. Order (each ends with the benchmark)

1. Benchmark harness on the six datasets (measure first).
2. Remove the switches: `unified`, `agentic.enabled`, skip list; delete v1 prompts and the old paths (§0, §2.1).
3. Quick shape wins: no-ID files, query tools on built tables, wide months as an AI tool.
4. One screen for plan + assumptions + approval.
5. Scale: background full-file check, measured on 541,909 rows.
6. Uplift and amount goals.
7. Lessons memory and cost reduction (< 100k tokens per case).
8. Next month's run; then next best product, clustering, forecasting.
