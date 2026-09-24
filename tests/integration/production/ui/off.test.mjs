/* auth_mode=off: the UI works exactly as before Phase 4b - no header on any call, no control gated,
   no sign-in - and the bar says plainly that access control is off (DEC-702). */
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

test("the overview renders and no call carries a token", async () => {
  await until(() => $(".stage-pill"), 3000, "the overview");
  await settle();
  assert.ok(calls.length >= 2);
  assert.ok(calls.every((c) => c.auth === null));
  assert.equal(w.location.hash, "#/");
});

test("the bar says access control is off, and offers the admin screens the local operator may open", async () => {
  await until(() => /Access control is off/.test($("#pb-bar").textContent), 2000, "the bar");
  assert.ok($('#pb-bar a[href="#/admin/users"]'));
  assert.equal($("#pb-signout"), null);
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
