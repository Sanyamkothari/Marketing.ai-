# Demo rehearsal (Plan J M111, DEC-1321)

**Status: the rehearsal by someone outside the team is PENDING.** An agent cannot do it, and the milestone's
acceptance ("the rehearsal completes without help") is not met until a person who has not worked on the product
follows [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) and nothing else, and fills in Part 1 below. Part 2 is the author's dry
run, recorded so the rehearser starts from what is already known.

---

## Part 1. For the rehearser (fill this in)

You are testing the script, not yourself. **Do not ask the team anything while you follow it.** If you get stuck,
write down where and why, and stop there: a stuck point is the result.

| | |
|---|---|
| Rehearser (name, role) | |
| Not on the product team? (yes / no) | |
| Date | |
| Machine, and who set it up | |
| Started at / finished at | |

**Setup, using only the "Before you start" section of the script**

| Step | Done without help? | Time it took | Where you got stuck, or what was unclear |
|---|---|---|---|
| 1. Got the full file (`fetch.py`) or knew you could not | | | |
| 2. Seeded the store (`seed_validated`) and read what it printed | | | |
| 3. Served it and opened the UI | | | |
| 4. Checked the screens against the summary (the `diff`) | | | |
| 5. Said whether you were on the full file or the sample, and how you knew | | | |

**The six screens, using only "The screens to click"**

| Screen | Found it? | Said the line the script gives? | What was unclear or different from the script |
|---|---|---|---|
| 1. Results: "Needs your attention" and "Value proven to date" | | | |
| 2. The campaign-effect model's page | | | |
| 3. The Output page: banner, offers, "Why some customers get no offer", Treat list | | | |
| 4. The conversion campaign | | | |
| 5. The Value Proof Pack | | | |
| 6. `DEMO_SUMMARY.md` | | | |

**Telling the story (the five questions)**, to a colleague, in your own words, out loud, from the script

| Question | Could you answer it from the page? | Did you say any number that is not on the summary? | Notes |
|---|---|---|---|
| 1. Who to contact, which e-mail | | | |
| 2. Who to leave alone | | | |
| 3. What it is worth | | | |
| 4. Does it beat the obvious alternative | | | |
| 5. How we know | | | |

**Honesty check.** Tick only what is true.

- [ ] I said, before the results, that this is public data and a retrospective reading, not a client's campaign.
- [ ] I said the list is worse than sending everyone the men's e-mail, without softening it.
- [ ] I said uplift modelling did not beat plain risk ranking.
- [ ] I said the model is not calibrated, so the "leave alone" names are a best guess.
- [ ] I explained the three things that look contradictory on the screens (two lists, "customers measured", the Pack's method text) before being asked.
- [ ] I quoted no number that is not on the summary page.
- [ ] I did not say, or imply, that this proves return on investment.

**Questions.** Of the table "Questions you will get", which could you not answer from the script? Which question did
the colleague ask that is not in it?

**Verdict.**

- [ ] Completed without help, nothing in the script needed changing
- [ ] Completed without help, but the script needs the changes listed above
- [ ] Did not complete (say at which step)

Signed off by (name, role, date): ______________________________

When this is filled in, commit it, change the README gate line from "pending" to the date, and open the manager
review. Until then the gate line says the human rehearsal is pending.

---

## Part 2. The author's dry run (an agent, 10 October 2026)

**What was done.** The script's "Before you start" and "The screens to click" were followed as written, against the
product started locally with `uvicorn`, on a store made by `python -m scripts.seed_validated`:

1. **Seed on the full file** (the 64,000-row prepared file, SHA-256 `434bc95c6e096dbe…`, copied from the M110 run
   because the download is blocked from this environment): the journey took 120 seconds. The generated
   `DEMO_SUMMARY.md` and `DEMO_SUMMARY.provenance.json` were byte-for-byte the committed ones, and the off-policy
   estimates, the approval checks and the conversion campaign's lift equalled the committed artefact's: the run
   reproduces.
2. **Seed on the committed sample** (a tenth of the file): 83 seconds. The command said "SAMPLE" and that its numbers
   are not the validated ones, and the generated page says so in its first lines. The story on the sample is
   different, as it should be: no range excludes zero, so the page says "Not shown" where the full file says "Yes" and
   "No". Do not demo the sample.
3. **A literal click-through** in headless Chromium (Playwright), by the words the script uses: Results, the trained
   model row, the model page, the scored row, the Output page, "Why some customers get no offer", the conversion
   campaign, the Value Proof Pack and its sections. All seven steps were found and showed the phrases the script
   quotes. The pack's PDF endpoint answered 200 in the seed.

**What was found, and what was done about it**

| Finding | Where | What the script now says |
|---|---|---|
| Two "Scored new data" rows look identical in the Results table (one is the risk model scoring the same customers) | Results | Says which row to open: the older one, whose page says "Scored with X-learner" |
| The Runs table shows the campaign-effect model's outcome as "AUUC 0" (rounded to a whole number), hiding 0.0004 | Results | Says it is rounded and to read the range on the model page |
| "Contact 7,596" (Output page) against "To treat 12,862" (Treat list card): the J5 fallback re-ranks the contact list but not the offer choice (DEC-1320 (o) 1) | Output | Listed under "looks contradictory" with the true answer |
| "Customers measured 25,599" and "12,862 contacted and 12,737 held back are compared": these count the customers the list meant to reach; outcomes exist for about a third | Results, campaign page | Listed; the summary states the kept counts and how many were left out |
| The Pack's Method text says the held-back group was chosen "before the campaign went out"; nothing went out (DEC-1320 (o) 3) | Value Proof Pack | Listed: say it is a replay aloud |
| "Net value of the offers given" on the Output page is the uncalibrated model's prediction and is larger than the measured range | Output | Listed: quote only the measured one |
| Persuadable and sleeping-dog counts on the Output page differ from the two reasons for no offer | Output | Listed |
| "Held back (holdout = 1)" on the Treat list card is blank because this run wrote no holdout assignment file (the card says so) | Output | Not mentioned; the card explains itself |
| The browser console logs certificate errors for external resources through this sandbox's proxy | all | Nothing: the pages render; check on a machine with normal network access |

**What the dry run could not show.** That a stranger can follow the script (that is Part 1). How long the story takes
to tell. Sign-in on (the screens were seen with sign-in off, as the Home guide describes). A projector or a phone.
The full file's download from a machine with normal network access (it is blocked here, so `fetch.py` was not run
and the copy of the prepared file was used).
