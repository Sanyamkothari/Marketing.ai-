/* Plan M, M181 part A (DEC-1611): accessibility of every page, in jsdom.

   Every page of the visible-text check (a11y/pages.mjs reads its PAGES list) is drawn against the API answers
   recorded from the validated Hillstrom demo, and `check()` (a11y/checks.mjs) looks at its markup: a name on every
   control, button and link, alt text on images, one H1, headings that do not jump, no repeated id.

   The pages are not clean today. `known.json` lists every defect the pages still have, per layout and page, each
   with a reason, the file and line, and the fix. The test fails on any defect that is not on the list, or that is
   there more often than listed, so the list can only shrink; fixing a page lowers its count (A11Y_UPDATE_KNOWN=lower
   rewrites the counts, and removes a defect nobody has any more). Nothing is hidden: the table of what is left is
   printed at the end of the run.

   One process draws one layout (the module graph is global): A11Y_LAYOUT=classic (default) or campaigns. The
   campaigns layout (ui/layout.js, M174) is switched on the way the application does it, by `GET /auth/me`
   carrying `ui_layout: "campaigns"`; this file fails if that did not change the page, rather than silently
   checking the classic one twice.

   Environment:
     A11Y_LAYOUT=classic|campaigns   which layout this process draws
     A11Y_UPDATE_KNOWN=lower         lower known.json's counts to what the pages show now
     A11Y_DUMP=<file>                write what the pages show (per page: rule|target counts) as JSON */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { installPage, settle } from "../harness.mjs";
import { recorded } from "../glossary/replay.mjs";
import { pageScripts } from "../simple/fake.mjs";
import { against, check, tally } from "./checks.mjs";
import { availableLayouts, loadPages } from "./pages.mjs";

const KNOWN = new URL("./known.json", import.meta.url);
const LAYOUT = process.env.A11Y_LAYOUT || "classic";
const mode = process.env.A11Y_UPDATE_KNOWN || "";
const dump = process.env.A11Y_DUMP || "";

assert.ok(availableLayouts().includes(LAYOUT), `layout ${LAYOUT} is not in this tree (ui/layout.js is missing)`);

const known = JSON.parse(fs.readFileSync(KNOWN, "utf8"));
const allowedFor = (page) => (known.defects[LAYOUT] || {})[page] || {};

const keyOf = (request) => {
  const query = new URLSearchParams(request.query);
  query.sort();
  const q = query.toString();
  return `${request.method} ${request.path}${q ? `?${q}` : ""}`;
};

const state = { inFlight: 0 };
const missing = new Set();
const server = async (request) => {
  state.inFlight += 1;
  try {
    const hit = recorded[keyOf(request)];
    if (request.path === "/auth/me" && hit && LAYOUT === "campaigns") {
      return { status: 200, body: { ...hit.body, ui_layout: "campaigns" } };
    }
    if (hit) return { status: hit.status, body: hit.body, headers: { "Content-Type": hit.content_type || "application/json" } };
    if (request.method === "GET") missing.add(keyOf(request));
    return { status: 404, body: { detail: { code: "NOT_RECORDED", message: "Not found." } } };
  } finally {
    state.inFlight -= 1;
  }
};

const { w } = installPage(server, { hash: "#/__start" });
for (const script of pageScripts()) await import(`../../../../../ui/${script}`);

async function untilStable(ms = 10000) {
  const started = Date.now();
  let last = null;
  let same = 0;
  while (Date.now() - started < ms) {
    await new Promise((resolve) => setTimeout(resolve, 30));
    const now = `${w.location.hash}:${document.body.innerHTML.length}`;
    if (state.inFlight === 0 && now === last) same += 1;
    else same = 0;
    last = now;
    if (same >= 6) return;
  }
}

async function visit(hash) {
  if (w.location.hash === hash) {
    w.location.hash = "#/__between";
    await untilStable();
  }
  w.location.hash = hash;
  await settle(2);
  await untilStable();
}

const PAGES = loadPages();
const found = {};
const headingNotes = {};
const interactiveCounts = {};

test(`the ${LAYOUT} layout is the one this process draws`, async () => {
  await visit("#/__start");
  await settle(4);
  await untilStable();
  const marked = document.documentElement.getAttribute("data-ui-layout");
  if (LAYOUT === "campaigns") {
    assert.equal(marked, "campaigns", "GET /auth/me carried ui_layout=campaigns but the page did not switch; update a11y/pages.test.mjs");
  } else {
    assert.equal(marked, null, "the classic layout must not mark <html data-ui-layout>");
  }
});

for (const page of PAGES) {
  test(`${LAYOUT} / ${page.id}: no accessibility defect beyond the known list`, async () => {
    missing.clear();
    await visit(page.hash);
    for (const step of page.steps || []) {
      await step();
      await settle(2);
      await untilStable();
    }
    const text = document.body.textContent.replace(/\s+/g, " ");
    // The campaigns layout redirects some old addresses, so the page's own sentence may be elsewhere; the classic
    // layout must show what the visible-text check expects, or this would be checking an error page.
    if (LAYOUT === "classic") assert.match(text, page.expect, `${page.id} did not draw`);
    assert.ok(document.body.querySelector("#app, main, body"), "no page");
    const result = check(document.body);
    const counts = tally(result.violations);
    found[page.id] = counts;
    headingNotes[page.id] = result.headings;
    interactiveCounts[page.id] = result.interactive;
    const verdict = against(counts, allowedFor(page.id));
    if (!mode) {
      assert.ok(
        verdict.ok,
        `${LAYOUT} / ${page.id} has accessibility defects that are not on known.json (fix the page, do not add to the list):\n${verdict.worse
          .map((v) => `  ${v.key}: ${v.count} found, ${v.allowed} allowed`)
          .join("\n")}`,
      );
    }
  });
}

test(`${LAYOUT}: known.json names only pages this layout draws, and gives every defect a reason`, () => {
  const ids = new Set(PAGES.map((p) => p.id));
  const stray = Object.keys((known.defects && known.defects[LAYOUT]) || {}).filter((id) => !ids.has(id));
  assert.deepEqual(stray, [], "known.json lists a page that is no longer checked; remove it");
  const used = new Set();
  for (const layout of Object.keys(known.defects)) {
    for (const keys of Object.values(known.defects[layout])) {
      for (const [key, count] of Object.entries(keys)) {
        assert.ok(Number.isInteger(count) && count > 0, `${key}: a count must be a whole number above zero`);
        used.add(key);
      }
    }
  }
  const unexplained = [...used].filter((key) => !known.reasons[key]);
  assert.deepEqual(unexplained, [], "every known defect needs a reason, a file:line and a fix in known.json's reasons");
  for (const [key, why] of Object.entries(known.reasons)) {
    assert.ok(used.has(key), `${key} has a reason but no page lists it; remove the reason`);
    assert.ok(why.reason && why.where && why.fix, `${key}: reason, where and fix are all required`);
  }
});

test(`${LAYOUT}: the table of what is left (the accessibility to-do list)`, () => {
  const rows = {};
  for (const counts of Object.values(found)) {
    for (const [key, n] of Object.entries(counts)) rows[key] = (rows[key] || 0) + n;
  }
  if (dump) fs.writeFileSync(dump, `${JSON.stringify(found, null, 1)}\n`);
  if (mode === "lower") {
    const next = JSON.parse(JSON.stringify(known));
    const layoutPages = (next.defects[LAYOUT] = next.defects[LAYOUT] || {});
    for (const [id, keys] of Object.entries(layoutPages)) {
      for (const key of Object.keys(keys)) {
        const now = (found[id] || {})[key] || 0;
        if (now === 0) delete keys[key];
        else keys[key] = Math.min(keys[key], now);
      }
      if (Object.keys(keys).length === 0) delete layoutPages[id];
    }
    fs.writeFileSync(KNOWN, `${JSON.stringify(next, null, 2)}\n`);
  }
  const total = Object.values(rows).reduce((a, b) => a + b, 0);
  const lines = Object.entries(rows)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, n]) => `#   ${String(n).padStart(4)}  ${key}`);
  const jumps = Object.entries(headingNotes)
    .filter(([, levels]) => levels.some((level, i) => (i === 0 ? level > 2 : level - levels[i - 1] >= 2)))
    .map(([id, levels]) => `${id}:${levels.join("")}`);
  const checked = Object.values(interactiveCounts).reduce((a, b) => a + b, 0);
  process.stdout.write(
    `\nAccessibility (${LAYOUT}): ${total} known defects still, ${checked} controls, buttons and links checked on ${Object.keys(found).length} pages\n${lines.join("\n")}\n` +
      `# heading levels that jump or start low (recorded, not failed): ${jumps.join(" ") || "none"}\n`,
  );
  assert.ok(true);
});

test("the checks themselves: planted defects are found, and sound markup is not flagged", () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  const rules = (html) => {
    holder.innerHTML = html;
    return check(holder).violations.map((v) => v.rule).sort();
  };
  const good = '<h1>Title</h1><label for="a">Name</label><input id="a"><button>Save</button><a href="#/x">Go</a><img src="x.png" alt="">';
  assert.deepEqual(rules(good), []);
  assert.deepEqual(rules('<h1>T</h1><input type="text">'), ["control-name"]);
  assert.deepEqual(rules('<h1>T</h1><input placeholder="Search">'), ["control-name"], "a placeholder is not a name");
  assert.deepEqual(rules('<h1>T</h1><label>Name <input></label>'), []);
  assert.deepEqual(rules('<h1>T</h1><input aria-label="Search"><select aria-labelledby="l"></select><span id="l">Pick</span>'), []);
  assert.deepEqual(rules('<h1>T</h1><button></button><a href="#/x"></a>'), ["button-name", "link-name"]);
  assert.deepEqual(rules('<h1>T</h1><button><span aria-hidden="true">?</span></button>'), ["button-name"]);
  assert.deepEqual(rules('<h1>T</h1><button aria-label="Help"><span aria-hidden="true">?</span></button>'), []);
  assert.deepEqual(rules('<h1>T</h1><a href="#/x"><img src="x" alt="Home"></a>'), []);
  assert.deepEqual(rules('<h1>T</h1><img src="x.png">'), ["image-alt"]);
  assert.deepEqual(rules('<h1>T</h1><a>no href is not a link</a><input type="hidden">'), []);
  assert.deepEqual(rules('<h1>T</h1><div hidden><button></button></div>'), [], "hidden content is not checked");
  assert.deepEqual(rules("<p>no heading</p>"), ["one-h1"]);
  assert.deepEqual(rules("<h1>A</h1><h1>B</h1>"), ["one-h1"]);
  assert.deepEqual(rules("<h1>A</h1><h3>B</h3>"), [], "a jump of two levels is recorded, not failed");
  assert.deepEqual(rules("<h1>A</h1><h4>B</h4>"), ["heading-jump"]);
  assert.deepEqual(rules('<h1>A</h1><p id="x"></p><p id="x"></p>'), ["duplicate-id"]);
  assert.deepEqual(rules('<h1>A</h1><div tabindex="3">x</div><div tabindex="0">y</div><div tabindex="-1">z</div>'), ["tabindex-positive"]);
  holder.remove();
});

test("a defect that is not on the list fails, and fewer than listed is fine", () => {
  const found1 = { "button-name|button.x": 2 };
  assert.equal(against(found1, { "button-name|button.x": 2 }).ok, true);
  assert.equal(against(found1, { "button-name|button.x": 1 }).ok, false);
  assert.equal(against(found1, {}).ok, false, "a new defect has an allowance of zero");
  assert.deepEqual(against({}, { "button-name|button.x": 2 }).better, [{ key: "button-name|button.x", count: 0, allowed: 2 }]);
});

test("the page shell: a language, a viewport that can zoom", () => {
  const html = fs.readFileSync(new URL("../../../../../ui/index.html", import.meta.url), "utf8");
  assert.match(html, /<html[^>]*\blang="[a-z]{2}/, "<html lang> is how a screen reader picks a voice");
  const viewport = html.match(/<meta name="viewport" content="([^"]*)"/);
  assert.ok(viewport, "a viewport meta is needed for the phone width");
  assert.doesNotMatch(viewport[1], /user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0)?\b/, "pinch zoom must stay available");
  assert.match(html, /<title>[^<]+<\/title>/);
  assert.match(html, /class="skip"[^>]*href="#app"/, "a skip link to the content");
  assert.match(html, /:focus-visible\{outline:/, "a visible focus style");
});
