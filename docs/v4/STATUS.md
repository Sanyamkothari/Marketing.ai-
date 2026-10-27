# v4 status: the method change and foundation fixes round 2

27 Oct 2026, branch `claude/beautiful-keller-kacuxh`. This round follows [DIAGNOSE_RESULTS.md](../v3/DIAGNOSE_RESULTS.md).
Each stream was built by one agent, reviewed adversarially by a second and fixed by a third. Only the streams' own
tests and their neighbours ran locally. CI runs the full suites, the real bench and the Gym.

| Stream | Doc | What changes by default | Proven |
|---|---|---|---|
| Uplift policy (`uplift_threshold`): cross-fitted uplift on purchase margin, cost threshold, paired gate against the best simple rule | [METHOD.md](METHOD.md) | Nothing (bench policy only) | Ties the agent; beats the best simple rule only on MegaFon; gate fallback bug fixed ([RESULTS.md](RESULTS.md)) |
| Agent ablations `agent_greedy`, `agent_uplift_only` | [METHOD.md](METHOD.md) | Nothing | Greedy at least as good everywhere; the click model adds nothing ([RESULTS.md](RESULTS.md)) |
| Stronger baselines (DR-learner, depth-2 policy tree) and the who probes | [BASELINES.md](BASELINES.md) | Nothing | The trait leave-alone is the cause of the collapse; learned baselines close to the agent |
| Calibration (`agent_calibrated`) | [CALIBRATION.md](CALIBRATION.md) | Nothing (opt-in) | Does not help: same on the bench, -1% to -3% in the Gym. Stays off |
| Evidence-gated Autopilot | [CALIBRATION.md](CALIBRATION.md) | **Yes**: Autopilot needs proof (a lower bound above 0, estimates no more than 2x off), or an explicit email-only "start without proof" choice | Tests |
| WhatsApp readiness: no-click valuation, template sync, quality and limits, Hindi and Hinglish opt-outs | [WHATSAPP.md](WHATSAPP.md) | **Yes** for WhatsApp. Default stop rule now pauses on RED, and YELLOW halves the budget | Tests. Meta payload shapes need a real account |
| Order-history CSV import (UCI and Shopify shapes) into the event store | [ORDER_IMPORT.md](ORDER_IMPORT.md) | New CLI and API route | Tests |
| Margin question at onboarding; proof prices in the shop's currency | [ONBOARDING_MARGIN.md](ONBOARDING_MARGIN.md) | **Yes**: onboarding asks for the margin, and the proof withholds profit until it is known | Tests |

Open at integration:
- After a guard demotion, the Autopilot gate measures its 14-day minimum from the brief's approval, not from the
  demotion. Fixing this needs a ledger reader in `Services`.
- The agent reads the margin only at start-up.
