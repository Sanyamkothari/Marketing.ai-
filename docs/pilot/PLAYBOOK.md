# Pilot playbook

**Status:** draft for review by Mukund. Not yet agreed with a client.
**Scope:** one telecom client; churn and root causes first, then a win-back campaign run with a
control group. About ten weeks from kick-off to the decision to continue.
**Companion documents:** [`DATA_REQUEST.md`](DATA_REQUEST.md) (what the client sends),
[`USABILITY_TEST.md`](USABILITY_TEST.md) (the internal dry run), `docs/DECISIONS.md` DEC-900 onwards.

A pilot fails for reasons tests never catch: a data request nobody could follow, results a
business user cannot read, no way to show value to the client's management. This playbook is the
plan that avoids those three. Every artefact it names is produced by the platform; nobody writes a
results slide by hand.

---

## 1. Success criteria (agreed at kick-off, in writing)

Agree these numbers with the client's marketing head in week 0, before any data arrives, and
record them in the kick-off note. Criteria agreed after the results are known are not criteria.

| # | Criterion | Measured by | Proposed threshold (to agree) |
|---|---|---|---|
| S1 | The data arrives usable | Data readiness report verdict | "Ready" or "Ready with warnings" by the end of week 3 |
| S2 | The churn model ranks customers well | Results report: share of all leavers in the top 10% flagged | At least 2.5 times what a random 10% would contain (25% of leavers or more) |
| S3 | The model beats a simple yardstick | Results report: "It beats the simple yardstick model" | Yes |
| S4 | The campaign changes behaviour | Campaign value view: customers kept, 95% range | The whole range is above zero |
| S5 | The campaign pays | Campaign value view: net value, with the client's own values | The low end of the net value range is above zero, or the client accepts a stated loss for learning |
| S6 | People can use it | Usability: the client analyst runs the pre-flight check and reads the readiness report unaided | Yes, no call needed |

S2's threshold should be set from the client's own history if they have run a churn model before;
2.5 times is a starting proposal, not a promise.

## 2. Exit criteria

Stop, or pause and re-plan, when any of these holds; say so the same week, in writing:

- **E1.** The data readiness verdict is still "Not ready" at the end of week 4 after two rounds of fixes.
- **E2.** Fewer than the minimum number of outcome examples exist in the history the client can send
  (the readiness report's "Examples of the outcome" box is amber and more history is not available).
- **E3.** The churn model does not beat the simple yardstick (S3 fails) after one round of feature work.
- **E4.** The client cannot hold back a random control group for the campaign. Without one, S4 and S5
  cannot be measured and the pilot cannot show value; do not run the campaign phase.
- **E5.** Any personal detail beyond a pseudonymised ID is sent twice after being flagged.

## 3. Roles

| Minfy | Client |
|---|---|
| **Pilot lead** - owns the plan, the weekly note and the success criteria | **Sponsor** (marketing head) - agrees the criteria, reads the results report, decides to continue |
| **Data scientist** - runs builds and models, reads every warning, reviews every report before it is sent | **Data analyst** - prepares the extracts, runs the pre-flight check, fixes readiness problems |
| **Solution engineer** - environment, access, security questions | **Campaign manager** - runs the campaign with the control group, sends the outcomes |
| **Reviewer** (Mukund) - signs off the playbook and the final readout | **Data protection contact** - approves the data request and the pseudonymisation method |

## 4. Week by week

| Week | What happens | Platform artefact | Done when |
|---|---|---|---|
| 0 | Kick-off. Agree success and exit criteria, roles, the control group share (default 10%) and the campaign window. Share the data request. | `DATA_REQUEST.md`, templates, pilot kit (`make pilot-kit`) | Criteria signed; client's data protection contact has approved the request |
| 1 | Client prepares extracts and runs the pre-flight check on their own laptop. Minfy answers questions by e-mail, not by call. | `preflight_report.html` (client side) | Pre-flight shows no "Problem" |
| 2 | Upload. Build the dataset. Send the readiness report. | Data readiness report (HTML/PDF) | Verdict sent within one working day of the upload |
| 3 | Fix round: the client fixes what the report lists; rebuild. | Readiness report, second version | S1 met, or E1 clock starts |
| 4 | First model: train, review the Data, Model and Output pages, approve the champion. Root-cause summaries per risk group. | Results report | S2 and S3 checked; results report reviewed internally |
| 5 | Results review with the sponsor: one page, two minutes. Agree the campaign: who is contacted, the offer, the control group. | Results report (PDF) | Sponsor agrees the campaign design |
| 6 | Score the current month; hand over the contact list with the control group held back. The campaign runs. | Output page, `scores.csv`, control group count | List delivered; control group recorded |
| 7-8 | Outcome window runs (60 days for churn; the value view shows the date results will be ready). Meanwhile: win-back model on past campaigns, if randomised history exists. | Value view ("results will be ready on …") | - |
| 9 | Client sends outcomes. Measure the campaign. Enter the client's values (value of a kept customer, offer and contact costs). | Campaign value view (HTML/PDF) | S4 and S5 checked |
| 10 | Readout to the sponsor: results report plus value view. Decision: continue, extend or stop. Export and review the in-app feedback. | Both reports; `pilot_feedback.csv` | Decision recorded |

Weeks 7-8 are set by the outcome window, not by effort: a 60-day churn outcome cannot be measured
sooner. If the client wants an answer earlier, measure an earlier signal (for example a recharge
within 30 days) and say which one it is in the readout.

## 5. Every week

- One short written note to the sponsor: what happened, what is next, any criterion at risk.
- Read the in-app feedback (`GET /pilot/feedback/export`, Admin) and turn anything real into an issue.
- Every report is reviewed by the Minfy data scientist before it is sent; nobody edits its numbers.

## 6. What the client never sends

Names, phone numbers, e-mail addresses, Aadhaar, PAN, addresses, account or card numbers. The data
request says so; the pre-flight check and the readiness report both flag any that arrive anyway.
Customer IDs are pseudonymised on the client's side with a key only the client holds.

## 7. Demo before the pilot

For the sales and kick-off meetings, use the demo environment rather than live training:
`make demo-seed` once, then `make demo` and open `/ui`. The demo is "Demo Telecom", entirely
synthetic; a guided tour runs on first visit, and the Pilot screen has every report ready.
