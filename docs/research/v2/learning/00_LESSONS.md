# What we learned from reading: ranked by impact

This page pulls together three research notes:

- [01 agent engineering](01_agent_engineering.md)
- [02 marketing ML in production](02_marketing_ml_production.md)
- [03 AI product and go-to-market](03_ai_product_gtm.md)

It also uses Anthropic's two harness posts and our own [real-data results](../../../v2/REAL_DATA_RESULTS.md).

**Caveat on sources.** The session's network blocked many sites, so several sources were read
through search extracts, not in full. Each note marks its sources [full] or [summary]. Check a
number before it leaves the team.

## 1. The brain (biggest impact)

1. **The default is the best "send to everyone" policy, chosen on held-out data.**
   - The agent targets only where held-out data shows targeting wins.
   - Report the agent's result on the customers where the two policies disagree.
   - Evidence: on Hillstrom, send-to-all beat the best targeting method in 48 of 50 held-out
     splits.
   - Our bar changes from "beat the fixed rule" to "beat the best blanket policy, out of sample"
     (02).
2. **Value comes from purchases measured against a control group, never from clicks.**
   - Formula: net value = P(buy) x relative lift x value - costs. The relative lift comes from the
     people we never contact and from past campaigns.
   - Use a per-customer model only when its ranking passes a held-out test (RATE/Qini).
   - Ranking customers and getting the size of the effect right are separate problems. Our 10x
     overestimate is a size error.
   - Add a nightly check that compares predicted and measured value per send (02).
3. **Treat fatigue as a weekly contact budget, tuned against measured unsubscribes.**
   - Add a penalty for sending the same message type again too soon.
   - Measure what unsubscribers do about 4 weeks later; do not assume their value drops to zero.
   - Vary contact limits at random in a small slice of customers to measure what fatigue really
     costs.
   - Dropping the lowest-value sends cut volume by 30% for 1.5% fewer clicks (02).
4. **Log the exact chance of every option, "do nothing" included.** Thompson sampling needs
   simulated estimates of these chances. Without them, offline scoring is wrong (02).
5. **Keep a permanent group of people we never contact.** That group is the ground truth for
   long-term effects; 14-day proxy metrics miss some changes (02).

## 2. The product and go-to-market

6. **Lead with the audit, then make it always-on.**
   - Hold out 10-20% of each existing Klaviyo flow and report what each flow really earns.
   - Then keep watching. A one-off audit loses customers fast.
   - A customer counts as activated at the first proof range that excludes zero (03).
7. **Be the independent referee of Klaviyo.** Klaviyo's Composer writes and launches campaigns. We
   prove what they earn. Copywriting is a commodity, as Jasper found (03).
8. **Price with a flat fee plus a guarantee before any share of profit.** A guarantee like "the
   month is free if the low end of the proof is below zero" works for now. Paying per outcome works
   only when the outcome is countable and undisputed (03).
9. **Win pilots through agencies.** Find 5 pilots through 2-3 Klaviyo agencies. Those installs also
   count toward Klaviyo's 5-install listing rule (03).
10. **Stop overclaiming.**
    - Drop "paid in results" and "3-10%" from the pitch until real pilots show them.
    - Never claim the highest autonomy level.
    - Show the signed limits as an "autonomy certificate" (03).
11. **Set autonomy per type of action, and approve policies rather than single messages.**
    - Low-risk actions move up a level after clean approved batches.
    - Approval prompts get rubber-stamped (about 93% are accepted).
    - Give sends a short cancel window (01, 03).

## 3. The product's own AI agents

12. **The Critic becomes a set of pass/fail judges, one per kind of failure.**
    - Each judge writes its critique before its verdict.
    - Check them against about 100 messages the founder labels, aiming for over 90% agreement on
      both passes and fails.
    - Build the test cases from real failures (01).
13. **Checkers never see the writer's reasoning.** A rejection goes back with its reason; after 3
    rejections it goes to a person (01).
14. **The Strategist asks instead of guessing.**
    - It returns a typed question with options.
    - Use the API's own structured-output enforcement; keep pydantic for the limits a schema
      cannot express.
    - The Creative keeps the brand kit as a fixed, cached prompt prefix, and code checks that
      message variants really differ (01).
15. **Trace every LLM call** with its prompt version and model id. Roll out prompt changes in
    Shadow first. LLM agents never hold send credentials (01).

## 4. How we build

16. **One pass/fail list of every claim we care about.**
    - Keep it in `docs/v2/STATUS.json`, with a progress log and a smoke check.
    - Every session starts by running the smoke check.
    - A claim flips to passing only on end-to-end evidence (Anthropic harness posts).
17. **Agree on "done" before building.**
    - The builder writes its pass/fail checks first.
    - Reviewers get hard thresholds plus a list of what does not count as a finding, which stops
      over-engineering.
    - Finishers paste the command and its output (01, harness posts).
18. **Turn our honesty rules into CI checks, not reviewer prose.**
    - Examples: no fitting on the test half, no dataset names in `autonomy/`, no mutable dataclass
      defaults.
    - Each stream lists its hidden decisions (names, units, time zones) when it merges (01).
19. **Re-check the setup with each new model.** Remove workarounds that are no longer needed (01,
    harness posts).
