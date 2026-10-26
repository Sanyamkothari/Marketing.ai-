# v3 decisions

The founder asked Claude to decide the open questions of [`plan-v3.md`](../../plan-v3.md) section 11 and of the
Data Doctor's order-log proposal ([`docs/plans/DATA_DOCTOR_EVENT_LOG_PATH.md`](../plans/DATA_DOCTOR_EVENT_LOG_PATH.md)
section 11), and to start building. These are those calls, made on 26 Oct 2026. Each one can be reversed; the
reason is given so it can be argued with.

## The order-log path (phase 0)

| # | Question | Decision | Why |
|---|---|---|---|
| B-1 | Which use cases offer the path? | `retail-win-back` and `win-back-campaign` only, through `order_log: true` on their templates. `rca` next, once churn as "no order for N days" has its own sentence. Not `targeted-advertisement` | Everyone in an order log has bought, so "converted within 30 days" has no honest negative class there |
| B-2 | Monthly snapshots or the library's single one? | **Monthly** (6 month-ends on UCI, 5,132 rows, 1,724 positives), with the time-based split on `snapshot_date` | v3's slow models (leaving risk, 90-day value, early signals) need more rows than one snapshot gives. The shipped `sample.csv` keeps working through the existing path |
| B-3 | Commit a ~1.7 MB gzipped real-data fixture? | **Yes**, sampled by customer, with the CC BY 4.0 attribution and its generator script | Offline tests on real rows beat synthetic ones, and CI must not depend on a host that this environment blocks |
| B-4 | Negative-quantity lines without a `C` prefix (stock adjustments) | **Count by quantity, as proposed**: quantity below zero is a return. The sentence shows how many are not `C…` lines | The prefix is specific to one dataset; quantity is not. In UCI nearly all of these lines have no customer and are already left out by Q2 |
| B-5 | A separate `order_log` flag? | **Yes**, `data_doctor.order_log`, default off | It keeps the parity checklist of `unified` untouched |
| B-6 | Guided setup's stop gains a pointer to the Data Doctor? | **Yes, behind `order_log`** | Without it a person on Guided setup never learns the path exists |
| B-7 | Who is the Analyst on a single-user install? | The install's owner holds every role, as the existing single-user role rules already give; the exact-sentence approval is still required | Removing the approval step would loosen a safety rule; one person approving their own sentence keeps the record |
| B-8 | Scoring next month: whole log again, or only new lines? | **The whole log again** for now. Appending new lines comes later, keyed on a content hash of the line | Re-uploading is idempotent and cannot double-count; appending needs row keys that survive a re-export (REAL_DATA_RESULTS failure 4) |

## v3

| # | Question | Decision | Why |
|---|---|---|---|
| V-1 | Email autopilot from day one? | **Yes**, inside the approved envelope, with every circuit breaker on. WhatsApp: one week shadow, one week approve-each-batch, then autopilot | An email mistake costs little and teaches us; a burned WhatsApp number cannot be undone |
| V-2 | Which image model, and where does it run? | **A provider interface with a fake implementation now**; the real model is chosen when phase 4 starts and this environment can reach one. Off by default | The pipeline (code-drawn prices, vision check, batch approval) is the hard part and does not depend on the model |
| V-3 | Ceiling on live experiments | **10% of the agent group**, at most 2 experiments per goal | Enough to learn something every few weeks at 20k+ customers, small enough that a losing arm costs little |
| V-4 | Auto-budget limit | **At most 20% of a goal's budget a week, only when the ranges do not overlap**, reported in the briefing with one-click undo | Moves money to what works without letting noise swing the budget |
| V-5 | Festival and payday calendar | **India first** (Diwali, Holi, Eid, Raksha Bandhan, Independence Day sale, year-end sales; salary days 1st and last of the month), in config so a country can be added without code | The launch market |
| V-6 | Cross-client starting points | **On by default, each client can turn it off** | It is how a small client starts smart; only pooled statistics from at least 5 clients are shared |

## Build setup

- Builders run on Sonnet; review and fixing run on Opus (the founder's call).
- Each stream builds in its own git worktree and branch, then is merged into `claude/beautiful-keller-kacuxh`.
- Shared contract fixed before the streams start: `CustomerState.features: dict[str, float]` and
  `CustomerState.features_as_of` (`autonomy/contracts/customer.py`). Streams add to it, never rename it.
