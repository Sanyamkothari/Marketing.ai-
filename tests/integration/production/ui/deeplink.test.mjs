/* A page loaded directly on #/admin/audit with a sign-in already held for the tab (a reload): the
   production module registers after app.js's first render has already drawn the overview, so
   index.js must route again - and must win even when the overview's slower paint lands afterwards. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $$, fixture, installPage, settle, signedInServer, until } from "./harness.mjs";

const tokens = { "tok-admin": fixture("me_admin") };
let releaseIndustries;
const industriesHeld = new Promise((resolve) => {
  releaseIndustries = resolve;
});
const server = signedInServer({
  tokens,
  routes: {
    // The overview answers only after the audit screen has painted: the slow paint lands on top.
    "GET /industries": async () => {
      await industriesHeld;
      return { status: 200, body: fixture("industries") };
    },
    "GET /audit/events": () => ({ status: 200, body: fixture("audit_page") }),
  },
});
const { w, calls } = installPage(server, { hash: "#/admin/audit" });
w.sessionStorage.setItem("marketing-ai.auth", JSON.stringify({ token: "tok-admin", expires_at: null }));

await import("../../../../ui/app.js");
await import("../../../../ui/modules/production/index.js");

test("a reload on an admin route ends on that route, even if the overview paints last", async () => {
  await until(() => $$("[data-pb-screen] tbody tr").length === 2, 3000, "the audit screen");
  releaseIndustries();
  await settle(10);
  await until(() => $$("[data-pb-screen] tbody tr").length === 2, 3000, "the audit screen again");
  assert.equal(w.location.hash, "#/admin/audit");
  assert.ok(calls.every((c) => c.auth === "Bearer tok-admin"), "every call, the first included, carried the token");
});
