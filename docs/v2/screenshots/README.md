# Screenshots of the marketer app

What the Autonomy app (`ui/v2/`) looks like against the real `/v2` API and its simulated demo
(`python3 -m api.v2.app`): a Gym shop of 1,200 customers run by the whole system, with the real agent
(`autonomy.runtime.agent.build_agent_policy`) deciding, for 56 simulated days up to today's 09:00 UTC
sweep. It has two goals. "Win back customers who have not bought in two months" ($5,000) has run on
autopilot for all 56 days; the screens show it. "Turn one-time buyers into repeat buyers" ($3,000)
started ten days ago at the APPROVE level, and its last two batches are still waiting for the marketer;
the two "batches" shots show its Live screen. Messages cost what real providers charge
(email $0.001, WhatsApp $0.04 a marketing message), and offers cost what each offer says per
redemption (free shipping $4, 10% off $6). They were taken on 20 Oct 2026 in headless Chromium.

Desktop is a 1440×900 viewport and phone is 390×844 (with mobile emulation). Each shot covers at most
the first two viewport heights (1440×1800 or 390×1688), so long screens such as Live and Operator stop
part-way down. The PNGs are reduced to a 256-colour palette to keep the folder small (about 1.3 MB).

| Screen | Desktop, light | Phone, light | Desktop, dark | Phone, dark |
| --- | --- | --- | --- | --- |
| Connect (`#/connect`) | [connect-desktop-light.png](connect-desktop-light.png) | [connect-phone-light.png](connect-phone-light.png) | | |
| Goal (`#/goal`) | [goal-desktop-light.png](goal-desktop-light.png) | [goal-phone-light.png](goal-phone-light.png) | | |
| Plan (`#/plan/<goal>`) | [plan-desktop-light.png](plan-desktop-light.png) | [plan-phone-light.png](plan-phone-light.png) | | |
| Live (`#/live/<goal>`) | [live-desktop-light.png](live-desktop-light.png) | [live-phone-light.png](live-phone-light.png) | [live-desktop-dark.png](live-desktop-dark.png) | [live-phone-dark.png](live-phone-dark.png) |
| Proof (`#/proof/<goal>`) | [proof-desktop-light.png](proof-desktop-light.png) | [proof-phone-light.png](proof-phone-light.png) | [proof-desktop-dark.png](proof-desktop-dark.png) | [proof-phone-dark.png](proof-phone-dark.png) |
| Operator (`#/operator/<goal>`) | [operator-desktop-light.png](operator-desktop-light.png) | [operator-phone-light.png](operator-phone-light.png) | | |
| Getting started (`#/start`) | [start-desktop-light.png](start-desktop-light.png) | [start-phone-light.png](start-phone-light.png) | [start-desktop-dark.png](start-desktop-dark.png) | [start-phone-dark.png](start-phone-dark.png) |
| Batches (`#/live/<the APPROVE goal>`) | [batches-desktop-light.png](batches-desktop-light.png) | [batches-phone-light.png](batches-phone-light.png) | | |

Dark mode is the app's own `prefers-color-scheme: dark` styling. The capture script asks the browser for
it, so nothing in the app had to change. `[data-theme="dark"]` on `<html>` gives the same result.

## Taking them again

```sh
python3 -m api.v2.app --port 8010 &      # the demo API (about 45 s to simulate); it also serves the app at /v2/app/
node docs/v2/screenshots/capture.mjs     # all 22 shots; or name screens: ... capture.mjs live start
```

The script asks `GET /v2/goals` which goals to show: the autopilot goal for the screens and the
APPROVE goal for the batches shots (set `GOAL_ID` and `APPROVE_GOAL_ID` to choose others). Goal ids
are stable across runs, but the dates move with the day you run it, and a batch you approve or reject
in the app stays decided until the API restarts, so retake the batches shots on a fresh API.

`capture.mjs` needs node 22 or newer (for its built-in WebSocket) and a Chromium. It defaults to the
Playwright headless shell in `/opt/pw-browsers`; set `CHROME` to use a different browser, and set
`APP_URL` or `OUT_DIR` if you need other values. It talks to Chromium over the DevTools
protocol instead of using `chrome --screenshot`. That flag has two problems: headless Chrome will not
open a window narrower than 500px, so a "390px" shot is really a cropped 500px layout, and it cannot ask
for dark mode. For each shot the script also lists any element that sticks out past the right edge of
the viewport, with the start of its text. The page hides sideways overflow, so without this check a clipped element is easy to miss.
On desktop shots it also lists any box that scrolls sideways because its content is too wide (a table
whose last column is cut off at its frame). Two shots in this folder came back with one: see "Retaken
with the real agent" below.

The script writes full-colour PNGs. To make them as small as the committed ones, convert them to a
palette with Pillow. Use the max-coverage method: the default (median cut) turns the few pixels of accent blue
into slate grey, and the fast octree merges the 1px row rules into white.

```sh
python3 -c "import glob; from PIL import Image; [Image.open(f).convert('RGB').quantize(256, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE).save(f, optimize=True) for f in glob.glob('docs/v2/screenshots/*.png')]"
```

## What the screenshots showed, and what was fixed

These fixes are in `ui/v2/styles.css` and `ui/v2/screens/feed.css`, and the shots above were taken
after them:

- **Phone top bar.** It took three rows (162px of a sticky header on an 844px screen), and Inbox and
  Pause wrapped onto a line of their own. Now it is one row: the "A" mark, the goal, Inbox and Pause. The
  wordmark text is hidden on phones, but the link keeps its label. The nav is tighter, so all five steps
  fit at 390px, and a fade on the right edge shows that it scrolls to Operator mode.
- **Live, Proof and Operator** sat 16px further in than the other screens, because both `.main` and
  `.ap-page` added padding. On a phone that cost 24px of width. Inside the shell, `.ap-page` no longer
  adds its own padding.
- **Proof charts** scaled their text with the SVG. The labels came out about 19px on desktop and about
  6px on a phone. The chart is now at most 480px wide, which is its viewBox, so its text is drawn at the
  size the CSS sets. On phones the text is set larger in viewBox units, so it shows at about 11px. The
  value labels are moved 6px right so they no longer start on the dashed zero line.
- **Operator decisions table**: short cells broke over lines (`demo-` / `everyone-` / `v0`, `Email ·` /
  `No offer`). Now only the reason column wraps, and on a phone the table scrolls sideways in its frame.
  Two-word headers ("Exp. net", "Attention price") may wrap, because with every cell on one line, whole
  headers made the table 31px wider than a desktop card and cut its JSON buttons off.
- **Connect**: the "What is connected" empty state stretched to the height of the card beside it. On a
  phone it touched the Sending health card. Cards in `.grid-2` now keep their own height and have a gap
  below them.
- **Goal**: the Budget field ran the full width of the page on desktop. It is now at most 320px wide.
- **Top-left corner**: a faint grey smudge came from the hidden "Skip to content" link's shadow. The
  link now has a shadow only when it has focus.

## Retaken for the pitch promises (20 Oct 2026)

All 22 shots were retaken after every screen was reworked to keep the promises in
[PITCH.md](../PITCH.md) (see [PROMISE_MAP.md](../PROMISE_MAP.md)), and the Getting started screen
(`#/start`) was added at desktop and phone width, light and dark. `capture.mjs` now also prints any console
error or uncaught exception a page raised (`CONSOLE: ...`); none of the 22 shots, and none of the same routes in
mock mode (`?mock=1`), raised one.

Fixed since the round before, and visible in these shots:

- **Plan, phone**: WhatsApp template names wrap inside the card.
- **Batches**: a pending batch is headed in words ("56 customers: 46 by email, 10 by WhatsApp; 48 with no
  offer, 7 with free shipping, 1 with 10% off"), and each decision in the feed says what really happened
  ("Waiting for your approval", "Would send (Shadow)", "Delivered").
- **Inbox**: the top bar counts the same alerts as Live's Inbox, so both say 1 here.
- **Proof**: a verdict comes before the numbers, and the zero line is drawn only through each bar, so the value
  labels no longer cross it.
- **Live**: a "Yesterday" card at the top, with the day's two counts and every reason for leaving people alone.
  Its spend tile is "Spent on messages", the figure the budget limit counts, and it says that Proof's spend
  also counts the discounts customers used (here $38.40 against Proof's $175).
- **Every screen** shows the goal's status in the same words ("Running", "Paused", "Not started") and an
  autonomy badge under the goal switcher.

Still visible:

- **Operator, desktop**: the decisions table and the JSON blocks still scroll sideways in their frames
  (`SCROLLS SIDEWAYS: div.ap-table-wrap 1155>1036`). Operator mode is not one of the marketer's screens.
- **The demo has no first names, no product names and no Data Doctor report.** So the feed shows ids
  (`gym_000631`), the Plan's products card says the order data names no products, and Connect says the Data
  Doctor's report is not linked. Mock mode (`?mock=1`) shows all three.
- **The weekly briefing and the decision reasons** are written by the runtime and `autonomy/decide`, and still
  say "holdout" and "the $0.02 cost of another message". The screens add a plain line next to them.
- **The goal switcher** still cuts long goal text short (a native `<select>`); each screen repeats the full goal
  text under its title.

## Retaken for the premium design system (20 Oct 2026)

All 22 shots were retaken after every screen moved to the design system in [DESIGN.md](../DESIGN.md) (tokens,
Inter, hairline cards, one near-black primary per screen, pale pills, open KPI strips inside cards). None of the
22 shots, and none of the same routes in mock mode (`?mock=1`), raised a console error or scrolled sideways at
390px. `capture.mjs` now waits (up to 15 s after the 2.5 s settle) until no grey loading block is left in the
page, so slow blocks such as Live's feed are shot filled in, not as skeletons.

Changed since the round before, and visible in these shots:

- **Every screen** opens with the same page head (a 24px noun title, a grey line, actions or a stamp on the
  right) and lays its blocks on the 12-column grid (8 + 4 or 7 + 5).
- **Live on Approve** (the batches shots): the batches waiting for approval come first, above Yesterday, and
  only the newest batch's Approve is a primary button.
- **Operator**: the raw JSON wraps inside its grey well, so nothing scrolls sideways any more.
- **Message pictures** in the demo point at `assets.demo.invalid`, a name that can never load: Plan says the
  picture could not be loaded here, and Live leaves the thumbnail out, without asking the network for them.

## Retaken for the declutter pass (20 Oct 2026)

All 22 shots were retaken after the founder's "there is too much text on each page". Every screen now shows numbers
first, each card its title, its numbers and at most one short sentence, and every explanation moved into a
description that opens on hover, keyboard focus or tap (the dotted underlines and the (i) glyphs in these shots;
see section 6 of [DESIGN.md](../DESIGN.md)). Visible words fell by 41% to 74% per screen. None of the 22 shots, and
none of the same routes in mock mode (`?mock=1`), raised a console error or stuck out past the right edge at 390px.
A shot cannot show a description open: they are closed until someone hovers, focuses or taps.

Changed since the round before, and visible in these shots:

- **Every screen** uses the same page head (title, status pill, one grey line); the (i) after any title sits 8px
  after its words, and on a phone it wraps with the title's last word.
- **Plan**: the limits strip says the audience, budget, weekly limit and quiet hours once; the rules are names in
  two columns, the stop rules short rows; four ready messages before "Show all".
- **Live**: Yesterday is the pitch's sentence plus three figures; the feed is one line a decision; the Briefing is
  one row per section.
- **Proof**: the verdict, then one sentence with the money and its range; the share text is folded.
- **Connect, Goal, Getting started**: one line per card, the file and step details in descriptions.

Still visible: Operator lists all 200 decisions (the shots stop after two viewport heights), the demo has no
product names, and its pictures point at a host that never resolves.
