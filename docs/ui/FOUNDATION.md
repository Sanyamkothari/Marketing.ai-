# UI foundation (v1, WP0)

The shared pieces every screen package (WP1 to WP9) builds on. The design rules they implement are in
[`docs/UI_AUDIT.md`](../UI_AUDIT.md) section 3; the before-screenshots are in `docs/ui/before/`.

## Shoot screens

```
PLAYWRIGHT=/opt/node22/lib/node_modules/playwright/index.mjs \
BASE_DEMO=http://localhost:8791 BASE_EMPTY=http://localhost:8792 BASE_AUTH=http://localhost:8766 \
ONLY=home,run-output node scripts/ui_screens.mjs          # writes docs/ui/after/
```

`OUT` changes the output directory (default `docs/ui/after`), `FIXTURES` the upload files (default
`docs/ui/fixtures`), `SKIP_MOBILE=1` / `SKIP_DARK=1` shoot faster, `INVENTORY=1` lists the screens.

## CSS components (`ui/index.html`, "v1 foundation" block)

| Need | Use |
|---|---|
| Buttons | `.btn` + `.primary` (one per screen) / `.secondary` / `.quiet` / `.danger` (`.confirm` on the confirm step), `.sm` for row actions. `.run`, `.linkbtn`, `.cancel`, `.summary .again` stay as aliases. Disabled: the `disabled` attribute, reason in `.reason`. |
| Colour | Tokens only. New aliases `--btn` / `--btn-ink` (primary fill and label, light and dark). Text is `--ink`, `--ink2`, `--muted`; `--faint` never for text. |
| Cards | `.card`, `.card h3`, `.kv`, `.card-body`; calm notice `.card.notice-card` + `.notice-actions`. |
| Tables | `dataTable()` (below): `.tbl`, `.more` columns behind `details.tbl-more`, `.tbl-stack` stacked cards under 700px, `td.num` right-aligned with tabular figures. |
| Details | `details.tech` ("Technical details"), `details.adv` ("Advanced"). |
| States | `.empty-state`, `.apierr`, `.skel-*` (skeleton), `.toggletip`, `.menu` / `.menu-pop`, `.dialog`, `.chip.neutral`, `.count`. |
| Focus | One `:focus-visible` ring on every interactive element; no `outline:none` (a `.control` wrapper draws the ring for its select/input). |
| Page | `.screen` has 96px bottom padding; `#app` is not a live region: use `announceStatus()` for `#status`. |

## `ui/dom.js` helpers

| Helper | What it draws |
|---|---|
| `pageHead(inner)` | The header. The right side is the logo only; the client picker is in the top bar. |
| `crumbs(items)` | `Home › ... › current`. `items` are `{label, href}`; the last without `href` is the current screen. Never pass Home. |
| `backLink(uc)` / `journeyCrumb(uc)` / `journeyBack(uc)` | `Home › journey › use case` (nav), the `Home › journey` links, and the `{href,label}` for "Back to <journey>". |
| `headActions({primary, secondary, related})` | The header's actions in one order; each an HTML string or `{label, href \| id, attrs}`. `related` is a quiet link with `›`. |
| `errorBox(error, {title, retry})` | Plain sentence first (the glossary's `codes.<CODE>.title`; a 4xx's own sentence; else "Something went wrong while loading this page."), the glossary fix, an optional **Try again** (`[data-retry]`, `app.js` redraws the route), code and message in a closed **Details**. Class `.apierr` kept. |
| `notFound(what, back, error)` | "This run could not be found." + one "Back to <label>" button. |
| `noticeCard({title, text, action, attrs})` | A calm notice (planned use case, feature not available). |
| `emptyState({title, text, action})` | What will appear here + the one action that fills it. |
| `skeleton(kind, {title})` | A full `<main class="screen">` of grey blocks; `kind` is `"page"`, `"list"` or `"form"`. Paint it first. |
| `techDetails(pairs, summary)` | Ids, hashes, codes in monospace with copy buttons (`[data-copy]`), in a closed `details.tech`. |
| `toggletip(text, label)` | A `?` with one explanation (wired by `chrome.js`: `aria-expanded`, Escape). |
| `dataTable(cols, rows, {cls})` | `cols` = `{label, num, more}`; rows are cell HTML (escape it). At most 5 columns without `more`. `sortNote(text)` for the card title. |
| `NUMBER_LOCALE` (`en-US`), `DATE_LOCALE` (`en-IN`) | The only locales. `fmtInt`, `fmtPeople` (whole), `fmtMoney` (Intl INR, no literal currency sign), `fmtPct` (1 decimal), `fmtMetric` (3 decimals), `fmtStamp`, `fmtDate`. Replace every other `toLocaleString("en-IN")`. |
| `glossaryCode/Term/Metric/Setting(key)` | Plain wording from `configs/pilot/help.yaml` once registered (below); `null` otherwise. |
| `announceStatus(text)` | A short message to screen readers via `#status`. |

## Top bar (`ui/chrome.js`) and router seams (`ui/modules/router.js`)

`header#pb-bar.topbar` is mounted by `app.js` before `#app`: wordmark, `nav[aria-label="Main"]` with
Home, Build data (`#/pilot/kit`), Models (menu), Campaigns (`#/monitoring/runs`), Reports (`#/pilot`),
Admin (menu), then the context slot, sample-data chip, Help and user. Below 700px: wordmark, picker and
a Menu button. The active goal follows the route (`navFor(hash)` in `chrome.js`).

| Seam (from `modules/router.js`) | Use |
|---|---|
| `registerHeaderTool({name, html()})` | Unchanged. The client picker; drawn in the bar's context slot. Return `""` on routes where it means nothing (WP1). |
| `registerNavSlot(name, {html(), bind?(bar)})` | Slots: `"models"` (extra Models entries, `<a>`s), `"admin"` (Admin menu entries; Admin shows only when non-empty), `"demo"` (sample-data chip), `"help"` (Help entries; Help shows only when non-empty), `"user"` (user menu or Sign in), `"badge:approvals"` (a count). Several registrations per name are drawn in order. |
| `registerAccess({can(method, path), status?()})` | Role visibility. Items with a `need` (Waiting for approval: `GET /approvals`; Model health: `GET /schedules`) are hidden when `can` says no; `status() === "signed-out"` draws the wordmark and the user slot only. No provider: everything is drawn. Safe to call from `boot.js`. |
| `registerGlossary({code, term, metric, setting})` | Functions or maps, or the `GET /pilot/help` catalogue itself (`{codes, terms, metrics, settings}`). |
| `registerRunAction({name, applies(uc, run), html(uc, run)})` / `runActionsHtml(uc, run)` | Actions on a run's results (AI copy, root-cause notes, campaign results). Register from a module's `index.js`. |
| `setActiveNav(id)` | Override the active goal for the current route only (a scoring run's Output is `"campaigns"`). |
| `refreshTopBar()` | Redraw the bar after a slot's own state changed. `MODULES_CHANGED` and route changes redraw it anyway. |

Transitional (until WP1 and WP8 fill the `user`, `admin`, `demo` and `help` slots): the older strips
`#pb-bar` (Phase 4b user bar, renamed `#pb-userbar`) and `#pe-bar` (pilot) are adopted into the bar's
second row, so every link they had is still inside `#pb-bar`. When a module stops mounting its strip,
the row empties by itself. The client picker is still drawn on every route (the onboarding acceptance
test picks a client on Home); WP1 restricts it.

## `app.js`

A skeleton is painted at once on a route change (never on a repaint of the same route). A planned use
case shows the calm "Coming soon" notice named from the cached `/industries`; a missing use case or run
shows `notFound` with a way back (a missing run goes back to its use case); anything else `errorBox`
with Try again and "Back to <journey>". `document.title` uses the use case's name.
