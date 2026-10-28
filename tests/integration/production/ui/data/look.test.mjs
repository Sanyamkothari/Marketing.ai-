/* The Data page's look (Plan M M188, DEC-1618) in jsdom, against REAL bodies (PB_FIXTURES, written by
   tests/integration/data_page/test_data_ui.py): what the markup must say for the CSS to draw it and for a screen reader
   to read it. The words and the actions are sources.test.mjs's and actions.test.mjs's business.

   - the header is components.js's, with no action of its own: "Connect your data" is the one primary action;
   - underline tabs with the tab roles; the current one is also aria-current="page", as a link to a page is;
   - every source row has a type icon (a file or a connection) that screen readers skip - the words say the type - and
     a health chip: an icon hidden from screen readers and a word, in the tone of the status mapping;
   - rows, files and connections alike, keep rows/columns/size as quiet meta and the date as its own cell;
   - the empty state has an icon and a sentence. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage } from "../harness.mjs";

const ids = fixture("ids");
const kinds = fixture("kinds");
const world = { connections: fixture("connections").connections, uploads: fixture("uploads").uploads };

const server = async ({ method, path }) => {
  if (method === "GET" && path === "/connections/kinds") return { status: 200, body: kinds };
  if (method === "GET" && path === "/connections") return { status: 200, body: { connections: world.connections } };
  if (method === "GET" && path === "/uploads") return { status: 200, body: { uploads: world.uploads, total: world.uploads.length } };
  return null;
};

const { w } = installPage(server, { hash: "#/data" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/data/index.js");

const app = $("#app");
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
async function show(hash) {
  const parts = hash.replace(/^#\//, "").split("/");
  w.history.replaceState(null, "", `/ui/${hash}`);
  await router.resolveRoute(parts).render(app, parts);
}
const row = (id) => $(`[data-dt-id="${id}"]`);

test("the header is the look's: the title and one sentence; the connect card holds the one primary action", async () => {
  await show("#/data");
  const header = $("main > .page-header");
  assert.ok(header, "components.js pageHeader");
  assert.equal(header.querySelectorAll("h1").length, 1);
  assert.equal(header.querySelectorAll(".btn").length, 0, "no header action");
  assert.equal($$("main .btn.primary").length, 1, "the only one on the page");
  assert.equal(text($("[data-dt-connect] .btn.primary")), "Connect your data");
  assert.equal($(".crumbs"), null, "Data is a top-level place: no breadcrumb");
});

test("the tabs are underline tabs with the tab roles; the current one is also aria-current", async () => {
  const list = $('main [role="tablist"]');
  assert.ok(list && list.classList.contains("ds-tabs"));
  assert.equal(list.getAttribute("aria-label"), "Data sections");
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  assert.deepEqual(tabs.map(text), ["Sources", "Data Doctor"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("aria-selected")), ["true", "false"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("tabindex")), ["0", "-1"]);
  assert.equal(tabs[0].getAttribute("aria-current"), "page");
  assert.equal(tabs[1].hasAttribute("aria-current"), false);
  assert.equal($(".dt-tabs"), null, "the old tab strip is gone");
});

test("every source row has a type icon that screen readers skip, and a health chip: icon and word, in the mapping's tone", async () => {
  const rows = $$("[data-dt-source]");
  assert.ok(rows.length >= 4);
  for (const r of rows) {
    const type = r.querySelector(".dt-ico");
    assert.ok(type && type.querySelector("svg")?.getAttribute("aria-hidden") === "true", `${r.dataset.dtId}: a type icon, hidden from screen readers`);
    assert.equal(type.dataset.dtType, r.dataset.dtSource, "a file shows the file icon, a connection the connection one");
    assert.ok(!type.contains(r.querySelector(".dt-name")) && text(type) === "", "the icon says no words: the row's own words say the type");
    const chip = r.querySelector("[data-dt-health] .badge[data-status]");
    assert.ok(chip, `${r.dataset.dtId}: statusChip("health", …), a .badge[data-status]`);
    assert.equal(chip.querySelector("svg").getAttribute("aria-hidden"), "true");
    assert.ok(text(chip).length > 3, "and a word");
  }
  const file = row(ids.direct);
  assert.equal(file.querySelector(".badge").dataset.tone, "good");
  assert.equal(file.querySelector(".badge").dataset.status, "read");
  assert.equal(row(ids.pg).querySelector(".badge").dataset.status, "failed");
  assert.equal(row(ids.s3).querySelector(".badge").dataset.status, "connected");
  assert.equal(text(file.querySelector(".badge")), "Read correctly");
  const failed = row(ids.pg);
  assert.equal(failed.querySelector(".badge").dataset.tone, "critical", "a failed connection is critical");
  // M188 review 4: the chip is short (one word); the server's reason is the line under it, never inside the pill.
  assert.equal(text(failed.querySelector(".badge")), "Failed");
  const reason = failed.querySelector("[data-dt-health] .badge + [data-dt-health-detail]");
  assert.ok(reason && reason.classList.contains("dt-sub") && text(reason).length > 3, "the reason follows the chip as a quiet line");
  assert.equal(failed.querySelector(".badge").getAttribute("data-cn-status"), "failed", "the connection's own status stays on it");
  const connected = row(ids.s3);
  assert.equal(connected.querySelector(".badge").dataset.tone, "good");
  assert.equal(text(connected.querySelector(".badge")), "Connected");
  assert.match(text(connected.querySelector(".badge + [data-dt-health-detail]")), /^Tested /);
  for (const chip of $$(".dt-list .badge")) assert.ok(text(chip).split(" ").length <= 3, `a short chip, not a sentence: ${text(chip)}`);
  assert.equal($(".dt-list .pill, .dt-list .chip"), null, "no old pills");
});

test("rows, columns and size are quiet meta under the chip; the date is its own cell; actions are the look's buttons", async () => {
  const file = row(ids.direct);
  const meta = file.querySelector("[data-dt-health] .dt-sub");
  assert.match(text(meta), /^300 rows · \d+ columns · [\d.]+ (KB|MB)$/);
  assert.match(text(file.querySelector("[data-dt-when]")), /^Added /);
  const conn = row(ids.s3);
  const [test, edit, del] = [...conn.querySelectorAll(".dt-actions > .btn")];
  assert.deepEqual([test, edit, del].map(text), ["Test", "Edit", "Delete"]);
  assert.ok(test.classList.contains("secondary") && test.classList.contains("sm"));
  assert.ok(edit.classList.contains("tertiary") && del.classList.contains("tertiary"), "Edit and Delete are quiet");
  assert.equal(edit.getAttribute("aria-label"), `Edit ${conn.querySelector(".dt-name").firstChild.textContent}`);
});

test("Add a source: one card with a tile per kind, each with its icon and one small button", async () => {
  await show("#/data");
  const chooser = $("#dt-chooser");
  assert.ok(chooser && chooser.hidden, "closed until asked for");
  const tiles = $$("#dt-chooser .dt-kind");
  assert.ok(tiles.length >= 2);
  for (const tile of tiles) {
    assert.ok(tile.querySelector(".dt-kind-h .dt-ico svg"), `${tile.dataset.dtKind}: an icon`);
    assert.ok(tile.querySelector(".btn.secondary.sm, .dt-note"), `${tile.dataset.dtKind}: one small action, or why there is none`);
  }
  assert.equal(chooser.querySelectorAll(".card").length, 0, "no card inside the card");
});

test("an installation with no sources: an empty state with an icon and one sentence, in one card", async () => {
  const saved = { c: world.connections, u: world.uploads };
  world.connections = [];
  world.uploads = [];
  await show("#/data");
  const empty = $(".empty-state");
  assert.ok(empty.closest(".card"), "it sits in a card, not on the bare canvas");
  assert.ok(empty.querySelector(".es-icon svg"), "an icon");
  assert.equal(empty.querySelectorAll("a.btn, button.btn").length, 0, "the way forward is the connect card above");
  assert.equal($$("main .btn.primary").length, 1, "the page's one primary action is the connect card's");
  world.connections = saved.c;
  world.uploads = saved.u;
});
