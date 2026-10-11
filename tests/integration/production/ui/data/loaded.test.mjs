/* Plan M, M179: the Data page is in the running app only if `ui/index.html` loads it. This test imports exactly the
   scripts the real index page lists (never the module by hand), then opens #/data the way the router does.
   (PB_FIXTURES, written by tests/integration/data_page/test_data_ui.py.) */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage } from "../harness.mjs";
import { pageScripts } from "../simple/fake.mjs";

const kinds = fixture("kinds");
const server = ({ method, path }) => {
  if (method !== "GET") return null;
  if (path === "/connections/kinds") return { status: 200, body: kinds };
  if (path === "/connections") return { status: 200, body: fixture("connections") };
  if (path === "/uploads") return { status: 200, body: fixture("uploads") };
  return null;
};

installPage(server, { hash: "#/data" });
const router = await import("../../../../../ui/modules/router.js");
const scripts = pageScripts().filter((s) => s.startsWith("modules/data/"));
for (const script of scripts) await import(`../../../../../ui/${script}`);

test("ui/index.html loads the Data module", () => {
  assert.deepEqual(scripts, ["modules/data/index.js"]);
});

test("#/data and #/data/check are routes of the loaded page, not the not-found screen", async () => {
  for (const [hash, h1] of [["#/data", "Data"], ["#/data/check", "Data"]]) {
    const parts = hash.replace(/^#\//, "").split("/");
    const module = router.resolveRoute(parts);
    assert.ok(module, `${hash} has an owner`);
    await module.render($("#app"), parts);
    assert.equal($$("h1").length, 1);
    assert.equal($("h1").textContent, h1);
  }
});
