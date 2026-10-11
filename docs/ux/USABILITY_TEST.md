# Usability test kit: five people, two tasks, no help

Plan M, milestone M181 (DEC-1611). This kit is how Marketing AI finds out whether a marketer can use the campaign-centred
screens without being shown. It decides one thing: whether the `campaigns` layout becomes the default
(Plan M §5 M181 and §0). The switch itself is a separate change that waits for the results; nothing in this kit changes
the default.

**Results are recorded by a person, during or straight after each session, from the observer's sheet and the recording.
They are never estimated, remembered from another session or written by software.** A blank is better than a guess. The
form to fill in is [`usability_results_TEMPLATE.md`](usability_results_TEMPLATE.md).

Contents: 1 What is tested · 2 Who takes part · 3 Set-up · 4 Roles and order · 5 The two task cards · 6 Facilitator
script · 7 Observer's sheet · 8 Success rules · 9 After each task and at the end · 10 Recording the results · 11 The
automated checks that go with it · 12 Click-count journeys

---

## 1. What is tested

Two tasks, each given on a printed card, timed, with no help.

| | Task | Where a person should end up | Time limit |
|---|---|---|---|
| 1 | Plan a win-back campaign and download who to contact | A contact list saved on the computer | 10 minutes |
| 2 | Show what last month's campaign earned | The campaign's proven value, said aloud with how sure the product is | 5 minutes |

The product under test is the application in the `campaigns` layout, on the validated Hillstrom demo. The tests measure the
**screens**, not the person: if someone gets lost, the screen is at fault and the observer writes down where.

The targets come from Plan M §0 ("Measures of success"). They are repeated here so nobody has to open the plan during a
session:

| Plan M §0 measure | Target | How this kit checks it |
|---|---|---|
| Clicks from Home to a downloaded contact list (file already prepared) | at most 5, inside one wizard | The shortest path is pinned by an automated journey (section 12); the sessions count what people actually needed |
| Task success, 5 marketers, 2 tasks, no help | at least 4 of 5 per task | Observer's sheet, section 7; rules in section 8 |
| Engine words visible to a marketer | none outside Technical details | Automated visible-text check; in the sessions, every word that confused someone is written down |
| Primary buttons per page | 1 | Checked page by page in the jsdom page tests where a page asserts it; not yet one sweep over every page. The observer notes any moment a person could not tell which button to press |
| Places to add campaign results | 1 | Not covered by this kit's tasks (neither task adds a result); checked by the campaign page tests (M176) once merged |

## 2. Who takes part

**Five people**, each in a separate 45-minute session.

Recruit people who:

- work as a CRM, retention, lifecycle or growth marketing manager, or in a team that plans customer campaigns;
- have run a campaign in a tool such as CleverTap, MoEngage, Netcore, Braze, WebEngage or an e-mail platform in the past year;
- **have not seen Marketing AI**, not even in a demo, and have not read about it;
- are comfortable reading English on screen.

Do not recruit anyone who works at Minfy, anyone who helped build or sell the product, or anyone who has seen the screens
in a review. Mix the five if you can (different employers, different seniority). If a participant turns out not to meet
the criteria once the session starts, run it as a practice session, do not count it, and recruit another person.

The product owner chooses the five. Plan M §8 question 1 is still open: until the people are named, this kit has no
participants, and no result may be written for them.

Each person is told, before agreeing: what the session is, that the screens are being tested and not them, that the
screen and voice may be recorded, who will see the notes, and that they can stop at any time. Get a yes in writing or on
the recording. Use initials or P1 to P5 in every record; keep names out of the results.

## 3. Set-up

One computer for the participant, a second person (or a second window) for the observer, a timer, and a screen recorder.

### 3.1 The demo store

The tasks run on the validated Hillstrom demo (the public e-mail experiment): a seeded store that already holds the
customer file, a trained model, a scored contact list and two measured campaigns. The repository seeds it with one
command (`library/DEMO_SCRIPT.md` has the detail and the honest limits):

```
python library/hillstrom-email/fetch.py          # once; the full file, checked against its published digest
python -m scripts.seed_validated --force         # about three minutes; prints the store's path
```

Without the full file the second command uses a committed sample (a tenth of the rows): fine for rehearsing, but the
numbers on screen are then not the validated ones, and the results must say so.

### 3.2 One store per participant

Task 1 creates a campaign and Task 2 reads results, so every participant starts from the same untouched store. After
seeding, copy the store once for each person and serve their copy:

```
cp -r <the store it printed> store-p1      # repeat for p2 to p5, and one for the practice session
MARKETING_AI_UI_LAYOUT=campaigns MARKETING_AI_DATA_DIR=store-p1 \
  .venv/bin/uvicorn api.main:app --port 8000
```

Do not set `MARKETING_AI_DEMO_MODE`: that switch is for the synthetic Demo Company, and on this store it would put a
"Sample data: not loaded" notice in the top bar and on the Reports page, which is false and would mislead the participant.
`MARKETING_AI_UI_LAYOUT` is the deployment setting that arrives with the new shell (M174, DEC-1604); before that milestone is merged
there is only the classic layout and this kit cannot be run. Open `http://localhost:8000/ui` in a fresh browser profile (no saved sign-ins, no "tour done" flag), full screen, on a
display at least 1440 pixels wide. Close the server when the session ends and start the next copy.

### 3.3 Before every session (the facilitator, five minutes)

- [ ] The left sidebar shows five places and the bell and Help at the top right. If the top bar shows Home, Connections,
      Results and Settings instead, the layout is `classic`: the setting was not picked up. Stop and fix it.
- [ ] There is no "Sample data" notice in the top bar (if there is, demo mode is on: restart without `MARKETING_AI_DEMO_MODE`).
- [ ] The answer key (section 7.3) is filled in from **this** store's Reports page, before the participant arrives.
- [ ] The downloads folder is empty, and the browser asks where to save (or saves to a folder you can see).
- [ ] The recorder is running and the timer is reset.
- [ ] The task cards (section 5) are printed. The participant sees only the cards.

### 3.4 Optional comparison

For a before-and-after picture, a second group of five can do the same tasks on the `classic` layout
(`MARKETING_AI_UI_LAYOUT=classic`). It is not needed for the switch-over decision.

## 4. Roles and order

- **Facilitator**: talks to the participant, reads the cards, follows the script in section 6.
- **Observer**: silent, fills the sheet in section 7, runs the timer, counts clicks. One person can do both only for a
  practice session.
- Counterbalance the order: P1, P3 and P5 do Task 1 first; P2 and P4 do Task 2 first. Record the order.
- Run **one practice session** with a colleague first (not counted) to fix the set-up and the timing.

## 5. The two task cards

Print one card per task. Read it aloud and leave it on the table. Do not add anything.

> **Task 1**
> Your company has customers who have not bought anything for a while. Use this tool to plan a campaign to win them back.
> Then get a file with the customers you would send it to, and save it on this computer.
> Tell me when you think you are finished.

> **Task 2**
> Last month your team ran an e-mail campaign. Your manager asks how much money it earned for the company, and how sure
> you can be of that number. Use this tool to find the answer and tell me what you would say to your manager.
> Tell me when you think you are finished.

The cards avoid every menu name and every product word on purpose: "contact list", "goal", "Proof Pack", "comparison
group" and the names of the five places must come from the screens, not from the facilitator.

### 5.1 What counts as finished

| Task | Success | Success with difficulty | Fail |
|---|---|---|---|
| 1 | A contact list file is saved, opens, and holds customers; no help; within 10 minutes | The same, but with a request for help that the facilitator declined, or after leaving and returning to the task | No file; the person gave up; time ran out; help was given |
| 2 | The person says the amount the screen shows (as a floor or range if the screen gives one) **and** how sure the product is, in words that match the screen's evidence label; no help; within 5 minutes | The same after reading the wrong campaign first | Wrong or no figure; says it is proven when the screen does not; gave up; time ran out; help was given |

"Success" and "Success with difficulty" both count as success in section 8; the difference is recorded so the screens can
be improved. Check Task 2's answer against the **answer key** in section 7.3.

## 6. Facilitator script

Plain words. Do not name menus, buttons, pages or product terms at any point.

**Opening (about 3 minutes)**

> Thank you for coming. We are testing a tool, not you. Nothing you do can be wrong: if something is hard to find, that is a
> problem with the tool and it helps us to see it. I will give you two short tasks, one at a time, on a card. Please think
> aloud: say what you are looking at, what you expect, and what you are unsure about. I will not be able to answer
> questions about the tool while you work, because we want to see what it is like for someone alone with it. When you
> think you have finished a task, say so. You can stop at any time. We will record the screen and your voice so that
> we can write notes accurately; the recording is deleted after the notes are written. Do you have any questions before we
> start? Are you happy to go ahead?

Start the recorder and the timer when the person starts reading the first card.

**During a task**

- Say nothing about the screen. Do not point, do not say "click".
- Allowed, neutral prompts only, in these words:
  - "What are you thinking now?"
  - "What would you do next?"
  - "I cannot answer that. What would you try?"
  - After about two silent minutes: "Take your time. If you would like to stop this task, you can say so."
- If the person asks for help: "I cannot help with this one. What would you try?" Note that they asked. If they ask a
  second time, offer to stop the task and record a fail.
- Do not react to a good or a bad choice (no nodding, no sighs).
- Stop the task at the time limit, whatever is on screen.

**Words the facilitator does not say** (anywhere, including in the prompts): Overview, Campaigns, Data, Reports, Settings,
New campaign, wizard, goal, contact list, Proof Pack, value proof, comparison group, holdout, priority, likelihood,
propensity, uplift, model, run, train, score, approve, Doctor, glossary. If the person uses one of these words first, you
may repeat it back; you may not introduce it.

**After each task**: ask the one-question rating (section 9.1), then reset: close the downloaded file, go back to the
first screen of the application.

**Closing (about 5 minutes)**: the three open questions in section 9.2, then thanks, then stop the recorder. Do not explain
the tool or show what you were hoping they would find until the recorder is off and every question has been asked.

## 7. Observer's sheet

Copy section 7.1 once per participant per task (the results template has the same blocks ready to fill in).

### 7.1 Per task

| Field | What to write |
|---|---|
| Participant | P1 to P5 |
| Task, order | 1 or 2; first or second |
| Start time / end time | From the participant starting to read the card to saying they are finished (or the limit) |
| Time taken | End minus start, in minutes and seconds |
| Clicks | Count every mouse click or key press that activates a control (a link, a button, a menu item, a card, a tab), from the first screen to the finish. Do not count scrolling, typing, or clicks that do nothing on screen. Do count the browser's Back button |
| Wrong turns | Every time the person opens a page that is not on the way to the answer. Write the page's name as it appears on screen, and the time |
| Words that confused | Exact words the person read aloud, asked about, or got wrong. Quote them. Also write where on screen |
| Help | Number of times the person asked; what was asked; what the facilitator said |
| Outcome | Success / Success with difficulty / Fail (rules in section 5.1) |
| Quote | One sentence the person said that sums up the experience, verbatim |
| Notes | Anything else: hesitation, hovering, hunting for a button, misreading a number |

### 7.2 Task 2 only: what the person said

Write down verbatim: the figure they gave, whether they called it proven or not, and what they said it meant for their
manager. Then mark it right or wrong against the answer key.

### 7.3 Answer key (fill in before the first session, from the seeded store)

Open Reports and the measured campaigns on the store you are serving, and write what a correct answer is. Do this once
per store; stores seeded from the same file give the same figures.

| Item | Value (copy from the screen, with the units) |
|---|---|
| Campaign(s) that have results on this store | |
| Value earned, as the screen states it (floor or range) | |
| Evidence label on the screen (Proven / Estimated / Not yet proven) | |
| Which file was used (the full validated file, or the sample) | |

If the store was seeded from the sample, write that here and in the results: the figures are then not the validated ones.

### 7.4 Click reference

The shortest path for Task 1 is listed in the automated journey test (section 12). Paste its steps here before the first
session so the observer can compare. For Task 2, write the shortest path you can find, then check it with a colleague.

## 8. Success rules

The switch to `campaigns` as the default is considered when **all** of rules R1 to R3 hold. Rules R4 to R6 are fixed before
the switch whether or not R1 to R3 hold. These rules are the kit's reading of Plan M §0 and §5 M181; where a rule goes beyond
the plan it says so, and the product owner can change it before the first session (not after).

| # | Rule | Source |
|---|---|---|
| R1 | **At least 4 of the 5 participants succeed at Task 1, and at least 4 of 5 at Task 2**, unaided and inside the time limit. "Success with difficulty" counts. | Plan M §0, last row |
| R2 | **Task 1: the shortest path to a saved contact list is at most 5 clicks** (pinned by the automated journey), **and the median participant who succeeded needed at most 7** (the shortest path plus two for ordinary exploring). | Plan M §0, "at most 5 clicks"; the allowance of two is this kit's, not the plan's |
| R3 | **Nobody was told which button to press.** Any session in which the facilitator helped counts that task as a fail. | Plan M §0 ("no help") |
| R4 | **No word confused two or more participants without an action to change it.** Every word on the confusion list (section 7.1) that two people named is either changed or explained in the results, with an owner. | Plan M §0 (engine words), §4 |
| R5 | **The average ease rating (section 9.1) is at least 5.5 out of 7 for each task.** Lower is not a veto; it creates an action list. 5.5 is a commonly used benchmark for this question and is this kit's choice. | This kit |
| R6 | **No cause of failure is repeated.** If two participants failed a task for the same reason, that reason is fixed before the switch even when R1 holds. | This kit |

**If R1 to R3 hold**: the product owner may switch the default layout to `campaigns` (a separate change that records this
result in the decision log). **If any of R1 to R3 fails**: write the gaps, fix them, and re-run the failing task with at
least three new participants who meet section 2. Record both rounds; do not overwrite the first.

The person who decides is the product owner, from the recorded results. Nothing in the repository decides it.

## 9. After each task and at the end

### 9.1 After each task: one rating (the Single Ease Question, 1 to 7)

> Overall, how easy or how difficult was that task? (1 means very difficult, 7 means very easy.)

Record the number the person says. Do not suggest one. Ask "What made you say that number?" and note the answer.

### 9.2 At the end: three open questions

1. What was the most confusing thing you saw today?
2. What did you expect to find that you did not?
3. If you could change one thing first, what would it be?

Ask them in this order, in these words. Write the answers word for word.

## 10. Recording the results

1. Copy [`usability_results_TEMPLATE.md`](usability_results_TEMPLATE.md) to `docs/ux/usability_results_<date>.md` before
   the first session.
2. During each session the observer writes on paper or straight into the copy. Fill in the participant's block within an
   hour, while it is fresh, from the sheet and the recording.
3. Compute the rule table (section 8) only when all five sessions are done, and have a second person check the counts.
4. Leave a cell empty if something was not observed. Do not average from memory, do not fill a gap with "probably", and do
   not ask software to write or summarise results that nobody observed.
5. Delete the recordings once the notes are checked. Keep the notes, which hold no names.

## 11. The automated checks that go with the test

The test uses people; these use the code. They do not replace the sessions, and the sessions do not replace them. Run all
of them before the first session so that a defect a machine can find is not discovered by a participant.

| Check | What it covers | How to run |
|---|---|---|
| Visible-text check | No engine word on any page outside Technical details | `pytest tests/integration/test_visible_text_ui.py` |
| Accessibility checks | A name on every control, button and link; alt text; one H1; headings that do not jump; colour contrast of the palette against WCAG AA; known defects listed with a reason, so a new one fails | `pytest tests/integration/test_accessibility_ui.py -s` |
| Keyboard path | Tab through the Overview, the Help menu and the first step of the wizard, with a visible focus style | `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers pytest tests/integration/test_keyboard_path_ui.py` |
| Screenshot set | The main pages in both layouts at 1440 by 900 and 390 by 844, for a visual review before the sessions | `python -m scripts.ui_screenshots <folder>` |

The screenshot set goes to the folder you name and is not committed. Use `--layouts campaigns` for one layout,
`--pages overview,wizard` for some pages, and `--base http://localhost:8000` to photograph the store you will test on
(name exactly the one layout it is running with `--layouts`; the pages for a run and a campaign also need
`--run r_...` and `--campaign c_...` from that store, or they are left out and listed under "Skipped"). Its `INDEX.md` lists pictures that drew nothing, pages that scroll
sideways on the phone, and console errors. Open the phone pictures of the Overview, Campaigns and a campaign page: Plan M
§6 requires them to be usable at 390 pixels.

The accessibility and keyboard checks keep **lists of defects they found** (`tests/integration/production/ui/a11y/known.json`,
`contrast_known.json`, and `KNOWN` in the keyboard test), each with a reason, the place in the code and the fix. Read those
before the sessions: a defect that is on the list is a defect a participant using the keyboard or a screen reader will meet.

## 12. Click-count journeys

An automated journey fixes the shortest path, so the click target in Plan M §0 cannot drift.

- **Task 1** (Overview to a saved contact list, at most 5 decisions): pinned by
  `tests/integration/test_campaign_wizard_journey.py`, added with M177 (DEC-1607). Paste its list of clicks into the
  observer's sheet (section 7.4). If that file is not in the repository yet, M177 has not been merged and the New campaign
  wizard is not ready to test.
- **Task 2** (Overview to the proven value of a measured campaign): no automated journey yet. It is written once the
  Overview and Campaigns pages (M175, M176) are merged, as part B of this milestone. Until then the observer's reference path
  is the one in section 7.4, found by hand.

Neither journey replaces a person: it proves the route exists and is short, not that a person finds it.
