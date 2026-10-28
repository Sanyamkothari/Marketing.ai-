/* Plan H M81/M82: the whole page on an empty installation, sign-in off, demo mode off. The bar is the
   four places; Home is the generic journey with no industry chooser, no client chooser and nothing
   from the demo; a use case's Setup opens on the Data Doctor. */
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
  assert.ok(pageScripts().includes("modules/datadoctor/index.js"));
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
  // Plan M M172 (DEC-1602): the lifecycle stage is a small label on each goal card (no stage columns, no colour
  // legend), and the operational goals fold under a closed "More goals" after the rest, in the journey's order.
  await until(() => $$("[data-goals] a.uc").length === 5, 3000, "the journey");
  const generic = fixture("industries").industries[0];
  assert.equal(generic.id, "generic");
  assert.equal($("main h1").textContent, generic.journey_label);
  const stageOf = Object.fromEntries(generic.stages.flatMap((s) => s.use_cases.map((u) => [u.id, s.name])));
  assert.deepEqual(
    $$(".uc-list a.uc").map((a) => a.querySelector(".uc-stage").textContent.trim()),
    $$(".uc-list a.uc").map((a) => stageOf[a.getAttribute("href").replace("#/uc/", "")]),
  );
  assert.deepEqual(
    [...new Set($$(".uc-list a.uc .uc-stage").map((p) => p.textContent.trim()))].sort(),
    ["Awareness", "Churn", "Onboarding", "Service", "Win-back"],
  );
  const ids = generic.stages.flatMap((s) => s.use_cases.map((u) => u.id));
  const more = ["order-fulfillment", "fault-prediction"];
  const cards = $$(".uc-list a.uc").map((a) => a.getAttribute("href"));
  assert.deepEqual(
    cards,
    [...ids.filter((id) => !more.includes(id)), ...ids.filter((id) => more.includes(id))].map((id) => `#/uc/${id}`),
  );
  assert.equal($("[data-more-goals]").open, false, "More goals is closed");
  assert.doesNotMatch($("main").textContent, /What do the colours mean/);
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

test("a use case's Setup opens on the Data Doctor, with Manual setup one click away", async () => {
  const id = fixture("ids").use_case;
  w.location.hash = `#/uc/${id}`;
  await until(() => $(".setup-tabs"), 3000, "the Setup tabs");
  assert.equal($('[data-setup-tab="datadoctor"]').getAttribute("aria-pressed"), "true");
  assert.equal($('[data-setup-tab="manual"]').getAttribute("aria-pressed"), "false");
  assert.ok($("[data-dds-place=\"setup\"]"), "the Data Doctor screen");
  assert.equal($("#f-file"), null);
  $('[data-setup-tab="manual"]').click();
  await until(() => $("#f-file"), 2000, "Manual setup's upload");
  assert.equal($(".tn-item.on").textContent.trim(), "Home", "a use case belongs to Home");
});
