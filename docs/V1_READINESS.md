# Is v1 ready?

**v1** means: Plans D and E are merged into `main`, `main` is green, and a non-technical person can
download the repository to a laptop, start the demo, and use every core journey without help.

Checked on 24 Sep 2026 against `main`.

## Verdict: READY WITH CAVEATS

Every core journey works end to end in a real browser, on a fresh clone, following only the
written instructions ([QUICKSTART.md](QUICKSTART.md)). The checks found five blockers. All five are
fixed on `main`, with regression tests, and the journeys that exposed them were walked again. What
remains is the caveats below. They are fine for a demo, but must be said out loud.

---

## Blockers found and fixed

"Blocker" means something a first user would hit and could not get past alone.

| # | What a user saw | Fix | Decision |
|---|---|---|---|
| 1 | Opening `http://localhost:8000`, the address the server prints, gave a JSON "Not Found". Only `/ui` worked. | `/` redirects to the screens, and `make demo` prints which address to open and how to stop it. | DEC-953 |
| 2 | With sign-in on, every screen showed a red `AUTH_REQUIRED` box beside the logo after signing in, the client picker fell back to "+ New client", and the demo badge vanished. | Signing in reloads the page as the signed-in person. | DEC-955 |
| 3 | A model trained from a prepared Telco file could not score a second file of the same format: "SCHEMA_MISMATCH … Partner (was string, now boolean)". Journey (b) could not finish. | Yes/No columns are read the same way when training and when scoring (CSV and Parquet), for models trained before the fix too. | DEC-956 |
| 4 | Score mode opened on the wrong model: the champion, which was trained on different columns, or another client's model with the step locked. | Score mode starts on the model just trained, then the selected client's newest model, then the champion. A stale error clears when the model changes. | DEC-959 |
| 5 | The AI-written screens (assistant, root-cause notes, campaign copy) showed the fake AI's stand-in text, and the assistant's card opened a CSV training form that does not apply to it. | In demo mode with no AI service, those screens show one "Needs AI service connection" notice. | DEC-954 |

Also fixed, because a demo would show them:

- **A false drift alarm.** Every scoring run on a built dataset reported "Drifted · max PSI 13.8"
  because the drift check measured the (always empty) churn label itself. The label and the keys are
  no longer drift features, including for baselines written before the fix (DEC-957).
- **"Class balance 0% positive"** on the Data page of runs on built datasets (really 18.6%). The
  split counted positives in the use case's default label column instead of the dataset's own
  (DEC-958).
- **An inconclusive demo campaign.** With 2,000 synthetic customers the churn campaign's 95% range
  included zero about one seed in six. The demo now has 4,000 customers (DEC-960).

The two reviewer rulings are applied: one code registry for every check (DEC-950), and the uplift
links are drawn in the prototype, with screenshots 01, 14 and 17 re-shot (DEC-952). The demo's churn
campaign is now measured on its Campaign results page (DEC-951).

---

## Caveats: fine for a demo, but say them out loud

1. **Everything is synthetic.** "Demo Telecom" is generated data with planted effects. The numbers
   show how the product works, not how well it will work on a client's data.
2. **No AI-written text in the demo.** The assistant, root-cause notes and campaign copy need an
   AI service (Amazon Bedrock) and show "Needs AI service connection". They work on Bedrock when it
   is configured, but that is not part of v1's demo.
3. **Sign-in is off in `make demo`.** You act with every role. Use `make demo-signin` to show roles
   and separation of duties (QUICKSTART §3).
4. **The first model search is deliberately fast.** Demo models use one model family and a
   2-minute limit, so they are seeded in minutes. A real pilot searches longer.
5. **Only built datasets for periodic data.** Data with one row per customer per month goes
   through *Build from raw tables*. The Setup screen cannot yet pick an already-built dataset for a
   second use case (it rebuilds from the tables).
6. **The raw-table mapper can mix up dates.** On the demo's complaints table it suggests the
   resolution date as the complaint date (it says it is only 60% sure). Check the mapping step.
7. **7-digit customer IDs can be flagged as phone numbers.** The warning says the IDs will be
   masked for training. The scores file still carries the real IDs.
8. **The campaign copy's audience can be empty** for an uplift campaign, because the copy targets
   the churn bands, not the uplift segments. It is hidden in the demo anyway (caveat 2).

---

## The core journeys

Each journey was walked in Chromium, in demo mode, through the UI as a business user would. The
approvals journey was walked with sign-in on, as each role. Where the API was used, it was only to
diagnose a failure.

| | Journey | Result | Note |
|---|---|---|---|
| a | Choose industry and use case | **Pass** | Telecom, Banking, Ad tech, E-commerce and Insurance each show their use cases; Telco Customer Churn opens its Setup. |
| b | Upload a prepared file → train → results with reasons → score a new file | **Pass** (after fixes 3 and 4) | 4,000-row Telco file trained in about 2 minutes. Model page shows feature importance, Output shows a reason per row, and the second file scores and downloads. |
| c | Build from raw tables → dataset → train → score next month's tables | **Pass** (after fix 4) | Six raw tables for a new client: upload, roles, mappings, build, train, then next month replayed through the saved recipe and scored. About 4 minutes of clicking plus 2 minutes of training. |
| d | Uplift: segments, contact list, Campaign results | **Pass** | Persuadables, sure things, lost causes and sleeping dogs; treat list downloads; both demo campaigns show measured results. |
| e | Approvals: the trainer cannot approve; an Approver can | **Pass** | `demo-lead` sees "You started the training run that produced this model, so another Approver has to decide on it." Analyst and Viewer cannot approve. `demo-approver` approves with a reason, and the model becomes champion. |
| f | Reports: data readiness, business results, value view (PDF and HTML) | **Pass** | All three render in the page and download as PDF. The value view is a rupee range. |
| g | Pilot kit: `make pilot-kit`, pre-flight on the demo raw tables | **Pass** | Kit built (968 KiB), installed in a clean virtual environment in 16 s. Pre-flight: clean tables exit 0; the broken extract exits 1 naming `ENTITY_DUPLICATE_KEYS` in `customers.csv`. |
| h | Audit log, erasure request, consent | **Pass** | Erasure runs as a background job and appears in the register; consent lookup answers (the client must be chosen); every action is in the audit log. |
| i | Guided tour and "What does this mean?" help | **Pass** | Six tour steps, each landing on a real screen; "?" opens a plain-language explanation for settings and warning codes. |

---

## Fresh-clone test

Twice, in a brand-new directory and a clean environment (no `uv`, no pip cache), following only the
written instructions:

1. **From the local checkout, following the README** (before QUICKSTART existed).
2. **From GitHub, following [QUICKSTART.md](QUICKSTART.md) word for word.**

| Step | Time | Result |
|---|---|---|
| `git clone https://github.com/Sanyamkothari/Marketing.ai-.git` | 2 s | **Failed.** It fetches the repository's default branch, `claude/gracious-noether-y0njma`, which holds only the original plan, so `make setup` does not exist. QUICKSTART now clones with `--branch main`. Switching the default branch is the owner's to do. |
| `git clone --branch main …` | 2 s | ok |
| `make setup` (pip, no cache) | 71 s | ok, `setup ok: autogluon.tabular 1.6.3`. On a slower laptop connection, expect 5 to 15 minutes. |
| `make demo-seed` (4,000 customers) | 2 min 36 s | ok |
| `make demo`, open `http://localhost:8000` | seconds | **Failed** in run 1 (JSON "Not Found" at `/`); ok after fix 1. The churn campaign: 12.7 points fewer customers left, 95% range 7.7 to 17.8 points, p < 0.001. |
| `make demo-signin` | seconds | ok: five demo users printed; `demo-lead` signs in with Analyst + Approver; the API refuses a caller who is not signed in. |
| `make pilot-kit`, pre-flight on the demo raw tables | 16 s install | ok |

No other step needed a guess, and none took over 5 minutes here; on a laptop, `make setup` may.

---

## Checks on `main`

| Check | Result |
|---|---|
| `make lint` (ruff, black, mypy --strict, generated-file drift) | clean |
| `make test-all` with Postgres and jsdom required (`MARKETING_AI_REQUIRE_POSTGRES=1`, `REQUIRE_JSDOM=1`) | _final two runs: see below_ |
| `make infra-lint`, `make infra-test` | clean; 257 passed |
| Prototype suite (`make prototype-test`) | 89 passed |

**Skips.** The only skips in the full suite are 6 optional cross-checks against the `scikit-uplift`
and `causalml` libraries, which the project does not install. No Postgres or jsdom test was skipped.

---

## What changed in the UI

Part 3 made the product usable without training, changing UI and presentation only. No engine
logic, check, champion rule or API contract changed, and no capability was removed: anything that
left a screen was moved, grouped or put behind a disclosure. The audit is
[`UI_AUDIT.md`](UI_AUDIT.md). Before/after screenshots of every screen are in
[`ui/before/`](ui/before/) and [`ui/after/`](ui/after/), indexed in [`ui/README.md`](ui/README.md).

- **One top bar, by goal.** Home · Build data · Models · Campaigns · Reports · Admin replaces two
  unrelated strips of links. Admin items and approvals appear only to the roles allowed to use them.
  The client picker, the sample-data chip, Help (tour, feedback) and the user menu sit at its right.
- **The header's right side is the logo alone** (the owner's request).
- **Verdict first, one primary action per screen.** Results say what happened in a sentence and offer
  the next step: score new customers, download the contact list, see the value in rupees.
- **Plain language.** No internal codes, file names, ids or phase numbers on screen. They sit under
  Technical details. Labels and explanations come from `configs/pilot/help.yaml`. Campaign results
  say "customers kept" for an outcome to prevent, matching the value view.
- **Progressive disclosure.** Advanced settings, settings planned for later, technical metrics and
  lineage are collapsed. Tables show a few columns, with "Show more columns".
- **Calm states.** Loading skeletons, helpful empty states, errors with "Try again", and one notice
  for a feature this environment cannot offer.
- **One design system.** The same header, spacing, type scale, cards, buttons and colours from the
  existing theme tokens on every screen. Keyboard focus is visible, and every screen works at 390 px
  and in dark mode.
- **The prototype matches the product.** `marketing-ai-prototype.html` was updated, with its tests
  and screenshots (CHANGELOG-prototype Revision 6).

---

## What v1 does not include

- **AWS deployment.** The infrastructure is written and tested offline, but nothing is deployed;
  that waits on an AWS account.
- **A real LLM.** The AI-written features run on a deterministic stand-in unless Amazon Bedrock is
  configured.
- **Sending messages.** The product recommends who to contact and writes a contact list. It sends
  nothing: no email, SMS or WhatsApp.
- **Real client data.** Only synthetic data has been through it end to end.
- **The self-learning layer** (Phase 5, "Evolve"): automatic retraining from measured outcomes
  beyond the scheduled retrain.
