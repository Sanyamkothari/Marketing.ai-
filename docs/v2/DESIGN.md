# Autonomy design system: calm, premium, made for daily use

**Who it's for.** A business owner or marketer who opens the app every morning to see what happened,
approve a batch, check proof and get on with their day. It should feel like a quiet, expensive tool:
think Linear, Stripe Dashboard or Mercury, not a landing page.

**Sources.**
- The app-screen rules of [taste-skill](https://github.com/leonxlnx/taste-skill) (MIT), a study of
  23 real product screens, copied in full to `docs/v2/design-reference-app-screens.md`.
- The component feel of [21st.dev](https://21st.dev) and shadcn: crisp 1px borders, near-black
  primary buttons, segmented controls, generous whitespace.
- **We copy the patterns, not the code.** Our UI is plain HTML, CSS and ES modules, with no
  framework, no build step and no CDN.

---

## 1. The feel in five rules

1. **The data is the design.** No decoration that doesn't carry meaning: no gradients, glows,
   glass, icons in coloured circles, sparklines on every number, emoji or illustrations.
2. **One quiet surface system.** Off-white page, white cards, 1px hairline borders, no shadows on
   resting cards. Shadows only on things that float (menus, popovers, dialogs, toasts).
3. **One accent, used sparingly.** The brand blue appears on data (the proof bar, the selected
   thing) and on focus only, about 3% of pixels. The primary button is near-black.
4. **Type does the hierarchy.** One face (Inter, self-hosted), four sizes, three weights, tabular
   figures on every number.
5. **Calm motion.** 120–200 ms ease-out on hover and press, no bouncing, and nothing moves when
   data loads.

---

## 2. Tokens (put on `:root` in `ui/v2/styles.css`)

```css
:root {
  /* light */
  --ground: #f9fafb;  --surface: #ffffff;  --raised: #f4f5f6;  --active: #eeeeee;
  --border: #e8e8e8;  --rule: #f1f2f3;     --control: #d1d5db;
  --text: #111111;    --muted: #5f6368;    --faint: #6b6b6b;      /* every text grey is >= 4.5:1 */
  --primary: #1d1f20; --primary-ink: #ffffff;                     /* near-black primary button */
  --accent: #1e57bd;  --accent-tint: #edf2fa;                     /* brand blue: data, focus, selected */
  --positive: #247b50; --negative: #c4242a; --warning: #92400e;
  --pill-positive-bg: #dcfae5; --pill-positive-ink: #04250e;
  --pill-negative-bg: #ffedea; --pill-negative-ink: #831c1e;
  --pill-warning-bg:  #fffbeb; --pill-warning-ink:  #92400e;      /* notice border #fde68a */
  --pill-neutral-bg:  #eeeeee; --pill-neutral-ink:  #3d3d3d;

  --font: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --fs-title: 24px; --fs-section: 16px; --fs-kpi: 28px; --fs-body: 14px; --fs-small: 12px;
  --lh-body: 20px;  --lh-small: 16px;

  --radius-card: 8px; --radius-control: 6px; --radius-chip: 999px;   /* tight radii, chosen once */
  --gap: 16px; --card-pad: 20px; --pad: 24px; --section-gap: 32px;
  --control-height: 34px; --row-height: 48px; --touch: 44px;

  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
  --t-press: 120ms; --t-hover: 150ms; --t-ui: 200ms;
  --shadow-float: 0 8px 24px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);  /* menus, dialogs, toasts only */
}
/* dark: true neutrals, elevation by lightness, no navy */
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* same as below */ } }
:root[data-theme="dark"] {
  --ground: #0a0a0a; --surface: #111111; --raised: #1a1a1a; --active: #222222;
  --border: #262626; --rule: #1c1c1c; --control: #404040;
  --text: #f5f5f5; --muted: #b4b4b4; --faint: #949494;
  --primary: #f5f5f5; --primary-ink: #0a0a0a;
  --accent: #8bb0f5; --accent-tint: #172238;
  --positive: #3fc07a; --negative: #e5566f; --warning: #e5a93b;
  --pill-positive-bg: #172a1d; --pill-positive-ink: #3fc07a;
  --pill-negative-bg: #2c1a20; --pill-negative-ink: #e5566f;
  --pill-warning-bg:  #2b2414; --pill-warning-ink:  #e5a93b;
  --pill-neutral-bg:  #222222; --pill-neutral-ink:  #f5f5f5;
}
.num, table, .kpi-value { font-variant-numeric: tabular-nums; }
```

**Font.** `@font-face` for Inter from `ui/fonts/inter/` (already in the repo, OFL), served with the
app. No external font requests.

---

## 3. Type scale

| Role | Size / weight | Notes |
|---|---|---|
| Page title | 24 / 600, -0.01em | A noun ("Live", "Proof"), never a hero headline |
| Section / card title | 16 / 600 | Sentence case |
| Body, table cells, nav | 14 / 400, line height 20 | |
| KPI value | 28 / 600 | The only big numbers; money with its range under it at 12 |
| Labels, meta, captions | 12 / 400–500, `--muted` or `--faint` | Never under 11 |
| Pills | 12 / 500 | Pale tint with dark ink of the same hue |

At most 6 sizes on a screen. Weight 700 does one job per screen at most.

---

## 4. Components (shadcn-like, hand-written)

- **Buttons:** primary (near-black fill), secondary (white with a `--control` border), ghost (text
  only) and destructive (red text or outline; the big Pause is a solid near-black button with a
  pause glyph, turning red only while paused). Height 34px (44px on a phone), radius 6, 150 ms hover.
- **Segmented control** for the autonomy dial (Shadow | Approve | Autopilot): one rounded track,
  with the selected segment on white plus a hairline border. One sentence under it explains the
  choice.
- **Cards / panels:** white, 1px `--border`, radius 8, padding 20, **no shadow**. Group KPIs in
  *one* card split by hairlines, not four floating cards.
- **KPI strip:** label (12, `--muted`) over value (28/600) over range (12, `--faint`). No icons, no
  sparklines, no green "+12%" pills.
- **Tables / lists:** 12px grey sentence-case headers, 1px row rules, no zebra stripes, numbers
  right-aligned in tabular figures. They become two-line rows under 640px.
- **Pills:** pale tint + dark ink. Status in words ("Sent", "Left alone", "Waiting for you").
- **Decision row** (Live feed): customer, action in plain words, channel and time in muted text, the
  reason sentence on line 2. "Left alone" rows are equally first-class, in neutral ink.
- **Empty states:** keep the frame and the title, with one sentence and the next action. No
  illustrations.
- **Loading:** grey blocks at the real size of the content, no shimmer, no layout shift.
- **Errors:** inside the block that failed: one sentence, the time of the last good data, and a Retry
  link.
- **Notices** (for example "Not proven on your business yet: start in Shadow"): `#fffbeb` with a
  `#fde68a` border and `#92400e` 12–13px text. Calm, not alarming.
- **Toasts / dialogs:** the only things with `--shadow-float`.

---

## 5. Layout

- **Shell:** a quiet top bar on the page ground, holding the wordmark, goal switcher, autonomy badge,
  "To approve" count, Inbox count and Pause. Under it, the five numbered steps as a tab row. No
  coloured sidebar.
- **Canvas:** max width 1200px, centred, 24px padding (16px on a phone). One left edge and one right
  edge, with every block aligned to them.
- **Grid:** 12 columns with a 16px gap. Mixed spans (8+4, 7+5) beat uniform four-up grids.
- **Every screen answers one question**, and its biggest block answers it:
  - Live: what happened, and what needs me?
  - Proof: did it make money?
  - Plan: what will it do, within what limits?
- **Phone (390px):** the top bar becomes 48px with the title and Pause. The steps scroll sideways in
  their own row. KPIs go into a two-column grid, tables into two-line lists. Touch targets are at
  least 44px.

---

## 6. Words

- Plain English for a business owner. Say "people we never contact" first; "holdout" only in small
  print.
- Sentence case everywhere. No exclamation marks. Numbers come from the API, money always with its
  range, and "Not sure yet" when the range includes zero.
- The product's honest status shows where it matters, quietly (a notice), never as a banner.

---

## 7. Banned (the generated-dashboard tells)

- Gradients, glows, glass, backdrop blur.
- Navy or tinted sidebars.
- Icons in tinted circles.
- Sparklines on KPIs; rainbow charts (one hue per chart, neutrals for the rest).
- Violet or indigo accents.
- Uppercase tracked table headers; zebra rows.
- Emoji in the UI.
- KPI numbers at 48px or more.
- A hero header or "Welcome back".
- Lorem, Acme, or vanity totals.
- Pagination under five rows.
- Coloured buttons everywhere (the accent appears on one control at most).

## 8. The check (every screen, before it ships)

1. Screenshots at 1440×900 and 390×844, light and dark. **Look at them.**
2. Check every text colour is at least 4.5:1; focus rings are visible; Tab order is sensible.
3. Count font sizes (6 at most), accent use (data and focus plus one control) and shadows (floating
   things only).
4. Confirm nothing shifts when data loads, and empty, error and loading states exist for every
   block.
5. Ask one question: would a marketer open this every morning, and would it feel like a tool they
   paid good money for?

---

## 9. How to use the system (for screen builders)

Everything below lives in `ui/v2/styles.css` (tokens, base, components), `ui/v2/screens/shell.css` (the top
bar, the steps, the note under them) and `ui/v2/components.js` (helpers that build the markup). Inter is served
from `ui/v2/fonts/` (OFL, `font-display: swap`); nothing is fetched from outside.

**Tokens.** Use the section-2 names only: `--ground --surface --raised --active --border --rule --control --text
--muted --faint --primary --primary-ink --accent --accent-tint --positive --negative --warning --pill-*`, the type
sizes `--fs-*` / `--lh-*`, `--radius-card|control|chip`, `--gap --card-pad --pad --section-gap --control-height
--row-height --touch --canvas`, the motion `--ease-out --t-*` and `--shadow-float`. Working extras: `--accent-ink`,
`--notice-bg|border|ink`, `--skeleton`, `--ring` (the focus halo on fields), `--mono`. The old names (`--bg --ink
--brand* --good* --critical* --neutral* --surface-2 --r --r-sm --pill --gutter --max --shadow --pop` and every
`--ap-*`) are aliases of these and read the new palette; move to the new names when you touch a file, and
delete your screen's own `:root` token blocks (feed.css) once nothing reads them. `--shadow` and `--ap-shadow`
are `none` on purpose. `?theme=light|dark` forces a theme (`[data-theme]` on `<html>`).

**Page frame.** `.main` is the canvas (1200px centred, 24px padding, 16px on a phone). Start a screen with
`pageHead({title, sub, actions, stamp})` → `.page-head` > `h1.page-title` + `p.page-sub`, actions and a
`freshness(iso, {onRefresh})` stamp on the right. Lay blocks out with `.grid` (12 columns, 16px gap, cards keep
their own height) and `.span-3 … .span-9` on the children (full width on a phone); `.stack` for a vertical run,
`.row` for an inline cluster, `.section-head` for a heading row between blocks.

| Need | Class | Helper |
|---|---|---|
| Card / panel | `.card` (`.panel`), `.card-head` > `.card-title` + `.card-meta`, `.card-actions`, `.card-flush` for an edge-to-edge table, `.divider` | `card({title, meta, body, actions, flush})` |
| Buttons | `.btn` + `.btn-primary` (one per screen) / `.btn-secondary` / `.btn-ghost` / `.btn-danger` (= destructive); `.btn-sm`, `.btn-lg`, `.btn-block`; `.link-btn` for Retry, View all | `button(label, onClick, {kind, size, block, disabled})` |
| Fields | `.field` > `label` + input + `.field-hint` / `.field-error`; `.form-row`, `.form-actions` | — |
| Autonomy dial | `.segmented` (radios in one track) + `.segmented-note` (the one sentence) | `segmented({legend, options, value, onChange, note})` |
| KPIs | `.kpis` > `.kpi` > `.kpi-label`, `.kpi-value`, `.kpi-range` / `.kpi-note` (one container split by hairlines; 2 columns on a phone) | `kpiStrip([{label, value, range, note}])`; pass money through `range()` so it always carries its range |
| Tables | `.table-wrap` > `table.table-stack`; `th.num` / `td.num` right-aligned; `td.is-key` is the figure kept on line 1 under 640px | `table({columns: [{key, label, align, lead, format}], rows, empty})` |
| Lists of rows | `.list` > `li` with `.row-title` and `.row-meta` (one rule between rows) | — |
| Status | `.pill` + `.pill-good|warning|critical|info|neutral` (also `-positive|-negative|-accent`), `.pill-dot` | `pill(text, tone, {dot})`, `statusTone(status)` |
| Honest status | `.notice` (amber), `.notice-neutral` (grey aside) | `notice(children, {tone})` |
| Loading | `.skeleton` blocks at the content's real size; `.loading` for a whole screen | `skeleton({width, height})`, `skeletonLines(n)`, `skeletonKpis(labels)`, `loading()` |
| Empty | `.empty` > `.empty-title` + one sentence + the next action | `emptyState(title, text, action)` |
| Error | `.error-box` in the failed block, `.error-actions` (last good time, Retry) | `errorBox(title, err, {lastGood, retry})` |
| Freshness | `.stamp` | `freshness(iso, {label, onRefresh})` |
| Floating | `.toast` (bottom right), `.dialog` (`<dialog>`, the only other shadow) | `toast(message, tone)`, `dialog({title, body, actions}).showModal()` |
| Numbers | `.num` / `.tabular` (tabular figures; `.num` also right-aligns) | `int`, `num`, `pct`, `money`, `range`, `dash` |

**Rules of thumb.** Ink links (`a` is `--text` with a grey underline); the accent only on data, focus and the one
selected thing. One `.btn-primary` per screen. A card's period or stamp goes in `.card-meta`, not in its title.
Use `h2.section-title` / `.card-title` at 16px and `h3` at 14px; no other sizes besides 24 (title), 28 (KPI) and
12 (labels). Never hide a promise on a phone unless the same words are on screen elsewhere (the shell hides the note's trial day only because the badge above says it): fold it to line 2 or a short line instead. Nothing new
gets a `box-shadow` unless it floats.

---

## 10. Status (20 Oct 2026)

**Applied.** The whole marketer app (`ui/v2`) now runs on this system: the section-2 tokens in light and dark
(`styles.css`, with the old names kept as aliases), Inter served from `ui/v2/fonts/`, the shell's quiet top bar and
numbered steps (`shell.css`), and the helpers in `components.js`. Every screen was rebuilt on it, then reviewed
by a separate critic against screenshots at 1440×900 and 390×844, light and dark, on the demo API and on
`?mock=1`, and fixed in rounds: Getting started, Connect and Goal; Plan; Live (with the shared `feed.css`); Proof
and Operator. A last pass made the shared parts consistent across screens: KPI strips inside a card are open
(no box in a box), `.pill-info` and the shell's Approve badge are neutral (the accent stays on data, focus and
the To approve count), every fold has the same drawn chevron, empty states are a solid hairline on `--raised`
instead of a dashed border, `.page-sub` avoids one-word last lines, a stacked table keeps its caption full width,
only one primary button shows per screen (the newest batch's Approve on Live), and pictures that can never load
are not requested. Every promise in PROMISE_MAP.md is still on screen, every number comes from the API, money
keeps its range, and the UI, app, promise and API tests pass with no promise check dropped.

**The critics' final scores** (out of 10):

| Screen | Premium feel | Clarity | Consistency | Phone | Dark |
| --- | --- | --- | --- | --- | --- |
| Getting started, Connect, Goal | 8 | 8 | 8.5 | 8 | 8.5 |
| Plan | 8 | 8 | 8 | 7.5 | 8 |
| Live | 8 | 8 | 8.5 | 8 | 8.5 |
| Proof, Operator | 8 | 8 | 8 | 7.5 | 8 |

**Still not premium enough.**
- The autonomy dial is three described radio cards, not the segmented control of section 4: the tests pin all
  three levels' sentences on screen (promises 3 and 19), which a segmented track would hide.
- The page head is not yet one pattern: Plan puts its status pills on the right, Live puts them under the title
  (Pause takes the right), Proof writes the level as a grey line. One shared slot for status would be calmer.
- Live: on Approve, the side column ends well before the main column (cards keep their own height), and the
  feed shows raw product ids (`bars_trail`) when the data has no names. The weekly briefing from the runtime
  still leads with a bare money figure and the word "holdout".
- Plan: a message the editor failed says so four times; the limits strip repeats three facts from the plan card
  below it; `--rule` hairlines are very faint in dark mode.
- Proof: the share text and the KPI round the return on spend differently (90× against 90.1×); the chart repeats
  each bar's range as text. Operator on a phone still lists all 200 decisions before the customer lookup.
- Error boxes cannot show "the time of the last good data" yet, because nothing records it.
- The demo data has no product names, no first names and pictures on a host that never resolves, so the real-data
  screenshots look sparser than the product will.
