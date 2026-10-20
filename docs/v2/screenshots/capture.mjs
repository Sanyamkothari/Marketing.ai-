// Takes the screenshots in this folder: every screen of the marketer app at desktop and phone width,
// plus Live and Proof in dark mode.
//
// It drives headless Chromium over the DevTools protocol (node 22's built-in WebSocket, no packages)
// because `chrome --screenshot` cannot do what we need: headless Chrome will not make a window
// narrower than 500px, so a 390px phone shot would be a cropped 500px layout, and it has no way to
// ask for dark mode. Here the viewport and `prefers-color-scheme` are set exactly.
//
//   python3 -m api.v2.app --port 8010 &          # the demo API, which also serves the app
//   node docs/v2/screenshots/capture.mjs         # writes the PNGs next to this file
//
// Environment: CHROME (browser binary), APP_URL (default http://127.0.0.1:8010/v2/app/),
// OUT_DIR (default this folder), GOAL_ID (default goal_winback, the demo goal).
// It also prints any element that sticks out past the right edge of the viewport, because the page
// hides horizontal overflow and a clipped element is otherwise easy to miss.

import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const CHROME =
  process.env.CHROME ?? "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell";
const APP_URL = process.env.APP_URL ?? "http://127.0.0.1:8010/v2/app/";
const OUT_DIR = process.env.OUT_DIR ?? HERE;
const GOAL = process.env.GOAL_ID ?? "goal_winback";
const SETTLE_MS = 2500;

const DESKTOP = { name: "desktop", width: 1440, height: 900, mobile: false };
const PHONE = { name: "phone", width: 390, height: 844, mobile: true };

const SCREENS = [
  ["connect", "#/connect"],
  ["goal", "#/goal"],
  ["plan", `#/plan/${GOAL}`],
  ["live", `#/live/${GOAL}`],
  ["proof", `#/proof/${GOAL}`],
  ["operator", `#/operator/${GOAL}`],
];

const SHOTS = [];
for (const device of [DESKTOP, PHONE]) {
  for (const [screen, hash] of SCREENS) SHOTS.push({ screen, hash, device, scheme: "light" });
}
for (const device of [DESKTOP, PHONE]) {
  for (const screen of ["live", "proof"]) {
    const hash = SCREENS.find(([s]) => s === screen)[1];
    SHOTS.push({ screen, hash, device, scheme: "dark" });
  }
}

function startChrome() {
  const profile = mkdtempSync(join(tmpdir(), "autonomy-shots-"));
  const proc = spawn(
    CHROME,
    ["--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--remote-debugging-port=0",
      `--user-data-dir=${profile}`, "about:blank"],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  return new Promise((resolve, reject) => {
    let seen = "";
    proc.stderr.on("data", (chunk) => {
      seen += chunk;
      const match = seen.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) resolve({ proc, profile, wsUrl: match[1] });
    });
    proc.on("exit", (code) => reject(new Error(`chrome exited (${code}): ${seen}`)));
  });
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  });
  const send = (method, params = {}, sessionId) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  return new Promise((resolve) => ws.addEventListener("open", () => resolve({ ws, send })));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Elements whose right edge is past the viewport (ignoring ones inside a scrolling box, which is fine).
const OVERFLOW_PROBE = `(() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= vw + 1) continue;
    let p = el.parentElement, scrolled = false;
    while (p && p !== document.body) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === "auto" || ox === "scroll") { scrolled = true; break; }
      p = p.parentElement;
    }
    if (scrolled) continue;
    const name = el.tagName.toLowerCase() + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\\s+/).join(".") : "");
    out.push(name + " right=" + Math.round(r.right));
  }
  return out.slice(0, 12);
})()`;

// Boxes that scroll sideways because their content is wider than they are. The probe above skips
// whatever is inside them, so a desktop table cut off at its frame (its last column hidden until you
// scroll) would otherwise pass unnoticed. Printed for desktop only: on a phone wide tables and the nav
// are meant to scroll.
const SCROLL_PROBE = `(() => {
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const ox = getComputedStyle(el).overflowX;
    if ((ox !== "auto" && ox !== "scroll") || el.scrollWidth <= el.clientWidth + 1) continue;
    const name = el.tagName.toLowerCase() + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\\s+/).join(".") : "");
    out.push(name + " " + el.scrollWidth + ">" + el.clientWidth);
  }
  return out.slice(0, 12);
})()`;

async function shoot(send, sessionId, shot) {
  const { device, scheme } = shot;
  await send("Emulation.setDeviceMetricsOverride",
    { width: device.width, height: device.height, deviceScaleFactor: 1, mobile: device.mobile }, sessionId);
  await send("Emulation.setEmulatedMedia",
    { features: [{ name: "prefers-color-scheme", value: scheme }] }, sessionId);
  await send("Page.navigate", { url: "about:blank" }, sessionId);
  await send("Page.navigate", { url: APP_URL + shot.hash }, sessionId);
  await sleep(SETTLE_MS);
  const { result: overflow } = await send("Runtime.evaluate",
    { expression: OVERFLOW_PROBE, returnByValue: true }, sessionId);
  const { result: scrolls } = await send("Runtime.evaluate",
    { expression: SCROLL_PROBE, returnByValue: true }, sessionId);
  const { result: tall } = await send("Runtime.evaluate",
    { expression: "document.documentElement.scrollHeight", returnByValue: true }, sessionId);
  // At most two viewport heights, so the committed PNGs stay small.
  const height = Math.min(tall.value, device.height * 2);
  const { data } = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
    clip: { x: 0, y: 0, width: device.width, height, scale: 1 },
  }, sessionId);
  const file = `${shot.screen}-${device.name}-${scheme}.png`;
  writeFileSync(join(OUT_DIR, file), Buffer.from(data, "base64"));
  let note = overflow.value.length ? `  OVERFLOW: ${overflow.value.join(", ")}` : "";
  if (!device.mobile && scrolls.value.length) note += `  SCROLLS SIDEWAYS: ${scrolls.value.join(", ")}`;
  console.log(`${file} ${device.width}x${height}${note}`);
}

const { proc, profile, wsUrl } = await startChrome();
try {
  const { ws, send } = await connect(wsUrl);
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  const only = process.argv.slice(2);
  for (const shot of SHOTS) {
    if (only.length && !only.includes(shot.screen)) continue;
    await shoot(send, sessionId, shot);
  }
  ws.close();
} finally {
  // Wait for Chrome to exit before removing its profile, or it is still writing into the folder.
  await new Promise((resolve) => {
    proc.once("exit", resolve);
    proc.kill();
  });
  rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
}
