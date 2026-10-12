# Visual design: the design system as built

Plan M M185 (DEC-1615). The look of Marketing AI lives in two files:

- `ui/theme.css`: the font, the tokens, base type, the page frame, the shell and every component, as classes. It is
  loaded by one `<link>` in `ui/index.html`'s PLAN-M block, after every other style, so it wins the cascade. Both
  layouts use it. The classic layout gets the new font, colours and components; its markup is not changed.
- `ui/components.js`: one helper per component. Each is a pure function that returns HTML and escapes every piece of
  text it is given. Pages use these helpers rather than writing their own markup for a button, a chip or a tile.

The look changes how things are drawn, never what a page does. Dark mode is out of scope.

## Tokens

| Group | Token | Value | Use |
|---|---|---|---|
| Font | `--font` | Inter Variable (shipped in `ui/fonts/inter/`, OFL-1.1), then system fonts | everything |
| Type | `--fs-title` / `--lh-title` | 28 / 36, weight 600 | the page title (H1); 24 / 32 on a phone |
| | `--fs-section` / `--lh-section` | 18 / 26, 600 | a section title |
| | `--fs-card` / `--lh-card` | 15 / 22, 600 | a card title |
| | `--fs-body` / `--lh-body` | 14 / 22, 400 | body text |
| | `--fs-small` / `--lh-small` | 13 / 20 | table cells, help text |
| | `--fs-caption` / `--lh-caption` | 12 / 16, muted | captions, table headers |
| Neutrals | `--ink` `--ink2` `--muted` `--line` `--line2` | #111827 #374151 #667085 #E5E7EB #D1D5DB | text, secondary text, captions, hairlines |
| | `--bg` / `--surface` / `--soft` | #F5F6F8 / #FFFFFF / #F9FAFB | the canvas, cards, hover |
| Brand | `--brand-blue` | #1E57BD (a deployment may set its own, M182) | primary buttons, active navigation, links, focus rings, selected states |
| | `--brand-50` `--brand-100` `--brand-200` | 8%, 12%, 24% of the brand colour on white | selected backgrounds, range bars |
| | `--brand-700` | the brand colour with 12% black | a primary button on hover |
| Status | `--good` / `--good-t` | #15803D on #E8F6EE | good |
| | `--warning` / `--warning-t` | #B45309 on #FEF3C7 | needs attention |
| | `--critical` / `--critical-t` | #B42318 on #FDECEC | a problem |
| | `--info` / `--info-t` | the brand colour on its tint | in progress |
| | `--neutral` / `--neutral-t` | #374151 on #F3F4F6 | not started, not yet known |
| Spacing | `--s1` to `--s7` | 4, 8, 12, 16, 24, 32, 48 px | every gap and padding |
| Shape | `--r-sm` `--r` `--r-lg` `--r-pill` | 6, 8, 12, 999 px | controls 8, cards 12, chips round |
| Frame | `--page-max` `--side-w` `--bar-h` | 1200, 216, 56 px | the page width, the sidebar, the top bar |
| Motion | `--dur` `--ease` | 140 ms, ease-out | hover, press, the drawer. Turned off when the person asks for reduced motion |

Rules:

- Every brand-coloured thing reads `var(--brand-blue)` or one of its tints, so a deployment's own colour reaches it.
  No rule names the default blue.
- Status colours mean status. They always come with an icon and a word, never colour alone.
- Every text and background pair reaches 4.5:1 (3:1 for large text). The contrast test reads these tokens.
- Charts keep the validated blue ramp `--bs0` to `--bs4`. Do not add chart colours.

## The page frame

- There is no page card. Content sits on the canvas, at most 1200 px wide, with 32 px padding on a desktop and 16 px on
  a phone.
- Cards are white with 12 px corners and a 1 px `--line` border. This is the only card style. A card inside a card
  loses its border.
- The page header has the title and a one-line subtitle on the left, and the page's one primary button on the right.
  On a phone the button goes below the text.
- Breadcrumbs: the rule is that they appear only on pages below a top-level place, never on Overview, Campaigns, Data,
  Reports or Settings. `pageHeader` takes `crumbs` and draws nothing without them. This is the target the page
  milestones (M186 to M188) apply: as built in M185, the older page modules still draw a single-level crumb on
  Overview ("Home › Overview") and Campaigns.
- The shell (campaigns layout):
  - The sidebar runs the full height of the screen. The product's name and mark are at its top, then the five places,
    each with a 24 px line icon.
  - The active place has a brand tint, brand text and a 3 px brand bar on its left.
  - "by Minfy" sits small at the sidebar's foot.
  - A deployment's own logo (M182, `MARKETING_AI_BRAND_LOGO_URL`) takes the same place at the sidebar's foot, without
    the "by"; on a phone it appears inside the drawer, at its foot.
  - The top bar is 56 px tall and white with a hairline. It holds the bell and Help on the right, each a 40 x 40 target
    on every screen size. There is no profile menu yet.
  - On a phone the sidebar is a drawer behind **Menu**, and Minfy (or the deployment's logo) appears at the drawer's
    foot.

## Components (`ui/components.js`)

| Helper | Draws | Use it for | Do not |
|---|---|---|---|
| `pageHeader({title, subtitle, crumbs, primary, secondary})` | H1, one-line subtitle, the primary action on the right | the top of every page | add a second primary button, or breadcrumbs on a top-level place |
| `button({label, href or id, kind, size, icon, disabled, busy})` | `primary` (brand fill), `secondary` (white, hairline), `tertiary` (brand text), `danger`; 40 px, or 32 px with `size: "sm"` | every action | colour a button by hand, or use primary twice on a page |
| `statusChip(kind, value, {word})` | a 24 px pill with a 12 px icon and the word, in the status colours | a campaign's status, what a result can claim, a source's health | show a status as colour alone |
| `tagChip(label)` | a quiet grey outline | labels that are not a status ("Win-back") | use a status colour for a label |
| `statTile({label, value, unit, caption, range})`, `kpiRow(tiles)` | label (13 px muted), value (28 px, proportional figures), caption; a row of 2 to 4 (two to a row on a phone, where a unit that does not fit wraps whole to its own line) | the few numbers that matter on a page | put more than four in a row, or a sentence in the value |
| `valueRange({low, high, estimate, text})` | a small interval bar (160 px at most, shrinking to 64 px in a narrow tile or cell): track, the range in brand, a dot at the estimate, a tick at zero when the range is near it, with the words beside it | every "likely X to Y" | drop the words: they are what a screen reader reads |
| `emptyState({icon, title, text, action})` | a 40 px line icon, one sentence, one action | every list or card that can be empty | leave a list blank, or offer two actions |
| `tabs({label, items, current})` | underline tabs with role=tablist and role=tab, `aria-selected`, a roving tabindex; Left, Right, Home and End move (`wireTabs`, installed by the bar) | the campaign page, Reports, Data | draw tabs as pills or buttons |
| `stepper({steps, current})` | numbered circles joined by a line: done is a brand circle with a check, current is a brand ring, upcoming is grey; labels below; on a phone, "Step 2 of 5 · Customers". A done step given as `{label, href}` is a link back, reachable by Tab with the focus ring | the New campaign wizard | use it for navigation between pages |
| `attentionItem({tone, title, text, meta, action})` | a coloured icon that names its tone, a title, one line, the action on the right (below on a phone) | "Needs your attention" and alerts | squeeze the text beside its button on a phone |
| `statusOf(kind, value)` | `{key, tone, icon, word}` | anything that needs the mapping without the chip | invent another mapping |

Tables use the existing `dataTable` (`ui/dom.js`, class `.tbl`). Headers are 12 px, weight 600, muted, in sentence
case. Rows are at least 52 px tall and tinted on hover. Number cells and their headers take class `num`
(`<td class="num">`): right-aligned, tabular figures. A long explanation does not belong in a cell: show the number (and
a value range) and put the explanation on a second line, `<span class="td-sub">…</span>` inside the cell (12 px, muted,
its own line), or on the detail page. On a phone the table becomes stacked row cards.

Forms use 40 px controls (`.input`) with 8 px corners, a hairline and a brand focus ring. The label goes above
(`.form-label`) and help text below (`.form-help`).

## The status mapping (`statusOf`)

| Kind | Value | Tone | Icon | Word |
|---|---|---|---|---|
| campaign | `draft` | neutral | dashed circle | Draft |
| | `live` | info (brand) | dot | Live |
| | `waiting` (`waiting_for_results`) | warning | clock | Waiting for results |
| | `measured` | good | check | Measured |
| evidence | `proven` (`causal`) | good | check in a circle | Proven |
| | `estimated` | info (brand) | approximately | Estimated |
| | `not_yet_proven` (`not_proven`) | neutral | dashed circle | Not yet proven |
| health | `read` | good | check | Read correctly |
| | `connected` | good | check | Connected |
| | `not_tested` | warning | clock | Not tested yet |
| | `needs_attention` | warning | warning sign | Needs attention |
| | `failed` | critical | cross in a circle | Failed |

An unknown value is neutral and shows its own text. `{word}` replaces the word with the server's own sentence, for
example "Failed: the file is empty", and keeps the tone and the icon. Until the page milestones draw `statusChip`, the
theme gives the chips the older modules draw the same tones and icons (`theme.css` section 8):

- the campaign status chip (`.chip.cp-status`) on Campaigns and the campaign page;
- the evidence label on Reports (a pill or chip inside `[data-claim="proven|estimated|not-proven"]`);
- the health chips on Data and the Connections page (`.cn-badge` with `data-dt-status` or `data-cn-status`).

M187 and M188 replace these with `statusChip("evidence" | "health", …)` and then the bridges can go.

Chips keep their own width in any parent (`width: fit-content`): a chip in a card is never a full-width band.

## Accessibility

- Contrast is AA everywhere. `--muted` is #667085, one step darker than before, so captions on the canvas still
  reach 4.5:1.
- Focus is visible everywhere: a 2 px brand ring, offset 2 px.
- Touch targets are at least 40 px, the bar's bell and Help on a phone included.
- Status never relies on colour alone.
- Motion is off when the person asks for reduced motion.

## Tests

- `tests/integration/test_components_ui.py` checks the font, the link, the tokens and the brand rules. It also runs
  the components in jsdom (`production/ui/components/`).
- `production/ui/a11y/contrast.test.mjs` checks contrast with the tokens of `index.html` and then `theme.css`.
- `test_shell_browser.py` checks the look in Chromium at 1440 px and 390 px.
- `test_brand_browser.py` checks that a custom colour recolours the primary button, the active place's tint and bar,
  and the selected tab.
- `python -m scripts.ui_screenshots <folder>` takes the screenshot set for a visual review.
