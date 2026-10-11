/* Plan M M182 (DEC-1612): with the default brand the page is byte for byte what it was, in BOTH layouts.

   The check is differential, as M174's classic check is (shell/classic.test.mjs), so it never goes stale as main moves
   on. tests/integration/test_brand_ui.py copies ui/ and these tests into a scratch tree, takes M182 out of the copy
   (the lines between the `// <M182>` and `// </M182>` marks of ui/app.js, ui/chrome.js and ui/shell.js, today's literal
   text put back where the markup calls the brand, and ui/brand.js deleted) and runs this file there with BRAND_REF_OUT
   set: it draws the pages in five states of the session and writes, per state and page, the top bar's markup, where the
   page ended, the attributes of <html>, the tab's title and icon, and the ids of the <style> elements in <head>. It
   then runs this file on the real tree with BRAND_REF pointing at that file: the same states, drawn with an answer
   that carries the default `brand` and with one that carries none (an API before M182), must match it exactly.

   The five states, in order: classic bar, API sends no brand | classic bar, default brand | campaigns shell, API sends
   no brand | campaigns shell, default brand | nobody signed in (the narrow shell). */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { installShellPage, loadModules, me, untilStable, visit, withoutBrand } from "./support.mjs";

const OUT = process.env.BRAND_REF_OUT;
const REF = process.env.BRAND_REF;
const UC = "hillstrom-email";
const TRAIN_RISK = "r_20261010_11100001";
const SCORE_RISK = "r_20261010_11100004";
const C_CONVERSION = "c_20261011_c9276210";

const HASHES = [
  "#/",
  "#/overview",
  "#/results",
  "#/campaigns",
  `#/campaigns/${C_CONVERSION}`,
  "#/data",
  "#/reports",
  "#/settings",
  "#/connections",
  "#/approvals",
  "#/help/glossary",
  `#/uc/${UC}`,
  `#/uc/${UC}/run/${TRAIN_RISK}`,
  `#/campaign/${UC}/${SCORE_RISK}`,
  "#/pilot",
  "#/cost",
];

const STATES = [
  { name: "classic, no brand sent", me: () => withoutBrand(me("default_classic")) },
  { name: "classic, default brand", me: () => me("default_classic") },
  { name: "campaigns, no brand sent", me: () => withoutBrand(me("default_campaigns")) },
  { name: "campaigns, default brand", me: () => me("default_campaigns") },
  { name: "nobody signed in", me: () => null },
];

const world = { me: STATES[0].me(), approvals: null };
const { w, state } = installShellPage({ hash: "#/__start", world });
// The page's <title> and icon, as ui/index.html has them (the harness page has neither).
const title = document.createElement("title");
title.textContent = "Marketing AI · Minfy";
document.head.appendChild(title);
const icon = document.createElement("link");
icon.setAttribute("rel", "icon");
icon.setAttribute("href", "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='14' fill='%231E57BD'/></svg>");
document.head.appendChild(icon);
await loadModules();
await untilStable(state);
const session = await import("../../../../../ui/modules/production/session.js");

function shell() {
  const bar = document.getElementById("pb-bar");
  return {
    bar: bar ? bar.outerHTML : null,
    html: document.documentElement.getAttributeNames().map((n) => `${n}=${document.documentElement.getAttribute(n)}`),
    title: document.title,
    icon: document.head.querySelector('link[rel="icon"]').getAttribute("href"),
    styles: [...document.head.querySelectorAll("style")].map((s) => s.id || "(no id)"),
  };
}

async function drawStates() {
  const drawn = [];
  for (const [index, s] of STATES.entries()) {
    world.me = s.me();
    await session.loadMe();
    await untilStable(state);
    const pages = {};
    for (const hash of HASHES) {
      if (w.location.hash === hash) await visit(state, "#/__between");
      await visit(state, hash);
      pages[hash] = { ...shell(), at: w.location.hash };
    }
    drawn.push({ name: s.name, index, pages });
  }
  return drawn;
}

if (OUT) {
  test("draw every page with M182 removed (the reference)", async () => {
    const drawn = await drawStates();
    const body = {
      how: "Written by default.test.mjs with BRAND_REF_OUT, on a copy of ui/ with M182 taken out, on the recorded Hillstrom demo.",
      states: drawn,
    };
    fs.writeFileSync(OUT, `${JSON.stringify(body, null, 1)}\n`);
  });
} else {
  if (!REF) throw new Error("BRAND_REF is not set: run these through tests/integration/test_brand_ui.py");
  const snapshot = JSON.parse(fs.readFileSync(REF, "utf8"));

  test("every state of the session draws today's page, byte for byte, with the default brand", async () => {
    const drawn = await drawStates();
    assert.equal(drawn.length, snapshot.states.length);
    for (const [index, now] of drawn.entries()) {
      const before = snapshot.states[index];
      assert.equal(now.name, before.name);
      for (const hash of HASHES) {
        const why = `${now.name}, ${hash}`;
        assert.equal(now.pages[hash].at, before.pages[hash].at, `${why}: ended somewhere else`);
        assert.equal(now.pages[hash].bar, before.pages[hash].bar, `${why}: the top bar changed`);
        assert.deepEqual(now.pages[hash].html, before.pages[hash].html, `${why}: <html>'s attributes changed`);
        assert.equal(now.pages[hash].title, before.pages[hash].title, `${why}: the tab's title changed`);
        assert.equal(now.pages[hash].icon, before.pages[hash].icon, `${why}: the tab's icon changed`);
        assert.equal(now.pages[hash].styles.includes("brand-styles"), false, `${why}: the brand's style is on a default page`);
        if (index === 0) assert.deepEqual(now.pages[hash].styles, before.pages[hash].styles, `${why}: the page's styles changed`);
      }
    }
  });

  test("nothing is remembered in the browser for the default brand", () => {
    assert.equal(w.localStorage.getItem("marketing-ai.brand"), null);
  });
}
