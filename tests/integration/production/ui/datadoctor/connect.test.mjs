/* "Connect your data" (Plan O, v1): with the Data Doctor on, the Data page starts with ONE action. Choosing files there
   (several at once) goes straight into the Data Doctor, which reads them together as one new check; the other kinds of
   source stay behind a quiet button. The fake API is the one of the other Data Doctor suites (`common.mjs`). */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the Data page's Connect your data suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  const { $, $$, installPage, settle, until } = await import("../harness.mjs");
  const { CASE, fx, makeWorld, serverFor } = await import("./common.mjs");

  const world = makeWorld({ at: "question" });
  world.cases = { cases: [] };
  const fake = serverFor(world);
  const kinds = { kinds: [{ kind: "postgres", label: "PostgreSQL", creatable: true, available: true }] };
  const failing = { second: false, sent: [] };
  installPage(
    (request) => {
      if (request.method === "POST" && request.path === "/uploads") {
        failing.sent.push(request.path);
        if (failing.second && failing.sent.length === 2) {
          failing.second = false;
          return { status: 422, body: { detail: { code: "UPLOAD_BAD", message: "That file has no rows." } } };
        }
        world.uploads += 1;
        return { status: 201, body: { upload_id: `u_${world.uploads}`, profile: null } };
      }
      if (request.method === "GET" && request.path === "/connections/kinds") return { status: 200, body: kinds };
      if (request.method === "GET" && request.path === "/connections") return { status: 200, body: { connections: [] } };
      if (request.method === "GET" && request.path === "/uploads") return { status: 200, body: { uploads: [], total: 0 } };
      return fake(request);
    },
    { hash: "#/data" },
  );
  await import("../../../../../ui/app.js");
  await import("../../../../../ui/modules/production/index.js");
  await import("../../../../../ui/modules/agent/index.js");
  await import("../../../../../ui/modules/decide/index.js");
  await import("../../../../../ui/modules/data/index.js");
  void fx;

  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

  test("the Data page starts with one primary action, Connect your data, and the other sources are quiet", async () => {
    await until(() => $("[data-dt-connect]"), 4000, "the Connect your data card");
    assert.equal($$(".btn.primary").length, 1, "one primary action on the page");
    assert.equal(text($(".btn.primary")), "Connect your data");
    const input = $("#dt-files");
    assert.ok(input.multiple, "several files at once");
    assert.ok(input.getAttribute("accept").includes(".csv"));
    assert.equal($("#dt-chooser").hidden, true, "the other kinds of source are closed");
    assert.ok(!$('[data-dt-kind="file"]'), "no second way to choose a file");
    $("[data-dt-open-chooser]").click();
    assert.equal($("#dt-chooser").hidden, false, "the quiet button opens them");
    assert.ok($('[data-dt-new="postgres"]'), "the other source types stay reachable");
  });

  test("two files chosen go straight into one new check, with no further click", async () => {
    const input = $("#dt-files");
    const files = [new File(["a,b\n1,2\n"], "customers.csv"), new File(["a,b\n1,2\n"], "orders.csv")];
    Object.defineProperty(input, "files", { value: files });
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
    await until(() => window.location.hash === "#/data/check", 3000, "the Data Doctor tab");
    await until(() => world.posted.some((p) => p.path === "/cases"), 4000, "the check opened");
    assert.equal(world.uploads, 2, "both files were sent");
    const made = world.posted.find((p) => p.path === "/cases");
    assert.equal(made.body.upload_ids.length, 2, "read together, as one check");
    assert.ok(made.body.title.includes("customers.csv"));
    await settle(4);
    await until(() => $("[data-dt-doctor-screen] [data-dds-place='data']"), 3000, "the Doctor screen");
    assert.ok(CASE);
  });

  test("when the second of two files fails, it is named, and a retry does not send the first again", async () => {
    window.location.hash = "#/data";
    await until(() => $("#dt-files"), 3000, "the Data page");
    failing.sent.length = 0;
    failing.second = true;
    const made = world.posted.filter((p) => p.path === "/cases").length;
    const mk = () => [new File(["a,b\n1,2\n"], "good.csv"), new File(["a,b\n1,2\n"], "broken.csv")];
    const files = mk();
    const input = $("#dt-files");
    Object.defineProperty(input, "files", { value: files });
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
    await until(() => /broken\.csv could not be read/.test(text($("#app"))), 4000, "the failed file named");
    assert.match(text($("#app")), /1 file before it went through and will not be sent again/);
    assert.equal(world.posted.filter((p) => p.path === "/cases").length, made, "no check opened with half the files");
    assert.equal(failing.sent.length, 2);
    // The person chooses both files again.
    const retry = $("#dds-file");
    Object.defineProperty(retry, "files", { value: files });
    retry.dispatchEvent(new window.Event("change", { bubbles: true }));
    await until(() => world.posted.filter((p) => p.path === "/cases").length > made, 4000, "the check opened");
    assert.equal(failing.sent.length, 3, "only the failed file went up again");
    const body = world.posted.filter((p) => p.path === "/cases").at(-1).body;
    assert.equal(body.upload_ids.length, 2, "both files are in the one check");
  });
}
