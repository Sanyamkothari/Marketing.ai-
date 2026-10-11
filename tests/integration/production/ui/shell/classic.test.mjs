/* Plan M M174 (DEC-1604): with `ui_layout` "classic" - the default - the app is byte for byte what it was.

   The check is differential, so it never goes stale as main moves on (M172's words and M173's Help menu change the
   classic bar on purpose). tests/integration/test_shell_ui.py copies ui/ and these tests into a scratch tree and
   removes M174 from it - every line between the `// <M174>` and `// </M174>` marks of ui/app.js and ui/chrome.js,
   which are the whole of M174 there (layout.js and shell.js are new files nothing else imports) - and runs this
   file there with SHELL_CLASSIC_OUT set: it draws every page of M170's visible-text check and writes, per page,
   the top bar's markup (`#pb-bar`'s outerHTML), where the page ended, the attributes of <html> and the ids of the
   <style> elements in <head>. It then runs this file on the real tree with SHELL_CLASSIC_REF pointing at that
   file, and the same pages, drawn twice - with a `GET /auth/me` that has no `ui_layout` field, as an API before
   M174 answers, and with the real answer that says `ui_layout: "classic"` - must match it exactly.

   (On the tree M174 was built on, 24958ff, the stripped ui/app.js and ui/chrome.js are byte-identical to the
   files as they were; test_shell_ui.py checks that the marks pair up and that nothing of M174 is left after.) */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { recorded } from "../glossary/replay.mjs";
import { installShellPage, loadModules, me, untilStable, visit } from "./page.mjs";

const OUT = process.env.SHELL_CLASSIC_OUT;
const REF = process.env.SHELL_CLASSIC_REF;
const UC = "hillstrom-email";
const WIN = "win-back-campaign";
const TRAIN_RISK = "r_20261010_11100001";
const TRAIN_UPLIFT = "r_20261010_11100002";
const SCORE_UPLIFT = "r_20261010_11100003";
const SCORE_RISK = "r_20261010_11100004";
const C_CONVERSION = "c_20261011_c9276210";
const C_SPEND = "c_20261011_5ee8a5d4";

/** The pages of the visible-text check (glossary/visible_text.test.mjs PAGES), by address. */
const HASHES = [
  "#/",
  "#/results",
  `#/uc/${WIN}`,
  `#/uc/${UC}`,
  `#/uc/${UC}/run/${TRAIN_RISK}`,
  `#/uc/${UC}/data/${TRAIN_RISK}`,
  `#/uc/${UC}/model/${TRAIN_RISK}`,
  `#/uc/${UC}/output/${TRAIN_RISK}`,
  `#/uc/${UC}/run/${SCORE_RISK}`,
  `#/uc/${UC}/data/${SCORE_RISK}`,
  `#/uc/${UC}/model/${SCORE_RISK}`,
  `#/uc/${UC}/output/${SCORE_RISK}`,
  `#/campaign/${UC}/${SCORE_RISK}`,
  `#/uc/${UC}/run/${TRAIN_UPLIFT}`,
  `#/uc/${UC}/data/${TRAIN_UPLIFT}`,
  `#/uplift/${UC}/model/${TRAIN_UPLIFT}`,
  `#/uplift/${UC}/output/${TRAIN_UPLIFT}`,
  `#/uc/${UC}/run/${SCORE_UPLIFT}`,
  `#/uc/${UC}/output/${SCORE_UPLIFT}`,
  `#/campaigns/${C_CONVERSION}`,
  `#/campaigns/${C_SPEND}`,
  `#/pilot/proof/${C_CONVERSION}`,
  `#/pilot/proof/${C_SPEND}`,
  "#/pilot",
  `#/pilot/value/${SCORE_RISK}`,
  "#/settings",
  "#/connections",
  "#/doctor",
  "#/uplift",
  "#/monitoring/runs",
  `#/monitoring/runs/${SCORE_RISK}`,
  "#/audit",
  "#/monitoring/alerts",
  "#/monitoring/schedules",
  "#/cost",
  "#/approvals",
  "#/pilot/kit",
  "#/connections/new/postgres",
];

// The recorded answer, without the field whatever the recording says: an API from before M174.
const { ui_layout: _dropped, ...before } = recorded["GET /auth/me"].body;
const world = { me: before, approvals: null };
const { w, state } = installShellPage({ hash: "#/__start", world });
await loadModules();
await untilStable(state);
const session = await import("../../../../../ui/modules/production/session.js");

function shell() {
  const bar = document.getElementById("pb-bar");
  return {
    bar: bar ? bar.outerHTML : null,
    html: document.documentElement.getAttributeNames().map((n) => `${n}=${document.documentElement.getAttribute(n)}`),
    styles: [...document.head.querySelectorAll("style")].map((s) => s.id || "(no id)"),
  };
}

async function drawAll() {
  const out = {};
  for (const hash of HASHES) {
    if (w.location.hash === hash) await visit(state, "#/__between");
    await visit(state, hash);
    out[hash] = { ...shell(), at: w.location.hash };
  }
  return out;
}

if (OUT) {
  test("draw every page with M174 removed (the reference)", async () => {
    const drawn = await drawAll();
    const body = {
      how: "Written by classic.test.mjs with SHELL_CLASSIC_OUT, on a copy of ui/ with the M174 marks of ui/app.js and ui/chrome.js removed, on the recorded Hillstrom demo, sign-in off.",
      at: Object.fromEntries(Object.entries(drawn).map(([hash, s]) => [hash, s.at])),
      bars: Object.fromEntries(Object.entries(drawn).map(([hash, s]) => [hash, s.bar])),
      html: Object.fromEntries(Object.entries(drawn).map(([hash, s]) => [hash, s.html])),
      styles: Object.fromEntries(Object.entries(drawn).map(([hash, s]) => [hash, s.styles])),
    };
    fs.writeFileSync(OUT, `${JSON.stringify(body, null, 1)}\n`);
  });
} else {
  if (!REF) throw new Error("SHELL_CLASSIC_REF is not set: run these through tests/integration/test_shell_ui.py");
  const snapshot = JSON.parse(fs.readFileSync(REF, "utf8"));

  // The styles a page has grow as screens are first drawn (each module injects its own once), so they are compared on
  // the first pass, which draws the pages in the order the file was written in; later passes check that the shell's
  // own styles never arrive.
  const compare = (drawn, why, { styles = true } = {}) => {
    for (const hash of HASHES) {
      assert.equal(drawn[hash].at, snapshot.at[hash], `${hash}: ended somewhere else (${why})`);
      assert.equal(drawn[hash].bar, snapshot.bars[hash], `${hash}: the top bar changed (${why})`);
      assert.deepEqual(drawn[hash].html, snapshot.html[hash], `${hash}: <html>'s attributes changed (${why})`);
      if (styles) assert.deepEqual(drawn[hash].styles, snapshot.styles[hash], `${hash}: the page's styles changed (${why})`);
      assert.equal(drawn[hash].styles.includes("sh-styles"), false, `${hash}: the shell's styles are on a classic page`);
    }
  };

  test("an API that says nothing about the layout gets today's bar, byte for byte, on every page", async () => {
    assert.equal("ui_layout" in world.me, false, "an answer from before the field");
    compare(await drawAll(), "no ui_layout in GET /auth/me");
  });

  test("ui_layout: classic (the default) gets today's bar, byte for byte, on every page", async () => {
    world.me = me("me_off_classic");
    assert.equal(world.me.ui_layout, "classic");
    await session.loadMe();
    await untilStable(state);
    compare(await drawAll(), "ui_layout classic", { styles: false });
  });
}
