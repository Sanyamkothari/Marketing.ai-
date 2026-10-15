/* Plan M, M179 acceptance: "everything on today's Connections page is on this page". The Connections page is drawn,
   then the Data page, against the same answers, and the actions of the first are compared with the second's.
   (PB_FIXTURES, written by tests/integration/data_page/test_data_ui.py.) */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage } from "../harness.mjs";

const ids = fixture("ids");
const kinds = fixture("kinds");
const connections = fixture("connections").connections;
const uploads = fixture("uploads").uploads;
const aiSlot = (over) => ({ connected: false, source: "none", provider: null, provider_label: null, model: null, has_key: false, key_readable: true, third_party: false, last_test: null, editable: true, locked_reason: null, ...over });

const server = ({ method, path }) => {
  if (method !== "GET") return null;
  if (path === "/connections/kinds") return { status: 200, body: kinds };
  if (path === "/connections") return { status: 200, body: { connections } };
  if (path === "/uploads") return { status: 200, body: { uploads } };
  if (path === "/ai-service") return { status: 200, body: { slots: { product: aiSlot({}), deliverable: aiSlot({}) }, providers: [] } };
  return null;
};

installPage(server, { hash: "#/connections" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/connections/index.js");
await import("../../../../../ui/modules/data/index.js");
const app = $("#app");

async function show(hash) {
  const parts = hash.replace(/^#\//, "").split("/");
  await router.resolveRoute(parts).render(app, parts);
}

/** The Connections page's actions, as "what:which": a test, an edit and a delete for each saved connection, and a
 *  set-up form for each kind that can be set up. */
function connectionsPageActions() {
  const actions = new Set();
  for (const b of $$("[data-cn-test]")) actions.add(`test:${b.dataset.cnTest}`);
  for (const b of $$("[data-cn-delete]")) actions.add(`delete:${b.dataset.cnDelete}`);
  for (const a of $$("[data-cn-connection] a[href^='#/connections/']")) actions.add(`edit:${a.getAttribute("href").split("/").pop()}`);
  for (const a of $$("a[href^='#/connections/new/']")) actions.add(`new:${a.getAttribute("href").split("/").pop()}`);
  return actions;
}

function dataPageActions() {
  const actions = new Set();
  for (const b of $$("[data-dt-test]")) actions.add(`test:${b.dataset.dtTest}`);
  for (const b of $$("[data-dt-delete]")) actions.add(`delete:${b.dataset.dtDelete}`);
  for (const a of $$("[data-dt-edit]")) actions.add(`edit:${a.dataset.dtEdit}`);
  for (const a of $$("[data-dt-new]")) if (a.dataset.dtNew !== "file") actions.add(`new:${a.dataset.dtNew}`);
  return actions;
}

test("every action of today's Connections page is on the Data page", async () => {
  await show("#/connections");
  const before = connectionsPageActions();
  assert.ok(before.has(`test:${ids.s3}`) && before.has(`edit:${ids.pg}`) && before.has(`delete:${ids.pg}`), "the Connections page offers the row actions");
  assert.ok(before.has("new:s3") && before.has("new:postgres"), "and a set-up form for each kind");
  assert.ok([...before].filter((a) => a.startsWith("new:")).length >= 5, "all the kinds that can be set up");

  await show("#/data");
  const after = dataPageActions();
  const missing = [...before].filter((a) => !after.has(a));
  assert.deepEqual(missing, [], "nothing the Connections page can do is missing from the Data page");
});

test("the only things the Connections page draws that the Data page leaves to Settings are the two AI service cards", async () => {
  await show("#/connections");
  const aiLinks = $$("[data-cn-ai] a").map((a) => a.getAttribute("href")).sort();
  assert.deepEqual(aiLinks, ["#/connections/ai/deliverable", "#/connections/ai/product"]);
  await show("#/data");
  // Plan M M192 (DEC-1622): straight to Settings › AI service, not to Settings (whose row used to open Connections,
  // which the campaigns layout redirects back here: a loop).
  assert.equal($("[data-dt-settings]").getAttribute("href"), "#/settings/ai", "the Data page says where the AI service is");
});

test("the Data page's note names the place in words and links it: Settings › AI service (M192)", async () => {
  await show("#/data");
  const note = $("[data-dt-settings]").closest("p");
  assert.equal(note.textContent.replace(/\s+/g, " ").trim(), "Connect an AI service in Settings › AI service.");
  assert.equal($("[data-dt-settings]").textContent, "Settings › AI service");
});

test("the Sources tab says in one line whether the Product AI is connected", async () => {
  await show("#/data");
  const line = $("[data-dt-ai]");
  assert.equal(line.dataset.dtAi, "not-connected");
  assert.match(line.textContent, /Product AI not connected/);
  assert.equal(line.querySelector("a").getAttribute("href"), "#/settings/ai");
});

test("the old Connections route is still registered and still draws", async () => {
  await show("#/connections");
  assert.equal($("h1").textContent, "Connections");
  assert.ok($("[data-cn-connection]"), "its cards are there");
});
