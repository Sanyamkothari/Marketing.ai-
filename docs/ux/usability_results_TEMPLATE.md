# Usability test results: TEMPLATE

> **Read this first.** Results are recorded by a person, from the observer's sheet and the recording, during the session or
> straight after it. **They are never invented, estimated, remembered from another session, averaged from memory or
> written by software.** A cell nobody observed stays empty. This template holds no results: every `____` is for a person to
> fill in. A copy with any cell filled in that was not observed is not a record of the test and must not be used to decide
> anything.
>
> Copy this file to `docs/ux/usability_results_<date>.md`, fill in the copy, and leave this template untouched. The kit
> that explains each field is [`USABILITY_TEST.md`](USABILITY_TEST.md); section numbers below refer to it.

## 1. The test

| Field | Value |
|---|---|
| Date(s) of the sessions | ____ |
| Round (1 for the first time; 2 or more when a failing task is re-run after fixes) | ____ |
| Facilitator (initials) | ____ |
| Observer (initials) | ____ |
| Layout tested (`campaigns` or `classic`) | ____ |
| Build tested (the git commit shown by `git rev-parse --short HEAD`) | ____ |
| Demo store seeded from (full validated file, or the committed sample) | ____ |
| Browser and screen size | ____ |
| Who filled in this record, and when | ____ |

## 2. Answer key (kit section 7.3, written before the first session)

| Item | Value as shown on the screen |
|---|---|
| Campaign(s) with results | ____ |
| Value earned, as the screen states it | ____ |
| Evidence label on the screen | ____ |

## 3. Participants (initials or P1 to P5 only; no names)

| P | Job title | Years in marketing | Campaign tools used in the past year | Meets section 2 (yes/no) | Task order |
|---|---|---|---|---|---|
| P1 | ____ | ____ | ____ | ____ | ____ |
| P2 | ____ | ____ | ____ | ____ | ____ |
| P3 | ____ | ____ | ____ | ____ | ____ |
| P4 | ____ | ____ | ____ | ____ | ____ |
| P5 | ____ | ____ | ____ | ____ | ____ |

Practice session held with a colleague before the first participant (yes/no): ____

## 4. Task 1: plan a win-back campaign and download who to contact (limit 10 minutes)

| P | Time taken | Clicks | Wrong turns (page, time) | Asked for help (count) | Outcome (Success / With difficulty / Fail) | Ease rating (1 to 7) | Why that number |
|---|---|---|---|---|---|---|---|
| P1 | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P2 | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P3 | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P4 | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P5 | ____ | ____ | ____ | ____ | ____ | ____ | ____ |

Reference shortest path (pasted from the journey test, kit section 7.4): ____

## 5. Task 2: show what last month's campaign earned (limit 5 minutes)

| P | Time taken | Clicks | Wrong turns (page, time) | Asked for help (count) | Figure the person gave | How sure they said it is | Right against the answer key (yes/no) | Outcome | Ease rating (1 to 7) | Why that number |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P2 | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P3 | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P4 | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ |
| P5 | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ | ____ |

## 6. Words that confused (quote exactly; one row per word per person)

| Word or phrase as shown | Where on screen (page, area) | Who (P) | What they thought it meant |
|---|---|---|---|
| ____ | ____ | ____ | ____ |

## 7. What people said, verbatim

For each participant, one sentence that sums up the experience (kit section 7.1), and the answers to the three closing
questions in the words they used.

| P | Quote | Most confusing thing | Expected to find and did not | The one thing to change first |
|---|---|---|---|---|
| P1 | ____ | ____ | ____ | ____ |
| P2 | ____ | ____ | ____ | ____ |
| P3 | ____ | ____ | ____ | ____ |
| P4 | ____ | ____ | ____ | ____ |
| P5 | ____ | ____ | ____ | ____ |

## 8. The success rules (kit section 8)

Fill in only when all five sessions are done, from the tables above, and have a second person check every count.

| # | Rule | Counted from the tables | Holds (yes/no) | Checked by |
|---|---|---|---|---|
| R1 | At least 4 of 5 succeed at Task 1, and at least 4 of 5 at Task 2 | Task 1: ____ of 5. Task 2: ____ of 5 | ____ | ____ |
| R2 | Shortest path at most 5 clicks; median clicks of those who succeeded at most 7 | Shortest path: ____. Median: ____ | ____ | ____ |
| R3 | Nobody was told which button to press | Sessions with help given: ____ | ____ | ____ |
| R4 | No word confused two or more participants without an action | Words named by two or more: ____ | ____ | ____ |
| R5 | Average ease rating at least 5.5 for each task | Task 1: ____. Task 2: ____ | ____ | ____ |
| R6 | No cause of failure is repeated | Repeated causes: ____ | ____ | ____ |

## 9. Actions

One row for every gap, confused word and repeated cause. Fixes go through the normal change process; this table only
records them.

| Gap or word | Seen by (P) | Page and file | What changes | Owner | Done on |
|---|---|---|---|---|---|
| ____ | ____ | ____ | ____ | ____ | ____ |

## 10. Decision (product owner only)

R1 to R3 all hold (yes/no): ____

Decision on the default layout (switch to `campaigns` / fix the gaps and re-test / other): ____

Decided by, and date: ____

Reference to the decision log entry: ____
