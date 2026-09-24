# UI audit

What the Marketing AI screens look like today, what gets in a business user's way, and what the UI work in Part 3 will change. Part 3 changes UI and presentation only. It does not change engine logic, checks, champion rules or API contracts, and it removes no capability: anything that goes away from a screen is moved, grouped or hidden behind a disclosure.

## How this audit was made

- **Date:** 24 Sep 2026, on the code at `de7d013`.
- **Coverage:** 59 screens and 136 screen/state pairs (full, empty, loading, error, plus special states such as a failed run, a validation refusal, a wrong password, a Viewer and each signed-in role). Every pair was shot at desktop (1440 x 1000) and mobile (390 x 844), and every full state was also shot in desktop dark mode. That is 336 full-page screenshots.
- **Servers:** a seeded demo copy (demo mode on, access control off), an empty data directory (no demo), and a sign-in server with demo users `demo-viewer`, `demo-analyst`, `demo-approver` and `demo-admin`. Loading states hold the named API calls open. Error states use a missing id or an injected HTTP 500. No state was faked in the page itself.
- **Also used:** the issues marked `ui-polish` and `caveat` from the browser walk of the four user journeys, and live Playwright probes for contrast, focus, overflow and role gating.
- **Where the screenshots are:** `docs/ui/before/<screen>--<state>--<desktop|mobile|desktop-dark>.png`. The script that takes them is `scripts/ui_screens.mjs` (copied from the audit's `shoot.mjs`), so the same set can be shot again into `docs/ui/after/`.
- **Not captured:** an Approvals card with a waiting challenger and a Missed runs list with rows (neither exists on any server, and making one would change engine state), the Phase 1 validate-before-run list (reaching it would start a real training run), Build from raw tables mid-build, the "no client yet" empty state (the API creates a `Demo` client the first time `/clients` is read), and downloaded files and PDFs.

The ten principles we measured against, referred to below as P1 to P10:

1. One primary action per screen. Secondary actions are quieter and destructive actions are set apart.
2. Progressive disclosure. Advanced settings, technical metrics, ids, hashes, raw JSON and logs sit behind a closed "Details" or "Advanced".
3. Navigation by what the user wants to do. At most 6 top-level items. Admin and ops only for the roles allowed to use them.
4. A feature that is unavailable here (AI service not connected, AWS not set up, self-learning off) is hidden or gets one calm notice. It never shows a broken or empty screen.
5. Plain language. No internal codes, milestone names or phase numbers. Every number has a label. Reuse the text in `configs/pilot/help.yaml`.
6. One page header and one spacing scale, type scale, card and set of button styles, with colours from the existing tokens. No one-off styles.
7. Every empty state is helpful and offers the one action that fills it.
8. Tables have fewer default columns (the rest behind "Show more columns"), a sensible default sort, and readable numbers (%, ₹, thousands separators, one locale).
9. Forms are grouped and pre-filled, validation appears next to its field, and there is never more than one screen of fields before "Advanced".
10. Works at mobile width and in dark mode, works from the keyboard, has a visible focus ring and enough contrast.

---

## 1. The owner's request: logo only on the right of the header

**Today.** `pageHead()` in `ui/dom.js` draws `<div class="headtools">${clientPicker}${LOGO}</div>` on every screen. The client chooser ("Client [Demo Telecom ▾]" with the hint "Tables and recipes are kept per client.") sits above the Minfy logo (`home--full--desktop.png`). That includes screens where the chooser means nothing or cannot work:

- **Sign in.** It shows a red "AUTH_REQUIRED Sign in to do this." box under the chooser and offers "+ New client" to a signed-out visitor (`signin--full--desktop.png`, `signin--error--desktop.png`).
- **Account, Users, Audit log, Privacy, Monitoring, Approvals, AI service.** None of these is filtered by client (journeys issue 3/5).
- **Mobile.** The chooser and its hint move above the page title (`home--full--mobile.png`).

**Change.**

- `pageHead()` draws the logo alone on the right, on every screen.
- The client chooser moves into the new top navigation bar (section 2), at its right end, as a compact "Client: Demo Telecom ▾".
- The hint becomes a "?" toggletip on the chooser: "Your uploaded tables and data set-ups are saved separately for each client."
- The ids `#f-client`, `#f-client-name`, `#f-client-add` and `#f-client-cancel`, and the option label "+ New client", stay the same, so the onboarding acceptance test keeps working.
- The chooser appears only on screens whose content depends on the client: use-case Setup, Build data, the uplift Setup and the Reports lists. It is not drawn on sign-in, account, admin, privacy, approvals or monitoring.
- `registerHeaderTool()` stays as the seam, but the top bar calls `headerToolHtml()`, not `pageHead()`.

---

## 2. Navigation (decided)

Today there are two unrelated strips above every page:

- `#pb-bar` (production, right-aligned) with "Access control is off · Everyone acts as the local operator, with every role. Users Audit log Privacy Monitoring Approvals", or "Signed in as … Viewer Admin … Change password Sign out".
- `#pe-bar` (pilot, left-aligned, one row lower) with "Demo: Demo Telecom, synthetic data · Pilot · Take the tour".

Below them is the header with the chooser, and under the header an injected "Uplift modelling ›" pill. On mobile the two strips take about 160 px before the page card starts (`userbar--full-admin--mobile.png`).

These are replaced by **one top bar**, `<header id="pb-bar" class="topbar" role="banner">` with `<nav aria-label="Main">`. The root keeps the id `pb-bar`, so the existing jsdom assertions `#pb-bar a[href=…]` keep passing.

### Top-level items (6, by goal)

| Item | Opens | What lives under it (existing routes, none removed) | Who sees it |
|---|---|---|---|
| **Home** | `#/` | The use-case catalogue per industry (`#/`, `#/industry/*`), plus a "For you" line with one next step per role | Everyone signed in (and everyone when sign-in is off) |
| **Build data** | `#/pilot/kit` (new sub-view of the pilot module; its content moves from the `#/pilot` card "1. Data request kit") | Asking the client for data (data request kit, templates, pre-flight command under "For your IT team"), a "Build a dataset from raw tables" list of predictive use cases linking to each one's Setup, data readiness reports (`#/pilot/view/readiness/*`) | Everyone. Upload and build controls stay disabled and explained for a Viewer |
| **Models** | Menu | Use-case Setup, runs and the Data/Model/Output pages of training runs (`#/uc/<uc>`, `/run/*`, `/data/*`, `/model/*`); Uplift models (`#/uplift`, `#/uplift/<uc>`, uplift run/model pages); Waiting for approval (`#/approvals`, with a count badge for Approvers); Model health: Alerts, Schedules, Missed runs (`#/monitoring/alerts`, `#/monitoring/schedules[/<id>]`, `#/monitoring/missed`); AI assistant and root-cause notes (`#/generative/assistant/*`, `#/generative/rca/*`) | Everyone. "Waiting for approval" is shown to roles that can read `/approvals` |
| **Campaigns** | `#/monitoring/runs`, retitled "Campaigns": every scoring run with its result status (No results yet / Results on <date> / Measured) | Who to contact (`#/uc/<uc>/output/<score run>`, `#/uplift/<uc>/output/<score run>`), campaign results (`#/campaign/*`), recording outcomes (`#/monitoring/runs/<id>`), value in rupees (`#/pilot/value/*`), campaign copy (`#/generative/copy/*`) | Everyone. Upload controls are Analyst only |
| **Reports** | `#/pilot`, retitled "Reports" | Model results reports (`#/pilot/view/results/*`), campaign value reports and PDFs. Readiness reports are listed under Build data | Everyone |
| **Admin** | Menu | Users (`#/admin/users`), Audit log (`#/admin/audit`), Privacy: consent, erasure, access requests, retention (`#/privacy/*`), AI service connection (`#/generative/connection`), Export feedback (the `/pilot/feedback/export` download), and the sign-in status notice | Only when `can("GET", "/users")` is true: Admin, or everyone when sign-in is off. Hidden entirely for Viewer, Analyst and Approver |

The Models menu also offers "Train or score a model: choose a use case on Home" as its first entry. The active item follows the route. `#/uc/*` highlights Models, except the Output page of a scoring run, which highlights Campaigns.

### Right side of the bar (utilities, not nav items)

- **Client: Demo Telecom ▾.** Only on per-client routes (section 1). "+ New client" is offered only to roles that can `POST /clients`. "Demo Telecom (broken extract)" is listed last as "Demo Telecom: practice data with problems". The default is the demo manifest's clean client when nothing is remembered.
- **Sample data chip.** Shown in demo mode (keeps class `.pe-demo`, text "Sample data: Demo Telecom"). It replaces the yellow demo badge row.
- **Help ▾.** "Take the tour" (only when `demo_mode && seeded`) and "Send feedback" (keeps id `#pe-fb-btn` and opens the same panel, anchored under the bar). The floating Feedback button goes away.
- **User menu.** Shows the display name and one role label (the highest role, never the implied "Viewer" next to it). Under it: Change password, then Sign out (`#pb-signout`) at the bottom, set apart. Signed out, this is just "Sign in".
- **Sign-in off.** Instead of a user menu, a quiet neutral chip "Sign-in off". Clicking it shows: "Access control is off: everyone has every role. An administrator turns sign-in on." Only the misconfigured state ("Sign-in is not configured") stays a visible warning.
- **Signed out.** The bar shows the wordmark and "Sign in" only. There are no nav items, client chooser, tour or feedback.
- **Mobile (below 700 px).** One 56 px row: wordmark, client chooser, "Menu" button. The menu panel lists the six items, Help and the user menu.

### What leaves the page body

- The injected "Uplift modelling ›" / "Uplift for this use case ›" pill becomes a quiet related link in the use case's own header actions ("Also: target with uplift ›"). It is never offered on generative, notice, planned or error screens (the prototype's `upliftOffered()` rule).
- "Campaign results for this run" becomes the fourth flow block on a scoring run's results.
- Every "‹ Customer Lifecycle" back link becomes the breadcrumb "Home › <journey> › <screen>".

---

## 3. Design rules

All of these come from the tokens already in `ui/index.html` `:root` and its two dark blocks. No new colour values. The rules below that need them add alias tokens that point at existing ones.

**Tokens and colour**

- Text uses `--ink` (primary), `--ink2` (table cells, secondary) and `--muted` (hints, captions, labels) only.
- `--faint` (#9CA3AF, 2.5:1 on white) is for borders, arrows, icons and disabled chrome. It is never for text a user must read. This moves `.hint`, `.chint`, `.next`, `.note`, `.caption`, `.legend .lbl`, `.stage-d .sc`, `.advisory-note` and `.progress .pt` (pending) to `--muted`.
- Status colours are `--ok`/`--ok-t`, `--warn`/`--warn-t` and `--bad`/`--bad-t`, and they only mean status. A warning is never shown in `--ok` green: today the "1 warning" run detail and the failed RANDOMNESS pill are both green.
- AI-type colours (`--p`, `--g`, `--h`) only code the AI type (card border, type chip). The Hybrid chip text moves from `--h` to `--ink2` on `--h-t` (it is 3.36:1 today).
- New alias tokens: `--btn` and `--btn-ink`. In light mode they are `var(--ink)` and `var(--surface)` (the current `.run` look). In both dark blocks they are `var(--p)` and `var(--stage-ink)`, about 6:1. This replaces the light `--stage` slab and the `#fff` on `#6E9BEA` (2.79:1) of `.pe-btn.primary`. `.tab.on` and `.stage-pill` in dark mode use `--soft` with an `--ink` label and a 3 px `--c` top border.
- Declare `color-scheme: light` on `:root` and `color-scheme: dark` in both dark blocks, so native file inputs, checkboxes, dates and selects follow the theme.

**Spacing**

- The scale is 4, 8, 12, 16, 24, 32, 48 px, and nothing else.
- Page padding stays `.screen` (44/64 px desktop, 28/20 px below 900 px) and gains `padding-bottom: 96px`, so no fixed element covers the last row.
- Card inner padding is 16 px vertical and 20 px horizontal (as `.card h3` and `.kv` are today).
- The gap between sections is 24 px (`.stack`). The gap between fields is 12 px (`.frow`), and 8 px between a label and its control.

**Type scale** (Inter, as today)

| Use | Size and weight |
|---|---|
| H1 | 28 px / 700 (`.h1`) |
| Verdict line at the top of a results screen | 20 px / 600 |
| KPI value | 22 px / 600 (`.kpi .v`) |
| Page description | 15 px / 400, `--muted` |
| Card title and body | 14 px (title 600) |
| Controls, tables, buttons | 13 px |
| Labels, breadcrumbs, captions, pills | 12 px |

Nothing smaller than 12 px, so the 11 px eyebrows ("DATA", "CAMPAIGN RESULTS", "FOR INFORMATION") are removed. Numbers in tables and KPIs use `font-feature-settings: "tnum"`.

**Page header** (one pattern on every screen)

```
[breadcrumb: Home › Customer lifecycle › Telco Customer Churn › Model]            [logo]
H1 (the thing on screen, e.g. "Telco Customer Churn")
Description: one sentence, --muted, max-width 640px
[at most one context chip]   [primary button] [secondary] [quiet related link]
────────────────────────────── rule ──────────────────────────────
```

- `pageHead(inner)` keeps its signature and always ends with `LOGO` alone.
- New helpers in `dom.js`: `crumbs(items)` (the root is always "Home") and `headActions({primary, secondary, related})`.
- The run-page tabs (Data · Model · Output · Campaign results) sit under the rule without the "01/02/03" numbers. The run id and time move out of the tab bar into Details.

**Cards**

- Use `.card` only, with `.card h3` as the title, `.kv` for key-value rows and `.tbl-wrap` for tables.
- Delete the module copies: `.pe-card`, `.pb-card` and `.gcard`-style duplicates.
- A calm notice is `.card.notice-card` (DEC-954 pattern) with a title, one or two sentences and one button.

**Buttons** (one set in `index.html`; `.run` stays as an alias of the primary for the tests and `PROTOTYPE_RULES`)

| Class | Look | Use |
|---|---|---|
| `.btn` | 40 px high, 8 px radius, `0 20px` padding, 13 px/600 | Base for every button |
| `.btn.primary` | `--btn` fill, `--btn-ink` text | At most one per screen |
| `.btn.secondary` | `--surface` fill, 1 px `--line2` border, `--ink` text | Every other button |
| `.btn.quiet` | No border, `--brand-blue` text, underline on hover | Replaces `.linkbtn`, `.again`, `.cancel`, "Run again / change settings" and underlined text buttons |
| `.btn.danger` | `--surface` fill, 1 px `--bad` border, `--bad` text; filled `--bad` only on the confirm step | Destructive actions |
| `.btn.sm` | 32 px high | Row actions |

- A destructive action always sits at the far end, after a `.spacer`, and always has a two-step confirm. The pattern already exists in `schedules.js`: "Delete it and its history? Yes, delete / Keep".
- Downloads are `.btn.secondary` with a leading icon, or `.btn.primary` when the download is the screen's main job (a contact list, a report PDF).
- A disabled button keeps full contrast (`--soft` fill, `--muted` text, not `opacity:.45`) and shows its reason next to it in `.reason`.

**Focus and keyboard**

- One ring: `outline: 2px solid var(--brand-blue); outline-offset: 2px` on `:focus-visible` for `a`, `button`, `select`, `input`, `textarea`, `summary` and `[tabindex]`. This removes `outline:none` in `.uform` and `.clientpick input`.
- Every toggle (menus, Help, feedback, tour, help "?") has `aria-expanded`, closes on Escape and returns focus to its trigger.
- Hit targets are at least 24 x 24 px (the help "?" keeps an 18 px glyph with a 24 px box).
- Add a "Skip to content" link. Remove `aria-live="polite"` from `#app` and add one `#status` live region for short status messages.

**Tables**

- At most 5 default columns. The others go behind a "Show more columns" toggle (`.tbl-more`, a `details` that reveals the hidden `th`/`td` using a class on the table).
- The first column is the human name (file, customer, use case, person), never an id.
- Every table has a default sort, stated in the card title ("newest first", "highest risk first").
- Numbers are right-aligned with `tnum`.
- Below 700 px each row becomes a stacked card (`.tbl-stack`: each cell shows its `data-label`), with no sideways scroll and no clipped key column.

**Numbers and dates** (one module-level constant in `dom.js`)

- `NUMBER_LOCALE = "en-US"` for every count and rate, so digit grouping matches the engine-written sentences ("24,000"). This replaces the `en-IN` in `fmtInt`, `pilot/screen.js` and `uplift/charts.js`.
- Money comes from `fmtMoney(n)`, built with `Intl.NumberFormat(NUMBER_LOCALE, {style: "currency", currency: "INR", maximumFractionDigits: 0})`, so no literal "₹" appears in source (`SAMPLE_VALUES`).
- Dates keep `en-IN` ("24 Sept 2026, 05:54 am") through `DATE_LOCALE`, and show the zone when it is not the browser's.
- Percentages have at most 1 decimal. Metrics have at most 3. Counts of people are whole numbers, with fractions only inside Details.
- `fmtN`'s "22K" is not used where a user compares numbers.

**Forms**

- Group fields under `fieldset`/`legend` headings. Pre-fill every default.
- Each label sits above its control, with a helper line under it in `--muted`.
- Validation text sits under its field in `--bad`, linked with `aria-describedby`, never in a status line far away.
- One screen of fields at most, then `details.adv` "Advanced".
- A select always uses `.control.sel`. The native `.pb-input` select, the bare `.pe-fb select` and `.ptype select` go.
- File uploads use `.control.file`.

**Empty, error and loading states** (shared helpers in `dom.js`)

- `emptyState({title, text, action})`: a sentence saying what will appear here, plus the one button that fills it ("Train a model", "Add your raw tables", "New schedule").
- `errorBox(error)` keeps its name and the `.apierr` class. It leads with a plain sentence: the `help.yaml` `codes.<CODE>.title` when the code is catalogued, otherwise "Something went wrong while loading this page." It adds a "Try again" button where a reload exists, and puts the raw code and server message inside a closed `<details>` "Details". Tests read `.apierr` text, which still includes the closed Details.
- `notFound(what, back)`: "This run could not be found. It may have been deleted, or the link is incomplete." plus one back button.
- `skeleton(kind)`: grey `--track` blocks in the shape of the page (header, 4 KPI tiles, 2 cards), painted at once. It is never blank and never leaves the previous screen showing.
- "This run has not produced `<file>`.json yet." is replaced everywhere by a sentence about when the value will exist ("Measured on the training run", "Available after scoring"). A tile or card that cannot apply to the run's mode is hidden. The em dash stays for one missing cell only.

**Technical detail**

- Ids, hashes, fingerprints, request ids, raw codes, lineage, hyperparameters and engine versions go inside `<details class="tech"><summary>Technical details</summary>`, in monospace, each with a copy button.
- Because they stay in the DOM, text-based tests still find them.

---

## 4. Cross-cutting findings

**C1. Two top strips, a header tool and an injected pill before any content.** Covered by sections 1 and 2. Screenshots: `home--full--desktop.png`, `home--full--mobile.png`, `userbar--full-admin--desktop.png`, `signin--error--mobile.png`.

**C2. Raw codes lead every error.** For example:

- "HTTP_500 The API answered 500."
- "RUN_NOT_FOUND No run with id 'r_missing'."
- "USE_CASE_PLANNED This use case is not available yet." under "This screen could not be loaded"
- "AUTH_REQUIRED", "MISMATCH", "ROLE_REQUIRED", "SCHEDULE_NOT_FOUND"

None of them offers a way forward. Change: the `errorBox`, `notFound` and "Try again" rules in section 3. Screenshots: `home--error--desktop.png`, `usecase-results--error--desktop.png`, `usecase-planned--error--desktop.png`, `admin-users--error--desktop.png`.

**C3. Loading shows nothing or leaves the previous screen.**

- `usecase-setup-train--loading` and `usecase-results--loading` are blank below the bars, because `showUseCase` awaits `GET /use-cases` before it paints.
- `#/pilot` keeps the Overview on screen until four requests settle.
- The Schedules loading state shows a free-text "Use case id" input that turns into a select.

Change: one skeleton pattern, painted first. Screenshots: `usecase-results--loading--desktop.png`, `pilot--loading--desktop.png`, `monitoring-schedules--loading--desktop.png`.

**C4. Internal ids as headline text.** Examples:

- `ds_20260924T055351999370 (built)` as the Data block title
- `r_20260924_60ed6916` in tab bars
- `src_…`, `map_…`, `spec_…` and `sha256:v1:…` in lineage
- `u-5385701cf7954e8f` in Users, Audit and Approvals
- `c_demo_telecom_1` in Schedules and Alerts
- `er_…`, `ret_…` and 64-character plan hashes in Privacy

Change: show names and dates; put ids in "Technical details" (P2, P5).

**C5. Internal codes as pills.** `TREATMENT_NOT_RANDOM`, `SUPPRESSION_COLUMN_MISSING`, `PII_DETECTED`, `MAPPING_LOW_CONFIDENCE`, `NO_ENTITY_SOURCE`, `SCHEMA_MISMATCH` and `THRESHOLD_FALLBACK` are shown as they are. Change: show the `help.yaml` `codes.<CODE>.title` as the heading, with its meaning and fix, and keep the code in a `data-code` attribute. `pilot/help.js` must match on `data-code` as well as on pill text, or every "?" disappears.

**C6. Jargon without explanation.** ROC-AUC, PR-AUC, AUUC, Qini, PSI, p-value, CI, ATE, champion/challenger, primary key, target, entity, snapshot, recipe, AutoML, SHAP, calibration, tombstone, principal, LLM, "Hybrid ★ + ★★". `help.yaml` already has plain `terms`, `metrics.*.name`, `settings.*.meaning` and `codes.*`. Use them as visible labels and "?" toggletips, not new copy.

**C7. Buttons.** `.run` (dark) is the only real button. Every other action is 12 px underlined text, with destructive ones mixed in: Cancel, Remove, Disable, Delete, Reject, Reset to default, Erase. Each module also brings its own button, input and card CSS (`pe-*`, `pb-*`, `u-*`, `g*`). There are one-off `style=""` attributes in `usecase.js`, `steps.js` and in `production` and `generative` files (about 100 in total). Change: the button set, and the removal of module copies (section 3).

**C8. Contrast and focus.**

- `--faint` is used for readable text at 2.54:1 in light mode and 3.65:1 in dark.
- The dark-mode primary buttons are 2.79:1.
- The Hybrid chip is 3.36:1.
- Only `a`, `.run` and `.control` have a designed focus ring. The header select shows `outline:none`, and "Take the tour" uses the browser's 1 px ring.
- `#app` has `aria-live="polite"`, which re-announces the whole screen on every paint.

**C9. Mobile.**

- Tables scroll sideways and cut off the key column: Sources, Mapping, Users, Alerts, Audit, the groups table and the evaluation table.
- The Home timeline is 1190 px wide in a 350 px card with no cue.
- Advanced settings is 6,295 px tall inline.
- The fixed Feedback button covers content on almost every mobile shot (KPI tiles, checkboxes, table cells).

**C10. Dark mode.**

- `--stage` flips to #E5E7EB, so the primary button, the active tab and the stage pills become glaring light slabs.
- Report iframes stay white.
- Native controls render light (no `color-scheme`).

**C11. One number locale.** `fmtInt`, `pilot/screen.js` and `uplift/charts.js` use `en-IN` ("1,86,270"), while engine sentences use western grouping ("24,000"). There are fractional people ("280.9"), 4-decimal scores, and "22K" next to "22,000". Change: the section 3 rule.

**C12. The client chooser implies filtering that does not happen.** Previous runs, the champion shown, approvals, the Pilot lists and scoring defaults are not filtered by client (journeys 0/9, 3/5, and the Score-mode blocker). Change: filter client-side on `run.client_id`, which `GET /runs` already returns, where the list is per client. Otherwise label each row with its client. Hide the chooser where nothing is per client.

**C13. Role gating gaps.** A Viewer (verified on `:8766`) sees these enabled:

- the uplift upload (`#u-file`) and the campaign upload and measure (`#u-camp-file`, `#u-camp-run`)
- the ROI "Save values"
- "export all feedback"
- 12 "Remove" links and the rest of the onboarding panel (pick-files, confirm-role, set-role, delete-source, accept-all, save-mapping, preview, build, use-dataset)
- "+ New client"

Change: add these to `production/gate.js` `ACTION_CONTROLS`, which is UI only; the API already refuses. For a Viewer, replace a fully greyed form with one calm read-only notice, keeping the gated controls in a closed section. M46 says controls are disabled and explained, never hidden.

**C14. Two implementations of the same screen.**

- `usecase.js` `runningHtml` and uplift `views.js` `runningBody`, plus a second `groupStages`.
- `pages.js` `upliftModelPage` and `upliftOutputPage` next to `uplift/views.js` `modelPageHtml` and `outputPageHtml`. The uplift results' Data block leads into the Phase 1 shell titled "Win-back Propensity + GenAI Content".
- `runsCard` and `upliftRunsCard`.

Change: one component each. `#/uc/<uc>/{model,output}/<uplift run>` redirects to `#/uplift/…`.

**C15. Screens that point nowhere, or that nobody can find.**

- RCA notes (`#/generative/rca/*`) and Campaign copy (`#/generative/copy/*`) have no link anywhere.
- The AI service screen is linked only from a badge.
- The uplift index is reachable only from an injected pill.
- A shared deep link to a result lands on the Overview on a first visit, because the tour rewrites the hash.

Change: the navigation in section 2, entry buttons on the relevant Output pages through a `registerRunAction` seam (hidden when AI is unavailable), and a tour that is offered rather than forced.

**C16. Planned use cases look live.** Of the 22 catalogue cards, planned ones (Banking 2 of 4, the whole Ad-tech journey, E-commerce 2 of 3, Insurance 1 of 2) are styled like working cards and end in "USE_CASE_PLANNED". Change: a "Coming soon" tag and a muted card, and a calm notice when one is opened by URL.

**C17. The UI asks for files that a run never writes.**

- `decile_lift.json` on scoring runs (a 404 plus "has not produced decile_lift.json yet").
- On the empty server, all 53 audit events are failed `runs.artefact_download` calls caused by this.

Change: request artefacts by run mode (`PAGE_ARTEFACTS` is UI-owned).

**C18. The same campaign in two vocabularies.**

- The Campaign page says "Incremental conversions −120 / Treated / Control (held out)".
- The value view says "Customers kept 120 / Contacted / Control group (not contacted)".
- The tour promises "how many extra customers the campaign kept".

Change: read the existing `outcome_is_good` from `GET /pilot/roi/{run}` and use one vocabulary on both pages (journeys 2/0, 2/1).

**C19. Demo-state noise.** The demo identity appears three times (badge, chooser, the `#/pilot` line). "Demo Telecom (broken extract)" is the first option and the default on a fresh visit (journeys 1/10, 3/7). The tour opens by itself for every signed-in user and is offered even when there is no demo data. Change: the Sample data chip, the chooser order and default, and a tour offered only when `demo_mode && seeded`.

**C20. Developer references in visible text.**

- `docs/GENERATIVE.md`
- `MARKETING_AI_AUTH_MODE=local`
- `python -m scripts.create_user`
- `python -m scripts.preflight …`
- `aws configure --profile NAME`
- `decile_lift.json`
- `Engine Marketing AI 0.1.0`
- `presets medium_quality`

Change: move them into a closed "For administrators" or "For your IT team" section, or remove them where the screen has a real action instead.

**Needs a ruling (outside "UI only"):**

- The server-rendered report HTML in `engine/pilot/document.py` and `roi.py`: the "Minfy logo" and "Client logo" dashed placeholders, a doubled status label, "READY WITH WARNINGS" on a value callout, a lakh figure that loses its minus sign, and Indian grouping.
- "Client not recorded" and "Data period not recorded" on uplift reports.
- The label column included in the drift check ("Drifted · max PSI 13.816").
- `split.json` `positive_rate` 0.
- A Label source that is wrong for user uploads.

These are engine or report-template text. They are recorded here and not in the UI work packages.

---

## 5. Screen by screen

Screenshot names are `docs/ui/before/<id>--<state>--<viewport>.png`. "Change" lists what Part 3 will do, and each screen names its single primary action.

### 5.1 Chrome and Home

#### Shared chrome (every screen)
- **Clutters it.**
  - The two strips (`#pb-bar` right-aligned, `#pe-bar` left-aligned one row lower, with 12 px of negative margin between them), then the header with the chooser, the hint and the logo, then the rule, then the injected "Uplift modelling ›" pill row.
  - Two amber pills of equal weight: "Access control is off" and "Demo: Demo Telecom, synthetic data".
  - Links are styled four ways: underlined text, a pill, "‹ back" and "›" crumbs.
  - The fixed Feedback button covers the last content row, for example "Data retention 90 days" in `tour--full-step4--desktop.png`.
- **Duplicated.**
  - The demo identity appears three times.
  - The chooser is redrawn inside every header.
  - Back links differ on every screen. The production failure screen hard-codes "‹ Customer Lifecycle" even for Banking.
  - Buttons, inputs and cards are defined four times (`.run`, `.pe-btn`, `.pb-*`, `.u-*`).
- **Not understood.**
  - "Everyone acts as the local operator, with every role."
  - "Tables and recipes are kept per client." ("recipes", and the promise is not true, see C12)
  - "+ New client" as a select option.
  - "Pilot" and "Monitoring" as link names.
  - The tab title "telco-churn · Marketing AI" (a raw id).
- **Change.**
  - Sections 1 to 3.
  - `document.title` uses the use case's name.
  - Add a skip link and a `#status` live region.
  - Give `.screen` bottom padding.
- **Primary action.** None. The chrome uses quiet link and menu styles only, so each screen's own primary action stays the most prominent thing on it.

#### home: Overview (`#/`)
- **Clutters it.**
  - Six bands before the first card.
  - The AI type is shown three times per card: legend, stage chip and card border.
  - The stage pills are numbered "01" to "05" with "→" arrows. In dark mode they become the brightest blocks on the page.
  - The one instruction, "Select a use case to see its Data → Model → Output pipeline.", is `--faint` text at the bottom.
  - On mobile the timeline is 1190 px wide with a hidden sideways scroll, and Feedback covers cards.
  - In the loading state the header has no chooser, so the layout jumps.
- **Duplicated.**
  - The H1 "Marketing AI" repeats the product name, and the real subject, "Customer Lifecycle", is the grey subtitle.
  - The legend repeats the chips.
  - The industry select and the client select look alike on mobile.
  - The "Uplift modelling" pill here and "Uplift for this use case" on use cases are two routes into the same thing.
- **Not understood.**
  - "Lifecycle stage → AI use cases"
  - "★ Predictive AI / ★★ Generative AI / ★ + ★★ Hybrid"
  - "pipeline"
  - "RCA (Root Cause Analysis)"
  - "HTTP_500 The API answered 500."
  - The empty install looks identical to the demo, with no hint of where to start.
- **Change.**
  - Breadcrumb "Home". H1 = the journey label ("Customer lifecycle"). Description: "Pick what you want to predict or improve. Each card opens its setup."
  - "Industry: Telecom ▾" moves into the header row.
  - Keep the coloured card border. Replace the legend and the stage chips with a single "What do the colours mean?" toggletip.
  - The uplift pill moves to the Models menu.
  - Planned cards get a muted style and a "Coming soon" tag (from `u.status`).
  - In demo mode, the demo use case card gets a "Sample data ready" tag.
  - A "For you" line with one link per role: an Approver sees "N models waiting for approval", an Analyst sees "N open alerts".
  - Empty state: "No models yet. Choose a use case below and upload your data to train the first one."
  - Loading: skeleton stage columns.
  - Error: "We could not load the use cases." with a "Try again" button.
  - Below 700 px, the stages stack vertically with no arrows.
  - Dark mode: the stage pills use `--soft` with a coloured top border.
- **Primary action.** Open a use case (in demo mode, the highlighted "Telco Customer Churn").
- **Shots.** `home--full--desktop.png`, `home--full--mobile.png`, `home--full--desktop-dark.png`, `home--empty--desktop.png`, `home--loading--desktop.png`, `home--error--desktop.png`

#### home-industry: another industry (`#/industry/banking`)
- **Clutters it.** The same header clutter as Home. The uplift pill is missing here only, so the layout differs between industries.
- **Duplicated.** The same triple AI-type coding as Home.
- **Not understood.**
  - Planned cards ("Account Opening Assistant", "Primary Bank Attrition") look live and end in USE_CASE_PLANNED.
  - Ad-tech is a dead-end journey.
  - "Client Lifecycle" is shown while the chooser says "Demo Telecom".
- **Change.**
  - Same as Home, including "Coming soon".
  - Industries with nothing available sort last, with the suffix "(coming soon)".
  - When the client's industry differs, add a note: "Showing Banking use cases. Your client Demo Telecom is set up for Telecom."
- **Primary action.** Open an available use case.

#### tour: guided tour (demo, first visit)
- **Clutters it.**
  - A fixed card, bottom left, that highlights nothing. At step 4 it covers "Lift by score decile", and on mobile it covers the KPI tiles.
  - "Skip tour", "Back" and "Next" all have equal weight.
  - It opens by itself for every signed-in user, including an Admin.
- **Duplicated.** The "Take the tour" link plus the auto-start. Step 6 repeats the Pilot description.
- **Not understood.**
  - Step 2 says "click Run", but the button is "Run training".
  - Step 4 lands on "Drifted · max PSI 13.816" and "Lift (top decile) —".
  - Step 5 promises "how many extra customers the campaign kept" on an inconclusive, −120 result.
  - Without demo data, steps 2 to 5 say "Open a use case with a finished run".
- **Change.**
  - Offer the tour only when `demo_mode && seeded`, from the Help menu, or as a one-line "New here? Take a 2-minute tour" offer (Start / No thanks).
  - Button order: "Next" is primary, "Back" secondary, "Skip tour" a quiet link. Add progress dots.
  - Escape closes the tour.
  - Outline each step's target region and place the card away from it. On mobile, show it as a bottom sheet.
  - Text fixes: "click Run training". Step 5 becomes "whether the campaign made a measurable difference, with a range".
  - Never override a deep link.
- **Primary action.** Next (Finish on step 6).
- **Shots.** `tour--full--desktop.png`, `tour--full-step4--desktop.png`, `tour--full--mobile.png`

#### help-popover: "What does this mean?"
- **Clutters it.**
  - 45 "?" buttons appear once Advanced is open, several alone on their own line.
  - The popover covers the next field (for example "Outliers").
  - It is `position:fixed`, so it detaches from its "?" when the page scrolls.
  - On mobile it covers the form at full width.
- **Duplicated.** The aria-label repeats the code title. That is fine.
- **Not understood.** The popover has no title naming the setting. The target is 18 x 18 px.
- **Change.**
  - Put the "?" inline after the last word of the label, in a nowrap span, with a 24 px hit area.
  - Make it a toggletip: `aria-expanded`, a title line (the setting label or `codes[code].title`) and a "×" close.
  - Position it in document coordinates, below the label. On mobile, use a bottom sheet.
  - Text from `help.yaml` only.
- **Primary action.** None. Read and close.

#### feedback: Feedback dialog
- **Clutters it.** The floating pill covers content on every screen, and the panel opens over the page.
- **Duplicated.** The native select (black focus ring on desktop, orange on mobile) and the `.pe-btn` buttons duplicate the shared ones.
- **Not understood.** Nothing major.
- **Change.**
  - The trigger moves into Help ▸ "Send feedback" (keeps `#pe-fb-btn`) and opens an anchored dialog under the bar.
  - Use the shared `.control.sel` and buttons. "Send" is primary, "Cancel" is quiet.
  - Escape closes it and focus returns to the trigger.
  - Keep the text "Thank you, it was recorded." and auto-close after 2 s.
  - The hint becomes "Please don't include customer details."
- **Primary action.** Send.

#### ai-notice: AI service not connected (`#/uc/ai-onboarding-assistant`)
- **Clutters it.** An "Uplift for this use case ›" pill above a chat-assistant notice. A 720 px card with no action.
- **Duplicated.** The same notice appears on the assistant, RCA and copy screens. That is fine.
- **Not understood.** "(see docs/GENERATIVE.md)". It gives no way to get the service connected. The Home card gives no warning.
- **Change.**
  - Title: "AI writing is switched off in this demo".
  - An Admin gets the primary button "Connect an AI service" (`#/generative/connection`). Others get "Ask your administrator to connect an AI service." and "Back to Customer lifecycle".
  - No uplift link on generative screens.
  - A "Needs AI service" tag on the Home card.
- **Primary action.** Back to the journey (Admin: Connect an AI service).

#### usecase-planned: a planned use case opened by URL (`#/uc/criteo-uplift`)
- **Clutters it.** A red failure box "USE_CASE_PLANNED" under "This screen could not be loaded", with an uplift pill above it.
- **Duplicated.** It looks exactly like a real 500 error.
- **Not understood.** The code, "could not be loaded", and the fact that the use case is never named.
- **Change.**
  - In `app.js` `failure()`, `USE_CASE_PLANNED` renders the calm notice card instead.
  - H1 = the card's name from the cached `/industries` payload.
  - Body: "Coming soon. This use case is planned but not available yet. The other use cases in this journey work today."
  - No code and no uplift link.
- **Primary action.** Back to <journey>.
- **Shots.** `usecase-planned--error--desktop.png`

### 5.2 Setup and Build data

#### usecase-setup-train: Setup, Train mode (`#/uc/<uc>`)
- **Clutters it.**
  - Six strips above Step 1: access bar, demo bar, breadcrumb, title, chips, uplift pill.
  - Three numbering systems on one screen: Setup 1 to 3, Advanced 1 to 8, raw panel 1 to 4 (and build progress 1 to 7).
  - Locked Steps 2 and 3 are drawn at `opacity:.4` before anything is uploaded.
  - Previous runs lists every client's runs with raw dataset ids, and its right column is clipped ("LightGE", "ROC-AUC 0.955", "Classification (yes / n").
  - The loading state is blank.
- **Duplicated.**
  - The dataset hint appears twice, word for word.
  - The "Selected" tag repeats the card's fill.
  - The mode help repeats Step 1.
  - Previous runs appears on Setup, Running and Results.
- **Not understood.**
  - "Primary key", "Target column", "feature"
  - "AutoML (recommended)", "Each AutoML stage can be configured"
  - "ds_… (built)", "train · Classification (yes / no)", a bare "ROC-AUC 0.9554"
  - "Stage Churn", "★ Predictive AI", "Uplift"
  - "HTTP_500 The API answered 500."
  - A Viewer sees two reasons side by side ("Only an Analyst can start a run. Upload a dataset to continue").
- **Change.**
  - The header follows the one pattern: the chooser moves to the top bar, the uplift link becomes a quiet related link, and there is one context chip.
  - Before a file is chosen, show only Step 1. Steps 2 and 3 become one-line placeholders ("2 Columns: after upload").
  - Drop the duplicate hint and the "Selected" tag.
  - Plain labels, in `configs/engine.yaml` `ui:` copy only: "Customer ID column", "What to predict (outcome column)", "Best model, picked automatically (recommended)". Keep "Run training", "Score data" and "Score new data".
  - Numbers only on the top-level Setup steps.
  - Previous runs:
    - filter by `run.client_id`, or label each row with its client;
    - show "Built dataset · 22,000 rows" with the id in Details;
    - label the metric with the `help.yaml` `metrics.roc_auc` name;
    - let the right column wrap;
    - show 5 rows and "Show all runs".
  - Paint "Loading Telco Customer Churn…" and a skeleton before awaiting.
  - Error: a plain sentence and "Try again", with no uplift link and no chooser.
  - Viewer: one calm card, "You can view this use case's runs and results. Training and scoring need the Analyst role." The form stays in a closed section and Previous runs is shown at full width.
  - `--faint` text becomes `--muted`.
- **Primary action.** Run training.
- **Shots.** `usecase-setup-train--full--desktop.png`, `--full--mobile.png`, `--loading--desktop.png`, `--error--desktop.png`, `usecase-viewer--full--desktop.png`

#### usecase-setup-file: after a prepared file is uploaded
- **Clutters it.**
  - The file is previewed twice: a table and monospace column chips.
  - An unlabelled "483.7 KB".
  - The problem-type row mixes a pill, a sentence, a "change…" link-styled select and a stray chevron.
  - In dark mode the primary button is a light slab.
- **Duplicated.** Column names appear in the table and again as chips. The problem type appears here, in Previous runs, on Results and on the Data page.
- **Not understood.**
  - "Primary key", "Target column", "Problem type … detected from the target column change…"
  - "+11 more"
  - "Metrics and models will switch to regression defaults."
  - A composite key is auto-filled with a float column (journeys 3/2).
- **Change.**
  - One preview: at most 6 columns, then "Show all 21 columns". The chips move into that disclosure.
  - Label the size ("File size") or move it to Details.
  - A confirmation line after the pre-fill: "We matched Customer ID → customerID and Outcome → Churn." with a quiet "Change" that reveals the selects.
  - "Predicting: Yes / No outcome" as plain text. "change…" becomes a labelled select in Advanced, keeping its warning.
  - `appearance:none` on the select.
  - Viewers get no "change…".
  - The dark-mode primary uses `--btn`.
- **Primary action.** Run training.

#### usecase-setup-advanced: Advanced settings (8 stages, 45 fields)
- **Clutters it.**
  - About 3,000 px tall on desktop and 6,295 px on mobile.
  - Stage 3 is six disabled "Coming later" controls, and Retraining is disabled too.
  - Stray "?" buttons float above checkboxes (`help.js` appends to the empty `&nbsp;` `.sub`).
  - An empty "Exclude columns from features".
  - Technical summary lines ("Balanced · 4 of 4 algorithms · ensembling · 50 trials · 30 min · 5-fold CV").
  - "Configure" in `--faint`, and it is not a button.
- **Duplicated.** The Stage 3 summary repeats the "Coming later" note six times. Stage numbers compete with the Setup steps. Governance settings repeat the Admin policy screens.
- **Not understood.** PII handling, outliers clip, leakage, stratified split, CV folds, tuning trials, ensembling, calibration, decision threshold, SHAP, fairness, PSI, "Replace champion if better by (%)", and a 0 to 1 score scale that is never explained.
- **Change.**
  - Advisory-only stages and fields move behind one toggle at the foot: "Show settings planned for a later release (7)". This is schema-driven (`field.advisory`), so nothing names a setting.
  - A folded stage summary reads "Using recommended settings" or "You changed 2 settings" (compared with `field.default`). The technical summary becomes a caption inside the open stage. Keep the data_split "Time-based … by snapshot_date" sentence visible.
  - One stage open at a time. "Configure" becomes a real disclosure with a chevron.
  - Put the "?" inside the `.check` label for checkboxes.
  - Hide "Exclude columns" until a file is loaded.
  - Show the `help.yaml` meaning as a visible caption on the 3 to 5 most-used fields. Score thresholds say "score from 0 to 1".
  - On mobile, Advanced opens as a full-height sheet with sticky stage headers.
- **Primary action.** Run training. Nothing inside Advanced should look like a required step.
- **Shots.** `usecase-setup-advanced--full--desktop.png`, `--full--mobile.png`

#### usecase-setup-score: Setup, Score new data mode
- **Clutters it.**
  - The model option packs four facts into one line: "LightGBM · ROC-AUC 0.9554 · 24 Sept 2026, … · Champion".
  - Step 3 is locked (`pointer-events:none`) until data is uploaded. That is the journeys blocker: a new client starts on another client's champion.
  - Empty state: "No trained model yet" next to "Upload a dataset to continue" (the wrong next step).
  - A stale SCHEMA_MISMATCH box stays after the model changes.
- **Duplicated.** The model is described in the select, in Previous runs and on the Results summary.
- **Not understood.** "Primary key", "LightGBM · ROC-AUC · Champion", "recipe", "SCHEMA_MISMATCH … The template for this use case lists them."
- **Change.**
  - The model choice becomes Step 1 and is always usable. It defaults to the chosen client's newest champion (filtered client-side).
  - With no model, one card: "No trained model yet. Train a model on past data first, then come back to score new customers." with the button "Train a model". `blocker()` checks for a model before data.
  - Option text: "<model> · trained 24 Sep 2026 · in use".
  - Clear `s.validation` when `#f-scorerun` changes.
  - SCHEMA_MISMATCH shows its `help.yaml` title, meaning and fix, with the code in Details.
- **Primary action.** Score data.
- **Shots.** `usecase-setup-score--full--desktop.png`, `usecase-setup-score--empty--desktop.png`

#### client-new: "+ New client" form
- **Clutters it.** The form replaces the select inside the header: an input with no placeholder and underlined "Add" and "Cancel". Errors are squeezed into the header column, and on mobile the logo drops to its own row.
- **Duplicated.** The chooser on every screen. The fixture client "Demo Telecom (broken extract)" is first and the default.
- **Not understood.**
  - "+ New client" as an option.
  - "recipes".
  - A fresh install silently gets a client named "Demo".
  - "Add" with an empty name does nothing, and Enter does not submit.
  - A Viewer is offered "+ New client".
- **Change.**
  - It lives in the top-bar chooser. "+ New client" (kept as the option label) opens a small popover `<form>` anchored to the chooser.
  - The field is "Client name" with the placeholder "e.g. Acme Broadband". "Add client" is primary and "Cancel" is quiet.
  - Enter submits and Escape cancels. "Enter a name for the client" appears under the field. API errors appear inside the popover.
  - Hidden for roles without `POST /clients`.
  - Fixture clients are listed last and labelled.
  - First run: a one-time tooltip, "We created a first client called Demo; rename or add your own."
- **Primary action.** Add client.

#### build-raw-1-sources: Build from raw tables, Sources
- **Clutters it.**
  - A 7-column table in which the Role select is an empty 40 px box ("A..", "U..", or blank).
  - A "Remove" link on every row with no confirmation, plus "Confirm" links.
  - A wall-of-text role glossary.
  - The demo client lists 12 sources: this month's and next month's tables together, with no grouping and no duplicate warning.
  - The step stays open after it is done, so the page reaches 9,259 px (desktop) and 12,516 px (mobile).
  - The greyed parent Steps 2 and 3 show below the panel.
- **Duplicated.** The panel header repeats the card above it. "Configure" vs "✓ Done" reuses the Advanced component. Four levels of step numbering.
- **Not understood.**
  - "Key candidate", "Time candidate", "Coverage 100%"
  - "Entity", "Other Event"
  - "One source must be set to the entity role…"
  - "1,90,932"
- **Change.**
  - Default columns: File, Rows, Role. The role shows as readable text ("Customer table") with "Change". Key, time and coverage (as "Customers matched") go behind "Show more columns". The Role select is wide enough to read.
  - One primary "Confirm roles" that confirms every proposed role (keeping per-row `confirm-role`).
  - "Remove" moves into a row menu with "Remove activity.csv from this client? This cannot be undone."
  - The glossary moves into a "?" on the Role header.
  - Empty state: "Add your raw tables: one CSV or Parquet file per table (customers, activity, bills…). We will suggest what each one is." with the upload as the only action.
  - Duplicate files get a calm note.
  - A finished step folds to "6 tables · roles confirmed ✓ · Edit".
  - Below 700 px, rows become stacked cards.
  - Viewer: controls gated (C13).
- **Primary action.** Confirm roles, then continue to Mapping.
- **Shots.** `build-raw-1-sources--full--desktop.png`, `--full--mobile.png`, `--empty--desktop.png`

#### build-raw-2-mapping: Mapping
- **Clutters it.**
  - 12 cards, each with "Accept all suggestions" and "Save mapping" (24 actions).
  - 6 columns per card, and a truncated "Our column" select ("Entity …", "N...").
  - Event-table options read "[object Object]" (because `steps.js` uses the `typical_columns` objects as names).
  - "[REDACTED], [REDACTED], …" samples, and 10-line free-text samples.
  - Nested value maps.
  - A stale NO_ENTITY_SOURCE error stays on every card after the customer table is saved.
- **Duplicated.** The same accept/save pair on every card. The CUST_ID → Entity key row on every card.
- **Not understood.** "Our column", "Confidence 60%", "Your choice", the type names, "Value map", "Keep as-is / Set null", "Not saved yet", the check codes, "event_time".
- **Change.**
  - Use `typical_columns[i].name` as the value and its description as the label.
  - One primary at the top, "Accept suggestions and save all". It stops on cards with a match below 85% and highlights "Check this match". A quieter per-card "Save" stays (keep `[data-act="accept-all"]`).
  - Cards fold to "complaints.csv · 6 columns · 5 matched · 1 needs your check".
  - Default columns: Your column, Maps to (full label), Example values (3, one line). Type, confidence (shown as "Sure"/"Check") and value map go behind "Show more columns".
  - "Our column" becomes "Maps to", "Set null" becomes "Leave blank", "Keep as-is" becomes "Keep".
  - Pills show their `help.yaml` title, with the code in `data-code`.
  - Hide NO_ENTITY_SOURCE once a customer-role source exists.
  - A column whose samples are all redacted shows "Hidden (personal data)".
  - On mobile, one card per column.
- **Primary action.** Accept suggestions and save all.
- **Shots.** `build-raw-2-mapping--full--desktop.png`, `--full--mobile.png`

#### build-raw-3-features: Features and label
- **Clutters it.**
  - Ten checkboxes that lead with snake_case names, with monospace window chips.
  - An inline 6-field "+ Add a feature" form.
  - The label sentence has no padding and its input is unstyled (`style="width:64px"`).
  - Three snapshot fields.
  - The Preview blocker lists all 12 file names.
  - Preview output: a row table, null-rate bars and a per-snapshot table.
- **Duplicated.** Features are listed here, in the build report and on the Data page. The blocker repeats Mapping's status.
- **Not understood.** "An entity counts as churn_next_60d if…", "Snapshots: Periodic", "Max snapshots", "bill_trend_3m_vs_6m", "Building a 200-entity sample…", "Positive rate per snapshot", "Function", "Window (days)".
- **Change.**
  - Lead each feature with its description, with the technical name as a muted caption and the window as a normal pill.
  - The list folds to "10 measures will be built from your tables ✓ Review". "Add a feature" opens a dialog.
  - Label sentence: "A customer counts as having left if they do nothing for [60] days after the prediction date." (using `uc.entity`), with `.control` styling and no inline style.
  - Snapshots becomes "Learn from the last 12 month-ends" with a "Change" link.
  - Blocker: "Save the mapping for 12 tables in step 2 first", linked to step 2.
  - Preview leads with one sentence ("Sample of 200 customers: 18.6% left within 60 days; 2 measures are often empty"). The rest goes behind "Show preview details".
- **Primary action.** Preview, then continue.

#### build-raw-4-build: Build and review
- **Clutters it.**
  - About 3,600 px after a build: a 7-row progress list, Sources & coverage (raw `src_…` ids, lowercase headers), 12 snapshot bars, Dropped features, the Future-data check and 5 full-width code pills.
  - "Use this dataset" is at the very bottom, and its only feedback is small grey text.
- **Duplicated.** Row counts appear three times. The stale heading "Warnings were found - review before building." shows after the build. The Future-data sentence repeats the counts.
- **Not understood.** "coverage 100%", "2024-12-31 (censored) 41.5%", "2 feature(s) built from usage.", "Full future-data check: …", the code pills, the alarming PII_DETECTED on customer ids, and "Dropped features / No features were dropped."
- **Change.**
  - A verdict first: "Dataset ready: 22,000 rows across 12 month-ends, 5 things to review", with the filled "Use this dataset" at the top.
  - Everything else goes behind "Show build details" (`[data-leak-check]` stays in the DOM with its text unchanged).
  - Warnings get `help.yaml` titles, messages and fixes, with the code in Details. The heading becomes "Things to review".
  - The coverage table shows file names and title-case headers.
  - Snapshots: "Share who left, by month-end" and "Left out: too recent to know the outcome".
  - After "Use this dataset", the panel folds to "Built dataset · 22,000 rows · Change" and scrolls to Step 2.
- **Primary action.** Build dataset, then "Use this dataset".
- **Shots.** `build-raw-4-build--full--desktop.png` (after a build: `journeys/c-raw-tables/11-train-build-report.png`)

#### build-raw-score: Score mode, "Upload this month's tables"
- **Clutters it.**
  - "Saved recipe spec_2c27bda196cc".
  - Four steps where the user makes no decision in three of them.
  - The locked model step below decides which recipe is replayed (the journeys blocker).
  - Two empty-table sentences.
  - The same warning list on every monthly build.
- **Duplicated.** The panel title is the same as in Train mode. The Features step repeats the recipe. The monthly build report duplicates the training one.
- **Not understood.** "recipe", "The selected model was trained on another client's tables. Choose that client in the header…" (the chooser is moving), "Narrow future-data check", and "No snapshots produced." right after "1 snapshot date(s)".
- **Change.**
  - Model first and always clickable, defaulting to this client's newest champion. If this client has none: "Demo Telecom has no trained model yet. Train one from this client's tables first." with "Train a model".
  - Refusals: "…switch client in the top bar (Client ▾)" and "This model was trained from a single prepared file. Score it with a prepared file instead."
  - "Using the setup from 24 Sep 2026", with the spec id in Details.
  - One visible step, "Add this month's files". After that, one summary line ("6 tables matched last month's setup ✓").
  - "Build this month's dataset" is the single primary.
  - Show only warnings that differ from training.
  - Hide an empty Snapshots card.
  - Keep "replayed exactly as it was", the role in the third cell, and "Scoring complete" / "N rows scored".
- **Primary action.** Score data (after Build this month's dataset).

### 5.3 Run pages and uplift

#### usecase-running: run in progress
- **Clutters it.**
  - The full use-case intro and uplift pill come before the progress card (progress starts at y≈540 on mobile).
  - Previous runs is clipped.
  - Placeholder rows 3 to 5 are 60 px each.
  - "Cancel" is an underlined link glued to "Running…".
  - Feedback covers rows 4 and 5 on mobile.
- **Duplicated.** `runningHtml` and `runningBody`, plus `groupStages`, exist twice.
- **Not understood.** "groups kept whole by entity_key", "15K train · 3K validation · 3K test", "1 warning" in green, "Evaluating on hold-out set", and "Champion" on a scoring row.
- **Change.**
  - "Training your model… This usually takes a few minutes. You can leave this page; the run keeps going."
  - Details in plain words with full counts ("15,000 / 3,000 / 3,000"). A warning is shown in `--warn`, and the technical detail goes behind Details.
  - "Cancel run" becomes a separated `.btn.danger.sm` with a confirm step (keeps `#f-cancel`).
  - Previous runs collapses to "Previous runs (2) ›" while running.
  - One shared Running component with uplift.
- **Primary action.** None while running. "Cancel run" is the only, quieter control.

#### usecase-results: training results
- **Clutters it.**
  - A 6-item summary strip that includes a stray lowercase "champion" and a raw dataset id.
  - The Data block's title is a 22 px raw id that breaks mid-id on mobile.
  - "AI pipeline", "01 DATA / 02 MODEL / 03 OUTPUT" and "✓ Done" on every block.
  - "Subscribers at risk: —".
  - Loading is blank. The error is a bare RUN_NOT_FOUND.
- **Duplicated.** The blocks repeat the page tabs. "ROC-AUC 0.9554" appears three times and the dataset id three times.
- **Not understood.**
  - "champion"
  - "Selected by AutoML"
  - "key entity_key + snapshot_date · target churn_next_60d"
  - The failed run: "Every candidate model failed…" followed by three "✓ Done" blocks.
- **Change.**
  - Headline: "✓ Model trained: LightGBM ranks customers correctly 96% of the time (ROC-AUC 0.955)". Then "Approved model", or "Waiting for an Approver: an Approver other than you must approve it", linked to `#/approvals`.
  - Map every version status to words.
  - Primary: "Score new customers with this model". Secondary quiet link: "Change settings and train again".
  - The Data block is titled with the file name or "Built dataset, 24 Sep 2026". The Output block says "Available after scoring".
  - Failed run: blocks show "Not completed", the error comes first, and "Try again" is the primary.
  - A skeleton while loading. A not-found state with "Back to Telco Customer Churn".
- **Primary action.** Score new customers with this model (on a failed run: Try again).
- **Shots.** `usecase-results--full--desktop.png`, `--loading--desktop.png`, `--error--desktop.png`, `--error-failed-run--desktop.png`

#### usecase-results-score: scoring results
- **Clutters it.** "Campaign results for this run ›" looks like a tertiary pill in the injected row. "Champion model · ROC-AUC —". A raw id title. No link to the customer list.
- **Duplicated.** "2,000 rows scored" in the summary vs "2K rows scored" in the block. The Champion badge twice.
- **Not understood.** "ROC-AUC —", "key entity_key + snapshot_date", "2K", "Subscribers at risk: 855" (it is High + Medium), and the fact that campaign results need outcomes uploaded later.
- **Change.**
  - "✓ 2,000 customers scored. 855 are at risk (High or Medium)."
  - Primary: "Download contact list (CSV)".
  - Secondary: "See who to contact" and "Measure campaign results".
  - Campaign results becomes the fourth flow block, "After the campaign: measure results", via a flow-block seam instead of DOM injection.
  - Model block: "Approved model: LightGBM (trained 24 Sep 2026)".
  - Full counts.
- **Primary action.** Download contact list (CSV).

#### run-data: Data page
- **Clutters it.**
  - A 5-column lineage wall of ids (about 400 px on desktop, 1,000 px on mobile).
  - Format, size, encoding and fingerprint.
  - 11 identical "Clip percentile <column>" rows.
  - 15 × 4 feature rows, clipped on mobile.
  - The "DATA" eyebrow and the run id in the tab bar.
  - The empty and error states have no action.
- **Duplicated.** Rows appear as 22K, 22,000 and "22,000 row(s)". The dataset id twice. The run id twice. Clip percentile twice.
- **Not understood.** "Class balance 0% positive" (backend bug, needs a ruling), "Train / val / test 70 / 15 / 15", "Leakage check Passed", "Dedupe subset all_columns…", "Fingerprint", "Label source Kaggle…", "Date range —", "Feature set (15 of 18 columns)", "role entity".
- **Change.**
  - A "Data at a glance" card with every number labelled: "22,000 customer-months from 2,000 customers · 15 details used to predict · 5.1% of values missing · passed the leakage check". Hide the date range when it is unknown.
  - Lineage behind "Details: data lineage", with file names first.
  - Format, size, encoding and fingerprint into Details.
  - Group pipeline steps ("Extreme values capped on 11 columns").
  - Feature table: Detail and Missing by default, sorted by missing descending. Type and transform behind "Show more columns".
  - Drop the eyebrow. The tab bar shows "Trained 24 Sep 2026".
  - Empty: "No finished run yet. Train a model to see where its data came from." with "Go to Setup".
  - A "Next: Model ›" button.
- **Primary action.** Next: Model › (empty state: Go to Setup).
- **Shots.** `run-data--full--desktop.png`, `--full--mobile.png`, `--empty--desktop.png`, `--error--desktop.png`

#### run-model: Model page
- **Clutters it.**
  - Four KPI tiles, two of them metric codes.
  - 11 technical Training setup rows ("53 leaves · lr 0.100026", "Engine Marketing AI 0.1.0").
  - A 5-metric table at 4 decimals.
  - 15 importance rows, 6 of them 0%, and not sorted.
  - A leaderboard where all 5 rows say "LightGBM".
  - A full card for "No sensitive column was configured."
  - The baseline column is clipped on mobile.
- **Duplicated.** LightGBM three times. ROC-AUC and PR-AUC in the tiles and the table. The threshold twice. Two different "trained" times. The `pages.js` `upliftModelPage` duplicate.
- **Not understood.** ROC-AUC, PR-AUC, F1, Recall, Precision, "baseline (logistic regression)" (which beats the model on 4 of 5 metrics, unflagged), hyperparameters, calibration, "THRESHOLD_FALLBACK", "permutation", the confusion-matrix labels, "presets medium_quality".
- **Change.**
  - A verdict card first, from `help.yaml` `metrics.roc_auc`: "It ranks a customer who will churn above one who won't 96% of the time (1 is perfect, 0.5 is a coin toss)." When the baseline beats the model, add a calm warning: "A simple yardstick model scored slightly higher (0.960 vs 0.955); consider retraining with more data."
  - Tiles: "Model", "Ranking quality 0.955", "Catches 80% of churners", "Trained 24 Sep 2026".
  - Training setup, the leaderboard, the confusion matrix, the engine and the hyperparameters go behind "Technical details".
  - "What drives the score": the top 8, sorted, with "Show all 15".
  - THRESHOLD_FALLBACK shows its `help.yaml` title.
  - Show the Fairness card only when fairness was evaluated.
  - One timestamp.
  - "Next: Output ›".
- **Primary action.** Next: Output › (on an approved model, a secondary "Score new customers").
- **Shots.** `run-model--full--desktop.png`, `--full--mobile.png`

#### run-output: Output of a scoring run
- **Clutters it.**
  - An empty "Lift by score decile" card saying "…has not produced decile_lift.json yet." and a "Lift (top decile) —" tile.
  - A 10-row "Actions & monitoring" config dump.
  - The sample table shows raw keys ("1000001|2025-03-30"), raw scores and "days_since_last_activity ↑ (240)".
  - The download is a 12 px link at the bottom.
  - The sample table is clipped to 2 columns on mobile.
- **Duplicated.** Bands appear twice. The control group twice. 855 = 851 + 4, and that is never said. The `pages.js` `upliftOutputPage` duplicate.
- **Not understood.** "decile_lift.json", "Score field churn_prob", "Drift alert PSI above 0.2", "Drifted · max PSI 13.816" (the label column is in the check; needs a ruling), "Retraining On drift" (not active yet, according to `help.yaml`), "Suppression opted out · contacted < 14d", "Control (hold out)".
- **Change.**
  - Primary "Download contact list (CSV)" at the top.
  - Tiles: "Customers to contact 855 (High 851 + Medium 4)", "Customers scored 2,000", "Held back to measure results 200 (10%)".
  - No lift card or tile on scoring runs, and stop requesting `decile_lift.json`.
  - "Bands & actions" becomes the main card.
  - "Settings used for this run" is closed and worded from `help.yaml`, with Retraining shown as "Not active yet".
  - Drift is explained with the DRIFT_DRIFTED title, PSI in Details.
  - "Sample of 10 customers": Customer, Churn risk %, Band, Main reason in words, Action.
  - Stacked cards on mobile.
  - Empty: "No scoring run yet. Score new customers to get a contact list." with "Go to Setup".
- **Primary action.** Download contact list (CSV).
- **Shots.** `run-output--full--desktop.png`, `--full--mobile.png`, `--empty--desktop.png`

#### run-output-train: Output of a training run
- **Clutters it.** Three dash tiles. "Reasons —" and "Drift measured —". Every decile bar labelled, and the chart clipped after D7 on mobile. A no-button sentence about switching mode.
- **Duplicated.** "Lift (top decile) 5.04x" appears in the tile and on the D1 bar. The settings card is identical to the one on the scoring page.
- **Not understood.** "5.04x", "19.8485%", "D1…D10", "PSI".
- **Change.**
  - One tile: "The top 10% of scores contain 5× as many churners as average" (`help.yaml` `terms.lift`/`decile`). The dash tiles become one line: "Customer counts appear after you score new data."
  - Primary: "Score new customers with this model".
  - Label D1 to D3 only, 1 decimal, and fit the chart in 390 px.
  - Settings card collapsed, without the rows that cannot apply.
- **Primary action.** Score new customers with this model.

#### uplift-index: Uplift modelling list (`#/uplift`)
- **Clutters it.** A flat list of 8 use cases, including the chat assistant, with no status. The hint comes after the list. A dense intro. Feedback covers the last row on mobile.
- **Duplicated.** A second catalogue next to Home. The uplift explanation appears in five places.
- **Not understood.** "Uplift modelling", the "persuadable … would convert anyway … react badly" paragraph, "HTTP_500". The back link is hard-coded to "Customer Lifecycle".
- **Change.**
  - Title: "Measure what a campaign changes (uplift)".
  - A 2-line intro, with the hint above the list.
  - Rows show a status ("Uplift model trained 24 Sep" / "No uplift model yet").
  - Generative use cases are hidden or greyed.
  - Reached from Models.
  - The breadcrumb is built from the journey.
  - The error gets "Try again". Loading shows skeleton rows.
  - The pinned prototype sentences change together with the prototype (WP9).
- **Primary action.** Choose a use case.

#### uplift-setup, uplift-setup-score, uplift-setup-file: Uplift Setup
- **Clutters it.**
  - Three chips ("Stage Win-back", "★ + ★★ Hybrid", "Uplift").
  - The subtitle is repeated in the card help.
  - Locked step 2 shows three disabled selects.
  - A jargon footer ("Model (Qini curve, AUUC) → …") that also shows in score mode.
  - "Champion" on scoring rows.
  - After upload, the validation list uses code pills (TREATMENT_NOT_RANDOM, SUPPRESSION_COLUMN_MISSING × 2, RANDOMNESS). A **green** RANDOMNESS pill sits under the red refusal (journeys 2/4). The "?" buttons land in different places and are cut off on mobile. The treatment select is truncated. The list pushes Train about 1,000 px down on mobile.
  - In dark mode the locked text and the disabled button have very low contrast.
- **Duplicated.** The Phase 1 Setup layout copied with u-* ids. AUC 0.88 vs 0.885. The suppression item twice. "(every result will be labelled not causal)" twice.
- **Not understood.** "Primary key", "Treatment column", "0/1, assigned at random", "persuadables", "sleeping dogs", "X-learner (LightGBM) · AUUC", "turn the matching switch off in Actions & output" (no such section here), and "1 problem must be fixed or acknowledged…".
- **Change.**
  - One "Uplift" chip. One explanatory sentence: "Needs a past campaign where a random group of customers was held back."
  - Labels:
    - "Customer ID column"
    - "Column that says who was contacted (1) or held back (0)" (from `help.yaml` TREATMENT_* text)
    - "Result column"
  - Keep the ids `u-pk`, `u-treatment` and `u-target`.
  - Hide step 2 until a file exists.
  - A mode-specific footer.
  - Plain run labels.
  - Validation:
    - a headline "1 thing to fix before training (2 warnings can be ignored)";
    - `help.yaml` titles, with codes in `data-code`;
    - inline-block pills;
    - the randomness measure merged into the refusal ("Measured 0.885, 0.5 = random, limit 0.6"), and red when it fails;
    - repeated codes grouped;
    - the fix points to something on this screen;
    - warnings folded under "Show 2 warnings";
    - the acknowledgement checkbox next to Train.
  - Score mode:
    - help text: "Upload the customers you plan to contact. You get a list of who to contact, who to leave alone, and a random group held back to measure the campaign.";
    - model option: "Uplift model trained 24 Sep 2026 (approved)".
  - Gate `#u-file`, `#u-run` and `#u-cancel` for Viewers (C13).
  - The error gets "Try again". Loading shows the use case's name.
- **Primary action.** Train uplift model (score mode: Score customers).
- **Shots.** `uplift-setup--full--desktop.png`, `uplift-setup-file--error-validation--desktop.png`, `--mobile.png`, `uplift-setup-score--full--desktop-dark.png`

#### uplift-running
- **Clutters it.** The full header before the progress. Cancel as a link. The Phase 1 label "Generating explanations & saving".
- **Duplicated.** Same as usecase-running.
- **Not understood.** "2 warnings · treatment column treatment" in green, "train 7K · test 3K · stratified on treatment and outcome".
- **Change.** The shared Running component. "Checking the campaign data: 2 warnings" in `--warn`, and "Learning group 7,000 · testing group 3,000 customers".
- **Primary action.** None. Cancel run is separate and confirmed.

#### uplift-results: uplift run results
- **Clutters it.** A crammed summary strip. The Output block of a training run implies a contact list exists. Previous runs below.
- **Duplicated.**
  - The blocks duplicate the tabs.
  - The Data block leads into the Phase 1 page shell, whose tabs open `pages.js`'s second uplift Model and Output pages titled "Win-back Propensity + GenAI Content", so one run has two Model pages and two Output pages.
  - AUUC three times.
- **Not understood.** "X-learner (LightGBM) · AUUC 0.0254", "Qini curve", "Four segments on the hold-out", RUN_NOT_FOUND.
- **Change.**
  - Headline: "✓ Uplift model trained. Contacting the top 10% the model picks raises reactivation by about 21 points."
  - Primary: "Score customers with this model".
  - Keep the user inside `#/uplift/…`: an uplift-shelled Data page, and redirect `#/uc/<uc>/{model,output}/<uplift run>`.
  - A scoring run's primary is "Download contact list (CSV)".
- **Primary action.** Score customers with this model.

#### uplift-model: Qini, AUUC, deciles
- **Clutters it.**
  - Four jargon tiles and 10 technical setup rows.
  - A 10 × 8 decile table that duplicates the chart.
  - A CI that wraps to 4 lines.
  - An empty "Top share (%)" form with a dark primary "Estimate".
  - 7 px axis text on mobile.
- **Duplicated.** AUUC and its interval three times. The Qini coefficient twice. The ATE at two precisions. The decile values in the chart and the table. The `pages.js` duplicate.
- **Not understood.** AUUC, CI, Qini, ATE, X-learner, bootstrap, "Randomness check (AUC)", "off-policy estimate", "pts", "persuadable".
- **Change.**
  - The finding first: "Contacting the top 10% the model picks: +20.6 points more reactivations than not contacting them (likely between +13.1 and +30.8)."
  - Tiles: Top 10% gain, Top 30% gain, Everyone contacted, "Model beats random targeting: Yes". AUUC, the interval and Qini go behind "Technical metrics" with `help.yaml` "?".
  - Qini chart title: "Gain from targeting by the model vs at random".
  - The decile table goes behind "Show table" with 4 default columns.
  - Training setup goes into Technical details.
  - "What if…" is collapsed, pre-filled with 10, and "Estimate" is a secondary button.
  - One precision throughout.
  - Primary: "Score customers with this model".
  - On mobile, readable charts or the table instead.
- **Primary action.** Score customers with this model.
- **Shots.** `uplift-model--full--desktop.png`, `--full--mobile.png`

#### uplift-output: segments and treat list
- **Clutters it.**
  - Two tiles with the same number (2,241) and a dash tile.
  - A 10-row recommendation, 5 rows of which are dashes.
  - Segment formulas.
  - The download is a 12 px link in the last card.
  - The stepper numbering differs from other pages and wraps on mobile.
- **Duplicated.** Tiles repeated as rows. The `pages.js` duplicate with other labels. The campaign link twice.
- **Not understood.** 2,804 persuadables vs 2,241 recommended (the 563 are never explained; journeys 2/6), "246.7" people, "Model's own prediction 350", the segment names, "P(outcome if not treated) ≥ 17.4%", and "Why not more…" next to "No budget set".
- **Change.**
  - Primary "Download contact list (CSV)" at the top, with the line: "Contact 2,241 customers. Of 2,804 persuadable customers, 563 are held back at random to measure the campaign or were opted out."
  - Tiles: "Customers to contact 2,241", "Extra reactivations expected about 247 (likely 133 to 357)", "Held back to measure 10%".
  - Hide the net value until cost and value are set, with one line saying so.
  - Segments: a plain meaning plus a labelled share, with formulas in Details.
  - One stepper across every uplift page: "Data · Model · Contact list · Campaign results", with steps disabled when they do not apply.
  - Secondary: "Measure campaign results".
- **Primary action.** Download contact list (CSV).
- **Shots.** `uplift-output--full--desktop.png`

### 5.4 Campaigns, Reports and Pilot

#### campaign-results: churn campaign (`#/campaign/<uc>/<score_run>`) and campaign-results-uplift: win-back
- **Clutters it.**
  - Four equal KPI tiles (incremental, absolute lift, relative lift, p-value), and nothing says whether the campaign worked.
  - A 9-row "How it was measured" card with zero counters.
  - The page name three times (crumb, eyebrow, tab), three chips, and a raw run id on the tab bar.
  - An always-open upload card whose disabled "Measure the campaign" is the heaviest button on the page.
  - Feedback covers values. The table scrolls sideways on mobile.
  - Uplift: "Not in either group 5,493" is the biggest, unexplained number.
- **Duplicated.** The value view shows the same numbers with the opposite sign and other words (C18). Lift appears three times with different rounding. The breadcrumb goes through "Uplift" for a churn campaign.
- **Not understood.** "Incremental conversions −120" (fewer churners is good), "Absolute/Relative lift", "pts", "p-value", "95% CI", "converted" meaning "left", "churn_next_60d", "Excluded: window not elapsed", "280.9", RUN_NOT_FOUND with a "‹ Uplift modelling" back link, "primary key", "suppressed".
- **Change.**
  - A verdict first, from `outcome_is_good`. Win-back: "The campaign worked: about 281 extra customers came back (between 168 and 374)." Churn: "We cannot yet tell whether the campaign kept customers: most likely 120 kept, but the range (−3 to 250) includes zero."
  - Two labelled tiles: "Extra customers because of the campaign" and "Response rate, contacted vs not contacted". Relative lift, p-value and absolute lift go into "Statistical details". Use `help.yaml` terms for the "?".
  - "How it was measured" is closed, zero rows are hidden, and "Not part of the test (opted out or not selected)". Add a reconciliation line: "Of 8,000 scored: 2,241 contacted, 266 held back, 5,493 not part of the test."
  - Whole people. One decimal for rates. Rows named "Contacted" and "Not contacted (control group)".
  - One chip at most. Title the page with the campaign name. Breadcrumb: Campaigns › <campaign>.
  - Once a report exists, the upload collapses to "Measure again with a new outcomes file".
  - Gate `#u-camp-file` and `#u-camp-run`.
  - Not found: "We could not find this campaign." with "See all campaigns".
- **Primary action.** See the value in rupees (before a report exists: Upload outcomes).
- **Shots.** `campaign-results--full--desktop.png`, `--full--mobile.png`, `campaign-results-uplift--full--desktop.png`, `campaign-results--error--desktop.png`

#### pilot: Pilot hub (`#/pilot`, becomes Reports)
- **Clutters it.**
  - Four equal numbered cards with 3 to 4 line paragraphs.
  - The kit card is the longest element on the page, including a pre-flight command clipped at the card edge (scrollWidth 439 vs 272).
  - Raw ids on every row.
  - Readiness rows say "telco-churn · 2,000 rows" with no train or score tag.
  - "export all feedback (Admin)" is shown to everyone.
  - A repeated demo sentence.
  - Tall empty cards with no action.
  - The same red "HTTP_500" three times.
  - Nothing painted while loading.
- **Duplicated.** Campaign value lists the same runs as Campaigns. The top-bar "Pilot" link plus the crumb. The lists are not filtered by client.
- **Not understood.** "Pilot", "pseudonymising", "python -m scripts.preflight…", "Template", the ids, "version 1 · X-learner (LightGBM)", "Value view", "Demo raw tables (broken extract)", "DEMO_NOT_SEEDED", "export all feedback (Admin)".
- **Change.**
  - Retitle it "Reports", listing Model results and Campaign value. The kit moves to Build data (`#/pilot/kit`), with commands and templates behind "For your IT team" and the code box wrapping.
  - Rows show names, dates and train/score tags, with one "Open" and a quieter "PDF".
  - Filter or label rows by client.
  - Empty states each have one action ("Go to Models", "Score customers").
  - One calm error with "Try again".
  - Paint a skeleton and fill cards one at a time.
  - The feedback export moves to Admin, shown only if `can()` allows it.
  - Drop the duplicate demo line and the card numbers.
  - Keep the text "Data request kit" on the kit page.
- **Primary action.** Open the latest report (empty state: Download the data request).
- **Shots.** `pilot--full--desktop.png`, `--full--mobile.png`, `--empty--desktop.png`, `--error--desktop.png`, `--loading--desktop.png`

#### pilot-readiness, pilot-results-report: report viewers
- **Clutters it.**
  - A fixed 70vh sandboxed iframe around a 2,400 px document, so there are two scrollbars and the content is clipped at the fold.
  - "Minfy logo" and "Client logo" dashed placeholders.
  - The status label is doubled.
  - "Download as PDF" is styled as muted subtitle text.
  - The frame is a white slab in dark mode. Feedback overlaps it on mobile.
- **Duplicated.** The page H1, the report H1 and the breadcrumb all say the same thing. "Client Demo Telecom" three times.
- **Not understood.**
  - "Dataset ds_…"
  - UTC dates next to local ones
  - "PAY_DT"
  - "3 point(s)…"
  - "one click on the mapping screen" with no link
  - "version 1 (champion)"
  - Win-back: "Client not recorded"
  - Error: "There is nothing to report on yet." for a missing dataset, or for a use case with no champion
- **Change.**
  - A primary "Download PDF" button in the header. "Not ready" also gets "Open the mapping screen".
  - Header: "Data readiness · <use case>" with a status pill. Results: "Model results · <use case>" with the subtitle "Approved model, version 1, trained 24 Sept 2026".
  - Size the frame to its content (`sandbox="allow-same-origin"`, no scripts). This also fixes the localStorage console error (journeys 2/10). Give it a paper surround in dark mode.
  - Specific error states: "This data check no longer exists." / "This use case has no approved model yet…" with one button. The PDF link is hidden when there is no report.
  - Report-template text needs a ruling.
- **Primary action.** Download PDF.

#### pilot-value: Campaign value (rupees)
- **Clutters it.**
  - The input form fills the fold before the answer.
  - "Save values" lands mid-row.
  - A misaligned checkbox and truncated inputs.
  - "READY WITH WARNINGS" on a value callout.
  - On mobile the frame looks like an empty white box.
  - On error, the full empty form is still shown.
- **Duplicated.** The contacted/control table and range from the Campaign page, with the opposite sign. The inputs printed again inside the report. The net value twice.
- **Not understood.** "Value view", "What one extra customer is worth (₹)", "The outcome is one we want more of (untick when…)", "churn_next_60d", "-₹2,00,401 (2.00 lakh)" with the minus sign lost, "-1.1x to 4.6x".
- **Change.**
  - The verdict and the report first. The inputs go into "Your estimates", collapsed, with "Save and recalculate" at the end.
  - Groups: Value (worth; basis) and Costs.
  - A radio pair: "We want more of this outcome (e.g. customers coming back)" / "We want less of this outcome (e.g. customers leaving)".
  - 422 errors appear next to their field. Full-width inputs. The INPUTS names are unchanged.
  - Gate the save for Viewers.
  - Not found: "We could not find this campaign." No results yet: "Rupee value appears once the campaign's results are measured" with "Upload outcomes".
  - Cross-links with the Campaign page, using the same words.
  - Auto-height frame.
  - Server text needs a ruling.
- **Primary action.** Download PDF (secondary: Save and recalculate, Analyst only).
- **Shots.** `pilot-value--full--desktop.png`, `--full--mobile.png`, `--error--desktop.png`

### 5.5 Admin, ops, sign-in and AI

#### userbar: Home per role
- **Clutters it.**
  - Two misaligned strips. An Admin gets 8 underlined links. About 155 px on mobile.
  - "Viewer" is shown next to every role.
  - An amber "Access control is off" pill and sentence on every screen.
  - "Change password" and "Sign out" look the same as the admin links.
- **Duplicated.** "Change password" three times. Demo identity twice. No personal next step for any role.
- **Not understood.** "local operator", "Access control is off" (reads like a security fault), "Pilot", "recipes", the stars.
- **Change.** Section 2: one bar, one role label, the Sign-in off chip, a "For you" line on Home, and the mobile menu.
- **Primary action.** Open a use case, or the role's "For you" link.
- **Shots.** `userbar--full-admin--desktop.png`, `--full-admin--mobile.png`, `--full-viewer--desktop.png`, `--full-authoff--desktop.png`

#### signin: Sign in
- **Clutters it.**
  - The chooser with "+ New client" and a red "AUTH_REQUIRED Sign in to do this." in the header.
  - "Not signed in · Sign in", which links to the current page.
  - "Pilot" and "Take the tour".
  - A "‹ Customer Lifecycle" back link that loops back to sign-in.
  - A redundant "Your account" card title.
- **Duplicated.** Two errors in the error state. "Sign in" three times.
- **Not understood.** "AUTH_REQUIRED", "BAD_CREDENTIALS", "MARKETING_AI_AUTH_MODE=local".
- **Change.**
  - The bar shows the wordmark and "Sign in" only. No chooser, no back link.
  - One centred 440 px card with Username, Password and the primary "Sign in".
  - The failure message appears under the password field ("The username or password is not right.").
  - Sign-in off: "Sign-in is turned off on this installation. You can use everything without an account." with "Go to Home". The environment variable goes under "For administrators".
- **Primary action.** Sign in.
- **Shots.** `signin--full--desktop.png`, `--error--desktop.png`, `--error--mobile.png`, `chrome-home/signed-out-home.png`

#### account: Change your password
- **Clutters it.** The chooser in the header, a hard-coded back link, and a "New password" card whose first field is the current password.
- **Duplicated.** "Change password" three times. "For demo-analyst".
- **Not understood.** "MISMATCH" as a bold heading.
- **Change.**
  - Opens from the user menu, with no chooser.
  - Card title "Change your password".
  - Validation under the fields: "The two new passwords are not the same." and a live "12+ characters" check.
- **Primary action.** Change password.

#### admin-users: Users
- **Clutters it.**
  - The create form comes before the list, so the list is below the fold on mobile.
  - "Change roles", "Disable" and "Set password" are equal links, and Disable has no confirmation.
  - "by system:bootstrap" and "by u-5385701cf7954e8f".
  - Tabs repeat the bar links.
  - The table is cut at "Sta…" on mobile, and Feedback covers a checkbox.
- **Duplicated.** The role hint twice when editing.
- **Not understood.** The actor ids, "First password · 12+ characters", "python -m scripts.create_user", "HTTP_500", "ROLE_REQUIRED", role meanings shown only as hover titles.
- **Change.**
  - The list "People · 5" comes first, with one primary "Add user" that opens the form (open automatically when there are no users).
  - "Disable" moves to the far end in `--bad` with "Disable demo-analyst and sign them out? Yes, disable / Keep".
  - "Added" shows the date and the creator's name, or "Set up at install" for the bootstrap account.
  - Role meanings are visible under each checkbox.
  - Sign-in off: a calm notice.
  - Error: "We could not load the people list." with "Try again".
  - Refused: the server sentence, no code, and "Go to Home".
  - Stacked cards on mobile. Sort active users first, then by name.
- **Primary action.** Add user.
- **Shots.** `admin-users--full--desktop.png`, `--full--mobile.png`, `--error-refused--desktop.png`

#### admin-audit: Audit log
- **Clutters it.**
  - Seven filters, always open.
  - Seven columns, five of them monospace ids ("method=POST · route=/auth/login · status_code=200").
  - A page of 50 rows is about 5,900 px.
  - "Write a retained export" has the same weight as the CSV download.
  - On the empty server, all 53 events are artefact 404s (C17).
- **Duplicated.** Actor ids next to names on Users. Tabs repeat the bar.
- **Not understood.** "Actor (user id)", "auth.login", "models.approve", "Object type/id", "append-only", "UTC day", "retained export", lowercase status pills, a mm/dd/yyyy input next to "24 Sept 2026".
- **Change.**
  - Filters: Who (people by name, sends `actor_id`), What (plain action families mapped to the same prefixes) and a date range. The rest goes behind "More filters". The query is unchanged.
  - Columns: When, Who, What (plain label, raw code kept in the row), Result. The others go into row Details.
  - "Search" is primary and "Download CSV" secondary. "Write a retained export" moves to More actions with the sentence "Saves a tamper-proof copy for compliance."
  - Subtitle: "A permanent record of every sign-in, change and download. It cannot be edited."
  - Collapse runs of identical failures. Two-line cards on mobile.
- **Primary action.** Search.
- **Shots.** `admin-audit--full--desktop.png`, `admin-audit--empty--desktop.png`

#### privacy-consent, privacy-erasure, privacy-access, privacy-retention: Privacy tabs
- **Clutters it.**
  - Three equal cards with black buttons (consent).
  - Raw purpose ids and a run-on "use case → purpose" line.
  - A CSV column spec as the first thing in the import card.
  - A free-text "Client id (optional)" on three tabs.
  - Native controls render light in dark mode.
  - Erasure: register ids, model ids three times, the same black "Erase" as harmless buttons, and a storage-internals description.
  - Access: a jargon paragraph and a truncated placeholder.
  - Retention: "Dry run", plan id and a 64-character hash as the first rows, and a lone "Plan again" above the answer.
- **Duplicated.** The legal subtitle on all four tabs. Principal and client fields on three tabs. Flagged models twice.
- **Not understood.** "Data principal id", "DPDP", "hashed", "ledger", "tombstones", "LLM cache", "completed with exceptions", "training_input:dataset", "NOT_REWRITTEN", "manifest", "SHA-256", "Dry run", "Plan hash", "Keep, remove row samples".
- **Change.**
  - Consent: primary "Look up a customer". Import collapses behind a secondary button. Purposes become "How consent is set up", by name. The field is "Customer ID (as in your files)". The client defaults from the top bar ("For client: Demo Telecom"), with the override in Advanced. The lookup result leads with "Purpose / May we contact them?", and the hash and ledger go in Details. One quiet legal line keeps "not legal advice". The upload uses `.control.file`.
  - Erasure: "Erase" is a danger button, enabled only after the tick. Description: "Finds this customer in every file Marketing AI holds and permanently removes them. This cannot be undone." The register shows Requested, Status in words, Rows removed and Models to retrain (count), with ids behind "Show more columns". Merge in the retraining card, keeping "flagged for retraining at the next scheduled cycle (not now)". The completion report leads with one sentence.
  - Access: title "Export a customer's data", with SHA-256 and manifest in Details. Consider merging with Erasure into "Customer requests" (grouping only).
  - Retention: title "Data past its keep-until date". Lead with the answer and the `help.yaml` `governance.retention_days` sentence. The hash and id go in Details. "Check again" is quiet. "Delete these N files" is a danger button after the review tick. Columns: What, Created, Kept for, What happens.
  - Errors: a plain sentence with "Try again".
- **Primary action.** Consent: Look up a customer. Erasure: Erase (destructive, with a confirm tick). Access: Export and download. Retention: Delete these files (only when there are items).
- **Shots.** `privacy-consent--full--desktop.png`, `--full--desktop-dark.png`, `privacy-erasure--full--desktop.png`, `privacy-retention--full--desktop.png`

#### monitoring-schedules and monitoring-schedule: Schedules
- **Clutters it.**
  - A 9-field create form, always open (cron, time zone, recipe id, dataset id, model version id).
  - Row actions "Open Run now Pause Delete" wrap onto two lines, with Delete under Open.
  - Raw ids ("telco-churn", "c_demo_telecom_1").
  - "Sync retraining schedules".
  - The loading state draws a free-text "Use case id".
  - Detail page: 9 key-value rows including "Recipe · dataset · model — · ds_… · —", an 8-column firings table showing "DATASET_COMPOSITE_KEY_NOT_WIRED" (a real failure of the seeded drift check), a status filter over one row, and an always-open Edit form. The not-found state has no back link.
- **Duplicated.** Cadence "02:00 Asia/Kolkata" next to "Next due 08:30 pm" in another zone. The client field repeats the chooser. The Edit form repeats the create form. The firing error repeats the alert.
- **Not understood.** "Cron line", "Onboarding recipe id", "Or a fixed dataset id", "Model version id", "Sync retraining schedules", "Check drift", "challenger/champion", "Firings", "Due slot", "local-operator".
- **Change.**
  - The list comes first, with one primary "New schedule".
  - Empty state: "Nothing runs on its own yet. Schedule monthly scoring or a drift check so you don't have to remember."
  - The form shows Use case (by name), What it does, How often and Start now. Everything else goes into Advanced, and the payload is unchanged.
  - Columns: What, Use case, How often, Next run (with zone), Status. Clicking the row opens it.
  - "Run now" and "Pause" are quiet buttons. "Delete" is set apart and keeps the two-step confirm.
  - "Check drift" becomes "Check if new customers look different", with `help.yaml` help.
  - Detail page: title "Check drift · Telco Customer Churn". "Run now" is primary. Facts first, ids in Details. "Run history" shows When, Result in words, and Run. Edit is collapsed. Not found: "This schedule no longer exists." with a back button.
- **Primary action.** New schedule (detail page: Run now).
- **Shots.** `monitoring-schedules--full--desktop.png`, `--loading--desktop.png`, `monitoring-schedule--full--desktop.png`, `--full--mobile.png`

#### monitoring-alerts, monitoring-missed: Alerts and Missed runs
- **Clutters it.**
  - Alerts has 8 columns, and the Schedule and Run ids wrap over 3 to 4 lines. They are links but look like plain text (`a{color:inherit}`).
  - A raw code inside the message.
  - Cut after "What" on mobile.
  - Missed runs is a whole tab for a list that is empty in the demo.
- **Duplicated.** Alerts repeat the drift line, the firing error and the Missed runs tab. "Alerts · 2" under "Alerts".
- **Not understood.** "Drift above threshold", "DATASET_COMPOSITE_KEY_NOT_WIRED", "Acknowledge", lowercase severity, "Due slots nobody fired…", "SCHEDULE_MISSED".
- **Change.**
  - Columns: Severity, What happened (a plain kind, with DRIFT titles from `help.yaml`), Use case, Raised, Action. The message and links go into an expandable row. Sort open critical alerts first.
  - One outline "I'm on it" button per row (keeps `data-ack`).
  - Empty state: "No open alerts. You'll see one here if a scheduled job fails, results drop, or new customers look very different."
  - Missed runs becomes a view inside Schedules, offered when N > 0. The route stays. Explanation: "Runs that should have happened while Marketing AI was switched off. It runs once to catch up."
- **Primary action.** Acknowledge an alert. Missed runs: none.
- **Shots.** `monitoring-alerts--full--desktop.png`, `--full--mobile.png`, `monitoring-missed--full--desktop.png`

#### monitoring-runs and monitoring-run: Outcomes (moves to Campaigns)
- **Clutters it.**
  - The run id is the first column, "done" is a pill on every row, and Started and Finished are the same minute.
  - The detail page's title is "Telco Customer Churn · r_…", with "State done score".
  - "Open the Output page" looks like plain text.
  - Two empty cards before any upload, and an always-visible "Outcome column".
- **Duplicated.** Previous runs. "‹ All scoring runs" plus the tab.
- **Not understood.** "Incrementality input", "Treated minus control", "Scored, not in the file", "Suppressed, left out", "PERFORMANCE_DROP", "Alert level", "Worse than test by".
- **Change.**
  - This becomes the Campaigns list: Use case, Scored on, Customers scored, and Results ("Not added yet" / "Added"). The run id and client go behind "Show more columns". The action is "Add results" or "View results".
  - Empty state: "No customers have been scored yet…" with "Go to use cases".
  - Detail page: one card, "Add what happened to these customers", with the upload and the primary "Upload and measure". Keep the text "No outcomes have been added for this run yet." inside it. Hide the empty cards. "Outcome column" goes into Advanced.
  - Renames: "Campaign effect (contacted vs control group)" and "Difference vs control group". Metric names come from `help.yaml`, with the code in Details.
  - A "See this run's results" button.
- **Primary action.** Add results / Upload and measure.

#### approvals: Approvals
- **Clutters it.**
  - The card head is "<use_case_id> · version N".
  - The meta line is "m_rca_1 · … · started by u-8001d277e81a4cd6" (journeys 3/3).
  - Every metric at 4 decimals.
  - "Approve" is a primary button with "Reject" as a link right next to it.
  - A long sign-in-off note.
  - The chooser is shown but does nothing here (journeys 3/5).
- **Duplicated.** The subtitle says "Challengers that beat their champion" but lists first models against no champion (journeys 3/4). "approved by demo-approver" (a name) next to "started by u-…" (an id).
- **Not understood.** "Challengers", "champion", "held-out rows", "(decides)", "Against no champion", "CHAMPION_CHANGED", "candidate".
- **Change.**
  - Subtitle: "Models waiting for your approval: new models that beat the one in use, and the first model of a use case."
  - "Trained by <display name> on 24 Sept 2026", looked up from the users list where readable, otherwise "another user". Ids go in Details.
  - The deciding metric comes first, with its `help.yaml` name and "Main measure". The others go behind "Show all measures". 3 decimals, and a Better/Worse word.
  - "Reject" becomes a separate outline danger button with "Rejecting archives this model."
  - The sign-in-off note becomes one quiet line.
  - No chooser.
  - Empty state: "No model is waiting for approval. When an Analyst trains a model that beats the current one, it appears here." A count badge in the nav.
- **Primary action.** Approve.
- **Shots.** `approvals--full-approver--desktop.png`, `approvals--empty--desktop.png`

#### usecase-viewer: Setup as a Viewer
- **Clutters it.**
  - A fully greyed form, a disabled "Run training", and two refusal notes in the middle.
  - Train/Score tabs the Viewer cannot use.
  - Previous runs, the part a Viewer can use, is squeezed into the narrow column and clipped.
  - About 1,000 px of disabled form on mobile.
- **Duplicated.** The dataset hint twice.
- **Not understood.** "ROC-AUC 0.951", "ds_… (built)", "train · Classification".
- **Change.** One calm notice plus "See latest results" as the primary. The form moves into a closed "See the setup steps" (gate notes and ids kept). Previous runs becomes a full-width table.
- **Primary action.** See latest results.
- **Shots.** `usecase-viewer--full--desktop.png`, `--full--mobile.png`

#### ai-connection: AI service connection (`#/generative/connection`)
- **Clutters it.**
  - Three sentences of AWS vocabulary.
  - The no-keys promise written twice.
  - Two black primary buttons ("Save", "Test connection") and "Reset to default".
  - A report with the ARN, account and raw model ids.
  - The chooser in the header.
- **Duplicated.** The promise twice. Reachable only from a badge.
- **Not understood.** "AWS connection", "Bedrock", "default chain", "AWS_PROFILE", "SSO", "instance role", "aws configure …", "Locked (deployed)", "Principal", "Credential method".
- **Change.**
  - Moves under Admin as "AI service".
  - Subtitle: "Lets Marketing AI write text (assistant answers, root-cause notes, campaign copy) using your company's AI service. No passwords or keys are typed here."
  - "Test connection" is primary. "Save" is secondary and enabled only when something changed. "Reset to default" is a quiet link set apart.
  - The report leads with "Connected" and "N of M AI models available", with the details closed.
  - Locked: "This is managed by your IT team on the server."
  - Keep the ids and radio-only inputs.
- **Primary action.** Test connection.

#### ai-assistant, ai-rca, ai-copy: generative screens
- **Clutters it.**
  - The demo shows only the notice, ending in "docs/GENERATIVE.md".
  - With no demo, a loud amber "Fake backend / Deterministic stand-in · … $0 could ever be spent" banner.
  - The type chip "★★" has no label.
  - Build/Evaluate tabs, Advanced, and a jargon footer.
  - RCA and copy with no run show "This screen could not be loaded / RUN_NOT_FOUND" and a Customer Lifecycle back link.
  - When they work, RCA and copy repeat the KPI tiles and the decile chart of the Output page ("…decile_lift.json yet").
  - Copy asks "Your name, for the approval record" of a signed-in user.
  - "Download messages" is a link while Approve and Regenerate are buttons.
- **Duplicated.** The notice on three screens. That is fine.
- **Not understood.** "Fake backend", "index", "LLM", "Reference questions", "Embedding model", "RCA", "Cost & guardrails", "cache hit", "copy judges", "band".
- **Change.**
  - The notice gets a role-aware action (see ai-notice).
  - The fake backend becomes one calm line: "Practice mode: answers are sample text, not from a real AI service." It is still a link to the connection screen.
  - "Built assistants" and "No assistant built yet." "AI model" instead of "LLM".
  - Fix the chip label.
  - Add entry buttons "Write root-cause notes" (RCA Output page) and "Write campaign copy" (win-back scoring run), through `registerRunAction`, hidden when AI is unavailable.
  - RCA drops the duplicated tiles and chart and links "See the scores". "Cost and safety checks" is closed.
  - Copy pre-fills `#g-approver` with the signed-in name (read-only). "Generate campaign copy", then "Download approved messages" as the primary. "4 of 12 messages approved".
  - Not found: a plain sentence with "Back to <use case>".
- **Primary action.** Assistant: Build assistant. RCA: Generate root causes. Copy: Generate campaign copy, then Download approved messages.
- **Shots.** `ai-assistant--full--desktop.png`, `ai-assistant--empty--desktop.png`, `ai-rca--empty--desktop.png`, `ai-copy--empty--desktop.png`

---

## 6. Work packages

Package 0 lands first. Packages 1 to 8 then run in parallel worktrees with disjoint file ownership. A package may edit a test file only if no other parallel package owns it. Tests shared with package 0 are edited after it merges. Package 9 runs last. A package that needs a change in a file it does not own either uses a seam that package 0 adds, or writes the request into its own PR for the owning package.

- **WP0 Foundation.**
  - Files: `ui/index.html`, `ui/dom.js`, `ui/chrome.js` (new), `ui/modules/router.js`, `ui/app.js`, `docs/UI_AUDIT.md`, `docs/ui/before/*`, `scripts/ui_screens.mjs`.
  - Delivers: tokens, button, card, table, form and state components; `pageHead` with the logo only; `crumbs`, `headActions`, `errorBox` with Details, `emptyState`, `notFound`, `skeleton`, `techDetails`, the locale constants and `fmtMoney`; the top bar with nav, slots, mobile menu and skip link; the router seams `registerNavSlot`, `registerAccess`, `registerGlossary` and `registerRunAction`; `app.js` loading, failure, planned notice and `document.title`.
- **WP1 Top bar integration, sign-in, account, Home.** `production/userbar.js`, `signin.js`, `session.js`, `boot.js`, `production/index.js`, `onboarding/clients.js`, `onboarding/index.js`, `ui/overview.js`.
- **WP2 Use-case Setup, running and results.** `ui/usecase.js`, `ui/settings.js`, `configs/engine.yaml` (the `ui:` copy block only).
- **WP3 Build from raw tables.** `onboarding/setup.js`, `panel.js`, `steps.js`.
- **WP4 Data, Model and Output pages.** `ui/pages.js`.
- **WP5 Uplift and campaign results.** `ui/modules/uplift/*`.
- **WP6 AI screens.** `ui/modules/generative/*`, `ui/availability.js`.
- **WP7 Admin, privacy, monitoring, approvals and gating.** The other `ui/modules/production/*` files: `users`, `audit`, `privacy`, `retention`, `schedules`, `alerts`, `outcomes`, `approvals`, `controls`, `downloads`, `styles`, `gate`.
- **WP8 Pilot: Reports, Build data kit, tour, help, feedback.** `ui/modules/pilot/*`, `configs/pilot/help.yaml` (additions only).
- **WP9 Prototype, pinned copy and after-screenshots.** `marketing-ai-prototype.html`, `tests/prototype/*`, `docs/prototype/*`, `scripts/prototype_screenshots.mjs`, `docs/ui/after/*`, and the prototype-pinned copy constants in `uplift/views.js` (after WP5).

---

## 7. The five biggest changes

1. **One top bar and a logo-only header** (the owner's request plus C1). `home--full--desktop.png`, `home--full--mobile.png`, `userbar--full-admin--desktop.png`, `signin--error--desktop.png`, `client-new--full--desktop.png`
2. **Calm states instead of codes and blank pages** (C2, C3, C16, AI notice). `usecase-results--loading--desktop.png`, `home--error--desktop.png`, `usecase-planned--error--desktop.png`, `run-output--empty--desktop.png`, `pilot--error--desktop.png`
3. **Verdict first, with one primary action on every results screen** (contact list, campaign verdict, rupee value). `run-output--full--desktop.png`, `usecase-results--full--desktop.png`, `campaign-results--full--desktop.png`, `uplift-output--full--desktop.png`, `pilot-value--full--desktop.png`
4. **Technical detail behind Details, plain words in front** (C4, C5, C6, tables). `run-data--full--desktop.png`, `run-model--full--desktop.png`, `uplift-model--full--desktop.png`, `admin-audit--full--desktop.png`, `monitoring-alerts--full--desktop.png`
5. **Setup and Build data one step at a time** (locked steps hidden, readable Sources and Mapping, Advanced tamed, model first when scoring). `usecase-setup-train--full--desktop.png`, `usecase-setup-advanced--full--mobile.png`, `build-raw-1-sources--full--desktop.png`, `build-raw-2-mapping--full--desktop.png`, `usecase-setup-score--empty--desktop.png`
