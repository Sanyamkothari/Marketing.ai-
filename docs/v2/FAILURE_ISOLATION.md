# Failure isolation

Autonomy is six engines in one process (see [ENGINES.md](ENGINES.md)). Each one can fail on its own: a
database goes away, a model provider is down, a carrier answers 503. When one fails, the others
should keep doing what is still safe, and nothing should go out that was not checked. This page is
the founder's failure-isolation table, row by row. Each row has tests that make that engine fail
and check the fallback.

The tests are in `tests/v2/failure/test_failure_isolation.py`. They run on in-memory fakes with
time passed in, and the file takes about two seconds:

```bash
python -m pytest tests/v2/failure -q -rx      # -rx prints any xfail's reason
```

**Status.** `pass` means today's code does what the row promises. `xfail` means the test is written
against the promised behaviour and fails today. It is marked `xfail(strict=False)` with the gap and
where to fix it, and nothing in it was weakened to make it pass. When one of these starts passing,
pytest reports it as XPASS. At that point, remove its marker and change its status here to `pass`.

Five tests use the three optional runtime hooks wired in this round: `gateway=`, `mandates=` and
`state_enricher=` on `AutonomyRuntime`. If a hook is missing, `_require_hook` xfails the test at
once, so a branch without the wiring reports the gap rather than failing with a TypeError.

## The table

| Engine | Failure | Expected behaviour | Test | Status |
|---|---|---|---|---|
| P1 Data Doctor / ingest | No new events arrive for days | The runtime keeps deciding (and acting) on the states it holds; nothing new is folded | `test_p1_ingest_stalled_runtime_keeps_deciding_on_cached_states` | pass |
| P1 Data Doctor / ingest | No new events arrive for days | A person is alerted that the feed has gone quiet (`ingest.*`) | `test_p1_ingest_stalled_raises_a_staleness_alert` | pass |
| P1 Data Doctor / ingest | The event feed is unreachable (`scan` raises) | The tick survives, decides on cached states, folds nothing and stores an `ingest.*` alert | `test_p1_event_feed_unreachable_keeps_deciding_on_cached_states_and_alerts` | pass |
| P1 derived signals | The `state_enricher` (another engine's scores) raises | The goal decides on the plain stored states; an alert says so | `test_p1_a_failing_state_enricher_decides_on_plain_states_and_alerts` | pass |
| P2 Mandate | (store not needed) The mandate already in hand | It keeps capping spend: a brief asking for 50,000 gets the ceiling less spend; over the ceiling is refused; past `valid_until` nothing goes | `test_p2_the_mandate_already_held_still_governs_and_refuses_budget_expansion` | pass |
| P2 Mandate | Budget expansion with the store up | A goal and brief raised to 50,000 still spend at most the mandate's ceiling | `test_p2_the_runtime_mandate_refuses_a_budget_expansion` | pass |
| P2 Mandate | The mandate store is down | The goal keeps running under the last unexpired mandate it read, a person is alerted, the expansion is refused, and nothing goes once that mandate expires | `test_p2_mandate_store_down_keeps_the_last_unexpired_mandate_and_refuses_budget_expansion` | pass |
| P3 Decision Hub | The policy raises for every customer of a goal | Nothing unvetted is sent for that goal, a `decide.error` alert is raised, and other goals still decide and send | `test_p3_policy_raising_sends_nothing_unvetted_alerts_and_spares_other_goals` | pass |
| P3 Decision Hub | The policy raises for one customer | Everyone else is decided and contacted; an alert is raised | `test_p3_policy_raising_for_one_customer_spares_the_rest` | pass |
| P3 Decision Hub | The policy raises | Each affected customer gets a logged DO_NOTHING (probability 1, reason names the failure), so proof and the off-policy log keep the moment | `test_p3_policy_raising_logs_do_nothing_for_that_tick` | pass |
| P3 Decision Hub | The policy cannot be built (`policy_factory` raises) | Only that goal is skipped for the tick, with an alert; other goals run | `test_p3_policy_that_cannot_be_built_is_isolated_to_its_goal` | pass |
| P4 Studio (LLM) | The model provider is unreachable | The Strategist drafts the goal kind's template brief (unapproved) and the weekly adjustment keeps the brief unchanged | `test_p4_strategist_falls_back_to_the_template_brief_and_keeps_the_plan` | pass |
| P4 Studio (LLM) | The model provider is unreachable | The Analyst writes its code-only template briefing, at once and without a second ask | `test_p4_analyst_falls_back_to_the_template_briefing` | pass |
| P4 Studio (LLM) | The model provider is unreachable | The Creative falls back to fixed template copy per channel x offer x theme, still DRAFT (the copy passes the Critic's rules; the Critic still gates) | `test_p4_creative_falls_back_to_template_copy` | pass |
| P4 Studio (LLM) | The Critic's judge is unreachable | The Critic's rules still reject rule-breaking copy, with no model call | `test_p4_critic_rules_still_gate_while_its_judge_is_down` | pass |
| P4 Studio (LLM) | The Critic's judge is unreachable | Copy that passes the rules is held as DRAFT with a `judge unavailable` note (never APPROVED unless the brand rules say `judge_optional`), without crashing the review | `test_p4_critic_holds_rule_passing_copy_while_its_judge_is_down` | pass |
| P4 Studio (LLM) | Analyst and Strategist both down during the weekly review | The plan stays as approved (same envelope, audience and approval) and the sweep still sends. The Analyst writes its template briefing, so the review completes and the Strategist stores an unchanged next version (before the Analyst fallback, the review was refused with a `weekly.refused` alert; the test accepts either) | `test_p4_runtime_weekly_review_survives_an_llm_outage_and_keeps_sending` | pass |
| P5 Dispatch Gateway | The only carrier answers 503 | The send is buffered on the queue with growing backoff, sent once when the carrier recovers, and never sent twice (replays and late jobs deliver nothing) | `test_p5_provider_down_buffers_with_backoff_and_sends_exactly_once` | pass |
| P5 Dispatch Gateway | Carrier down at 20:59; the retry lands at 21:00 | Quiet hours are checked again on the retry: it waits for 09:00, then sends once | `test_p5_quiet_hours_are_rechecked_when_a_buffered_send_retries` | pass |
| P5 Dispatch Gateway | A read timeout (the message may have gone) | Retried on the same carrier only, never failed over, so no second message lands | `test_p5_an_ambiguous_timeout_is_retried_on_the_same_carrier_never_failed_over` | pass |
| P5 Dispatch Gateway | The first carrier refuses outright | Failover to the backup at once; nothing is queued | `test_p5_a_definite_rejection_fails_over_to_the_backup_carrier_at_once` | pass |
| P5 Dispatch Gateway | Carrier down during an autopilot sweep (runtime with `gateway=`) | The tick buffers every send; later ticks deliver each exactly once | `test_p5_runtime_buffers_a_provider_outage_through_the_gateway` | pass |
| P5 Dispatch Gateway | Carrier down at a 20:59 sweep (runtime with `gateway=`) | The retry at 21:00 waits through quiet hours; the 09:00 tick sends each once | `test_p5_runtime_rechecks_quiet_hours_when_a_buffered_send_retries` | pass |
| P6 Incrementality Ledger | No delivery, click or purchase telemetry ever arrives | Dispatch continues; the budget still binds (spend comes from our own SENDs); holdout customers are never contacted, even by a policy that ignores the split | `test_p6_without_outcome_telemetry_dispatch_stays_within_budget_and_respects_holdout` | pass |
| P6 Incrementality Ledger | The proof report is not ready (the weekly Analyst raises) | A `weekly.refused` alert; the sweep still sends | `test_p6_a_lagging_proof_report_does_not_stop_dispatch` | pass |
| P6 telemetry | The gateway's telemetry sink raises | Sending is not stopped or distorted: one SEND, no FAIL, no exception | `test_p6_a_failing_telemetry_sink_does_not_stop_or_distort_a_send` | pass |
| P6 spend telemetry | The spend source (`spend_fn`) raises | The goal keeps deciding with the last budget it knew (never more) and a `spend.*` alert is raised | `test_p6_spend_source_down_keeps_the_last_known_budget` | pass |

27 pass, 0 xfail.

## How the runtime and gateway rows were closed

Each fix is small and local, with a one-line comment naming its row. The outage alerts
(`ingest.*`, `mandate.store_unavailable`, `spend.unavailable`, and `decide.error` for a policy that
cannot be built) are stored once a day, so a long outage does not flood the inbox.

- **P1 feed unreachable** (`service.py`, `_fold`): a failing scan is caught. The tick folds nothing,
  keeps the watermark (missed events fold once the feed is back), raises `ingest.unavailable` and
  decides on the states it holds. A full or nightly rebuild builds into a fresh spend ledger and
  switches to it only if the scan finished, so a failed rebuild never leaves a budget that looks
  unspent.
- **P1 stale feed** (`RuntimeConfig.ingest_stale_after`, default 2 days, `None` turns it off): watches
  the newest customer-behaviour event (signup, visit, view, cart, purchase, refund), so our own sends
  never make a dead feed look alive, and raises `ingest.stale`. Quiet time from before the process's
  first fold is not counted, so old history alone does not trip it.
- **P2 mandate store down** (`_read_mandate`): the last active, certified mandate read is kept per
  goal. Any real answer from the store (revoked, missing, unsigned) clears it. While the store raises,
  the goal runs under the held mandate until it expires, with a `mandate.store_unavailable` warning;
  its ceiling still binds, so a budget expansion is refused. After it expires the goal halts with
  `mandate.error`. `send_gate` uses the same held mandate.
- **P3 policy raises** (`_decide_goal`): that customer gets a logged DO_NOTHING decision (probability
  1, reason `LEFT_ALONE`), nothing is sent, `decide.error` is raised, and others carry on.
- **P3 policy cannot be built** (`_running`): only that goal is skipped for the tick, with a critical
  `decide.error`; the build is retried every tick.
- **P6 telemetry sink** (`gateway.py`, `_emit`): a raising sink is caught, logged and counted in
  `telemetry_errors`. The send's record and result are unchanged.
- **P6 spend source down** (`_budget_left`): the last good read is kept per goal. During the outage
  the budget left is the smaller of the last budget left and the current cap less the last spend,
  less what the goal's own sends since that read cost. A raised budget is never honoured, and
  `spend.unavailable` is raised. With no good read the budget left is 0. Under a mandate, unknown
  spend counts as infinite, so nothing passes the ceiling.

Extra tests: `tests/v2/resilience/test_runtime_and_gateway.py`.

## How the P4 rows were closed

Each fix is local to its agent and marked with a `P4 LLM unavailable` comment. "The model cannot be
reached" means `engine.llm.LLMError` or any other exception from the provider call. An answer that
arrives but does not parse is a different case, and it keeps its own handling.

- **Analyst** (`write_briefing`): writes the template briefing at once, without asking twice, and
  records `the model could not be reached (<code>)` in `problems`. Every number still comes from code.
- **Creative** (`_cell`): uses `TEMPLATE_COPY`, two fixed variants per theme. The offer appears only
  as `{{offer_label}}`, the copy uses only allowed slots, and it goes through the same footer and length
  checks as model copy, so it passes the Critic's rules on every channel. After the first failure,
  the rest of that build uses templates without calling the model again. `Creative.templated` lists
  those cells. An unreadable answer still skips its cell, as before.
- **Strategist**: already fell back on `LLMError` (template brief when drafting, unchanged next
  version when adjusting). Checked, and no change was needed.
- **Critic** (`_review_one`): when the judge cannot be reached, copy that passed the rules stays DRAFT
  with a `judge unavailable` note, so a later review can approve it. A judge run that already scored
  the item below the threshold still rejects it. Rule failures still reject without a model call.
  Copy is approved on the rules alone only when `BrandRules.judge_optional` is set (default False).

Extra tests: `tests/v2/resilience/test_agents_llm_fallbacks.py`.

## What the tests use

- **Stores.** `FlakyFeed` wraps the in-memory event store, and only its `scan` (the read path ingest feeds) goes down. Appends and per-decision lookups keep working, so de-duplication still works. `FlakyMandates` is a `MandateSource` whose reads all raise while it is down.
- **Policies.** `RaisingPolicy` crashes for every customer, or for some. `BudgetedRogue` keeps to `ctx.budget_left` but labels everyone agent, so only the runtime and the dispatcher can keep the holdout uncontacted.
- **Models.** `DownLLM` raises `LLMError("LLM_UNAVAILABLE")` on every call.
- **Carriers.** `Carrier` answers 503, or raises a timeout, until `up` is set. It honours message ids, the way providers with idempotency keys do. Because that would hide a gateway that re-sends an accepted message, it also counts every accepted call (`accepted`), and the "exactly once" checks use that count.

## Not covered yet

- Postgres-backed stores failing part-way through a write. Every test here uses in-memory fakes.
- A worker process dying while it holds a claimed job. The queue's claim timeout is WF1's to test.
- The negative-lift breaker reading a lagging proof. That belongs with the breaker's own tests.
