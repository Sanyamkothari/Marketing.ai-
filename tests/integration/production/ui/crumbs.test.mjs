/* Every screen's breadcrumb starts at the place the top bar marks for it (docs/UI_AUDIT.md §8.4
   items 3 and 12). Plan H M82 moved Build data, Schedules, Privacy, Admin and the Uplift workbench
   under Settings, the campaign screens and Waiting for approval under Results and the AI service under
   Connections, but their crumbs still read "Home › Uplift modelling", "Home › Model health ›
   Schedules", "Home › Campaigns › …"; and a use case read "Home › Customer Lifecycle › …", two crumbs
   opening one page. The whole page as index.html loads it, sign-in off (the real `GET /auth/me` in
   PB_FIXTURES), every other call answered 404 unless listed: the head of each screen is drawn either
   way, and the crumbs are in the head. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, until } from "./harness.mjs";

const industries = fixture("industries");
const planned = { status: 409, body: { detail: { code: "USE_CASE_PLANNED", message: "Planned.", path: null } } };
const server = (request) => {
  if (request.path === "/auth/me") return { status: 200, body: fixture("me_off") };
  if (request.path === "/industries") return { status: 200, body: industries };
  if (request.path === "/connection/aws") return { status: 200, body: fixture("connection") };
  if (request.path === "/use-cases/targeted-advertisement" || request.path === "/use-cases/primary-bank-attrition") return planned;
  return null;
};
const { w } = installPage(server, { hash: "#/" });

await import("../../../../ui/app.js");
await import("../../../../ui/modules/generative/index.js");
await import("../../../../ui/modules/production/index.js");
await import("../../../../ui/modules/uplift/index.js");
await import("../../../../ui/modules/pilot/index.js");
await import("../../../../ui/modules/simple/index.js");
await import("../../../../ui/modules/connections/index.js");

/** The breadcrumb as [label, href] pairs (href null for the screen itself). */
const trail = () => $$("#app .crumbs a, #app .crumbs .cur").map((e) => [e.textContent, e.getAttribute("href")]);
const marked = () => (($("#pb-bar .tn-item.on") || {}).textContent || "").trim().replace(/\s*\d+$/, "");
const PLACE_HREF = { Home: "#/", Connections: "#/connections", Results: "#/results", Settings: "#/settings" };

async function open(hash) {
  w.location.hash = hash;
  await until(() => $("#app .crumbs") && !$("#app [data-skeleton]"), 3000, `the head of ${hash}`);
  await settle(6);
  return trail();
}

const SETTINGS = ["Settings", "#/settings"];
const RESULTS = ["Results", "#/results"];

const CASES = [
  // Settings
  ["#/uplift", [SETTINGS, ["Uplift workbench", null]]],
  ["#/pilot/kit", [SETTINGS, ["Build data", null]]],
  ["#/pilot/view/readiness/ds-missing", [SETTINGS, ["Build data", "#/pilot/kit"], ["Data readiness", null]]],
  ["#/monitoring/schedules", [SETTINGS, ["Schedules", null]]],
  ["#/monitoring/alerts", [SETTINGS, ["Model health", "#/monitoring/alerts"], ["Alerts", null]]],
  ["#/admin/users", [SETTINGS, ["Users", null]]],
  ["#/admin/audit", [SETTINGS, ["Audit log", null]]],
  // Results
  ["#/pilot", [RESULTS, ["Reports", null]]],
  ["#/pilot/value/r-missing", [RESULTS, ["Value in rupees", null]]],
  ["#/monitoring/runs", [RESULTS, ["Campaigns", null]]],
  ["#/approvals", [RESULTS, ["Waiting for approval", null]]],
  ["#/campaign/targeted-advertisement/r-missing", [RESULTS, ["Not found", null]]],
  // Connections
  ["#/generative/connection", [["Connections", "#/connections"], ["Amazon Bedrock sign-in", null]]],
  ["#/connections/ai/product", [["Connections", "#/connections"], ["Product AI", null]]],
  ["#/connections/ai/deliverable", [["Connections", "#/connections"], ["Deliverable AI", null]]],
  // Home: the one journey is Home's own page, so it is not a crumb of its own
  ["#/uc/targeted-advertisement", [["Home", "#/"], ["Targeted Advertisement", null]]],
];

for (const [hash, expected] of CASES) {
  test(`${hash} reads ${expected.map(([label]) => label).join(" › ")}`, async () => {
    const got = await open(hash);
    assert.deepEqual(got, expected);
    const place = marked();
    assert.equal(got[0][0], place, "the first crumb is the place the bar marks");
    assert.equal(got[0][1], PLACE_HREF[place], "and opens it");
  });
}

test("the Privacy screens start at Settings, then Privacy", async () => {
  const got = await open("#/privacy/consent");
  assert.deepEqual(got.slice(0, 2), [SETTINGS, ["Privacy", "#/privacy/consent"]]);
  assert.equal(marked(), "Settings");
});

test("Campaign copy (marked Results) starts at Results and goes back to its scoring run", async () => {
  const { copyHtml } = await import("../../../../ui/modules/generative/copy.js");
  const { navFor } = await import("../../../../ui/chrome.js");
  const uc = { id: "win-back-campaign", name: "Win-back Campaign", marker: "G" };
  const state = { runId: "r1", run: { run_id: "r1", created_at: "2026-09-01T00:00:00Z" }, art: {}, busy: {} };
  const holder = w.document.createElement("div");
  holder.innerHTML = copyHtml(uc, state);
  const got = [...holder.querySelectorAll(".crumbs a, .crumbs .cur")].map((e) => [e.textContent, e.getAttribute("href")]);
  assert.equal(navFor("#/generative/copy/win-back-campaign/r1"), "results");
  assert.deepEqual(got, [RESULTS, ["Win-back Campaign", "#/uc/win-back-campaign/run/r1"], ["Campaign copy", null]]);
});

test("a use case of another journey (a URL-only industry) keeps that journey's crumb: it is a page of its own", async () => {
  const banking = industries.industries.find((i) => i.id === "banking");
  const got = await open("#/uc/primary-bank-attrition");
  assert.deepEqual(got, [["Home", "#/"], [banking.journey_label, "#/industry/banking"], ["Primary Bank Attrition", null]]);
});
