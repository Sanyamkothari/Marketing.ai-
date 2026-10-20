# Failure isolation

Autonomy is six engines in one process (see [ENGINES.md](ENGINES.md)). Each one can fail on its own: a
database goes away, a model provider is down, a carrier answers 503. When one fails, the others
should keep doing what is still safe, and nothing should go out that was not checked. This page is
the founder's failure-isolation table, row by row. Each row has tests that make that engine fail
and check the fallback.

The tests are in `tests/v2/failure/test_failure_isolation.py`. They run on in-memory fakes with
time passed in, and the file takes about two seconds:

```bash
python -m pytest tests/v2/failure -q -rx      # -rx prints each xfail's reason
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
| P1 Data Doctor / ingest | No new events arrive for days | A person is alerted that the feed has gone quiet (`ingest.*`) | `test_p1_ingest_stalled_raises_a_staleness_alert` | xfail |
| P1 Data Doctor / ingest | The event feed is unreachable (`scan` raises) | The tick survives, decides on cached states, folds nothing and stores an `ingest.*` alert | `test_p1_event_feed_unreachable_keeps_deciding_on_cached_states_and_alerts` | xfail |
| P1 derived signals | The `state_enricher` (another engine's scores) raises | The goal decides on the plain stored states; an alert says so | `test_p1_a_failing_state_enricher_decides_on_plain_states_and_alerts` | pass |
| P2 Mandate | (store not needed) The mandate already in hand | It keeps capping spend: a brief asking for 50,000 gets the ceiling less spend; over the ceiling is refused; past `valid_until` nothing goes | `test_p2_the_mandate_already_held_still_governs_and_refuses_budget_expansion` | pass |
| P2 Mandate | Budget expansion with the store up | A goal and brief raised to 50,000 still spend at most the mandate's ceiling | `test_p2_the_runtime_mandate_refuses_a_budget_expansion` | pass |
| P2 Mandate | The mandate store is down | The goal keeps running under the last unexpired mandate it read, a person is alerted, the expansion is refused, and nothing goes once that mandate expires | `test_p2_mandate_store_down_keeps_the_last_unexpired_mandate_and_refuses_budget_expansion` | xfail |
| P3 Decision Hub | The policy raises for every customer of a goal | Nothing unvetted is sent for that goal, a `decide.error` alert is raised, and other goals still decide and send | `test_p3_policy_raising_sends_nothing_unvetted_alerts_and_spares_other_goals` | pass |
| P3 Decision Hub | The policy raises for one customer | Everyone else is decided and contacted; an alert is raised | `test_p3_policy_raising_for_one_customer_spares_the_rest` | pass |
| P3 Decision Hub | The policy raises | Each affected customer gets a logged DO_NOTHING (probability 1, reason names the failure), so proof and the off-policy log keep the moment | `test_p3_policy_raising_logs_do_nothing_for_that_tick` | xfail |
| P3 Decision Hub | The policy cannot be built (`policy_factory` raises) | Only that goal is skipped for the tick, with an alert; other goals run | `test_p3_policy_that_cannot_be_built_is_isolated_to_its_goal` | xfail |
| P4 Studio (LLM) | The model provider is unreachable | The Strategist drafts the goal kind's template brief (unapproved) and the weekly adjustment keeps the brief unchanged | `test_p4_strategist_falls_back_to_the_template_brief_and_keeps_the_plan` | pass |
| P4 Studio (LLM) | The model provider is unreachable | The Analyst writes its code-only template briefing | `test_p4_analyst_falls_back_to_the_template_briefing` | xfail |
| P4 Studio (LLM) | The model provider is unreachable | The Creative falls back to fixed template copy, still DRAFT (no dynamic text, the Critic still gates) | `test_p4_creative_falls_back_to_template_copy` | xfail |
| P4 Studio (LLM) | The Critic's judge is unreachable | The Critic's rules still reject rule-breaking copy, with no model call | `test_p4_critic_rules_still_gate_while_its_judge_is_down` | pass |
| P4 Studio (LLM) | The Critic's judge is unreachable | Copy that passes the rules is held (never APPROVED), without crashing the review | `test_p4_critic_holds_rule_passing_copy_while_its_judge_is_down` | xfail |
| P4 Studio (LLM) | Analyst and Strategist both down during the weekly review | The plan stays as approved (same envelope, audience and approval) and the sweep still sends. Today the review is refused with a `weekly.refused` alert; once the Analyst falls back to its template, the Strategist stores an unchanged next version instead. The test accepts either | `test_p4_runtime_weekly_review_survives_an_llm_outage_and_keeps_sending` | pass |
| P5 Dispatch Gateway | The only carrier answers 503 | The send is buffered on the queue with growing backoff, sent once when the carrier recovers, and never sent twice (replays and late jobs deliver nothing) | `test_p5_provider_down_buffers_with_backoff_and_sends_exactly_once` | pass |
| P5 Dispatch Gateway | Carrier down at 20:59; the retry lands at 21:00 | Quiet hours are checked again on the retry: it waits for 09:00, then sends once | `test_p5_quiet_hours_are_rechecked_when_a_buffered_send_retries` | pass |
| P5 Dispatch Gateway | A read timeout (the message may have gone) | Retried on the same carrier only, never failed over, so no second message lands | `test_p5_an_ambiguous_timeout_is_retried_on_the_same_carrier_never_failed_over` | pass |
| P5 Dispatch Gateway | The first carrier refuses outright | Failover to the backup at once; nothing is queued | `test_p5_a_definite_rejection_fails_over_to_the_backup_carrier_at_once` | pass |
| P5 Dispatch Gateway | Carrier down during an autopilot sweep (runtime with `gateway=`) | The tick buffers every send; later ticks deliver each exactly once | `test_p5_runtime_buffers_a_provider_outage_through_the_gateway` | pass |
| P5 Dispatch Gateway | Carrier down at a 20:59 sweep (runtime with `gateway=`) | The retry at 21:00 waits through quiet hours; the 09:00 tick sends each once | `test_p5_runtime_rechecks_quiet_hours_when_a_buffered_send_retries` | pass |
| P6 Incrementality Ledger | No delivery, click or purchase telemetry ever arrives | Dispatch continues; the budget still binds (spend comes from our own SENDs); holdout customers are never contacted, even by a policy that ignores the split | `test_p6_without_outcome_telemetry_dispatch_stays_within_budget_and_respects_holdout` | pass |
| P6 Incrementality Ledger | The proof report is not ready (the weekly Analyst raises) | A `weekly.refused` alert; the sweep still sends | `test_p6_a_lagging_proof_report_does_not_stop_dispatch` | pass |
| P6 telemetry | The gateway's telemetry sink raises | Sending is not stopped or distorted: one SEND, no FAIL, no exception | `test_p6_a_failing_telemetry_sink_does_not_stop_or_distort_a_send` | xfail |
| P6 spend telemetry | The spend source (`spend_fn`) raises | The goal keeps deciding with the last budget it knew (never more) and a `spend.*` alert is raised | `test_p6_spend_source_down_keeps_the_last_known_budget` | xfail |

17 pass, 10 xfail.

## The gaps, and where to fix them

Each xfail's `reason` in the test file says the same thing in one line.

| Test | What happens today | Where to fix |
|---|---|---|
| `test_p1_ingest_stalled_raises_a_staleness_alert` | Nothing in the tick notices that no event has arrived for a long time. | `autonomy/runtime/service.py`, `_fold`: track the newest arrival, alert `ingest.stale` past a configurable age |
| `test_p1_event_feed_unreachable_keeps_deciding_on_cached_states_and_alerts` | `_fold` calls `events.scan()` unguarded: `ConnectionError` ends the tick before anything is decided, and no alert is stored. | `service.py`, `tick` / `_fold`: catch, keep the watermark, alert `ingest.unavailable`, go on to decide (the breaker and learning scans need the same guard) |
| `test_p2_mandate_store_down_keeps_the_last_unexpired_mandate_and_refuses_budget_expansion` | `_mandate_for` catches the outage, but then it **halts** the goal (`mandate.error`, `halted_goals`). This fails safe, but it is not what the row asks for. | `service.py`, `_mandate_for` / `_mandate_state`: keep the last good mandate per goal and act under it, with a warning, while it is unexpired |
| `test_p3_policy_raising_logs_do_nothing_for_that_tick` | `_decide_goal` skips a customer whose decision raised and logs no decision. That customer also misses the rest of the day's sweep. | `service.py`, `_decide_goal`'s `except` branch: record a NOTHING decision (probability 1), or rank with the static rules |
| `test_p3_policy_that_cannot_be_built_is_isolated_to_its_goal` | `_running` calls `policy_factory()` unguarded: one goal's broken bundle stops every goal. | `service.py`, `_running`: skip that goal with a `decide.error` alert |
| `test_p4_analyst_falls_back_to_the_template_briefing` | `write_briefing` catches `AgentOutputError` only; `LLMError` propagates. | `autonomy/agents/analyst.py`, `write_briefing` |
| `test_p4_creative_falls_back_to_template_copy` | `LLMError` propagates out of `build_library`. An unreadable answer leaves its cell empty, and there is no template copy. | `autonomy/agents/creative.py`, `_cell`: fixed template copy per cell, kept DRAFT |
| `test_p4_critic_holds_rule_passing_copy_while_its_judge_is_down` | `_review_one` catches `AgentOutputError` only. `LLMError` propagates, so the review fails closed, but by crashing. | `autonomy/agents/critic.py`, `_review_one`: hold the item with a note |
| `test_p6_a_failing_telemetry_sink_does_not_stop_or_distort_a_send` | `DispatchGateway._emit` calls the sink unguarded, inside the `FailoverAdapter`. A message the carrier accepted is logged as FAIL and queued for a retry, and then `dispatch()` raises. The retry does not deliver twice, because the gateway remembers the accepted attempt. | `autonomy/channels/gateway.py`, `_emit`: catch and count sink errors |
| `test_p6_spend_source_down_keeps_the_last_known_budget` | `_budget_left` calls `spend_fn` unguarded: the tick raises. | `service.py`, `_budget_left`: use the last known spend (never less), alert `spend.unavailable` |

## What the tests use

- **Stores.** `FlakyFeed` wraps the in-memory event store, and only its `scan` (the read path ingest feeds) goes down. Appends and per-decision lookups keep working, so de-duplication still works. `FlakyMandates` is a `MandateSource` whose reads all raise while it is down.
- **Policies.** `RaisingPolicy` crashes for every customer, or for some. `BudgetedRogue` keeps to `ctx.budget_left` but labels everyone agent, so only the runtime and the dispatcher can keep the holdout uncontacted.
- **Models.** `DownLLM` raises `LLMError("LLM_UNAVAILABLE")` on every call.
- **Carriers.** `Carrier` answers 503, or raises a timeout, until `up` is set. It honours message ids, the way providers with idempotency keys do. Because that would hide a gateway that re-sends an accepted message, it also counts every accepted call (`accepted`), and the "exactly once" checks use that count.

## Not covered yet

- Postgres-backed stores failing part-way through a write. Every test here uses in-memory fakes.
- A worker process dying while it holds a claimed job. The queue's claim timeout is WF1's to test.
- The negative-lift breaker reading a lagging proof. That belongs with the breaker's own tests.
