# Marketing AI — Plan M: A product a marketer understands in minutes

**Companion to:** Plan H (`MARKETING_AI_PLAN_H_SIMPLE.md`, the last simplification), Plans J, K and L (which added screens
since), `PARALLEL_WORK_PROTOCOL.md`.
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1600 … 1699 (claimed by M170)
**Milestones:** M170 – M181
**Status:** approved by the product owner on 11 October 2026 ("write a plan M and then start executing it").
**Evidence:** a read-only survey of the UI code (`ui/`, about 45 routes) and 82 screenshots of the app running on the
validated Hillstrom demo (11 October 2026). Findings are cited by page below.

---

## Plan M at a glance

**The problem.** The engine is good; the screens describe the engine. A marketer thinks "win back lapsed customers this
month, and prove it worked". Today they must translate that into *use case → train a model → score a file → treat list →
upload outcomes → Value Proof Pack*, across about 45 screens under four menu items. Plan H cut the menu to four items in
September; Plans I–L then added about thirty screens under them.

**The decision.** Re-centre the product on the thing a marketer already manages in CleverTap, MoEngage or Netcore: the
**campaign**. Keep the engine exactly as it is. Change only navigation, pages and words, and hide everything technical
behind one switch.

| Phase | What a person sees at the end | Milestones |
|---|---|---|
| **1 — Quick wins** | One contact list and one download button. One word for each thing. Money in ₹ and lakh, rounded. Goals named in business words. A Help menu. No orphan or broken pages. | M170–M173 |
| **2 — Campaign-centred structure** | A left sidebar with five places: Overview, Campaigns, Data, Reports, Settings, plus an approvals bell and Help. A five-step "New campaign" wizard. One campaign page with three tabs. One place to add results. Technical pages only behind "Show technical details". | M174–M180 |
| **3 — Prove it is simpler** | A five-person usability test with two tasks, timed. Click-count journeys pinned in tests. Visual and accessibility checks. | M181 |

**Measures of success** (each one has a test or a recorded session):

| Measure | Today | Target |
|---|---|---|
| Menu items | 4 (but about 45 routes under them, some unreachable) | 5, every route reachable from one of them |
| Clicks from Home to a downloaded contact list (file already prepared) | 4–6, across 3 views | ≤ 5, inside one wizard |
| Places to add campaign results | 5 | 1 (the campaign's Results tab) |
| Names for the contact file | 4 ("contact list", "treat list", "scored list", "Who to contact") | 1 |
| Engine words visible to a marketer (propensity, AUUC, SHAP, PSI, X-learner, "treat = 1" …) | many | 0 outside "Technical details" (a test renders the pages and checks) |
| Primary buttons per page | up to 3 | 1 |
| Task success, 5 marketers, 2 tasks, no help | not measured | ≥ 4 of 5 per task |

---

## 1. Decisions (taken by the product owner, 11 October 2026)

| # | Question | Answer |
|---|---|---|
| M1 | What is the main object a person works with? | **The campaign.** Runs and models become the engine underneath a campaign. |
| M2 | Navigation pattern? | **A left sidebar of five places** (Overview, Campaigns, Data, Reports, Settings). The approvals bell, Help (?) and profile go top right. This is the pattern CleverTap, MoEngage, HubSpot and Netcore use, so the target users already know it. Below 900 px wide it folds into a menu button. |
| M3 | Do marketers see models, training and scoring? | **No, by default.** Training happens on a goal's first campaign; scoring again is "Refresh the list". A per-person switch "Show technical details" (in the profile menu, remembered) shows the Data, Model and technical pages for analysts. Roles are unchanged. |
| M4 | Language? | **Professional and plain, from one glossary** (§4): full sentences, numbers with units, no engine words. The glossary is code (`ui/labels.js`) and a test enforces it. |
| M5 | Old links (bookmarks, links in e-mails and reports)? | **Every old hash keeps working**: it redirects to its new home. No page that works today is deleted; rarely used tools move to Settings → Advanced. |
| M6 | Operational use cases (Order Fulfillment, Fault Prediction)? | **Kept, under "More goals"** on the New campaign screen. They do not contact customers, so they have no campaign; they open the classic run screens. |
| M7 | Engine and API changes? | **None to behaviour.** API additions only where a screen needs a summary that today takes several calls (each listed per milestone, additive, with a RoutePolicy). |
| M8 | Build order? | **Phase 1 first** (safe, visible, a week); Phase 2 behind a deployment switch `ui.layout: classic \| campaigns` (default `campaigns` once M181 passes; `classic` stays for one release). |

---

## 2. What the current screens get wrong (the survey, condensed)

1. **Results is five pages on one screen** (`ui/modules/simple/pages.js` L213–276): "Needs your attention", "Value proven
   to date", Runs, Campaigns, Value Proof Packs, plus "Arbitration & Conflicts". Campaign names are whole sentences.
2. **The scoring Output page is about three screens long** (`ui/pages.js` L1353–1452 plus two panels). It has about ten
   cards and fourteen actions: bands, a second table of the same customers, a holdout calculator, a sample, "Treat list
   (treat = 1, holdout = 1, explore = 1)", drift events and technical details. "Customers worth an e-mail 1,645" sits
   beside "To treat 831" with no sentence saying why they differ.
3. **One file, four names, two buttons.** "Download contact list (CSV)" is `scores.csv` and "Download treat list (CSV)" is
   `treat_list.csv`, both on one page.
4. **Results go in five places**: the Measure panel, "Campaign results", Monitoring → Campaigns, "Measure now", and the
   audit form. Meanwhile **no screen creates a campaign record**: `POST /campaigns` has no caller in `ui/`.
5. **Engine words on screen**: "Train a model / Score new data", "Ensemble (LightGBM)", "X-learner", "AUUC 0",
   "Ranking quality 0.534", propensity, SHAP, PSI, "recipe", "Arbitration & Conflicts".
6. **Goals named as model types or operations**: "Payment Propensity", "RCA (Root Cause Analysis)", "Fault Prediction",
   "Order Fulfillment"; and a "What do the colours mean?" legend.
7. **Setup's Advanced settings show 45 fields** (38 active). That is appropriate for an analyst and wrong for a marketer.
8. **Loose ends**:
   - The Help menu is not drawn, so the tour and "Send feedback" have no trigger.
   - `#/cost` and `#/industry/<id>` are linked from nowhere.
   - `#/doctor`, `#/audit` and `#/monitoring/runs` are linked from one place each.
   - The data-readiness page shows "This data check no longer exists" on the demo.
   - Several pages log 404s or 409s while drawing their own empty states.

---

## 3. The target structure

```
┌───────────────┬──────────────────────────────────────────────────────── 🔔 3   ?   Priya ▾ ┐
│ 🏠 Overview    │                                                                            │
│ 📣 Campaigns   │   (page)                                                                    │
│ 🗂  Data        │                                                                            │
│ 📊 Reports     │                                                                            │
│ ⚙️  Settings    │                                                                            │
└───────────────┴────────────────────────────────────────────────────────────────────────────┘
```

| Place | What it holds | Replaces |
|---|---|---|
| **Overview** | Items in order: <br>• "Needs your attention" (up to 5 cards, each with one action)<br>• one headline: value proven to date (₹, a floor)<br>• live campaigns (up to 5, with status and next step)<br>• the button **+ New campaign**<br>• first visit only: a three-item checklist (connect or upload data, run your first campaign, add its results) | Home's timeline, the top of Results |
| **Campaigns** | A list (name, goal, status, contacted, result) with **+ New campaign**. Opening one shows three tabs (§3.2) | Results → Runs and Campaigns, `#/monitoring/runs`, `#/campaigns/<id>`, `#/campaign/<uc>/<run>` |
| **Data** | Data sources: connections and uploaded files, each with health and last refresh. **Data Doctor** ("Check my data") is a button at the top of this page. | Connections, `#/doctor` |
| **Reports** | Value Proof Packs, the programme summary (value to date with its rules), **Check a past campaign** (the audit form), and model reports (only with technical details on) | Results → Value Proof Packs and Value proven, `#/pilot`, `#/audit` |
| **Settings** | Team and roles · AI service · Privacy and consent · Compliance (when the switch is on) · Schedules · Costs · **Advanced** (the uplift workbench, building from raw tables, the data request kit, model health, the audit log) · About | Settings, `#/cost` and the monitoring pages |
| 🔔 (top right) | Models and proposals waiting for approval | the Results badge and `#/approvals` |
| ? (top right) | Take the tour · Glossary · Send feedback · What's new | the drawn-but-hidden `help` slot |

### 3.1 "New campaign": five steps, one screen

```
1 Goal        cards in business words (§4.2); "More goals" folds the operational ones
2 Customers   pick a data source or upload a file → Guided setup checks it (Plan G/L, unchanged) → Approve
3 Audience    who to contact (priority), which offer and channel (M100), who is held back for comparison,
              and a plain-language test size line ("enough to see a 2-point change")
4 Review      cost, expected value range, compliance result (M134 when on), then [Approve]
5 Send        Download the contact list (one file). The campaign becomes "Live"; its
              results date is set from the test plan
```

- Steps 2–3 run the existing train-then-score journey underneath. The first campaign for a goal trains; later ones reuse
  the approved model and only score. The person never chooses.
- The campaign record (`POST /campaigns`) and its test plan (`POST /campaigns/{id}/plan`) are created by the wizard, which
  closes survey finding 4.

### 3.2 The campaign page: three tabs

| Tab | Content |
|---|---|
| **Overview** | • Status and the next step ("Add results after 12 Nov")<br>• Three number tiles: contacted, held back for comparison, and expected value (a range)<br>• What was sent<br>• **One** primary button |
| **Audience** | • The contact list: priority, offer, channel and the top reason per customer, with a search box and 25 rows at a time<br>• One "Download contact list"<br>• "Why these customers?" (the top three reasons in business words) |
| **Results** | • Before results: one upload box ("Add results"), which is the only place to add them<br>• After: the Proof Pack's headline ("₹X more, at least ₹Y; proven"), the evidence label in words, and the full Proof Pack one click away |

With technical details on, a fourth tab, **Technical**, shows today's Data, Model and Output pages unchanged.

### 3.3 Page rules (every page, Phase 1 and 2)

- One H1, one sentence under it, **one primary button**.
- At most five number tiles or cards above the fold. Everything else is a closed "More" or "Technical details" section.
- Numbers carry units and are rounded for display: ₹ with lakh and crore, percentages to one decimal, counts with
  separators. Exact figures appear in tooltips and downloads.
- Every list has an empty state that says what to do next, with a button.
- Errors say what happened and what to do, never a status code. A page never fetches data it can tell does not exist.

---

## 4. Language

### 4.1 The glossary (`ui/labels.js`, enforced by M170's test)

| Concept | Shown to a marketer | Never shown outside Technical details |
|---|---|---|
| use case | **Goal** | use case |
| a run (train or score) | *(hidden)*; "Refresh the list" | run, train, score, scoring run |
| treat list, contact list, scored list | **Contact list** | treat list, scored list, `treat = 1`, `holdout = 1`, `explore = 1` |
| holdout, control group | **Comparison group** (not contacted) | holdout, control group, explore slice |
| uplift, campaign-effect model | "the effect of contacting them" | uplift, X-learner, S/T-learner, AUUC, Qini |
| propensity, score, probability | **Likelihood** | propensity, score (as a noun) |
| band High / Medium / Low | **Priority** High / Medium / Low | band |
| champion / challenger | Current model / New candidate | champion, challenger |
| Causal / Estimated / Not proven | **Proven / Estimated / Not yet proven** | causal, evidence label |
| SHAP, feature importance | "What drives it" | SHAP, feature, importance |
| drift, PSI | "Customers have changed since the model was trained" | drift, PSI |
| arbitration | "One message per customer" | arbitration |
| recipe | "The fixes you approved" | recipe |
| model names (Ensemble (LightGBM) …) | *(hidden)* | every algorithm name |

### 4.2 Goal names (display names only; ids, configs and APIs unchanged)

| Today | Goal name | Where |
|---|---|---|
| Targeted Advertisement | Find customers likely to buy | Goals |
| Win-back Campaign | Win back lapsed customers | Goals |
| Telco Customer Churn / churn | Keep customers likely to leave | Goals |
| Payment Propensity | Get customers to pay on time | Goals |
| RCA (Root Cause Analysis) | Understand why customers leave | Goals |
| E-mail Offer Choice (library) | Choose the best e-mail for each customer | Goals (demo) |
| AI Onboarding Assistant | Answer customer questions (AI assistant) | Goals |
| Order Fulfillment, Fault Prediction | *(unchanged)* | More goals |

The lifecycle stage (Awareness … Win-back) becomes a small label on each goal card. No colour legend is needed.

---

## 5. Milestones

Each milestone records one DEC entry with lettered sub-decisions. UI tests (jsdom and Playwright) change deliberately and
are never loosened. Every page keeps its RoutePolicy behaviour; no role sees more than today.

### Phase 1 — Quick wins (M170–M173)

**M170 — Plan M set-up, the glossary and the visible-text check** (DEC-1600)
- Claim DEC-1600…1699 and add `PLAN-M` blocks to the shared files, after PLAN-K.
- `ui/labels.js` exports the glossary (§4.1) and a `term()` helper. Pages use it as they are touched; M172 does the sweep.
- **The visible-text check**: a jsdom test renders each main page against the demo fixtures. It collects the visible text,
  excluding `<details class="tech">`, `[data-technical]` and the Technical tab, and fails on any glossary "never shown"
  term. Each page starts on an allow-list; M172 empties the list, and the test then forbids regressions.
- Acceptance: the claim, blocks and test exist. The allow-list records today's violations per page as the baseline.

**M171 — One contact list** (DEC-1601)
- One file for the marketer. `treat_list.csv` (M98) has every column a sending tool needs, so it becomes **"Download
  contact list"** wherever a list is offered. The scored list (`scores.csv`) moves under Technical details as "All scores
  (for analysts)".
- One sentence explains the gap between "worth contacting" and "will be contacted" on the run and output pages, using the
  numbers' own sources: comparison group, offer budget, consent and channel.
- The Output page drops to: tiles, Priority table, "Why these customers?", contact-list sample, and download. The holdout
  calculator moves to the campaign's Review step (Phase 2); until then it sits in a closed "Test size" section. Drift
  events move to Technical details.
- Acceptance: one download per page; the sentence is computed, not typed; jsdom and Playwright tests updated; file
  contents unchanged.

**M172 — Words and numbers** (DEC-1602)
- The glossary is applied across every page, and the visible-text allow-list is emptied.
- Goal display names follow §4.2; the operational goals go under "More goals"; the colour legend is replaced by labels.
- One money and number formatter (`ui/format.js`): ₹, lakh and crore, rounded for display, exact in tooltips. It is
  applied to Results, Value proven, Proof Pack tables and tiles. Server sentences already carry ₹; they are checked, not
  re-worded.
- Short campaign names on lists (≤ 40 characters, generated from goal and month); the long label becomes a subtitle.
- Acceptance: the visible-text test passes with an empty allow-list; snapshots of the money format; no API change.

**M173 — Help and loose ends** (DEC-1603)
- Help (?) menu with: take the tour (works outside demo mode), glossary (rendered from `ui/labels.js`), send feedback, and
  what's new.
- `#/cost` is linked from Settings, `#/industry/<id>` is removed (it redirects to Home), and `#/doctor`, `#/audit` and
  `#/monitoring/runs` get a second, logical way in.
- The broken data-readiness page on the demo is fixed at its cause.
- Pages stop fetching what they can tell does not exist (no console 404s or 409s for empty states). A Playwright sweep of
  every route asserts no console errors.
- Acceptance: every route is reachable by clicks from the menu (a crawler test); zero console errors on the demo sweep.

### Phase 2 — Campaign-centred structure (M174–M180), behind `ui.layout`

**M174 — The new shell** (DEC-1604)
- Left sidebar (§3), the top-right bell, Help and profile, and folding below 900 px.
- `ui.layout: classic | campaigns` deployment setting; old hashes redirect (§1 M5) through one table that a test reads.
- Acceptance:
  - keyboard navigation works and focus is visible;
  - the redirect table covers every old route (the test lists all registered routes);
  - with `classic`, the app is byte-identical to Phase 1.

**M175 — Overview** (DEC-1605)
- The page in §3: needs attention, value proven headline, live campaigns, + New campaign, first-visit checklist.
- One additive API, `GET /overview`, built from existing summaries (M105 cards, value to date, campaign list) so the page
  makes one call.
- Acceptance: on the demo the page fits one screen at 1440 × 900; each card has one action; empty states for a fresh install.

**M176 — Campaigns list and the campaign page** (DEC-1606)
- The list with status chips (Draft, Live, Waiting for results, Measured), and the three-tab page (§3.2).
- The Results tab is the single place to add results. The four other entry points redirect to it.
- "Refresh the list" re-scores with the current model through the existing route.
- Acceptance:
  - every campaign on the demo opens on all three tabs;
  - adding results from the tab produces the same report as today's route (byte-for-byte figures);
  - the other entry points redirect.

**M177 — "New campaign" wizard** (DEC-1607)
- The five steps (§3.1). Step 2 embeds Guided setup unchanged (Plan G/L).
- The wizard creates the campaign record and registers its test plan, reusing the planner's own numbers.
- First campaign for a goal: train, then score. Later campaigns: score with the approved model. An unapproved model sends
  the campaign to the bell.
- Acceptance:
  - a Playwright journey on the demo goes from + New campaign to a downloaded contact list in ≤ 5 decisions (clicks
    pinned in the test);
  - the test plan is frozen before download;
  - nothing new in the engine.

**M178 — Reports** (DEC-1608)
- Value Proof Packs, programme summary, **Check a past campaign** (today's audit form, regrouped into three short steps),
  and model reports (only with technical details on).
- Acceptance: every report reachable today is reachable here; the audit form's results are unchanged.

**M179 — Data** (DEC-1609)
- Connections and uploaded files on one page with health and last refresh, plus the "Check my data" (Data Doctor) entry.
- Acceptance: everything on today's Connections page is on this page, and Data Doctor is reachable in one click.

**M180 — "Show technical details"** (DEC-1610)
- A per-person switch, remembered: server-side for signed-in users, in browser storage when sign-in is off.
- With it on: the Technical tab on campaigns; the Data, Model and Output pages; model reports; algorithm names; Setup's
  Advanced settings.
- With it off, those are hidden. Roles still decide what a person may do.
- Acceptance: the visible-text test runs in both modes; with the switch off, no glossary "never shown" term appears
  anywhere.

### Phase 3 — Prove it (M181)

**M181 — Usability evidence and the switch-over** (DEC-1611)
- A five-person test kit (`docs/ux/USABILITY_TEST.md`) with two tasks, no help, and timed:
  1. Plan a win-back campaign and download who to contact.
  2. Show what last month's campaign earned.
  The kit includes the observer's sheet, the success rules and the questions. Results are recorded by a person, never
  invented.
- Playwright journeys pin the click counts; a screenshot set is kept for visual review; basic accessibility checks
  (contrast of the palette, labels on every control, keyboard path through the wizard).
- If the test meets §0's targets, `ui.layout` defaults to `campaigns`; otherwise the gaps are fixed first and the result
  is recorded.

---

## 6. Rules of work

- **Ownership.** Plan M owns:
  - `ui/chrome.js` navigation, `ui/labels.js`, `ui/format.js`;
  - `ui/modules/simple/**`, and new `ui/modules/campaigns/**`, `ui/modules/overview/**`, `ui/modules/reports/**`;
  - `docs/ux/**`.

  Edits in other modules' pages (Plan G's agent, Plan J's decide and measure, Plan K's agentic, Plan L's doctor and intake,
  production, pilot, uplift) are presentation-only. They are listed per milestone in `docs/CROSS_BRANCH_REQUESTS.md`.
- **The engine and API do not change behaviour.** New endpoints are additive summaries with a RoutePolicy.
- **Tests.**
  - jsdom tests for every page touched.
  - Playwright journeys for the two key tasks.
  - The visible-text check (M170).
  - The route crawler and console-error sweep (M173).

  Existing UI tests are updated deliberately, with the reason in the DEC, and never loosened.
- **Accessibility and devices.** Keyboard reachable, labels on every control, phone width (390 px) usable for Overview,
  Campaigns and the campaign page.
- **Make lint test before every commit; make test-all before every merge.**

## 7. Risks

| Risk | Mitigation |
|---|---|
| Analysts lose the detail they rely on | "Show technical details" brings back every page unchanged, and old links redirect |
| UI tests churn | Each milestone updates the tests it changes, deliberately; the visible-text check prevents the jargon coming back |
| Campaign-centred flow hides a real engine case (several offers, arbitration, uplift without a campaign) | The wizard covers single and multi-offer; arbitration shows as "One message per customer" on the Review step; everything else stays in Settings → Advanced with technical details on |
| Our own taste is not the users' | M181's five-person test decides the default layout, not us |

## 8. Open questions for the product owner

1. Who are the five testers for M181 (CRM or retention managers who have not seen the product)?
2. Is a brand colour and logo set ready for the sidebar, or do we keep the current palette?
3. Should "Campaigns" also list campaigns run in other tools (audited ones), or only Reports → "Check a past campaign"?
   We recommend listing them, marked "Run elsewhere".
