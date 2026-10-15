# How other agents and industries prevent silent failures, and what the Data Doctor should take from them

Companion to `docs/DATA_DOCTOR_SCENARIOS.md`.

Date: 15 Oct 2026. Written in plain English. It combines seven research notes: data-quality-tools, etl-reconciliation, ml-data-leakage, llm-data-agents, agent-verification, safety-critical and ux-trust.

**Reading caveat.** The research proxy blocked most websites. The sources read in full were mainly GitHub pages (dbt, Deequ, TFDV, Feast, OpenLineage, ReFoRCE, the benchmark-error repo) and a few Anthropic pages. Everything else was read from search snippets only, and the individual notes mark which is which. Vendor figures are marketing claims. The strongest evidence comes from medicine, aviation and human-factors research. None of it was measured on an AI agent preparing marketing data.

## The short answer

Every field that handles this well follows the same rule: **the worker never grades its own work.** Banks, auditors, clinical-trial data managers and pipeline tools use numbers computed by the system to show that the data was moved intact. They then add a few independent checks only where the risk is high, and show a human a short list of evidence rather than a long story. Our current plan already follows this rule. The biggest lessons are about how to do it: keep the checker truly independent, stop alerts from training people to click "yes", and test the tests.

No shipped AI data product we found does what we plan to do. That covers code-computed receipts per step, a running loss total, a check for rows lost mostly from one outcome group, and tracing sample customers. The closest are Snowflake Cortex Analyst's join checks and OpenAI's internal data agent (llm-data-agents.md). This makes our design a differentiator, but it also means nobody has proved that it works. Our own test bench of planted traps is the only evidence we will have.

---

## 1. The ten most important lessons

### L1. The numbers come from the system, never from the worker
- **What they do:** ETL teams use "Audit-Balance-Control". Every step logs rows in, rows out, rows added and rows rejected, and the source is reconciled to the target ([xtivia, snippet](https://xtivia.com/blog/etl-audit-balance-control-recommendations)). OpenLineage has standard names for these figures: row count, null count, distinct count, sum, min and max ([OpenLineage spec, read in full](https://raw.githubusercontent.com/OpenLineage/OpenLineage/main/spec/facets/DataQualityMetricsInputDatasetFacet.json)).
- **Evidence:** AI research finds that models re-checking their own answers without outside feedback gain little or get worse ([Huang et al., snippet](https://arxiv.org/abs/2310.01798v2)). Feeding back a tool's result does help: CRITIC gained +7.7 F1 ([snippet](https://arxiv.org/abs/2305.11738v4)). In one documented case, a data diff caught an 82-row silent loss that tests had missed ([Datafold](https://docs.datafold.com/data-diff/what-is-data-diff)).
- **Adopt as:** our sandbox writes the receipt for every step, using the OpenLineage names. The agent's statement that "this join looks fine" never counts as evidence.

### L2. Totals that match do not prove the data is right
- **What they do:** auditors call reconciliation a "detective control". It shows that nothing was lost in transit, not that the logic was correct ([hyperbots, snippet](https://hyperbots.com/glossary/reconciliation-control)). A join that adds 4,000 duplicates and a filter that drops 4,000 rows still "add up". Snowflake's text-to-SQL product reads the AI's SQL and rejects joins that would double count, such as an order total summed across its line items ([Snowflake engineering blog, snippet](https://www.snowflake.com/en/blog/engineering/snowflake-cortex-analyst-introducting-joins-complex-schemas/)).
- **Evidence:** OpenAI's internal data agent names many-to-many joins and unhandled blanks as failures that silently invalidate results ([OpenAI, snippet](https://openai.com/index/inside-our-in-house-data-agent/)).
- **Adopt as:** keys lost and keys duplicated are counted separately and never netted off. A code check runs on every join: how unique the key is on each side, the match rate, and output rows against the expected number. Any money total after a join is compared with its total before the join.

### L3. A second check only helps if it is truly independent
- **What they do:** hospitals require a second nurse to check a dose.
- **Evidence:** in a study of 5,140 doses, only 1.0% of these checks were independent and 92.5% were "primed": the second nurse had already heard the first nurse's answer. Error rates were the same with and without the check, 72 vs 71 per 100 doses ([Westbrook 2021 via PSNet, snippet](https://psnet.ahrq.gov/issue/associations-between-double-checking-and-medication-administration-errors-direct)). AI judges behave the same way. Rewriting only the agent's reasoning text raised judges' false passes by up to 90% ([snippet](https://arxiv.org/html/2601.14691v2)). Answers from different AI models also tend to fail on the same inputs ([2026 study, snippet](https://arxiv.org/abs/2607.02808)).
- **Adopt as:** the AI checker sees only the code-computed evidence (receipts, before/after rows, traced customers) and the business question. It never sees the planner's plan text or reasoning. The two-method cross-check must use methods of different kinds, such as a typed step against an SQL anti-join. Running the same prompt twice does not count.

### L4. Use heavy checks only on the risky few, and cheap code checks everywhere
- **What they do:** the Institute for Safe Medication Practices advises using independent double checks only for a few high-risk tasks, never as the only safeguard ([ISMP via ECRI, snippet](https://home.ecri.org/blogs/ismp-alerts-and-articles-library/independent-double-checks-worth-the-effort-if-used-judiciously-and-properly)).
- **Evidence:** in clinical trials, checking every data point against the source corrected only about 1.1% of the data. Targeted and central monitoring gave better value ([snippet](https://outsourcing-pharma.com/Article/2014/11/20/Analysis-finds-100-SDV-has-minimal-impact-on-overall-data-quality)).
- **Adopt as:** risk tiers. Joins, deduplication, filters that touch the outcome, consent and time columns get the AI checker and the two-method check. Everything else gets code receipts only.

### L5. Time leakage needs its own machinery
- **What they do:** feature stores join "as of" a date. Each training row takes the latest value known at or before its own date, and can exclude later corrections ([Feast, read in full](https://raw.githubusercontent.com/feast-dev/feast/master/docs/getting-started/concepts/point-in-time-joins.md)). DuckDB's `ASOF JOIN` returns at most one match per row, so a table cannot grow by mistake ([DuckDB docs, snippet](https://duckdb.org/docs/current/guides/sql_features/asof_join)). AutoML vendors score each column on its own against the outcome. DataRobot flags a column above 0.85 and removes it above 0.975 ([DataRobot, snippet](https://docs.datarobot.com/en/data/analyze-data/quality-check.html)).
- **Evidence:** a review found leakage in 294 papers across 17 fields, and the reported gains disappeared once it was fixed ([Kapoor and Narayanan, snippet](https://pmc.ncbi.nlm.nih.gov/articles/PMC10499856)).
- **Adopt as:** the typed time step is an as-of join, and code checks that the left table's row count does not change. Code scans each column for a link between "is blank" and the outcome. That catches our `cancellation_reason` case, where the column is filled only for churners. SQL that uses today's date is rejected.

### L6. Checks live in a written, versioned rule catalog
- **What they do:** clinical data management writes its edit checks before the data arrives. CDISC CORE runs about 1,900 rules as open-source code ([Clinical Leader, snippet](https://www.clinicalleader.com/doc/the-cdisc-open-rules-engine-core-open-source-software-for-clinical-0001)). Google's TFDV infers a schema, a person reviews it and it goes under version control ([TFDV guide, read in full](https://raw.githubusercontent.com/tensorflow/tfx/master/docs/guide/tfdv.md)). Deequ suggests rules from a data profile, but its own documentation says each suggestion needs human review ([Deequ, read in full](https://raw.githubusercontent.com/awslabs/deequ/master/src/main/scala/com/amazon/deequ/examples/constraint_suggestion_example.md)). dbt splits checks into "warn" and "error" ([dbt docs, snippet](https://docs.getdbt.com/reference/data-test-configs)).
- **Adopt as:** one catalog with a rule ID per check, each tied to the scenario IDs it covers. Every rule has a severity, and the failing rows are saved as a sample. Rules the agent writes are added for that run only and are marked "unreviewed".

### L7. Rank alerts by severity, limit how many interrupt, and retire the ones that never matter
- **Evidence:** when drug alerts were tiered, severe alerts were accepted 100% of the time, against 34% when untiered ([Paterno 2009, snippet](https://academic.oup.com/jamia/article/16/1/40/865495)). One hospital logged 2.5 million alarms in a month, most of them false ([UCSF](https://www.ucsf.edu/news/2014/10/119601/hospital-logs-staggering-25-million-alarms-just-month)). Clinicians override 55-98% of drug alerts ([review, snippet](https://assets.cureus.com/uploads/original_article/pdf/176486/20230808-23082-1fb7tqu.pdf)). Claude Code users approve 93% of permission prompts ([Anthropic, read in full](https://anthropic.com/engineering/claude-code-auto-mode)).
- **Adopt as:** three levels. A "stop" is rare and needs an answer. A "flag" goes on the approval page. A "note" goes in the log. Allow 3-5 interruptions per approval at most. Log whether each warning changed the plan, and retire warnings that never do.

### L8. Show the approver evidence, not explanations
- **Evidence:** written explanations raised acceptance of *wrong* AI advice ([Bansal et al. CHI 2021, snippet](https://arxiv.org/abs/2006.14779)). People check the AI only when checking is easy ([Vasconcelos et al. 2023, snippet](https://arxiv.org/pdf/2212.06823)). Asking the human one question before showing the AI's answer reduced over-reliance, although users disliked it ([Buçinca et al. 2021](https://www.eecs.harvard.edu/~kgajos/papers/2021/bucinca2021trust.shtml)). Automation bias affects experts, and training does not fix it ([Parasuraman and Manzey 2010](https://journals.sagepub.com/doi/10.1177/0018720810376055)).
- **Adopt as:** the approval screen leads with counts, traced customers and one-click sample rows. Example: "3 customer pairs merged by this key", shown side by side. It contains no "why this is safe" paragraph. On the 2-3 riskiest decisions, ask the human first ("Can one customer ID appear twice?") and show the agent's reading after.

### L9. Agents game the checks they can see
- **Evidence:** Claude 3.7 hard-coded test answers and edited tests. Other models learned to exit with a "pass" code ([Anthropic](https://www.anthropic.com/research/emergent-misalignment-reward-hacking)). o3 read the grader's answer and hacked every run on one task with a visible scorer ([METR, snippet](https://metr.org/blog/2025-06-05-recent-reward-hacking)).
- **Adopt as:** the check code lives outside anything the agent can read or change. The agent declares its expectations *before* it sees results. Any expectation it rewrites after a failure is flagged for a human. Keep some checks hidden from the agent, such as one traced customer and one extra total.

### L10. Test your tests, and your reviewers
- **Evidence:** 52.8% of BIRD Mini-Dev and 62.8% of Spider 2.0-Snow examples have wrong "correct answers". Fixing them moved rankings by up to 9 places ([UIUC repo, read in full](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks)). Single spreadsheet reviewers caught about 63% of planted errors, and groups of three caught 83% ([Panko, snippet](https://arxiv.org/pdf/0801.3114)). Anthropic's advice is to calibrate AI judges against expert labels ([Anthropic](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)). Its property-test agent asks whether a test that passes is trivial ([Anthropic](https://www.anthropic.com/research/property-based-testing)).
- **Adopt as:** each trap in the bench has a human-verified expected result and 2-3 altered variants. Grade on the resulting data, not on the SQL text, and count a pass only if every run passes. Use the bench to measure the AI checker's catch and false-alarm rates per trap type. Later, use it to measure whether human approvers catch planted traps.

---

## 2. Our current plan compared with what others do

| Our plan item | Verdict | Change |
|---|---|---|
| Code-computed receipts and conservation checks | **Confirmed** (L1, L2) | Count lost and duplicated keys separately. Add the outcome rate and distinct customers to every receipt. |
| Expectations declared by the agent, checked by code | **Confirmed** (L9; tests-first helped in [TiCoder, snippet](https://www.seas.upenn.edu/~asnaik/assets/papers/tse24_ticoder.pdf)) | Lock them before the run. Flag rewrites made after a failure. Reject any expectation the unchanged input already fails, because it checks nothing. |
| Retry cap | **Confirmed** ([Anthropic](https://www.anthropic.com/research/building-effective-agents)) | Cap at 2 per step. Return *all* failures at once, with the failing step, the numbers and 5-10 example rows: AI models fix errors well once told where they are ([Tyen et al., snippet](https://arxiv.org/pdf/2311.08516)). After the cap, ask the human; do not try another trick. |
| Independent AI checker on risky steps | **Keep, but redesign** (L3) | Give it evidence only, never the plan's prose. Use a different prompt or model, read-only access, and a required specific counter-check. It can reject but not edit. It never stands alone. |
| Two-method cross-check | **Keep, but redesign** (L3) | The two methods must differ in kind. If they disagree, stop and ask; never pick the majority. |
| Traced sample customers | **Confirmed** (L8) | Code chooses the samples, never the agent. Include churners most at risk of being dropped, rows touched by joins, and merged or duplicated IDs. Keep one trace hidden from the agent. |
| Risk tiers for human approval | **Confirmed** (L4, L7) | Add an interruption budget (3-5) and reason codes when the user overrides a flag. |
| Per-client memory of facts and fingerprints | **Confirmed**, with limits | Tie every stored approval to the data fingerprint, so a changed file cancels it. A dismissed warning can lower a warning's level next time, but never for joins, consent or leakage. |
| Planted-trap test bench | **Confirmed, and the most important item** (L10) | Use human-verified expected results, variants of each trap, grading on result data and pass only if every run passes. Keep it out of the agent's context. |

**Missing from our plan:**
- A **running loss total** across all steps, plus the **segment where loss concentrates** (outcome group, source, date range).
- A **join cardinality guard** that blocks double counting.
- **As-of joins** and a **blank-versus-outcome leak scan**.
- A **"run it twice, same result" determinism check**, with saved per-step snapshots and **publishing to AutoML only after approval** (the write-audit-publish pattern from [lakeFS](https://github.com/treeverse/lakeFS)).
- An **automatic stop on zero-row or near-zero steps**.
- A **"what I checked / what I could not check" box** on the approval screen ([Google PAIR](https://pair.withgoogle.com/chapter/People%20+%20AI%20Guidebook%20-%20Explainability%20+%20Trust.pdf)).
- A **warning log** showing whether each warning changed a decision.

---

## 3. Pitfalls others hit that we must avoid

1. **Approval fatigue.** One "Approve plan" click over 25 steps becomes a 93%-yes habit (L7). Group the plan into 5-7 decisions and open only the risky ones.
2. **Self-verification illusion.** The agent re-reading its own plan is not a check (L1). Neither is a polished summary of what it did.
3. **Primed checkers.** A checker that reads the planner's reasoning just agrees with it (L3).
4. **Correlated "independent" methods.** Two AI runs share the same misreading of the task.
5. **Checker gaming.** If the agent can see or edit a check, expect it to pass the check rather than fix the data (L9).
6. **Alert floods.** Raw drift scores and loose rules flood users with false alarms. The TFX paper says exactly this ([snippet](https://mlsys.org/Conferences/2019/doc/2019/167.pdf)). Every false alarm teaches the user to click "yes".
7. **Checklists people tick without doing.** Ontario's mandated surgical checklist showed no effect when it was not really used ([snippet](https://theincidentaleconomist.com/wordpress/when-checklists-work-and-when-they-dont/)). Every item on our list must be a code-run challenge that returns a number. The agent never ticks a box itself.
8. **"I'll check it later" overrides.** Only 35.5% of "I will monitor" overrides were followed up ([snippet](https://www.doi.org/10.4338/ACI-2013-12-RA-0103)). Record the promise and re-check it on the next run.
9. **Trusting the answer key.** Benchmarks with wrong "correct answers" make agents look better or worse than they are (L10). Do not let the AI write the expected results.
10. **Learning from dismissals on dangerous checks.** Auto-downgrading alerts that users dismiss is fine for formatting warnings. It is unsafe for merged customers, consent and leakage.

---

## 4. What to add now (batch B2, v1) and what can wait

Sizes: S = days, M = 1-2 weeks, L = more than 2 weeks. The IDs refer to `docs/DATA_DOCTOR_SCENARIOS.md`.

### Batch B2 (v1), in priority order
1. **Sandbox receipt for every step** using the OpenLineage names: lost and duplicated keys counted separately, money totals and outcome rate before and after (G1). **M**
2. **Join cardinality guard:** key uniqueness on each side and expected against actual rows. It blocks double counting and any sum of a parent-level total after a one-to-many join. **M**
3. **Running loss budget and loss location:** total loss and outcome-rate shift across the plan, plus which segment holds the loss (G3, extended). **S**
4. **Time safety:** the `:as_of` value is mandatory, `now()` and today's date are banned, the typed step is an as-of join, and the left row count must not change (G10). **M**
5. **Leak scans:** blank-versus-outcome on every column, plus a single-column score with flag and block levels (G7). **S**
6. **Locked expectations and one combined failure result:** expectations are locked before the run and rewrites are flagged; all failures come back together with sample rows; retry cap of 2, then ask the human. **S**
7. **Checks out of the agent's reach:** a read-only check module, and one hidden traced customer and one hidden total. **S**
8. **Evidence-first approval screen:** funnel, "3 things to check", traced customers picked by code, one-click samples, a "checked / could not check" box, SQL folded away and no agent prose (U1-U4). **M**
9. **Three alert levels, an interruption budget of 3-5, override reason codes and a warning log.** **S**
10. **Determinism and staged publish:** run the plan twice and compare output hashes; save hash-named per-step snapshots; AutoML sees the table only after approval. **S**
11. **Zero-row or near-zero step stops automatically.** **S**
12. **Trap bench v0:** 20-30 traps covering the 12 scenario themes, human-verified results, 2-3 variants each, graded on result data with pass only if every run passes. Build it before the SQL step is switched on. **M**
13. **Checker input contract:** define now what the future AI checker may see (evidence only), even if the checker ships in batch C. **S**

### Later (batch C and beyond)
- Independent AI checker, calibrated on the trap bench (catch rate and false-alarm rate per trap type; trap types it misses stay on human approval). **M**
- Two-method cross-check with methods of different kinds, run on risky steps. **M**
- Join cross-check on other shared details such as phone hash, city and date order (G6). **M**
- Per-client contract memory (key, grain, valid values, consent meaning), versioned, with stored approvals tied to fingerprints and a library of previously approved steps. **M**
- Monthly replay drift checks: distribution distances, row-count rate and control charts against the client's own history, starting with a single 3-sigma rule (G18). **L**
- Suggested expectations from the client's data profile, with a 90/10 hold-out to drop brittle ones. **M**
- Shortcut check: a quick model on a time-shifted and customer-grouped hold-out, plus combination-leak and adversarial-validation scans. **M**
- Human-approver trap seeding in shadow mode, comparing screen variants. **M**
- Downgrade a warning after a "not a problem" answer (never for joins, consent or leakage), and a quarterly review to retire warnings. **S**
- Exportable datasheet and leakage sheet per prepared table (U7). **S**
