/* Screenshots of every new prototype state, desktop and mobile.
   Usage: NODE_PATH=/opt/node22/lib/node_modules node scripts/prototype_screenshots.mjs */
import { chromium } from "playwright";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FILE = "file://" + path.join(ROOT, "marketing-ai-prototype.html");
const OUT = path.join(ROOT, "docs", "prototype");

const DESKTOP = { width: 1440, height: 1000 };
const MOBILE = { width: 390, height: 844 };

const open = async (page, hash) => {
  await page.goto(FILE + hash, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(120);
};
const click = async (page, sel) => { await page.click(sel); await page.waitForTimeout(80); };

/* Each new state, as a named recipe that leaves the page where it should be shot. */
const STATES = [
  ["01-client-selector", async (p) => { await open(p, "#/"); }],
  ["02-setup-step1-choice", async (p) => { await open(p, "#/uc/rca"); }],
  ["03-onboarding-sources", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
  }],
  ["04-onboarding-mapping", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
    await click(p, '[data-ob-open="2"]');
    await click(p, "#ob-vmap");
  }],
  ["05-onboarding-features", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
    await click(p, '[data-ob-open="3"]');
    await click(p, "#ob-preview");
  }],
  ["06-onboarding-build-report", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
    await click(p, '[data-ob-open="4"]');
    await click(p, "#ob-build");
    await p.waitForSelector(".report", { timeout: 15000 });
    await p.waitForTimeout(200);
  }],
  ["07-dataset-in-use", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
    await click(p, '[data-ob-open="4"]');
    await click(p, "#ob-build");
    await p.waitForSelector("#ob-use", { timeout: 15000 });
    await click(p, "#ob-use");
  }],
  ["08-score-mode-saved-recipe", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.seg button[data-mode="score"]');
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
  }],
  ["09-assistant-setup", async (p) => {
    await open(p, "#/uc/ai-onboarding-assistant");
    await click(p, "#f-sampledocs");
    await click(p, "#f-sampleqa");
  }],
  ["10-assistant-running", async (p) => {
    await open(p, "#/uc/ai-onboarding-assistant");
    await click(p, "#f-sampledocs");
    await click(p, "#f-sampleqa");
    await click(p, "#f-run");
    await p.waitForTimeout(260);
  }],
  ["11-assistant-results", async (p) => {
    await open(p, "#/uc/ai-onboarding-assistant");
    await click(p, "#f-sampledocs");
    await click(p, "#f-sampleqa");
    await click(p, "#f-run");
    await p.waitForSelector(".chatpanel", { timeout: 20000 });
    await p.click("details.cite >> nth=0");
    await p.waitForTimeout(150);
  }],
  ["16-assistant-ask", async (p) => {
    await open(p, "#/uc/ai-onboarding-assistant");
    await click(p, "#f-sampledocs");
    await click(p, "#f-sampleqa");
    await click(p, "#f-run");
    await p.waitForSelector("#g-ask", { timeout: 20000 });
    await p.fill("#g-question", "Why is the light on my router red?");
    await p.click("#g-ask button");
    await p.fill("#g-question", "Can you waive my late fee?");
    await p.click("#g-ask button");
    await p.waitForTimeout(150);
  }],
  ["17-winback-approved", async (p) => {
    await open(p, "#/uc/win-back-campaign/output");
    await p.fill("#cc-approver", "R. Menon");
    await p.click("[data-cc-ok]:not([disabled])");
    await p.click(".cccard.blocked [data-cc-re]");
    await p.waitForTimeout(120);
  }],
  ["12-rca-before-generation", async (p) => { await open(p, "#/uc/rca/output"); }],
  ["13-rca-root-causes", async (p) => {
    await open(p, "#/uc/rca/output");
    await click(p, "#rca-gen");
  }],
  ["14-winback-campaign-copy", async (p) => { await open(p, "#/uc/win-back-campaign/output"); }],
  ["15-data-lineage", async (p) => {
    await open(p, "#/uc/rca");
    await click(p, '.pickcard[data-src="raw"]');
    await click(p, "#ob-sample");
    await click(p, '[data-ob-open="4"]');
    await click(p, "#ob-build");
    await p.waitForSelector("#ob-use", { timeout: 15000 });
    await click(p, "#ob-use");
    await click(p, "#f-run");
    await p.waitForSelector(".results", { timeout: 20000 });
    // navigate through the router rather than the overlapping flow arrow,
    // so the in-memory run (and its lineage) survives
    await p.evaluate(() => { location.hash = "#/uc/rca/data"; });
    await p.waitForSelector(".lineage", { timeout: 10000 });
    await p.waitForTimeout(200);
  }],
];

/* Two of the new panels again in dark mode, to show the tokens carry over. */
const DARK = ["03-onboarding-sources", "14-winback-campaign-copy"];

const browser = await chromium.launch();
let n = 0;
for (const [name, drive] of STATES) {
  for (const [suffix, viewport, scheme] of [
    ["desktop", DESKTOP, "light"],
    ["mobile", MOBILE, "light"],
    ...(DARK.includes(name) ? [["desktop-dark", DESKTOP, "dark"]] : []),
  ]) {
    const ctx = await browser.newContext({
      viewport, colorScheme: scheme, reducedMotion: "reduce", deviceScaleFactor: 1,
    });
    const page = await ctx.newPage();
    await drive(page);
    await page.screenshot({ path: path.join(OUT, `${name}-${suffix}.png`), fullPage: true });
    await ctx.close();
    n++;
  }
  process.stdout.write(`${name} ✓\n`);
}
await browser.close();
console.log(`${n} screenshots written to docs/prototype/`);
