# Screenshots of the marketer app

What the Autonomy app (`ui/v2/`) looks like against the real `/v2` API and its simulated demo
(`python3 -m api.v2.app`): a Gym shop of 1,200 customers run by the whole system, with the real agent
(`autonomy.runtime.agent.build_agent_policy`) deciding, for 56 simulated days up to today's 09:00 UTC
sweep. It has two goals. "Win back customers who have not bought in two months" ($5,000) has run on
autopilot for all 56 days; the six screens show it. "Turn one-time buyers into repeat buyers" ($3,000)
started ten days ago at the APPROVE level, and its last batch (132 contacts) is still waiting for the
marketer; the two "batches" shots show its Live screen. Messages cost what real providers charge
(email $0.001, WhatsApp $0.04 a marketing message), and offers cost what each offer says per
redemption (free shipping $4, 10% off $6). They were taken on 20 Oct 2026 in headless Chromium.

Desktop is a 1440×900 viewport and phone is 390×844 (with mobile emulation). Each shot covers at most
the first two viewport heights (1440×1800 or 390×1688), so long screens such as Live and Operator stop
part-way down. The PNGs are reduced to a 256-colour palette to keep the folder small (about 1.4 MB).

| Screen | Desktop, light | Phone, light | Desktop, dark | Phone, dark |
| --- | --- | --- | --- | --- |
| Connect (`#/connect`) | [connect-desktop-light.png](connect-desktop-light.png) | [connect-phone-light.png](connect-phone-light.png) | | |
| Goal (`#/goal`) | [goal-desktop-light.png](goal-desktop-light.png) | [goal-phone-light.png](goal-phone-light.png) | | |
| Plan (`#/plan/<goal>`) | [plan-desktop-light.png](plan-desktop-light.png) | [plan-phone-light.png](plan-phone-light.png) | | |
| Live (`#/live/<goal>`) | [live-desktop-light.png](live-desktop-light.png) | [live-phone-light.png](live-phone-light.png) | [live-desktop-dark.png](live-desktop-dark.png) | [live-phone-dark.png](live-phone-dark.png) |
| Proof (`#/proof/<goal>`) | [proof-desktop-light.png](proof-desktop-light.png) | [proof-phone-light.png](proof-phone-light.png) | [proof-desktop-dark.png](proof-desktop-dark.png) | [proof-phone-dark.png](proof-phone-dark.png) |
| Operator (`#/operator/<goal>`) | [operator-desktop-light.png](operator-desktop-light.png) | [operator-phone-light.png](operator-phone-light.png) | | |
| Batches (`#/live/<the APPROVE goal>`) | [batches-desktop-light.png](batches-desktop-light.png) | [batches-phone-light.png](batches-phone-light.png) | | |

Dark mode is the app's own `prefers-color-scheme: dark` styling. The capture script asks the browser for
it, so nothing in the app had to change. `[data-theme="dark"]` on `<html>` gives the same result.

## Taking them again

```sh
python3 -m api.v2.app --port 8010 &      # the demo API (about 45 s to simulate); it also serves the app at /v2/app/
node docs/v2/screenshots/capture.mjs     # all 18 shots; or name screens: ... capture.mjs live batches
```

The script asks `GET /v2/goals` which goals to show: the autopilot goal for the six screens and the
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
palette with Pillow:

```sh
python3 -c "import glob; from PIL import Image; [Image.open(f).convert('RGB').quantize(256, dither=Image.Dither.NONE).save(f, optimize=True) for f in glob.glob('docs/v2/screenshots/*.png')]"
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

## Retaken with the real agent (20 Oct 2026)

All 16 shots were retaken against the simulated demo once the batch routes and the real agent had
merged, and the two batches shots were added. What they show that still needs fixing, and who owns it:

- **Plan, phone: WhatsApp template names stick out of the card.** The content list names each
  WhatsApp item's template (`Template goal_44665ea3bf21567fa441_v1_wh...`, one unbroken word), and on a
  390px screen seven of them run 1-59px past the edge (`capture.mjs` prints `OVERFLOW: li ...`). They
  are below the part of the page the shot covers. They need `overflow-wrap: anywhere`
  (`ui/v2/screens/plan.js` or its CSS).
- **Operator, desktop: the decisions table is cut off again.** With the real agent's policy name
  (`autonomy-agent-v1`, in monospace) the table is 1,156px wide in a 1,036px frame, so the Reason column
  is cut at the right edge until you scroll the frame. The JSON blocks above it also scroll sideways,
  which is fine. The Policy column needs to wrap, or the table to be narrower (`ui/v2/screens/feed.css`).
- **Batches: a pending batch's decisions read as sent.** Under "Waiting for your approval", the Live
  activity lists the held decisions as "Sent a reminder by WhatsApp ... send at 19 Oct, 19:00", though
  nothing in the batch has gone out. The reason is written when deciding, before anyone has approved
  (`autonomy/decide/explain.py`); the activity list could also mark held decisions (`ui/v2/screens/live.js`).
- **Batches: the batch summary uses raw ids.** "132 customers by email (70), whatsapp (62); offers: none
  (84), pct10 (34), freeship (14)." Channels and offers should read as they do on the Plan screen
  ("WhatsApp", "10% off", "Free shipping"). The runtime writes the summary (`autonomy/runtime/service.py`).
- **"Cost of another message" is the attention price, not a channel cost.** Reasons say "more than the
  $0.02 cost of another message" (or $0.03), while an email costs $0.001 and a WhatsApp message $0.04.
  That figure is the fatigue price of one more contact this week, and the wording invites the wrong
  comparison (`autonomy/decide/explain.py`).
- **The APPROVE goal's briefing describes the whole programme.** Its Live screen says "Spend so far is
  $179 of the $3,000 budget": the money and the spend are the programme's (one shared holdout), set
  against this goal's budget. The goal's own spend in ten days is far smaller. The Analyst writes the
  weekly briefing from the programme-wide proof (`autonomy/agents/analyst.py`, called by the runtime).
- **The briefing and the tiles disagree a little.** The briefing is as of yesterday's 09:00 sweep and
  counts the week up to then; the Proof page and the health tiles are as of now. So the briefing says
  $1,824 extra profit, unsubscribes 1.1% and 0.39 messages a customer a week, where Proof says $1,849
  and the tiles say 0.89% and 0.28. Each is right for its moment. An "as of" line beside the health
  tiles would help (it is there, at the bottom of the card, but easy to miss).
- **On a phone, "To approve" shows only its count.** The top bar keeps the badge ("1") and drops the
  words, so the goal switcher, the badge and Inbox sit close together (`ui/v2/app.js`, `styles.css`).
- **The APPROVE goal's own Proof** shows no figures, with two notes: another goal acts on the same
  customers, and none of its own 14-day outcome windows has closed yet. That is honest. No shot shows it.

What is right now: Proof makes a claim for the autopilot goal (extra profit $1,849, range $753 to
$2,944; return on spend 9.7x, range 3.9x to 15.4x, on $191 of messages and offers). The Plan's "Why
this plan" gives a reason instead of a parse error; the scripted demo model used to answer the
Strategist's weekly review in prose. The batch card shows one pending batch with Approve and Reject,
and the nine decided batches are folded away.

## Still visible from the first round (not CSS; owned by other files)

- The Inbox count in the top bar counts only warnings and criticals (`app.js`), while Live's Inbox counts
  every alert. In the first round that read 1 against 2; in this one the top bar shows no count while Live
  says "Inbox (1)" (the info alert that the data is simulated). This may be on purpose, but the two look
  like they disagree.
- On Proof, when a range crosses zero (per customer: -$3.96 to $6.16), the value label still crosses the
  dashed zero line. `proof.js` draws that line after the labels, so CSS cannot put the labels on top of it.
- On a phone, the current "Operator mode" tab is scrolled out of view in the nav. Scrolling the current
  tab into view would need a line in `app.js`.
- The goal switcher cuts the goal's text short at every width (it is a native `<select>`). Each screen
  repeats the full goal text under its title.
