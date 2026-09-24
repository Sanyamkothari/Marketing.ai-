# WP7 test changes (admin, privacy, monitoring, approvals, role gating)

Only text or layout that changed on purpose. No behavioural assertion was loosened: where a text match
on a raw code or a lowercase status became a plain word, the assertion now checks the code or status in
its `data-code` / `data-status` attribute instead, which is at least as strict.

## Changed assertions

| File | Test | Old | New | Why |
|---|---|---|---|---|
| `tests/integration/production/ui/flow.test.mjs` | the Users screen lists everyone, and the last Admin cannot be disabled | one click on `[data-toggle="disable"]` sends the PATCH | first click shows "Disable admin-person and sign them out?" and sends nothing; the PATCH follows a click on `[data-toggle="disable"][data-confirm]` | Disable now has the two-step danger confirm (plan: Users) |
| `ui/ops/privacy.test.mjs` | a lookup sends the id in the body only… | waits for "Ledger rows, oldest first" | waits for "Consent history, oldest first" | "ledger" is jargon (audit: privacy) |
| `ui/ops/privacy.test.mjs` | same | `.pill.bad` text `withdrawn`; a `.pill` text `no record` | the answer row for `marketing_communication` has `data-status="withdrawn"`, class `bad`, text "No, they withdrew consent"; `account_servicing` has `data-status="none"`, text "No, there is no consent on record" | the result leads with "May we contact them?" in words |
| `ui/ops/privacy.test.mjs` | an erasure is sent only once confirmed… | card title `Erasure register ·` | `Erasure requests ·` | plain title |
| `ui/ops/privacy.test.mjs` | same (twice), and retention | `/CONFIRMATION_REQUIRED/` in the page text | `[data-code="CONFIRMATION_REQUIRED"]` present | a check the screen makes itself is an inline field message, no code on screen |
| `ui/ops/privacy.test.mjs` | same | uploads progress row text `/failed/` | its pill has `data-status="failed"` and the row reads "Failed" | sentence-case status words |
| `ui/ops/privacy.test.mjs` | same | `/PRINCIPAL_ID_REQUIRED/` in the text | `[data-code="PRINCIPAL_ID_REQUIRED"]` present | inline field message |
| `ui/ops/privacy.test.mjs` | same | waits for "Per store" | waits for "Where they were found" | the per-store table moved under a disclosure; the report leads with one sentence |
| `ui/ops/privacy.test.mjs` | same | every store pill text `done` | every store pill `data-status="done"` and text "Done" | sentence-case status words |
| `ui/ops/privacy.test.mjs` | retention shows the dry run… | first `td` text equals the file key | `tr[data-key]` whose key is a planned item and whose text contains the key | first column is now "What" (kind + file name); the full key is under "Show more columns" |
| `ui/ops/privacy.test.mjs` | same | `Deleted N file\(s\)` | `Deleted N file(s)` with a proper plural (`files?`) | plain wording |
| `ui/ops/privacy.test.mjs` | a Viewer is told why… | `/ROLE_REQUIRED/` in the text | `[data-code="ROLE_REQUIRED"]` present (the server's sentence is still asserted) | refused screens show no code |
| `ui/ops/monitoring.test.mjs` | Run now shows the firing as recorded… | "Fired: <status>" and the result code in the text | "Ran now:"; the notice's pill `data-status` is the firing's status; its `[data-code]` is the result code; the code is still in the page (under Details) | codes in words, the code under Details |
| `ui/ops/monitoring.test.mjs` | one schedule: its firing history… | 5 `.pill.bad` with text `missed` | 5 `.pill.bad[data-status="missed"]` with text "Missed" | sentence-case status words |
| `ui/ops/monitoring.test.mjs` | editing a schedule… | card title `Score new data ·` | page H1 starts `Score new data ·` | the detail page's title is the schedule's name (H1) |
| `ui/ops/monitoring.test.mjs` | same | "Saved; next due" | "Saved. Next run" | plain wording |
| `ui/ops/monitoring.test.mjs` | alerts open on the open ones… | row text "Performance drop" | row text "The model did worse on real outcomes" | "What happened" in words |
| `ui/ops/monitoring.test.mjs` | same | waits for "Acknowledged by" | waits for "Marked as being dealt with by you" (the user id is still asserted in the confirmation, under Details) | people by name; "I'm on it" |
| `ui/ops/monitoring.test.mjs` | a finished scoring run… | card title `Scoring runs ·` | H1 "Campaigns" and card title `Campaigns ·` | Outcomes becomes Campaigns |
| `ui/ops/monitoring.test.mjs` | same | "Incrementality input", "Treated minus control" | "Campaign effect (contacted vs control group)", "Difference vs control group" | renames from the audit |
| `ui/approvals/approvals.test.mjs` | the Approver sees the head-to-head… | "<N> held-out rows, both models"; "started by <user id>" | "Compared with the model in use on the same 1,200 customers"; "Trained by another user on"; "started by <id>" absent; the id is under Technical details | people by name, ids in Details |
| `ui/approvals/approvals.test.mjs` | approving needs a reason… | `/REASON_REQUIRED/` in the text | `[data-code="REASON_REQUIRED"]` present | inline field message |
| `ui/approvals/approvals.test.mjs` | same | "approved: it is now the champion"; "No challenger is waiting for approval" | "was approved: it is now the model in use"; "No model is waiting for approval" | plain wording; the new empty state |

## Added assertions (stricter, nothing removed)

| File | Test | What it adds |
|---|---|---|
| `ui/gate.test.mjs` | a Viewer is refused the uplift, campaign, value, feedback, new-client and raw-tables controls too | every new `ACTION_CONTROLS` row is disabled for a Viewer with the server's own sentence as its title and, where `explain` is on, beside it; the Remove buttons carry the sentence as a title only; choosing an existing client stays allowed |
| `ui/gate.test.mjs` | an Analyst may use all of them but the feedback export; an Admin only the export | the same rows follow the real roles (Analyst runs, Admin exports feedback) |
| `ui/ops/privacy.test.mjs` | the forms name the client chosen in the top bar, and Advanced overrides it | the consent lookup sends the top bar's client (no `CLIENT_ID_REQUIRED`); a client typed under Advanced wins; the customer id is still only in the POST body |
| `ui/approvals/approvals.test.mjs` | the Approver sees the head-to-head… | the deciding measure is the first row, marked "Main measure"; the rest sit behind "Show all measures"; `approvalsCount()` (the top bar badge) is "1" |
| `ui/flow.test.mjs` | the Users screen… | no PATCH is sent on the first Disable click |
| `test_production_ui.py` | `REQUIRED_GATES` | the C13 actions (campaign results, ROI inputs, feedback export, new client, raw tables add/remove, save mapping, build a dataset) must each have a gate row |
