/* Plan H M82: the Settings page (`#/settings`) in the whole page with sign-in off - the AI service,
   Privacy and Schedules; the advanced tools folded away; no Admin while sign-in is off; About says
   the version and that sign-in is off - and, from its pure builder, the Admin group an Admin gets
   once sign-in is on. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const world = { runs: fixture("runs_empty"), waiting: null };
const { w } = await installWholePage({ hash: "#/settings", world });
const { settingsHtml } = await import("../../../../../ui/modules/simple/pages.js");

const hrefs = (sel) => $$(`${sel} a`).map((a) => a.getAttribute("href"));

test("Settings groups what a new user does not need, each a link to a screen that already exists", async () => {
  await until(() => $("[data-settings-main]") && $("[data-signin-off]"), 3000, "the Settings page");
  assert.equal($("main h1").textContent, "Settings");
  assert.deepEqual(hrefs("[data-settings-main]"), ["#/connections", "#/privacy/consent", "#/monitoring/schedules"]);
  assert.deepEqual(
    $$("[data-settings-main] a").map((a) => a.textContent),
    ["AI service", "Privacy", "Schedules"],
  );
  assert.equal($("#pb-bar .tn-item.on").textContent.trim(), "Settings");
});

test("the advanced tools are folded away: uplift, the data kit, raw tables, Model health, the assistant", () => {
  const details = $("[data-settings-advanced] details");
  assert.ok(details, "under a disclosure");
  assert.equal(details.open, false, "closed until asked for");
  assert.equal(details.querySelector("summary").textContent, "Advanced tools");
  assert.deepEqual(
    $$("[data-settings-advanced] a").map((a) => a.textContent),
    ["Uplift workbench", "Data request kit", "Build from raw tables", "Model health", "Document assistant"],
  );
  assert.deepEqual(hrefs("[data-settings-advanced]"), [
    "#/uplift",
    "#/pilot/kit",
    "#/",
    "#/monitoring/alerts",
    "#/uc/ai-onboarding-assistant",
  ]);
  const assistant = fixture("industries").industries.flatMap((i) => i.stages.flatMap((s) => s.use_cases));
  assert.ok(assistant.some((u) => u.id === "ai-onboarding-assistant"), "the assistant's id is a real use case");
});

test("with sign-in off there is no Admin group, and About says so with the version", () => {
  assert.equal($("[data-settings-admin]"), null);
  assert.equal($('#app a[href="#/admin/users"]'), null);
  assert.match($("[data-signin-off]").textContent, /Sign-in is off/);
  assert.match($("[data-settings-about]").textContent, new RegExp(fixture("healthz").version.replace(/\./g, "\\.")));
});

test("with sign-in on, an Admin gets Users and Audit log, and a role that may not sees less", () => {
  const draw = (can) => {
    const holder = w.document.createElement("div");
    holder.innerHTML = settingsHtml({ can, status: "signed-in", version: null });
    return holder;
  };
  const admin = draw(() => true);
  assert.deepEqual(
    [...admin.querySelectorAll("[data-settings-admin] a")].map((a) => a.getAttribute("href")),
    ["#/admin/users", "#/admin/audit"],
  );
  assert.equal(admin.querySelector("[data-signin-off]"), null);
  const viewer = draw((method, path) => !["/users", "/audit/events", "/privacy/purposes", "/schedules"].includes(path) && method === "GET");
  assert.equal(viewer.querySelector("[data-settings-admin]"), null);
  assert.equal(viewer.querySelector('a[href="#/privacy/consent"]'), null);
  assert.equal(viewer.querySelector('a[href="#/monitoring/schedules"]'), null);
  assert.equal(viewer.querySelector('a[href="#/connections"]'), null, "testing the AI service is a POST");
});
