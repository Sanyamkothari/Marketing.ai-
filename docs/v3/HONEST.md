# Foundation fix 1: tell the truth in the product and the docs

27 Oct 2026, branch `v3/f-honest`. This is [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) section 8 "Fix first" item 1
and [CMO_REVIEW.md](CMO_REVIEW.md) change 2 ("strip the demo of unearned numbers"). It is a fix to the plain (v2) agent
and the marketer app, not a v3 feature: **v3 stays off**, and nothing here turns any of it on.

Every change is small and keeps behaviour except for the thing that was untrue. Where a default number moved, it is
listed under "What moves" with the line that sets it.

## What was changed

| # | Was untrue | Change | Where |
|---|---|---|---|
| 1 | STATUS.md said "Built" for things that are not wired or proven | The table has three columns, Built / Wired into production / Proven on real data, with the verdicts copied from review section 7; v3 is marked parked research | `docs/v3/STATUS.md` |
| 2 | Nothing said the demo shop is simulated | A "Simulated shop" label under the title on Proof (first), Live, Plan, Goal and Connect, and a banner under the top bar for the demo server. Simulated means: the sample files (`?mock=1`), a server whose only data source is the Gym, or the server's own "simulated shop" note on the proof | `ui/v2/components.js` (`isSimulatedShop`, `simulatedLabel`), `ui/v2/app.js`, the five screens |
| 3 | A 111.6x "return on spend" on a simulated shop | No return-on-spend multiple is shown or written: not on Proof, not in the share text, not in the Analyst's briefing. `ProofSummary.roi` is still computed and served (the contract is unchanged); see "What moves" | `ui/v2/screens/proof.js`, `autonomy/agents/analyst.py` |
| 4 | Spend differed between Live and Proof with no explanation | They count different sets and now each says so; Proof names Live's number under its own when they differ | `ui/v2/screens/proof.js` (`spendWords`, `sentSoFar`), `ui/v2/screens/live.js` |
| 5 | A suggested budget of $1.00 | Without history, the suggested budget is costed on the dearest channel of the starting plan, not the email price; the Goal screen says what a suggested budget covers | `api/v2/deps.py` (`STARTING_PLAN_CHANNELS`), `ui/v2/screens/goal.js` |
| 6 | WhatsApp was "Ready" on the channels card and "Coming next" on the platforms card | It is "Coming next" on both, with the reason in its description | `ui/v2/screens/connect.js` |
| 7 | Follow-ups were listed and shown as executed while journeys are off | The Plan screen lists a follow-up only when the server sends one (`PlanFacts.follow_ups_run`); the sample decisions no longer show a follow-up step; the starting plan's rationale no longer promises one | `api/v2/insights.py`, `api/v2/deps.py`, `api/v2/app.py`, `ui/v2/screens/plan.js`, `ui/v2/mock/decisions.json`, `activity.json` |
| 8 | The sample briefing had lines the engine cannot produce | `briefing.json` is the Analyst's own template output from the sample proof and health. Gone: "would buy anyway" (PROMISE_MAP row 37: there is no reason code for it), "What it is learning" (row 43: nothing writes it), the invented "Watch" line. The sample "learned" line and the "learning" alert went too | `ui/v2/mock/briefing.json`, `today.json`, `alerts.json` |
| 9 | The recommender was quoted at its best case (58% more) only | It is quoted with its losses from [REAL_DATA_RESULTS.md](../v2/REAL_DATA_RESULTS.md) "Recommender lift" | `ui/v2/screens/plan.js`, `docs/v2/PITCH.md` |
| 10 | The editor gate was said to be enforced only in the UI | It is enforced by the server, and now tested through the real Critic. See below | `api/v2/deps.py` (already there), `api/v2/insights.py`, `ui/v2/screens/plan.js`, `ui/v2/api.js`, `tests/v2/api/test_content_gate.py` |
| 11 | The pitch led with autonomy and "paid in results" | It leads with proof; "paid in results" is dropped; WhatsApp is not "today"; copy is not "AI writes" | `docs/v2/PITCH.md`, `docs/v2/PROMISE_MAP.md` |

### The editor gate (row 10)

PROMISE_MAP row 23 said the server did not enforce it. By the time this branch was cut it did:
`Services.set_content_status` answers 409 to `approved` unless the Critic's last note starts with "approved"
(`editor_passed`). What was missing, and is now done:

- A test that drives the real `Critic` (a scripted judge) through every way it can answer and posts the result to the
  route: a rule break, a failed judge, a judge that is down (held as a draft), a message with no review, and the two
  that pass (judge, and "rules alone" when the brand allows it). Each failure gets a 409 and nothing changes; a person
  can still reject or retire any message.
- The screen disagreed with the server for a half-judged draft: `review_content` called it "passed", so the Plan screen
  offered Approve and the server refused. It now reports "not finished" (`passed` null) for it.
- The Plan screen offered Approve on a message nobody had checked, which the server refuses. It now offers only
  Reject there, and the sample API answers 409 as the server does.

## What moves

- **Suggested budget without history** (`suggest_opportunities`, the fall-back used when the app has counts but no
  order history): `budget = ceil(customers x 2 messages x cost of the dearest channel in STARTING_PLAN_CHANNELS)`. Before
  it used the email cost, so it was `ceil(customers x 2 x 0.001)`, a dollar or so. The costs are still
  `DEFAULT_CHANNEL_COSTS` (email 0.001, WhatsApp 0.03), which are dollar-sized numbers read in the shop's currency; making
  them per-shop is review "Fix first" 2 and is not done here. Budgets from the money-sized proposals
  (`autonomy.onboarding`, used when order history exists) are unchanged: they are the cost of the messages to the
  channel each customer can be reached on, and they stay as they were.
- **Briefing text**: the Analyst no longer writes "Return on spend: x (likely range ...)" in its money section, and
  `numbers` no longer carries `roi`, `roi_low`, `roi_high` there. `ProofSummary.roi` (and so `early_signal`/auto-budget
  readers of it, which are not wired) is unchanged.
- **PlanFacts** has one new field, `follow_ups_run`, default false; `docs/v2/openapi.json` has it. Production sets it
  from `AUTONOMY_V3`.
- **Spend**: no number moved. Both counts are what they were; the screens say which is which.

Why the return-on-spend multiple went: it divides extra profit by the cost the proof managed to charge, and a discount
redeemed under a code the agent did not issue is never charged (review section 4 row 4, a blocker that also hits v2). On
the sample and on a simulated shop the multiple is dominated by a denominator of a few dollars of email cost.
Recomputing it "on total cost including offer cost" is what `proof_rewards` already does where the offer ids match, so
the honest options were to drop it or to fix row 4 first. Row 4 is "Fix first" 5; until then there is no multiple.

## What I could and could not measure here

Compute is scarce, so I did not run the full demo (`python -m api.v2.app`, 1,200 customers over 56 days). The figures
below are the CMO review's, and I did not reproduce them:

- 111.6x return on spend, spend $24.14 on Live against $17.25 on Proof, a $1.00 suggested budget.

What I ran: the demo at 400 customers over 30 days (`build_demo_services(..., simulation=True)`, one build of about 35
seconds). In it the autopilot goal had `GET /v2/today` `spend_to_date` 0.94 and `GET /v2/proof` `spend` 0.646: the two
screens disagree there too, by the cause described in change 4 (Live counts every message sent so far, Proof counts the
agent group's contacts whose 14-day window has closed). With so few customers Proof had no incremental figure yet, so
there was no multiple to look at.

**Pending CI / a full demo run** (the comparison only a full run can make): start the demo with the default size and
read, for the win-back goal, `/v2/proof`, `/v2/today` and `/v2/opportunities/sized`:

    python -m api.v2.app --port 8010 &
    curl -s 'http://127.0.0.1:8010/v2/proof?goal_id=<id>' ; curl -s 'http://127.0.0.1:8010/v2/today?goal_id=<id>'
    curl -s 'http://127.0.0.1:8010/v2/opportunities/sized'

and check that Proof shows the Simulated shop label first and no multiple, that Proof's spend tile names Live's number
when they differ, and that no suggested budget is $1.00 for a goal over a few hundred customers.

## Not done here (and why)

- The weekly "what is actually on" briefing section (review A11) is a new feature and is left for the next stream.
- The fake demo's one draft message (`_demo_content` in `api/v2/deps.py`) still carries a hand-written editor pass
  ("judge: brand voice 5, honesty 5") on urgency copy. It is the fake-world demo only (`--fake`), and a real
  review would hold it as a draft and break the approval test. Review C12; open.
- The sample alerts still include a "WhatsApp quality rating turned yellow" auto-pause, which nothing in production
  can produce (review row 3: quality webhooks are dropped). It is not a briefing line, so it was left; it should go with
  the WhatsApp readiness work ("Fix first" 6).
- Money in the shop's currency ("Fix first" 2), refunds in the proof ("Fix first" 3), the 1.5x calibration gate and the
  Autopilot gate on real evidence ("Fix first" 4) and offer-code mapping ("Fix first" 5) are separate streams.
