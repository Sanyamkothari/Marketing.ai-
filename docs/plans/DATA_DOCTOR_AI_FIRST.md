# Data Doctor, AI first — design proposal

Status: proposal for the owner's approval. Nothing here is built yet. It replaces the "rules decide, AI comments"
order of today's Data Doctor with "the AI decides, rules are its tools". The order-log design
(`DATA_DOCTOR_EVENT_LOG_PATH.md`) becomes one of the AI's workflows instead of a rule path.

## 1. Why

Two test runs (2026-10-26) drove the real Data Doctor visit on a real 169,080-row sample of the UCI Online Retail
log, with a stand-in model answering every AI call (harness: one replay script, the product's own prompts, tools,
gates and checks; only the model was swapped).

What the AI got right without being told: the file is an invoice line log (9,147 invoices, 1,500 customers, ~85
rows each); December 2011 is a partial month; 25% of lines have no customer; `C...` invoices pair with sales
(+74,215 / −74,215); 1,771 exact duplicate rows may be genuine; the right table is one row per customer with
purchases, spend, returns, first/last date and recency. It asked about meaning instead of guessing.

What stopped it — every one a product rule, none a reasoning failure:

| # | What happened | Where |
|---|---|---|
| 1 | The prompt said `:as_of`, the engine only knows `$as_of` | fixed in `fa212d4` |
| 2 | A rule proposed reading `StockCode` (`82494L`) as lakh | fixed in `fa212d4` |
| 3 | The model's questions were replaced by fixed options that do not fit (no "C = return", no typed date) | question catalogue Q8/Q10/Q12 |
| 4 | Every answer option says "Your answer is recorded; it does not change the data" | question effects |
| 5 | The answer form takes an option id only — no typed value | `AnswerRequest` (`api/routes/case_visit.py:145`) |
| 6 | One `suggest_answer` per chat turn; a second ends the turn (`AGENT_SUGGESTION_LIMIT`) | `engine/doctor/agent_chat.py` |
| 7 | `$as_of` comes only from question Q8; the person's "use 2011-11-30" in chat is unusable, and a typed date in SQL is refused (`SQL_NEEDS_AS_OF`, then `SQL_FIXED_DATE`) — a deadlock | `engine/agent/sql_step.py`, `engine/doctor/tools_sql.py` |
| 8 | The turn ended with the product's fallback ("I could not answer that…") — the AI never told the person why | `agent_chat.py` turn end |

## 2. The shape: one brain, memories, tools, workflows, a playbook

**One brain.** One Data Doctor agent per case. It reads the files, plans, calls tools, talks to the person.
The visit state machine (`engine/doctor/visit.py`) stays as the frame (saved state, resume, approval, hand-off)
but TREAT/CONVERSE become the agent's loop instead of rules-then-review.

**Memories.**

| Memory | Holds | Today |
|---|---|---|
| Working (this case) | files, looks and receipts, the plan, the person's answers *with their values* | the case; extend answers with values (§4) |
| Client | what this client told us before ("blank ID = guest") | standing answers, `engine/doctor/standing.py` |
| File shape | how last month's file of this shape was treated | `engine/doctor/memory.py`, RECALL |
| Experience | lessons across jobs ("`C` invoices are cancellations in this export") | **new**: written as proposals, a person approves each before it enters the playbook |

**Tools** — the ~85 rows of `engine/doctor/catalogue.py`, in three kinds the registry already has:
- *Look* (read only, no approval): profile, value counts, repeats, dates, find values.
- *Check* (rules as facts): key uniqueness, coverage, leakage probe, min rows/positives, consent, the readiness
  report. They return findings and recommendations; **they no longer stop the case by themselves** — the agent
  reads them and decides, except the hard checks in §5.
- *Change* (proposals): typed steps, SQL steps, outcome definitions, joins. Always on a copy, always with a
  receipt, applied only after approval.

**Workflows** — bigger tools made of the small ones, which the agent may call whole or step by step:
order log → one row per customer (the event-log design), stack monthly files, join a list and its events,
define an outcome from events.

**The playbook** (system prompt `data_doctor_agent`): how to work (look → understand → plan → ask → change →
check → repeat), how and when to use each tool, house rules R1–R29, and a "lessons" section fed by approved
experience memory.

## 3. The loop

1. **Read.** Look tools on every file (sampled across the file, never only the top rows).
2. **Understand.** Run the check tools; read their findings next to its own looks. Decide what each file is.
3. **Plan.** A short plan: steps, the outcome, the as-of date, **assumptions listed one by one**.
4. **Ask.** One message to the person: the plan, why, and each assumption as a yes/no or a value to fill in
   ("Today's date for this data: 2011-11-30? [change]"). Free text is always allowed.
5. **Change.** On approval, apply on a copy; every step returns a receipt (rows in/out, customers lost, totals).
6. **Check.** Run the checks again on the result. Failed → back to 3 with the failure in view (at most 2 retries
   per step, then the person decides — rule from today's prompt).
7. **Ready.** When the readiness report passes, hand off.

Limits: steps and cost per turn (today's `max_chat_steps`, `ai_limits`), raised to fit this loop; when a limit is
reached the agent **always** gets one last call to tell the person where it stopped (fixes #8).

## 4. Fixing the deadlock first (milestone A1)

- **Answers carry values.** `AnswerRequest` gains an optional `value` (a date, a number, a short text) and a
  question option can declare it needs one ("I will type a date" → a date field). Answers become decisions the
  tools read: the as-of answer sets `$as_of` (fixes #5, #7).
- **The agent may propose a decision**, not only suggest an option: `propose_decision(kind="as_of", value=...,
  because=...)` → a pending item the person ticks; ticked, it *is* the answer (fixes #6, #7). The one-suggestion
  limit goes; the turn's step budget limits it instead.
- **Answers change the data when they mean to.** Each catalogue answer gets a real effect where one exists
  (e.g. Q12 "These are returns" → net against sales; Q10 "Only rows with a customer" → leave out blank IDs), or
  says honestly that it is a note (fixes #3, #4). The agent may add an option in its own words; the person's
  choice is recorded with the agent's sentence.
- **The last word.** A turn that ends on a limit or a refusal calls the agent once more for a `reply` (fixes #8).

Test: the same replay on the same file must reach a built customer table with a receipt.

## 5. What stays hard (code, not the AI)

The AI chooses *how*; it cannot skip *whether*:
- nothing changes the person's data without approval; every change has undo;
- the final readiness checks: at least 1,000 rows and 200 positives (`engine.yaml` validation), no future data
  in features (leak probe), consent for contact lists, no fuzzy ID joins;
- personal data stays masked to the model (egress gates);
- with no AI service connected, today's rules run as they do now (fallback).

## 6. Milestones (each ends with the replay test on the Online Retail file, shown to the owner)

| # | What | Size |
|---|---|---|
| A1 | Deadlock fix: answers with values, `propose_decision`, last word, real answer effects | M |
| A2 | Check tools return findings instead of stopping; the agent decides (advisor stops become findings) | L |
| A3 | Order-log workflow as a tool (customer table, returns netted, guests out, outcome "ordered again within 90 days") | L |
| A4 | Playbook rewrite around the loop; tool guide per tool | M |
| A5 | Experience memory with owner approval | M |
| A6 | UI: plan + assumptions + approve on one screen | M |

## 7. Questions for the owner

1. Approve A1 as the first build step?
2. Experience memory: who approves a lesson — the owner only, or any Analyst?
3. Turn limits: keep a hard cost cap per case (today 2,000,000 tokens / 200 calls) — still right?
