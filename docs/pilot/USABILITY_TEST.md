# Usability test script

**Who:** one colleague outside the project team, with no data-science background. **How long:** 45
minutes. **Where:** a laptop with the demo running (`make demo-seed` once, then `make demo`) and the
pilot kit unzipped (`make pilot-kit`). **Facilitator:** one person, who reads the tasks aloud and
does not help. **Note-taker:** a second person, or a recording with consent.

Say before starting: "We are testing the product, not you. Think aloud. If you are stuck for two
minutes, say so and we move on - that is a finding, not a failure."

Do not explain any screen before its task. Do not name a button.

---

## Task 1 - Understand what to send (8 min)

> "A telecom client wants to try this. Using only this document, tell me which files they must
> send, how many months of history, and one thing they must never send."

Give: `docs/pilot/DATA_REQUEST.md` (printed or on screen).

Observe:
- Do they find the required tables without reading every column table?
- Do they give the history in months correctly (the "at least" figure)?
- Do they mention pseudonymising the customer ID unprompted?
- Words they stop at or ask about (write them down verbatim).

Success: names the required tables, the minimum history and at least one item from "What not to send".

## Task 2 - Check files before sending (8 min)

> "The client's analyst has prepared these files. Check them before they are sent."

Give: the pilot kit folder and the demo's broken extract (Pilot screen → "Demo raw tables (broken
extract)", unzipped). They may use the kit's README.

Observe:
- Can they run `python -m scripts.preflight` from the README alone?
- Do they open the report without being told where it is?
- Do they say what the one problem is, in their own words?

Success: runs the check and states that some customer IDs appear more than once in the customer file.

## Task 3 - Read the data readiness report (7 min)

> "The files were uploaded. Is the data ready? If not, what exactly must the client change?"

Give: the Pilot screen. They find the readiness report for "Demo Telecom (broken extract)".

Observe:
- Time to the verdict. Do they read the traffic light first?
- Can they name the one blocking problem and its fix without scrolling to the warnings?
- Do the warnings confuse them into thinking the data is blocked twice?

Success: names the blocking problem (repeated customer IDs) and the fix (one row per customer).

## Task 4 - Explain the results in two minutes (10 min)

> "You have two minutes with the client's marketing head. Using this page, explain how good the
> model is and what they should do."

Give: the results report of Telco Customer Churn (Pilot screen → Results report → View).
Time it; stop at two minutes.

Observe:
- Do they use the headline ("the top 10% flagged contain X% of all leavers") and compare it to random?
- Do they say what to do (bands and actions) and mention the control group?
- Which term do they skip or misuse (lift, decile, control group, confidence interval)?

Success: states the headline with its comparison, and one action.

## Task 5 - Explain the value of the campaign (10 min)

> "The campaign has finished. What was it worth? How sure are we?"

Give: the Pilot screen → Campaign value → the churn campaign.

Observe:
- Do they give a range, not a single number?
- Do they say the value depends on the entered values (and where those came from)?
- Do they understand what the control group is for, in their own words?
- Ask them to change the value of a kept customer and say what changes (the rupees) and what does
  not (the number of customers kept).

Success: gives the range of customers kept and of net value, and says that the rupee value depends
on the inputs.

---

## After the session

1. Ask: "What was the most confusing moment?" and "What would you remove?"
2. Record each finding as an issue in the repository, one per finding, titled with the screen and
   the words the person used, labelled `pilot-usability`. Include the task number and whether it
   blocked the task.
3. Add a line per session to the log below.

## Session log

| Date | Participant (role, not name) | Tasks passed | Issues filed |
|---|---|---|---|
| _not yet run_ | | | |

The first internal session has not been run yet: it needs a colleague outside the team and a
facilitator, which the build of this script could not provide. Running it, and filing its
findings, is the open item M64 leaves (see the README's Plan E section).
