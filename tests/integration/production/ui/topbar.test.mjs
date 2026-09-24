/* The one top bar (ui/chrome.js, v1 docs/ui/FOUNDATION.md), in jsdom, with the role checks of the
   production session (`can`) over REAL `GET /auth/me` answers ($PB_FIXTURES): six goals for an Admin,
   no Admin for a Viewer, the wordmark and the user slot only when signed out, and the active goal
   following the route. The bar is driven through the store the router's seams forward to, so this
   file does not depend on which module registers the real slots. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle } from "./harness.mjs";

const { w } = installPage(async () => null, { hash: "#/" });

const chrome = await import("../../../../ui/chrome.js");
const dom = await import("../../../../ui/dom.js");
const { can } = await import("../../../../ui/modules/production/session.js");

const state = { me: fixture("me_admin"), status: "signed-in" };
chrome.setAccess({ can: (method, path) => can(method, path, state.me), status: () => state.status });
chrome.addNavSlot("admin", {
  html: () => (can("GET", "/users", state.me) ? `<a href="#/admin/users">Users</a>` : ""),
});
chrome.addNavSlot("user", {
  html: () =>
    state.status === "signed-out"
      ? `<a href="#/signin" data-user>Sign in</a>`
      : `<span data-user>${dom.esc(state.me.principal.display_name || state.me.principal.username)}</span>`,
});
dom.setHeaderTool({ name: "picker", html: () => `<div class="clientpick" data-client-picker>Client</div>` });

const bar = chrome.mountChrome(w.document);

const items = () => $$("#pb-bar nav[aria-label='Main'] > ul > li").map((li) => li.querySelector(".tn-item").textContent.trim());
const redraw = async () => {
  chrome.refreshChrome();
  await settle(2);
};

test("the bar is one header before #app, keeps the id pb-bar, and names its navigation Main", async () => {
  await settle(2);
  assert.equal(bar.id, "pb-bar");
  assert.equal(bar.tagName, "HEADER");
  assert.equal(bar.nextElementSibling, $("#app"));
  assert.ok($("#pb-bar nav[aria-label='Main']"));
  assert.ok($('#pb-bar a.wordmark[href="#/"]'));
});

test("an Admin sees the six goals, the client picker at the right, and the Users link under Admin", async () => {
  state.me = fixture("me_admin");
  state.status = "signed-in";
  await redraw();
  assert.deepEqual(items(), ["Home", "Build data", "Models", "Campaigns", "Reports", "Admin"]);
  assert.ok($("#pb-bar .tb-context [data-client-picker]"), "the picker sits in the bar's context slot");
  assert.ok($('#pb-bar a[href="#/admin/users"]'));
  assert.ok($('#pb-bar a[href="#/approvals"]'), "Waiting for approval, under Models");
});

test("a Viewer sees five goals: Admin is hidden, with no admin link anywhere in the bar", async () => {
  state.me = fixture("me_viewer");
  await redraw();
  assert.deepEqual(items(), ["Home", "Build data", "Models", "Campaigns", "Reports"]);
  assert.equal($('#pb-bar a[href="#/admin/users"]'), null);
});

test("signed out, the bar is the wordmark and Sign in: no goals, no client picker", async () => {
  state.status = "signed-out";
  await redraw();
  assert.equal($("#pb-bar nav[aria-label='Main']"), null);
  assert.equal($("#pb-bar [data-client-picker]"), null);
  assert.ok($('#pb-bar a[href="#/signin"]'));
  assert.ok($("#pb-bar .wordmark"));
  state.status = "signed-in";
  await redraw();
});

test("the active goal follows the route", async () => {
  const on = () => ($("#pb-bar .tn-item.on") || {}).textContent;
  const cases = [
    ["#/", "Home"],
    ["#/industry/banking", "Home"],
    ["#/uc/telco-churn", "Models"],
    ["#/uc/telco-churn/model/r1", "Models"],
    ["#/monitoring/runs", "Campaigns"],
    ["#/campaign/telco-churn/r2", "Campaigns"],
    ["#/pilot", "Reports"],
    ["#/pilot/kit", "Build data"],
    ["#/admin/audit", "Admin"],
  ];
  state.me = fixture("me_admin");
  for (const [hash, label] of cases) {
    w.location.hash = hash;
    await settle(3);
    assert.equal((on() || "").trim(), label, hash);
  }
  // A screen that knows more than its URL (a scoring run's Output) marks its own goal.
  w.location.hash = "#/uc/telco-churn/output/r3";
  await settle(3);
  chrome.setActiveNav("campaigns");
  await settle(2);
  assert.equal(on().trim(), "Campaigns");
  assert.equal($("#pb-bar .tn-item.on").getAttribute("aria-current"), "page");
});

test("a menu opens with aria-expanded and closes on Escape, focus back on its trigger", async () => {
  const trigger = $('#pb-bar [data-menu="tn-models"]');
  trigger.click();
  assert.equal(trigger.getAttribute("aria-expanded"), "true");
  assert.equal($("#tn-models").hidden, false);
  w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape" }));
  assert.equal($("#tn-models").hidden, true);
  assert.equal($('#pb-bar [data-menu="tn-models"]').getAttribute("aria-expanded"), "false");
  assert.equal(w.document.activeElement, $('#pb-bar [data-menu="tn-models"]'));
});

test("with no access provider every goal that has content is drawn (a deployment without sign-in)", async () => {
  chrome.setAccess(null);
  await redraw();
  assert.deepEqual(items(), ["Home", "Build data", "Models", "Campaigns", "Reports", "Admin"]);
});
