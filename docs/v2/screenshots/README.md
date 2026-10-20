# Screenshots of the marketer app

What the Autonomy app (`ui/v2/`) looks like against the real `/v2` API and its demo data (a simulated
shop with one goal, `goal_winback`: "Win back customers who haven't bought in 60 days, $5,000 this
month", on autopilot). They were taken on 20 Oct 2026 in headless Chromium.

Desktop is a 1440×900 viewport and phone is 390×844 (with mobile emulation). Each shot covers at most
the first two viewport heights (1440×1800 or 390×1688), so long screens such as Live and Operator stop
part-way down. The PNGs are reduced to a 256-colour palette to keep the folder small (about 1.1 MB).

| Screen | Desktop, light | Phone, light | Desktop, dark | Phone, dark |
| --- | --- | --- | --- | --- |
| Connect (`#/connect`) | [connect-desktop-light.png](connect-desktop-light.png) | [connect-phone-light.png](connect-phone-light.png) | | |
| Goal (`#/goal`) | [goal-desktop-light.png](goal-desktop-light.png) | [goal-phone-light.png](goal-phone-light.png) | | |
| Plan (`#/plan/goal_winback`) | [plan-desktop-light.png](plan-desktop-light.png) | [plan-phone-light.png](plan-phone-light.png) | | |
| Live (`#/live/goal_winback`) | [live-desktop-light.png](live-desktop-light.png) | [live-phone-light.png](live-phone-light.png) | [live-desktop-dark.png](live-desktop-dark.png) | [live-phone-dark.png](live-phone-dark.png) |
| Proof (`#/proof/goal_winback`) | [proof-desktop-light.png](proof-desktop-light.png) | [proof-phone-light.png](proof-phone-light.png) | [proof-desktop-dark.png](proof-desktop-dark.png) | [proof-phone-dark.png](proof-phone-dark.png) |
| Operator (`#/operator/goal_winback`) | [operator-desktop-light.png](operator-desktop-light.png) | [operator-phone-light.png](operator-phone-light.png) | | |

Dark mode is the app's own `prefers-color-scheme: dark` styling. The capture script asks the browser for
it, so nothing in the app had to change. `[data-theme="dark"]` on `<html>` gives the same result.

## Taking them again

```sh
python3 -m api.v2.app --port 8010 &      # the demo API; it also serves the app at /v2/app/
node docs/v2/screenshots/capture.mjs     # all 16 shots; or name screens: ... capture.mjs live proof
```

`capture.mjs` needs node 22 or newer (for its built-in WebSocket) and a Chromium. It defaults to the
Playwright headless shell in `/opt/pw-browsers`; set `CHROME` to use a different browser, and set
`APP_URL`, `OUT_DIR` or `GOAL_ID` if you need other values. It talks to Chromium over the DevTools
protocol instead of using `chrome --screenshot`. That flag has two problems: headless Chrome will not
open a window narrower than 500px, so a "390px" shot is really a cropped 500px layout, and it cannot ask
for dark mode. For each shot the script also lists any element that sticks out past the right edge of
the viewport. The page hides sideways overflow, so without this check a clipped element is easy to miss.
On desktop shots it also lists any box that scrolls sideways because its content is too wide (a table
whose last column is cut off at its frame). Every shot in this folder came back with neither.

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

## Still visible (not CSS; owned by other files)

- The Inbox count in the top bar reads 1 while the Live inbox lists 2 alerts. The top bar counts only
  warnings and criticals (`app.js`). This may be on purpose, but the two numbers look like they disagree.
- On Proof, when a range crosses zero (per customer: -$3.96 to $6.16), the value label still crosses the
  dashed zero line. `proof.js` draws that line after the labels, so CSS cannot put the labels on top of it.
- On a phone, the current "Operator mode" tab is scrolled out of view in the nav. Scrolling the current
  tab into view would need a line in `app.js`.
- The goal switcher cuts the goal's text short at every width (it is a native `<select>`). Each screen
  repeats the full goal text under its title.
