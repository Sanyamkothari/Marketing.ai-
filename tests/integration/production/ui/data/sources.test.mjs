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
  cases: [], // the Data Doctor checks (GET /data-doctor/cases)
  total: null, // the `total` the fake API answers with; null means "every upload", like the real route
  gate: null, // a promise the connection test waits for, to answer after the person has left
};
const sent = { tests: [], deletes: [] };

const server = async (request) => {
  const { method, path, query } = request;
  if (method === "GET" && path === "/connections/kinds") return { status: 200, body: kinds };
  if (method === "GET" && path === "/connections") {
    if (world.connectionsStatus !== 200) {
      return { status: world.connectionsStatus, body: { detail: { code: "INTERNAL", message: "Something went wrong." } } };
    }
    return { status: 200, body: { connections: world.connections } };
  }
  if (method === "GET" && path === "/auth/me") {
    return { status: 200, body: { principal: "p", auth_mode: "off", permissions: [], data_doctor: true } };
  }
  if (method === "GET" && path === "/data-doctor/cases") return { status: 200, body: { cases: world.cases } };
  if (method === "GET" && path === "/uploads") {
    if (world.uploadsStatus !== 200) {
      return { status: world.uploadsStatus, body: { detail: { code: "INTERNAL", message: "Something went wrong." } } };
    }
    const uploads = world.uploads.slice(0, Number(query.limit || 100));
    return { status: 200, body: { uploads, total: world.total ?? world.uploads.length } };
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
      if (world.gate) await world.gate;
      return { status: 200, body: id === ids.s3 ? s3Tested : pgTested };
    }
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/data" });
const router = await import("../../../../../ui/modules/router.js");
const session = await import("../../../../../ui/modules/production/session.js");
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
    ["connection", "connection", "file", "file", "file", "file"],
    "connections first, then the files, newest first",
  );
  assert.equal($$(".dt-list").length, 1, "one list");

  const ok = row(ids.s3);
  assert.equal(text(ok.querySelector(".dt-name")), "Exports" + "Amazon S3");
  // M188 (DEC-1618): the chip says the status in a word; when it was tested is the line under it, in the same order.
  assert.equal(text(ok.querySelector("[data-dt-health] .badge")), "Connected");
  assert.match(text(ok.querySelector("[data-dt-health-detail]")), /^Tested \d{2} \w{3} \d{4}/);
  assert.match(text(ok.querySelector("[data-dt-when]")), /^Last import \d{2} \w{3} \d{4}/, "the import from it dates its last refresh");

  const failed = row(ids.pg);
  assert.equal(text(failed.querySelector("[data-dt-health] .badge")), "Failed");
  assert.ok(text(failed.querySelector("[data-dt-health-detail]")).length > 3, "the server's reason, on the line under the chip");
  assert.equal(text(failed.querySelector("[data-dt-when]")), "No file imported yet", "it claims only what the list shows");

  const direct = row(ids.direct);
  assert.match(text(direct.querySelector("[data-dt-health]")), /^Read correctly300 rows · \d+ columns · [\d.]+ (KB|MB)$/);
  assert.match(text(direct.querySelector("[data-dt-when]")), /^Added \d{2} \w{3} \d{4}/);
  assert.match(text(direct.querySelector(".dt-sub")), /^File$/, "a file with no marker is not said to be uploaded by a person");

  const imported = row(ids.imported);
  assert.match(text(imported.querySelector(".dt-sub")), /^Imported from Exports$/);
  assert.match(text(imported.querySelector("[data-dt-when]")), /^Imported \d{2} \w{3} \d{4}/);
  assert.match(text(imported.querySelector("[data-dt-health]")), /^Read correctly600 rows/);
});

test("a file pulled from a connection, a renamed copy and a built dataset are each named for what they are", async () => {
  const copyOf = world.uploads.find((u) => u.upload_id === ids.copy);
  const built = { ...copyOf, upload_id: "u_built0000001", file_name: "customers_prepared.parquet", made_by: "recipe" };
  world.uploads = [...world.uploads, built];
  await show("#/data");

  const pulled = row(ids.pulled);
  assert.match(text(pulled.querySelector(".dt-sub")), /^Pulled from Exports$/);
  assert.match(text(pulled.querySelector("[data-dt-when]")), /^Pulled \d{2} \w{3} \d{4}/);

  const copy = row(ids.copy);
  assert.equal(copyOf.file_name, ids.hostile_file, "the copy has the same name as the file sent: the row says which it is");
  assert.match(text(copy.querySelector(".dt-sub")), /^Copy with renamed columns\. The file you sent is unchanged\.$/);
  assert.match(text(copy.querySelector("[data-dt-when]")), /^Made /);
  assert.match(text(row(ids.direct).querySelector(".dt-sub")), /^File$/, "the file that was sent keeps its own label");

  assert.match(text(row("u_built0000001").querySelector(".dt-sub")), /^Built by Guided setup from another file$/);
  assert.match(text(row("u_built0000001").querySelector("[data-dt-when]")), /^Built /);
  for (const r of $$("[data-dt-source='file']")) assert.ok(!/Uploaded/.test(text(r)), "no row claims a person uploaded it");
  world.uploads = world.uploads.filter((u) => u.upload_id !== "u_built0000001");
});

test("a connection whose data came by a pulled file dates its last import from it", async () => {
  const saved = world.uploads;
  world.uploads = saved.filter((u) => u.upload_id === ids.pulled);
  await show("#/data");
  assert.match(text(row(ids.s3).querySelector("[data-dt-when]")), /^Last import \d{2} \w{3} \d{4}/);
  world.uploads = saved;
});

test("a connection with no file in the list never claims that nothing came in", async () => {
  const saved = world.uploads;
  world.uploads = [];
  await show("#/data");
  for (const c of world.connections) {
    const said = text(row(c.connection_id).querySelector("[data-dt-when]"));
    assert.equal(said, "No file imported yet");
    assert.ok(!/nothing/i.test(said), "data can also reach a source without a file (Setup, the scheduled refresh)");
  }
  world.uploadsStatus = 500;
  await show("#/data");
  assert.equal(text(row(ids.s3).querySelector("[data-dt-when]")), "", "when the files cannot be listed it says nothing about imports");
  world.uploadsStatus = 200;
  world.uploads = saved;
});

test("a name that carries markup is shown as text", async () => {
  await show("#/data");
  assert.equal($("#app img"), null);
  assert.equal($("#app b[onmouseover]"), null);
  assert.equal(w.__pwned, undefined);
  assert.ok(text(row(ids.pg).querySelector(".dt-name")).includes("<img src=x"), "the name is on the page, as text");
  assert.ok(text(row(ids.direct).querySelector(".dt-name")).includes("<b onmouseover"), "so is the file name");
  // Plan M final integration: a source's name and a file's name are the person's words (M172 data-user-text).
  for (const id of [ids.pg, ids.direct]) {
    assert.ok(row(id).querySelector(".dt-name > [data-user-text]"), `${id}: the name is marked as the person's own`);
    assert.equal(row(id).querySelector(".dt-name > [data-user-text]").children.length, 0, "and stays text");
  }
});

test("files that belong to a Data Doctor check are listed once, under the check, with a link to it", async () => {
  const [a, b] = world.uploads;
  await session.loadMe();
  world.cases = [
    { case_id: "case_one", title: "a and 1 more", goal_id: null, state: "open", files: [a.file_name, b.file_name], upload_ids: [a.upload_id, b.upload_id], updated_at: a.created_at },
  ];
  await show("#/data");
  assert.equal(row(a.upload_id), null, "not a second time as its own row");
  assert.equal(row(b.upload_id), null);
  const check = row("case_one");
  assert.equal(check.dataset.dtSource, "check");
  assert.ok(text(check).includes("2 files"));
  assert.ok(text(check).includes(a.file_name));
  assert.ok(check.querySelector("a").getAttribute("href").endsWith("#/data/check/case/case_one"));
  assert.ok(row(world.uploads[2].upload_id), "a file outside any check stays listed");
  assert.ok(row(ids.s3), "connections stay listed");
  world.cases = [];
  await session.loadMe();
});

test("prepared copies sit under the check that holds their source; unrelated files stay listed", async () => {
  const saved = world.uploads;
  const [combined, orders] = saved;
  const copy = (id, n) => ({ ...combined, upload_id: id, file_name: `${combined.file_name} (prepared)`, made_by: "recipe", source_upload_id: combined.upload_id, created_at: n });
  const copies = [copy("up_prep_a", combined.created_at), copy("up_prep_b", combined.created_at)];
  const stray = { ...orders, upload_id: "up_stray", file_name: "orders.csv", made_by: "file", source_upload_id: null };
  world.uploads = [...saved, ...copies, stray];
  await session.loadMe();
  world.cases = [
    { case_id: "case_combo", title: "combined", goal_id: null, state: "open", files: [combined.file_name], upload_ids: [combined.upload_id], updated_at: combined.created_at },
  ];
  await show("#/data");
  assert.equal(row(combined.upload_id), null, "the source is under its check");
  assert.equal(row("up_prep_a"), null, "so is each prepared copy");
  assert.equal(row("up_prep_b"), null);
  assert.ok(row("up_stray"), "a file no check holds stays listed");
  world.uploads = saved;
  world.cases = [];
  await session.loadMe();
});

test("older Data Doctor checks are folded: the newest 3 show, the rest open on one button", async () => {
  const [a] = world.uploads;
  await session.loadMe();
  world.cases = Array.from({ length: 5 }, (_, i) => ({
    case_id: `case_${i}`,
    title: `check ${i}`,
    goal_id: null,
    state: "open",
    files: [`f${i}.csv`],
    upload_ids: i === 0 ? [a.upload_id] : [],
    updated_at: a.created_at,
  }));
  await show("#/data");
  assert.equal($$("[data-dt-source='check']").length, 3, "the newest three checks");
  assert.deepEqual(
    $$("[data-dt-source='check']").map((el) => el.dataset.dtId),
    ["case_0", "case_1", "case_2"],
    "newest first, as the server sent them",
  );
  assert.equal(row(a.upload_id), null, "an upload stays under its check, not a row of its own");
  assert.equal(text($("[data-dt-show-older]")), "Show 2 older checks");
  $("[data-dt-show-older]").click();
  await settle();
  assert.equal($$("[data-dt-source='check']").length, 5);
  assert.equal($("[data-dt-show-older]"), null);
  world.cases = [];
  await session.loadMe();
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

test("the other kinds of source open from the connect card; each links to the existing form", async () => {
  await show("#/data");
  const chooser = $("#dt-chooser");
  assert.equal(chooser.hidden, true, "closed until asked for");
  $("[data-dt-open-chooser]").click();
  assert.equal(chooser.hidden, false);
  const links = Object.fromEntries($$("[data-dt-new]").map((a) => [a.dataset.dtNew, a.getAttribute("href")]));
  for (const kind of kinds.kinds.filter((k) => k.creatable && k.available)) {
    assert.equal(links[kind.kind], `#/connections/new/${kind.kind}`, `${kind.label} has its set-up form`);
  }
  assert.equal(links.file, undefined, "a file is chosen on the connect card, not here");
  assert.equal(links.ai_service, undefined, "the AI service is not a data source");
});

test("the tabs link to Sources and Data Doctor", async () => {
  await show("#/data");
  const tabs = $$("[data-dt-tab]");
  assert.deepEqual(tabs.map(text), ["Sources", "Data Doctor"]);
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
  assert.equal($("[data-dt-cut]"), null, "all of them are shown: nothing to add");
  world.uploads = saved;
  world.connections = fixture("connections").connections;
});

test("when there are more files than the list holds, the button and the page say 'newest', never 'all'", async () => {
  const saved = world.uploads;
  world.uploads = Array.from({ length: 30 }, (_, i) => ({ ...saved[0], upload_id: `u_${String(i).padStart(12, "0")}`, file_name: `file-${i}.csv` }));
  world.total = 120; // the API holds 120 files; the page asked for the newest and got 30
  await show("#/data");
  assert.equal(text($("[data-dt-show-all]")), "Show the newest 30 files");
  assert.ok(!/Show all/.test(text($("main.dt"))));
  $("[data-dt-show-all]").click();
  await settle();
  assert.equal($$("[data-dt-source='file']").length, 30);
  assert.equal(text($("[data-dt-cut]")), "Showing the newest 30 of 120 files.");
  assert.equal(text(row(ids.pg).querySelector("[data-dt-when]")), "No import in the newest 30 files", "an older import may exist");
  world.total = null;
  world.uploads = saved;
});

test("a connection test that answers after the person has left does not paint over the page they went to", async () => {
  await show("#/data");
  sent.tests.length = 0;
  let release;
  world.gate = new Promise((resolve) => (release = resolve));
  row(ids.pg).querySelector("[data-dt-test]").click();
  await until(() => sent.tests.length === 1, 2000, "the test call");
  w.history.replaceState(null, "", "/ui/#/doctor");
  app.innerHTML = '<main class="screen"><h1 class="h1">Data Doctor</h1></main>'; // what the router drew for #/doctor
  release();
  world.gate = null;
  await settle(10);
  assert.equal(text($("h1")), "Data Doctor", "Doctor is still on the screen");
  assert.equal($("main.dt"), null, "the Data page did not come back");
});

test("drawing the page asks only for what it shows", async () => {
  calls.length = 0;
  await show("#/data");
  assert.deepEqual(
    calls.map((c) => `${c.method} ${c.path}`).sort(),
    ["GET /connections", "GET /connections/kinds", "GET /data-doctor/cases", "GET /uploads"],
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
  $("[data-dt-open-chooser]").click();
  const found = findNeverShown(visibleText($("main.dt")));
  assert.deepEqual(found, {}, "an empty allow-list: this page shows none of the glossary's never-shown words");
  await show("#/data/check");
  assert.deepEqual(findNeverShown(visibleText($("main.dt"))), {});
});
