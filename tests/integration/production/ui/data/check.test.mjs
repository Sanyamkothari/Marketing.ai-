/* The Data page, "Check my data" tab (ui/modules/data/, Plan M M179) in jsdom. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage } from "../harness.mjs";

const { w, calls } = installPage(() => null, { hash: "#/data/check" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/data/index.js");

const app = $("#app");
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
async function show(hash) {
  const parts = hash.replace(/^#\//, "").split("/");
  w.history.replaceState(null, "", `/ui/${hash}`);
  await router.resolveRoute(parts).render(app, parts);
}

test("the tab names Data Doctor and opens it with the one primary button", async () => {
  await show("#/data/check");
  assert.equal($$("h1").length, 1);
  assert.equal(text($("h1")), "Data");
  assert.equal($("[data-dt-tab][aria-current='page']").dataset.dtTab, "check");
  assert.equal(text($("#dt-check-h")), "Check my data");
  const primary = $$(".btn.primary");
  assert.equal(primary.length, 1);
  assert.equal(text(primary[0]), "Open Data Doctor");
  assert.equal(primary[0].getAttribute("href"), "#/doctor", "Data Doctor's page is unchanged and one click away");
});

test("the tab explains what Data Doctor does, in plain words", async () => {
  await show("#/data/check");
  const words = text($(".dt-check"));
  assert.match(words, /looks at your files before you use them/);
  assert.match(words, /what each file is/);
  assert.match(words, /what is missing/);
  assert.match(words, /how several files fit together/);
});

test("the tab asks the API for nothing", async () => {
  calls.length = 0;
  await show("#/data/check");
  assert.deepEqual(calls, []);
});

test("the Sources tab is one click back", async () => {
  await show("#/data/check");
  assert.equal($("[data-dt-tab='sources']").getAttribute("href"), "#/data");
});
