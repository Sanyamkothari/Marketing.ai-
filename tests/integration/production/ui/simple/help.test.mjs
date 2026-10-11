/* Plan M M173 (DEC-1603): the Help (?) menu in the whole page, with demo mode OFF. Take the tour (the real
   pages, skipping a step whose elements are not there), Glossary, Send feedback and What's new; the page each
   of them opens; the orphan routes now linked from Settings; `#/industry/<id>` going Home; and a readiness
   link by goal. The fake API is the one `fake.mjs` builds from the real app's answers. */
import { test } from "node:test";
import fs from "node:fs";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const NEWS_FILE = new URL("../../../../../ui/modules/simple/whats-new.json", import.meta.url);

const datasets = {
  datasets: [
    { dataset_id: "ds_old", use_case: "telco-churn", built_at: "2026-09-01T10:00:00Z", n_rows: 10, target: "churned" },
    { dataset_id: "ds_new", use_case: "telco-churn", built_at: "2026-10-01T10:00:00Z", n_rows: 20, target: "churned" },
  ],
};
const reportDoc = { schema_version: 1, kind: "readiness", title: "Data readiness report", subtitle: "", client_name: "Co", facts: [], blocks: [] };

const world = {
  runs: fixture("runs_empty"),
  waiting: null,
  extra: (request) => {
    if (request.path.endsWith("/whats-new.json")) return { status: 200, body: fs.readFileSync(NEWS_FILE, "utf8") };
    if (request.method === "GET" && request.path === "/datasets") return { status: 200, body: datasets };
    if (request.method === "GET" && request.path === "/pilot/readiness/ds_new") {
      return request.query.format === "json"
        ? { status: 200, body: reportDoc }
        : { status: 200, body: "<html><body><p>ready</p></body></html>" };
    }
    return undefined;
  },
};
const { w, calls } = await installWholePage({ hash: "#/", world });
globalThis.KeyboardEvent = w.KeyboardEvent; // the Help menu's entries close the menu with an Escape they dispatch
const { TOUR_TIMING } = await import("../../../../../ui/modules/pilot/tour.js");

// jsdom lays nothing out: every element is "visible" unless a test hides it, as the tour's `visible()` needs.
let hidden = () => false;
w.Element.prototype.getBoundingClientRect = function box() {
  return hidden(this) ? { width: 0, height: 0, top: 0, bottom: 0, left: 0, right: 0 } : { width: 100, height: 20, top: 100, bottom: 120, left: 0, right: 100 };
};
w.Element.prototype.scrollIntoView = () => {};
TOUR_TIMING.findEveryMs = 10;
TOUR_TIMING.skipAfterMs = 120;

const key = (target, name) => {
  const event = new w.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
};
const entries = () => $$("#tn-help a, #tn-help button").map((el) => el.textContent.trim());
const go = async (hash) => {
  w.location.hash = hash;
  await settle(4);
};

test("the Help menu is drawn with demo mode off, with the four entries in the plan's order", async () => {
  await until(() => $("#pb-bar .tb-help-btn") && entries().length === 4, 3000, "the Help menu");
  assert.equal(fixture("demo").demo_mode, false, "this is not the sample data");
  assert.deepEqual(entries(), ["Take the tour", "Glossary", "Send feedback", "What's new"]);
  assert.equal($("#pb-bar .tb-help-btn").getAttribute("aria-label"), "Help");
});

test("the tour starts from the Help menu outside demo mode, on the real pages, each step on its own elements", async () => {
  TOUR_TIMING.skipAfterMs = 4000; // pages load their data first: nothing may be skipped while it arrives
  const button = $("#pb-bar .tb-help-btn");
  button.focus();
  button.click();
  key(button, "ArrowDown");
  assert.equal(w.document.activeElement.textContent.trim(), "Take the tour", "reached with the arrow key");
  w.document.activeElement.click(); // Enter on a focused button
  await until(() => $("#pe-tour"), 2000, "the tour card");
  const card = () => $("#pe-tour");
  const next = () => card().querySelector("[data-pe-tour='next']").click();
  const steps = [
    ["Home", "#/"],
    ["Connections", "#/connections"],
    ["Results", "#/results"],
    ["Reports", "#/pilot"],
    ["Settings", "#/settings"],
    ["Help", null],
  ];
  assert.equal($("#tn-help").hidden, true, "starting the tour closes the menu");
  for (const [index, [title, hash]] of steps.entries()) {
    await until(() => card() && card().textContent.includes(`Step ${index + 1} of 6`), 3000, `step ${index + 1}`);
    assert.equal(card().querySelector("h2").textContent, title);
    if (hash) assert.equal(w.location.hash, hash);
    await until(() => $(".pe-target"), 3000, `the elements of "${title}" to be found`);
    next();
  }
  assert.equal($("#pe-tour"), null, "Finish closes it");
  assert.equal(w.localStorage.getItem("marketing-ai:pilot-tour-seen"), "1");
  TOUR_TIMING.skipAfterMs = 120;
});

test("a step whose elements are not on the page is skipped, and Back skips the same way", async () => {
  await go("#/");
  TOUR_TIMING.skipAfterMs = 700; // long enough for a page to draw, short enough to wait for a missing element
  hidden = (el) => el.matches("[data-pe-reports]"); // the Reports page draws, but its element is not there
  w.document.querySelector("#pb-bar .tb-help-btn").click();
  $("[data-pe-tour-start]").click();
  await until(() => $("#pe-tour"), 2000, "the tour card");
  const title = () => ($("#pe-tour") ? $("#pe-tour h2").textContent : null);
  const next = () => $("#pe-tour [data-pe-tour='next']").click();
  const back = () => $("#pe-tour [data-pe-tour='back']").click();
  await until(() => title() === "Home", 2000, "Home");
  next();
  await until(() => title() === "Connections", 2000, "Connections");
  next();
  await until(() => title() === "Results", 2000, "Results");
  next();
  await until(() => title() === "Reports", 2000, "Reports is shown first");
  await until(() => title() === "Settings", 3000, "Reports is skipped after a moment: no element of its page");
  back();
  await until(() => title() === "Reports", 2000, "back to Reports");
  await until(() => title() === "Results", 3000, "going back skips it too");
  hidden = () => false;
  TOUR_TIMING.skipAfterMs = 120;
  key(w.document, "Escape");
  assert.equal($("#pe-tour"), null, "Escape closes the tour");
});

test("the tour offers itself from the menu every time, however often it was seen", async () => {
  w.localStorage.setItem("marketing-ai:pilot-tour-seen", "1");
  await go("#/");
  w.document.querySelector("#pb-bar .tb-help-btn").click();
  $("[data-pe-tour-start]").click();
  await until(() => $("#pe-tour"), 2000, "the tour card");
  assert.match($("#pe-tour").textContent, /Step 1 of 6/);
  key(w.document, "Escape");
});

test("Glossary: Plan M's words and what they mean, with the engine's names folded under Technical names", async () => {
  await go("#/help/glossary");
  await until(() => $("[data-glossary]"), 3000, "the Glossary");
  assert.equal($("main h1").textContent, "Glossary");
  assert.equal($$("main h1").length, 1, "one H1");
  const rows = $$("[data-glossary] tbody tr").map((tr) => tr.querySelector("td").textContent.trim());
  for (const word of ["Goal", "Contact list", "Comparison group", "Likelihood", "What drives it", "One message per customer", "The fixes you approved"]) {
    assert.ok(rows.includes(word), `${word} is in the glossary`);
  }
  assert.ok(rows.includes("The effect of contacting them"));
  assert.ok(rows.some((r) => r.startsWith("Proven")));
  const tech = $("details.tech[data-technical]");
  assert.ok(tech, "the engine's names are folded away");
  assert.equal(tech.open, false);
  assert.match(tech.textContent, /holdout/);
  assert.match(tech.textContent, /uplift/);
  // What is visible outside the folded section never uses an engine word (Plan M §4.1's right-hand column).
  const visible = [...$("main").children].filter((el) => !el.querySelector("[data-technical]")).map((el) => el.textContent).join(" ");
  assert.doesNotMatch(visible, /\b(use case|treat list|scored list|holdout|control group|uplift|propensity|AUUC|Qini|SHAP|PSI|champion|challenger|recipe|arbitration)\b/i);
  assert.equal(w.document.title, "Glossary · Marketing AI");
  assert.equal($("#pb-bar .tn-item.on"), null, "no place of the bar is marked");
});

test("What's new: the short list in whats-new.json, newest first", async () => {
  await go("#/help/whats-new");
  await until(() => $("[data-whats-new]"), 3000, "What's new");
  assert.equal($("main h1").textContent, "What's new");
  const file = JSON.parse(fs.readFileSync(NEWS_FILE, "utf8"));
  const titles = $$("[data-news] strong").map((el) => el.textContent);
  assert.deepEqual(titles, file.entries.map((e) => e.title));
  assert.equal(titles[0], "A Help menu");
  assert.equal(w.document.title, "What's new · Marketing AI");
});

test("whats-new.json is a short list of entries that each have a day, a title and one sentence", () => {
  const file = JSON.parse(fs.readFileSync(NEWS_FILE, "utf8"));
  assert.ok(Array.isArray(file.entries) && file.entries.length >= 1 && file.entries.length <= 10);
  for (const entry of file.entries) {
    assert.deepEqual(Object.keys(entry).sort(), ["text", "title", "when"]);
    for (const value of Object.values(entry)) assert.equal(typeof value, "string");
    assert.ok(entry.text.length > 20 && entry.text.length < 260, `${entry.title}: one or two sentences`);
  }
});

test("What's new with the file missing says so calmly, never an error screen", async () => {
  const { whatsNewHtml } = await import("../../../../../ui/modules/simple/help.js");
  const holder = w.document.createElement("div");
  holder.innerHTML = whatsNewHtml([]);
  assert.match(holder.textContent, /Nothing to show yet/);
  assert.equal(holder.querySelector(".apierr"), null);
});

test("#/industry/<id> goes Home, which lists the goals of the other industries", async () => {
  const ecommerce = fixture("industries").industries.find((i) => i.id === "ecommerce");
  const goal = ecommerce.stages.flatMap((s) => s.use_cases).find((u) => u.id === "hillstrom-email");
  assert.ok(goal, "the fixture has the demo's own use case, listed in the e-commerce journey only");
  await go("#/industry/ecommerce");
  await until(() => w.location.hash === "#/", 2000, "the redirect");
  await until(() => $("[data-other-goals]"), 3000, "Home");
  const link = $(`[data-other-goals] a[href="#/uc/${goal.id}"]`);
  assert.ok(link, "the goal that only the old journey page listed is still one click from Home");
  assert.equal(link.textContent, goal.name);
  assert.equal($("[data-other-goals]").tagName, "DETAILS");
  assert.equal($("[data-other-goals]").open, false, "folded");
  assert.equal($("#industry-select"), null);
  const generic = fixture("industries").industries[0];
  assert.deepEqual(
    $$(".uc-list a.uc").map((a) => a.getAttribute("href")),
    generic.stages.flatMap((s) => s.use_cases.map((u) => `#/uc/${u.id}`)),
    "the journey itself is unchanged",
  );
  // a goal the generic journey already lists is not repeated in the folded list
  assert.equal($(`[data-other-goals] a[href="#/uc/${generic.stages[0].use_cases[0].id}"]`), null);
});

test("Settings links every orphan route (Costs, Data Doctor, Check a past campaign, campaigns waiting, approvals) and hides what a role may not open", async () => {
  const { settingsHtml } = await import("../../../../../ui/modules/simple/pages.js");
  const draw = (can) => {
    const holder = w.document.createElement("div");
    holder.innerHTML = settingsHtml({ can, status: "off", version: null });
    return [...holder.querySelectorAll("a")].map((a) => a.getAttribute("href"));
  };
  const all = draw(() => true);
  for (const href of ["#/cost", "#/doctor", "#/audit", "#/monitoring/runs", "#/approvals"]) assert.ok(all.includes(href), `${href} is linked`);
  const none = draw(() => false);
  for (const href of ["#/cost", "#/doctor", "#/audit", "#/monitoring/runs", "#/approvals"]) assert.ok(!none.includes(href), `${href} is hidden from a role that may not open it`);
});

test("a readiness link by goal follows the goal's newest build", async () => {
  calls.length = 0;
  await go("#/pilot/view/readiness/telco-churn");
  await until(() => w.location.hash === "#/pilot/view/readiness/ds_new", 3000, "the newest build of the goal");
  await until(() => $("iframe.pe-frame"), 3000, "the report");
  assert.ok(!calls.some((c) => c.path === "/pilot/readiness/telco-churn"), "no request for a dataset that is not one");
  assert.match($("main").textContent, /Data readiness/);
});

test("a readiness link for a goal nothing was built for says so, without asking for a report that cannot exist", async () => {
  calls.length = 0;
  await go("#/pilot/view/readiness/hillstrom-email");
  await until(() => $("main .notice-card, main [role='status']") && /No data check for/.test($("main").textContent), 3000, "the sentence");
  assert.match($("main").textContent, /No data check for E-mail Offer Choice yet/);
  assert.match($("main").textContent, /nothing has been built for this goal yet/);
  assert.ok($('main a[href="#/pilot/kit"]'), "with the way to Build data");
  assert.ok(!calls.some((c) => c.path.startsWith("/pilot/readiness/")), "no readiness request was made");
});

test("a readiness link that names nothing known still says the data check no longer exists", async () => {
  await go("#/pilot/view/readiness/ds-missing");
  await until(() => /no longer exists/.test($("main").textContent), 3000, "the sentence");
  assert.ok(calls.some((c) => c.path === "/pilot/readiness/ds-missing"), "this one is asked for, and the 404 is the answer");
});
