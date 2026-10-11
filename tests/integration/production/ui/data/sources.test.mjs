/* The Data page, Sources tab (ui/modules/data/, Plan M M179) in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/data_page/test_data_ui.py). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, until } from "../harness.mjs";

const ids = fixture("ids");
const kinds = fixture("kinds");
const s3Tested = fixture("s3_tested");
const pgTested = fixture("pg_tested");

const world = {
  connections: fixture("connections").connections,
  uploads: fixture("uploads").uploads,
  uploadsStatus: 200,
  connectionsStatus: 200,
};
const sent = { tests: [], deletes: [] };

const server = (request) => {
  const { method, path, query } = request;
  if (method === "GET" && path === "/connections/kinds") return { status: 200, body: kinds };
  if (method === "GET" && path === "/connections") {
    if (world.connectionsStatus !== 200) {
      return { status: world.connectionsStatus, body: { detail: { code: "INTERNAL", message: "Something went wrong." } } };
    }
    return { status: 200, body: { connections: world.connections } };
  }
  if (method === "GET" && path === "/uploads") {
    if (world.uploadsStatus !== 200) {
      return { status: world.uploadsStatus, body: { detail: { code: "INTERNAL", message: "Something went wrong." } } };
    }
    return { status: 200, body: { uploads: world.uploads.slice(0, Number(query.limit || 100)) } };
  }
  const one = /^\/connections\/([^/]+)(?:\/(\w+))?$/.exec(path);
  if (one) {
    const [, id, action] = one;
    if (method === "DELETE" && !action) {
      sent.deletes.push(id);
      world.connections = world.connections.filter((c) => c.connection_id !== id);
      return { status: 204, body: null };
    }
    if (method === "POST" && action === "test") {
      sent.tests.push(id);
      return { status: 200, body: id === ids.s3 ? s3Tested : pgTested };
    }
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/data" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/data/index.js");

const app = $("#app");
async function show(hash) {
  const parts = hash.replace(/^#\//, "").split("/");
  w.history.replaceState(null, "", `/ui/${hash}`);
  const module = router.resolveRoute(parts);
  assert.ok(module, `a module claims ${hash}`);
  await module.render(app, parts);
}
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const row = (id) => $(`[data-dt-id="${id}"]`);

test("the page is registered at #/data", () => {
  assert.deepEqual(
    router.phaseModules().find((m) => m.name === "data"),
    { name: "data", routes: ["data"] },
  );
});

test("one H1, one sentence, one primary button", async () => {
  await show("#/data");
  assert.equal($$("h1").length, 1);
  assert.equal(text($("h1")), "Data");
  assert.equal(text($(".desc")), "Where your customer data comes from, and whether it is ready to use.");
  assert.equal($$(".btn.primary").length, 1);
  assert.equal(text($(".btn.primary")), "+ Add a source");
});

test("connections and uploaded files are one list, each with its health and date", async () => {
  await show("#/data");
  const rows = $$("[data-dt-source]");
  assert.deepEqual(
    rows.map((r) => r.dataset.dtSource),
    ["connection", "connection", "file", "file"],
    "connections first, then the files, newest first",
  );
  assert.equal($$(".dt-list").length, 1, "one list");

  const ok = row(ids.s3);
  assert.equal(text(ok.querySelector(".dt-name")), "Exports" + "Amazon S3");
  assert.match(text(ok.querySelector("[data-dt-health]")), /^Connected · tested /);
  assert.match(text(ok.querySelector("[data-dt-when]")), /^Last import \d{2} \w{3} \d{4}/, "the import from it dates its last refresh");

  const failed = row(ids.pg);
  assert.match(text(failed.querySelector("[data-dt-health]")), /^Failed: /);
  assert.equal(text(failed.querySelector("[data-dt-when]")), "Nothing imported yet");

  const direct = row(ids.direct);
  assert.match(text(direct.querySelector("[data-dt-health]")), /^Read correctly300 rows · \d+ columns · [\d.]+ (KB|MB)$/);
  assert.match(text(direct.querySelector("[data-dt-when]")), /^Uploaded \d{2} \w{3} \d{4}/);
  assert.match(text(direct.querySelector(".dt-sub")), /^Uploaded file$/);

  const imported = row(ids.imported);
  assert.match(text(imported.querySelector(".dt-sub")), /^Imported from Exports$/);
  assert.match(text(imported.querySelector("[data-dt-health]")), /^Read correctly600 rows/);
});

test("a name that carries markup is shown as text", async () => {
  await show("#/data");
  assert.equal($("#app img"), null);
  assert.equal($("#app b[onmouseover]"), null);
  assert.equal(w.__pwned, undefined);
  assert.ok(text(row(ids.pg).querySelector(".dt-name")).includes("<img src=x"), "the name is on the page, as text");
  assert.ok(text(row(ids.direct).querySelector(".dt-name")).includes("<b onmouseover"), "so is the file name");
});

test("a connection keeps Test, Edit and Delete; a file has no action of its own", async () => {
  await show("#/data");
  const ok = row(ids.s3);
  assert.deepEqual(
    [...ok.querySelectorAll(".dt-actions > *")].map(text),
    ["Test", "Edit", "Delete"],
  );
  assert.equal(ok.querySelector("[data-dt-edit]").getAttribute("href"), `#/connections/${ids.s3}`, "the existing form stays");
  assert.equal(row(ids.direct).querySelectorAll(".dt-actions > *").length, 0);
});

test("Test runs the existing test and shows each step", async () => {
  await show("#/data");
  sent.tests.length = 0;
  row(ids.pg).querySelector("[data-dt-test]").click();
  await until(() => sent.tests.length === 1, 2000, "the test call");
  assert.deepEqual(sent.tests, [ids.pg]);
  await until(() => row(ids.pg).querySelector("[data-cn-report]"), 2000, "the report");
  assert.match(text(row(ids.pg).querySelector(".cn-verdict")), /^Not connected yet/);
  assert.ok(row(ids.pg).querySelectorAll("[data-cn-step]").length >= 1);
  assert.equal($$(".btn.primary").length, 1, "still one primary button");
});

test("Delete asks first; Keep it changes nothing; Delete removes the connection and keeps its files", async () => {
  await show("#/data");
  sent.deletes.length = 0;
  row(ids.pg).querySelector("[data-dt-delete]").click();
  assert.match(text(row(ids.pg)), /Delete .+\? Files already imported stay\./);
  row(ids.pg).querySelector("[data-dt-delete-no]").click();
  assert.deepEqual(sent.deletes, []);
  assert.ok(row(ids.pg).querySelector("[data-dt-delete]"), "back to the three actions");

  row(ids.pg).querySelector("[data-dt-delete]").click();
  row(ids.pg).querySelector("[data-dt-delete-yes]").click();
  await until(() => sent.deletes.length === 1, 2000, "the delete call");
  assert.deepEqual(sent.deletes, [ids.pg]);
  await until(() => row(ids.pg) === null, 2000, "the row to go");
  assert.ok(row(ids.s3), "the other connection stays");
  assert.ok(row(ids.imported), "files already imported stay");
});

test("+ Add a source opens the kinds; each links to the existing form; a file goes to Data Doctor", async () => {
  await show("#/data");
  const chooser = $("#dt-chooser");
  assert.equal(chooser.hidden, true, "closed until asked for");
  $("#dt-add").click();
  assert.equal(chooser.hidden, false);
  assert.equal($("#dt-add").getAttribute("aria-expanded"), "true");
  const links = Object.fromEntries($$("[data-dt-new]").map((a) => [a.dataset.dtNew, a.getAttribute("href")]));
  for (const kind of kinds.kinds.filter((k) => k.creatable && k.available)) {
    assert.equal(links[kind.kind], `#/connections/new/${kind.kind}`, `${kind.label} has its set-up form`);
  }
  assert.equal(links.file, "#/doctor");
  assert.equal(links.ai_service, undefined, "the AI service is not a data source");
  $("#dt-add").click();
  assert.equal(chooser.hidden, true, "and closes again");
});

test("Data Doctor is one click away", async () => {
  await show("#/data");
  const link = $("[data-dt-doctor]");
  assert.equal(text(link), "Check my data");
  assert.equal(link.getAttribute("href"), "#/doctor");
});

test("the tabs link to Sources and Check my data", async () => {
  await show("#/data");
  const tabs = $$("[data-dt-tab]");
  assert.deepEqual(tabs.map(text), ["Sources", "Check my data"]);
  assert.deepEqual(tabs.map((t) => t.getAttribute("href")), ["#/data", "#/data/check"]);
  assert.equal($("[data-dt-tab][aria-current='page']").dataset.dtTab, "sources");
  await show("#/data/sources");
  assert.equal($("[data-dt-tab][aria-current='page']").dataset.dtTab, "sources", "#/data/sources is the same tab");
});

test("an installation with no sources says so", async () => {
  const saved = { c: world.connections, u: world.uploads };
  world.connections = [];
  world.uploads = [];
  await show("#/data");
  assert.match(text($(".empty-state")), /No data sources yet/);
  assert.equal($$("[data-dt-source]").length, 0);
  assert.equal($$(".btn.primary").length, 1, "the way forward is the one primary button");
  world.connections = saved.c;
  world.uploads = saved.u;
});

test("when the files cannot be listed the connections still show, and the page says what failed", async () => {
  world.uploadsStatus = 500;
  await show("#/data");
  assert.ok(row(ids.s3), "connections draw");
  assert.match(text($(".apierr")), /The uploaded files could not be listed\./);
  world.uploadsStatus = 200;
});

test("when the connections cannot be listed the page offers to try again", async () => {
  world.connectionsStatus = 500;
  await show("#/data");
  assert.ok($(".apierr [data-retry]"), "a Try again button");
  world.connectionsStatus = 200;
});

test("a long list of files shows 25, then all", async () => {
  const saved = world.uploads;
  world.uploads = Array.from({ length: 30 }, (_, i) => ({ ...saved[0], upload_id: `u_${String(i).padStart(12, "0")}`, file_name: `file-${i}.csv` }));
  world.connections = [];
  await show("#/data");
  assert.equal($$("[data-dt-source='file']").length, 25);
  const more = $("[data-dt-show-all]");
  assert.equal(text(more), "Show all 30 files");
  more.click();
  await settle();
  assert.equal($$("[data-dt-source='file']").length, 30);
  assert.equal($("[data-dt-show-all]"), null);
  world.uploads = saved;
  world.connections = fixture("connections").connections;
});

test("drawing the page asks only for what it shows", async () => {
  calls.length = 0;
  await show("#/data");
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`).sort(),
    ["GET /connections", "GET /connections/kinds", "GET /uploads"],
  );
});

test("no never-shown word appears on the page, with a saved connection, a failed test and two kinds of file", async () => {
  const { findNeverShown } = await import("../../../../../ui/labels.js");
  const { visibleText } = await import("../glossary/collect.mjs");
  world.connections = fixture("connections").connections;
  await show("#/data");
  row(ids.pg).querySelector("[data-dt-test]").click();
  await until(() => row(ids.pg).querySelector("[data-cn-report]"), 2000, "the report");
  row(ids.s3).querySelector("[data-dt-test]").click();
  await until(() => row(ids.s3).querySelector("[data-cn-report]"), 2000, "the second report");
  $("#dt-add").click();
  const found = findNeverShown(visibleText($("main.dt")));
  assert.deepEqual(found, {}, "an empty allow-list: this page shows none of the glossary's never-shown words");
  await show("#/data/check");
  assert.deepEqual(findNeverShown(visibleText($("main.dt"))), {});
});
