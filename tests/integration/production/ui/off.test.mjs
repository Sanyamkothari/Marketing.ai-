/* auth_mode=off: the UI works exactly as before Phase 4b - no header on any call, no control gated,
   no sign-in. Plan H M81/M82 (DEC-1112, DEC-1113): the product is a single-user tool, so the bar is
   the four places and says nothing about sign-in; Settings says it is off (DEC-702) and offers no
   Admin group while it is. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, until } from "./harness.mjs";

const server = (request) => {
  if (request.path === "/auth/me") return { status: 200, body: fixture("me_off") };
  if (request.path === "/industries") return { status: 200, body: fixture("industries") };
  if (request.path === "/connection/aws") return { status: 200, body: fixture("connection") };
  if (request.path === "/auth/login") return { status: 409, body: { detail: { code: "AUTH_OFF", message: "Sign-in is off." } } };
  return null;
};
const { w, calls } = installPage(server, { hash: "#/" });

await import("../../../../ui/app.js");
await import("../../../../ui/modules/generative/index.js");
await import("../../../../ui/modules/production/index.js");
await import("../../../../ui/modules/simple/index.js");

test("the overview renders and no call carries a token", async () => {
  await until(() => $(".stage-pill"), 3000, "the overview");
  await settle();
  assert.ok(calls.length >= 2);
  assert.ok(calls.every((c) => c.auth === null));
  assert.equal(w.location.hash, "#/");
});

test("the bar is the four places with no sign-in chip; Settings says sign-in is off and hides Admin", async () => {
  await until(() => $$("#pb-bar nav[aria-label='Main'] .tn-item").length === 4, 2000, "the bar");
  await settle(2);
  assert.doesNotMatch($("#pb-bar").textContent, /Sign-in off|Access control is off/);
  assert.equal($('#pb-bar a[href="#/admin/users"]'), null);
  assert.equal($("#pb-signout"), null);
  w.location.hash = "#/settings";
  await until(() => $("[data-signin-off]"), 3000, "the Settings page's About");
  assert.match($("[data-signin-off]").textContent, /Sign-in is off/);
  assert.equal($("[data-settings-admin]"), null, "no Admin group while sign-in is off");
  assert.equal($('#app a[href="#/admin/users"]'), null);
  assert.ok($('#app a[href="#/privacy/consent"]'), "Privacy is still offered");
  assert.ok($('#app a[href="#/generative/connection"]'), "and the AI service");
});

test("the settings screen keeps every control the screen drew", async () => {
  w.location.hash = "#/generative/connection";
  await until(() => $("#c-test"), 3000, "the connection screen");
  await settle(2);
  assert.equal($("#c-test").disabled, false);
  assert.equal($$("[data-pb-gate]").length, 0);
  assert.equal($$(".pb-why").length, 0);
});

test("the sign-in route says there is nothing to sign in to", async () => {
  w.location.hash = "#/signin";
  await until(() => $("[data-pb-screen]"), 2000, "the sign-in screen");
  assert.match($("#app").textContent, /Sign-in is turned off on this installation/);
  assert.equal($("#pb-signin"), null);
});
