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
   resting cards. Shadows only on things that float (menus, descriptions, dialogs, toasts).
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
  chosen level; each segment's own sentence opens on hover over it.
- **Description:** a short floating box (max 280px, 13px, radius 6, `--shadow-float`) behind a dotted
  underline or a quiet (i). Hover after 150 ms, keyboard focus, or tap; tap again, tap outside or Esc
  closes it. It never shifts the layout and stays inside the window at 390px.
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
- **Toasts / dialogs / descriptions:** the only things with `--shadow-float`.

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

**Numbers first, explanations on hover.** The founder's rule (20 Oct 2026): "there is too much text on each page";
"instead of giving a link on how is this measured, if we hover over something we show the description then only".

1. **One sentence per card, at most.** A card is its title, its numbers, and at most ONE short sentence (what to do,
   or what the number says). Money always carries its range, but the range can be the short line
   ("$781 to $3,213"), not a sentence.
2. **Everything else is a description** (`withInfo` / `info`, section 9): how a number is measured, what it means,
   thresholds and limits, method notes, the sample-data or Shadow detail, the other choices' sentences, and teaching
   copy ("Doing nothing is a decision too"). It opens on hover (150 ms), on keyboard focus, and on tap on a touch
   screen; its words stay in the DOM, so screen readers and tests still find every promise.
3. **Say each thing once.** No card repeats another card's numbers; no page head repeats the top bar. If two blocks
   need the same fact, one shows it and the other links to it.
4. **No promise lost.** Every promise in `PROMISE_MAP.md` / `PITCH.md` stays reachable: on screen, or in a
   description on the thing it is about. Never invent a number to fill a gap; "Not sure yet" when a range includes
   zero.
5. **Plain English** for a business owner. "People we never contact", never "holdout" (not even in small print).
   Sentence case everywhere. No exclamation marks. The product's honest status shows where it matters, quietly (a
   notice of one sentence, the detail in its description), never as a banner.

**What goes where.**

| On the page | In a description |
|---|---|
| The title, the numbers and their ranges | How each number is measured, and the period it covers |
| One sentence: the verdict or the next step | Why, the thresholds, the method, the source |
| The status in a pill ("Running", "Waiting for you") | What that status means and what changes it |
| The chosen option's one sentence (the dial) | The other options' sentences, and when to pick each |
| A short notice ("Not proven on your business yet: start in Shadow") | The rest of the honest status |

**Word budget** (visible words, `document.body.innerText` with hidden elements left out, demo API, 1440 wide,
light; the top bar and nav are about 50 of them, and each (i) glyph counts as one). "Before" was measured on 20 Oct
2026 before the declutter pass, "After" when it finished (the target was about half), and "Polish" after the
plan-proof polish that cut the four screens still over budget. A phone (390) gives the same counts within a few
words, except Operator, and dark the same as light.

| Route | Before | Target | After | Polish | Change |
|---|---:|---:|---:|---:|---:|
| `#/start` Getting started | 413 | 200 | 223 | 200 | −52% |
| `#/connect` | 661 | 330 | 256 | 256 | −61% |
| `#/goal` | 311 | 160 | 185 | 160 | −49% |
| `#/plan/<id>` (win-back, Autopilot) | 1,705 | 850 | 593 | 447 | −74% |
| `#/live/<id>` (win-back, Autopilot) | 1,843 | 900 | 475 | 491 | −73% |
| `#/live/<id>` (repeat purchase, a batch waiting) | 1,387 | 700 | 407 | 419 | −70% |
| `#/proof/<id>` | 664 | 330 | 212 | 214 | −68% |
| `#/proof/<id>` (repeat purchase, too early to tell) | — | 330 | 191 | 218 | — |
| `#/operator` (desktop: all 200 decisions) | 7,994 | 4,000 | 3,941 | 3,941 | −51% |
| `#/operator` (phone, 390: the first 50, then "Show all 200") | — | — | 3,941 | 1,100 | — |

Live's two rows moved with the Live work done alongside the polish, not with it. With the sample data (`?mock=1`):
start 245, connect 320, goal 236, plan 537, live 534, proof 246, operator 345. What is left on Getting started and
Goal is the form, the goal list, the opportunity headlines and the steps' own words. Proof's share text is folded
("Show the text"); the counter cannot see a textarea's value, so the before figure was really about 80 words
higher. It does read screen-reader labels and a closed menu's options, which nobody sees.

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

**Page frame.** `.main` is the canvas (1200px centred, 24px padding, 16px on a phone). Start EVERY screen with
the one page head, `pageHead({title, sub, status, actions, stamp, info})` → `.page-head` > `.page-head-main`
(`.page-title-row` holding `h1.page-title`, an optional (i) and `.page-status` with the status pills, then one
`p.page-sub` line) and `.page-actions` on the right (the screen's primary action and/or a
`freshness(iso, {onRefresh})` stamp). `status` takes `pill()`s (the goal's state, its autonomy level); `sub` is ONE
short line (the goal's words, or one fact); anything longer goes in `info`, the title's description. Do not build
your own head (`.lv-head`, `.ap-head`, a grey line for the level): use this one. Lay blocks out with `.grid` (12 columns, 16px gap, cards keep
their own height) and `.span-3 … .span-9` on the children (full width on a phone); `.stack` for a vertical run,
`.row` for an inline cluster, `.section-head` for a heading row between blocks.

| Need | Class | Helper |
|---|---|---|
| Card / panel | `.card` (`.panel`), `.card-head` > `.card-title` + `.card-meta`, `.card-actions`, `.card-flush` for an edge-to-edge table, `.divider` | `card({title, meta, body, actions, flush})` |
| Buttons | `.btn` + `.btn-primary` (one per screen) / `.btn-secondary` / `.btn-ghost` / `.btn-danger` (= destructive); `.btn-sm`, `.btn-lg`, `.btn-block`; `.link-btn` for Retry, View all | `button(label, onClick, {kind, size, block, disabled})` |
| Fields | `.field` > `label` + input + `.field-hint` / `.field-error`; `.form-row`, `.form-actions` | — |
| Description | `.has-info` (dotted underline) or `.info-dot` (the (i)), `.info-tip` (the box, `role="tooltip"`, `hidden` until open), `.info-host` (a `display: contents` wrapper) | `withInfo(target, text)` → wrapper to put where the target goes; `info(text, {label})` → an (i); `titleInfo(text, about)` → a title's words with its (i) kept on the last word; `describe(target, tipEl)` for a box already in the page |
| Autonomy dial | `.segmented` (radios in one track; `.dial-track` spans its card) + `.segmented-note` (the one sentence, for the chosen option) | `segmented({legend, options: [{value, label, hint, text}], value, onChange})`: each option's `hint` + `text` become its segment's description |
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

**Descriptions, in practice.**

```js
import { el, info, kpiStrip, pageHead, pill, withInfo } from "../components.js";

pageHead({ title: "Proof", sub: goal.text, status: [pill("Running", "good")], actions: [shareButton],
           info: "Extra profit compares the people we contact with people we never contact, picked at random." });
kpiStrip([{ label: withInfo(el("span", {}, "Extra profit"), "Orders from people we contact, minus …"),
            value: money(e.point, cur), range: `${money(e.low, cur)} to ${money(e.high, cur)}` }]);
el("h2", { class: "card-title" }, "Rules it can never break", info("Built into how it decides; no setting can get round them."));
```

- Put the description on the thing it explains: the KPI label, the number, the pill, a row label (a dotted
  underline, `withInfo`). A page title, card title or section title takes an (i) after its words instead
  (`info()`, 8px after the words on their centre line, the same on every screen), and so does anything with no word
  to underline. Sub-headings inside a card (a step, a group label) are labels: they take the underline.
- One to three short sentences, plain words, no links (it cannot be clicked: put a link on the page instead).
- On a button or a link the click is the action, so its description shows on hover and keyboard focus only; give the
  control an `aria-label` with the same words for touch screens. The top bar's Pause does this (`#pause-scope`).
- Tests: the words are in the DOM (`textContent` finds them; `[role=tooltip]`, `aria-describedby` on the target).
  When a sentence moves into a description, point the test at the description; never drop the promise check.

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

**The declutter pass (20 Oct 2026).** The founder: "there is too much text on each page". Every screen now follows
section 6: numbers first, each card its title, its numbers and at most one short sentence, and every explanation in a
description that opens on hover, keyboard focus or tap. Visible words fell by 41% to 74% per screen (the word budget
in section 6). Each screen was rebuilt by one worker and reviewed by a critic against screenshots, then a last pass
made the shared parts consistent:

- One page head everywhere (`pageHead` with `status`): Live, Proof and Operator dropped their own heads and the grey
  autonomy line, since the top bar's badge shows the level.
- Descriptions look and behave the same on every screen: one box style (13px, at most 280px, `--shadow-float`), a
  dotted underline on words, numbers and labels, an (i) 8px after every page, card and section title, on the
  title's centre line. A Playwright pass on every route (demo and `?mock=1`) opened all of them: hover at 1440,
  tap on a 390 phone in dark mode (every box inside the window, closed by a tap outside), and keyboard Tab (every
  target opens on focus, Esc closes). No console errors, no sideways scroll at 390.
- Said once: Plan's limits strip holds the audience, budget, weekly limit and quiet hours, and the plan card no
  longer repeats them; a message the editor failed says so once; Live's Yesterday says its two counts once, in the
  pitch's sentence; Proof says the profit once, in its one sentence, and the chart's ranges are in its description;
  an Inbox item the self-pause banner already names is one quiet line ("Named in the banner above") with its words
  in the description; Connect shows one source as a line, not a table that repeats the totals.
- "Holdout" is gone from the screens (Live rewrites the runtime's briefing words; Proof glosses the API's notes).
- The leftover `.dial-option` styles are deleted from styles.css and plan.css.

**The plan-proof polish (20 Oct 2026).** The four screens still over budget were cut to it, and two Proof states fixed:

- Plan (593 → 447 words): the messages the editor passed on every check sit under ONE "✓ Checked by the editor"
  line, whose description holds the checklist; under it a ready message drops its "Ready to send" pill (the counts
  say it) and its "Don't use this one" button becomes a quiet circle-slash icon, drawn in CSS, with those words as
  its description and its label for touch. A message that needs a look (waiting for you, failed the editor, not checked
  yet) comes first with its own line, and is never folded; one the editor passed that still waits for you leads the
  group instead. Two quiet messages show before "Show all". "When it pauses itself" says
  "Pauses the goal" and "Pauses that channel" once each, as group labels, and the product fact's tag is shorter.
- Proof: the forecast's sentence is a whole one ("Its forecast covers the 14 days after each contact it decided on
  since 10 Oct 2026."). While it is too early to tell, the result card shows what the answer waits for: the three
  groups so far, "Each contact counts after 14 days" and the day the first ones count ("First from 24 Oct 2026",
  the forecast's `since` plus its `horizon_days`, shown only while that day is ahead), then the spend. The folded
  method card then leaves its group counts out, so they are said once.
- Operator: on a phone (under 640px) the decisions list draws 50 rows and a "Show all 200" button (about 1,100 words
  instead of 3,941); the filters still search everything fetched, and the customer lookup stays above the list.
- Getting started (223 → 200): no "Step n of 5" (the current step's pill says it), no link on a done step (the nav
  has Connect and Goal), "Budget (USD)" and "Save", a shorter 3–10% line and "You stay in control", the pitch's words.
- Goal (185 → 160): the box's hint and the budget's hint are their labels' (i), the Shadow line is "Every new goal
  starts in Shadow." with "Nothing is sent yet" in its description, and an opportunity's kind joins its count's
  description instead of a pill.

**Still wordy, or not premium enough.**
- Operator on a desktop still lists all 200 decisions (3,941 words). It is the technical screen, so it stays
  complete; the table scrolls inside its card.
- On the demo, Goal's opportunity count repeats the API's goal headline ("396 … signed up but never bought"); the
  API (`suggest_opportunities` in `api/v2/deps.py`) should leave the count out of its suggested goal text.
- Plan's word count includes four screen-reader labels a message ("Theme:", "Tone:", …), and Goal's includes the
  closed currency menu's eight options: the counter reads them, a sighted person does not see them.
- In the Shadow state, Getting started says the trial day three times (badge, note, step 3), because promise 58 and
  a test pin all three.
- Data, not UI: Live's weekly briefing and Proof are two API figures for nearly the same profit ($1,846 against
  $1,906), and the Approve goal's briefing carries the programme-wide figure while its own Proof says "Not sure
  yet". The briefing headline now sits only in a description, with its range. The runtime still writes "holdout";
  the screen rewrites it. Batch facts carry a forecast without a range because the API sends none.
- `--rule` hairlines are very faint in dark mode.
- Error boxes cannot show "the time of the last good data" yet, because nothing records it.
- The demo data has no product names and pictures on a host that never resolves, so the real-data screenshots look
  sparser than the product will.
