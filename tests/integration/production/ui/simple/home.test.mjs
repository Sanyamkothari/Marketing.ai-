/* Plan H M81/M82: the whole page on an empty installation, sign-in off, demo mode off. The bar is the
   four places; Home is the generic journey with no industry chooser, no client chooser and nothing
   from the demo; a use case's Setup opens on Guided setup. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage, pageScripts } from "./fake.mjs";

const world = { runs: fixture("runs_empty"), waiting: null };
const { w, calls } = await installWholePage({ hash: "#/", world });

const places = () => $$("#pb-bar nav[aria-label='Main'] > ul > li > .tn-item");
const pageText = () => w.document.body.textContent;

test("the page loads the Plan H module with every other one", () => {
  assert.ok(pageScripts().includes("modules/simple/index.js"));
  assert.ok(pageScripts().includes("modules/agent/index.js"));
});

test("the top bar is exactly Home, Connections, Results and Settings", async () => {
  await until(() => places().length === 4, 3000, "the bar");
  await settle(4);
  assert.deepEqual(
    places().map((a) => [a.textContent.trim(), a.getAttribute("href")]),
    [
      ["Home", "#/"],
      ["Connections", "#/connections"],
      ["Results", "#/results"],
      ["Settings", "#/settings"],
    ],
  );
  for (const gone of ["#/pilot/kit", "#/pilot", "#/monitoring/runs", "#/monitoring/alerts", "#/uplift", "#/approvals", "#/admin/users", "#/privacy/consent"]) {
    assert.equal($(`#pb-bar a[href="${gone}"]`), null, `${gone} is not in the bar`);
  }
  assert.equal($("#pb-bar [data-menu]:not([data-menu='tn-help'])"), null, "no menu but Help");
});

test("Home is the generic journey: the customer lifecycle in order, and no industry chooser", async () => {
  await until(() => $$(".stage-pill").length === 5, 3000, "the journey");
  const generic = fixture("industries").industries[0];
  assert.equal(generic.id, "generic");
  assert.equal($("main h1").textContent, generic.journey_label);
  assert.deepEqual(
    $$(".stage-pill").map((p) => p.textContent.trim()),
    ["Awareness", "Onboarding", "Service / Payments", "Churn", "Win-back"],
  );
  const cards = $$(".uc-list a.uc").map((a) => a.getAttribute("href"));
  assert.deepEqual(
    cards,
    generic.stages.flatMap((s) => s.use_cases.map((u) => `#/uc/${u.id}`)),
  );
  assert.equal($("#industry-select"), null, "no industry chooser");
  assert.doesNotMatch($("main").textContent, /Industry/);
});

test("one company: no client chooser anywhere, and the default client is used silently", async () => {
  await settle(6);
  assert.equal($("[data-client-picker]"), null);
  assert.equal($("#f-client"), null);
  assert.ok(calls.some((c) => c.method === "POST" && c.path === "/clients/default"), "the default client was made");
  assert.doesNotMatch(pageText(), /\bClient\b/);
});

test("demo mode off: no sample-data chip, no sample tags, no demo client, no sign-in chip", async () => {
  await settle(4);
  assert.equal($(".pe-demo"), null);
  assert.doesNotMatch(pageText(), /Sample data|Demo Telecom|practice data/);
  assert.doesNotMatch($("#pb-bar").textContent, /Sign-in off/);
  assert.match($("main").textContent, /No models yet/, "an empty start says what to do first");
});

test("a use case's Setup opens on Guided setup, with Manual setup one click away", async () => {
  const id = fixture("ids").use_case;
  w.location.hash = `#/uc/${id}`;
  await until(() => $(".setup-tabs"), 3000, "the Setup tabs");
  assert.equal($('[data-setup-tab="guided"]').getAttribute("aria-pressed"), "true");
  assert.equal($('[data-setup-tab="manual"]').getAttribute("aria-pressed"), "false");
  assert.ok($("#ag-file"), "the Guided upload");
  assert.equal($("#f-file"), null);
  $('[data-setup-tab="manual"]').click();
  await until(() => $("#f-file"), 2000, "Manual setup's upload");
  assert.equal($(".tn-item.on").textContent.trim(), "Home", "a use case belongs to Home");
});
