/* A module that registers late (a run action, a results list) must not make a page a module has already
   drawn fetch everything again: the audit screen below sends `GET /audit/events` again only when
   something that really changes it (not a registration) asks for a redraw. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $$, fixture, installPage, settle, signedInServer, until } from "./harness.mjs";

const tokens = { "tok-admin": fixture("me_admin") };
const server = signedInServer({
  tokens,
  routes: { "GET /audit/events": () => ({ status: 200, body: fixture("audit_page") }) },
});
const { w, calls } = installPage(server, { hash: "#/admin/audit" });
w.sessionStorage.setItem("marketing-ai.auth", JSON.stringify({ token: "tok-admin", expires_at: null }));

await import("../../../../ui/app.js");
await import("../../../../ui/modules/production/index.js");
const router = await import("../../../../ui/modules/router.js");

const audits = () => calls.filter((c) => c.path === "/audit/events").length;

test("a late registration does not fetch a page a module has drawn again", async () => {
  await until(() => $$("[data-pb-screen] tbody tr").length === 2, 3000, "the audit screen");
  await settle(10);
  const opened = audits();
  assert.ok(opened >= 1, "opening the page sent its request");

  router.registerRunAction({ name: "redraw-test", applies: () => false, html: () => "" });
  router.registerResultLink({ name: "redraw-test", applies: () => false, link: () => "" });
  await settle(10);
  assert.equal(audits(), opened, "registrations alone did not draw the page again");

  router.announceModulesChanged();
  await settle(10);
  await until(() => audits() === opened + 1, 3000, "one redraw for a real change");
  await settle(10);
  assert.equal(audits(), opened + 1, "and only one");
});

test("many registrations in a task are one announcement", async () => {
  let heard = 0;
  const listener = () => (heard += 1);
  w.addEventListener(router.MODULES_CHANGED, listener);
  router.registerRunAction({ name: "redraw-test-2", applies: () => false, html: () => "" });
  router.registerRunAction({ name: "redraw-test-3", applies: () => false, html: () => "" });
  router.announceModulesChanged();
  await settle(10);
  w.removeEventListener(router.MODULES_CHANGED, listener);
  assert.equal(heard, 1);
});

test("a change announced while the page was loading does not draw the page again", async () => {
  // `app.js` draws the first screen on `load`, after every change made while loading: the router's
  // announcement of those changes (`beforeLoad`) is for the top bar, not for another draw.
  await settle(10);
  const before = audits();
  w.dispatchEvent(new w.CustomEvent(router.MODULES_CHANGED, { detail: { registration: false, beforeLoad: true } }));
  await settle(10);
  assert.equal(audits(), before);
});
