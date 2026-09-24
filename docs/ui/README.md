# UI screenshots: before and after the v1 clean-up

`before/` is the product at `de7d013` (before the clean-up). `after/` is the product after it. Every
screen was shot in every state it has (full, empty, loading, error, and special states such as a
failed run or a signed-in role), at desktop width (1440 px) and mobile width (390 px), plus desktop
dark mode for every full state. The file names match: `<screen>--<state>--<desktop|mobile|desktop-dark>.png`.

How they were made: `node scripts/ui_screens.mjs`, with three servers.

| Server | Serves |
|---|---|
| `BASE_DEMO` | the seeded demo (`make demo-seed`), demo mode on, sign-in off |
| `BASE_EMPTY` | an empty data folder, no demo |
| `BASE_AUTH` | the demo with sign-in on and the demo users from `scripts/demo_users.py` |

The before set was shot on a demo seeded with 2,000 customers, the after set on one seeded with
4,000 (DEC-960), so some counts differ. The screens are the same screens.

What changed and why, screen by screen, is in [`../UI_AUDIT.md`](../UI_AUDIT.md). Every test
assertion changed on purpose is listed in [`test-changes/`](test-changes/).

## The five biggest changes

| # | Change | Before | After |
|---|---|---|---|
| 1 | **One top bar, logo-only header.** Two unrelated strips (access links; demo, Pilot, tour) and the client picker above the logo became one bar: Home, Build data, Models, Campaigns, Reports, Admin, with the client picker, sample-data chip, Help and user menu on the right. Admin appears only to roles allowed to use it. The header's right side is the logo alone. | `before/home--full--desktop.png` | `after/home--full--desktop.png` |
| 2 | **Results lead with a verdict and one primary action.** "Scoring complete: 4,000 rows scored" with **Download contact list (CSV)**. Campaign results say "customers kept", in the same words and sign as the value in rupees. | `before/usecase-results-score--full--desktop.png`, `before/campaign-results--full--desktop.png` | `after/usecase-results-score--full--desktop.png`, `after/campaign-results--full--desktop.png` |
| 3 | **Plain words first, technical detail behind Details.** Model quality in one sentence ("It ranks a subscriber who has the outcome above one who does not 96% of the time."). Ids, hashes, lineage, AUUC, PSI, p-values and check codes are under Technical details. No artefact file names on screen. | `before/run-model--full--desktop.png`, `before/run-output--full--desktop.png` | `after/run-model--full--desktop.png`, `after/run-output--full--desktop.png` |
| 4 | **Setup and Build data one step at a time.** Only the step you are on is open. Finished steps fold to one line with "Edit". Settings not in use yet sit behind one toggle. The mapping step's "[object Object]" options and one-letter selects are fixed. | `before/usecase-setup-train--full--desktop.png`, `before/build-raw-2-mapping--full--desktop.png` | `after/usecase-setup-train--full--desktop.png`, `after/build-raw-2-mapping--full--desktop.png` |
| 5 | **Calm states instead of codes and blank pages.** Errors lead with a sentence and "Try again", with the code under Details. Loading shows a skeleton, empty screens offer the one action that fills them, and an unavailable AI feature shows one "Needs AI service connection" notice. | `before/usecase-planned--error--desktop.png`, `before/ai-notice--full--desktop.png` | `after/usecase-planned--error--desktop.png`, `after/ai-notice--full--desktop.png` |
