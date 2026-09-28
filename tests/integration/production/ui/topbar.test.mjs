/* The one top bar (ui/chrome.js, v1 docs/ui/FOUNDATION.md), in jsdom, with the role checks of the
   production session (`can`) over REAL `GET /auth/me` answers ($PB_FIXTURES): since Plan H M82 the
   same four places for every role, the approvals count on Results, the wordmark and the user slot
   only when signed out, and the active place following the route. The bar is driven through the store the router's seams forward to, so this
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

// Plan H M82 (DEC-1113): the bar is exactly four places - Home, Connections, Results, Settings - for
// every role. Admin, Model health, Uplift and Waiting for approval are no longer in it: Settings and
// Results offer them (tests/integration/production/ui/simple/). The admin slot is kept, not drawn.
test("an Admin sees the four places and no admin link in the bar; the client picker's slot stays", async () => {
  state.me = fixture("me_admin");
  state.status = "signed-in";
  await redraw();
  assert.deepEqual(items(), ["Home", "Connections", "Results", "Settings"]);
  assert.deepEqual(
    $$("#pb-bar nav[aria-label='Main'] a.tn-item").map((a) => a.getAttribute("href")),
    ["#/", "#/connections", "#/results", "#/settings"],
  );
  assert.ok($("#pb-bar .tb-context [data-client-picker]"), "a registered header tool is still drawn in its slot");
  assert.equal($('#pb-bar a[href="#/admin/users"]'), null, "Users is on the Settings page now");
  assert.equal($('#pb-bar a[href="#/approvals"]'), null, "approvals are a badge on Results");
  assert.equal($("#pb-bar [data-menu]"), null, "no menus among the four places");
});

test("a Viewer sees the same four places", async () => {
  state.me = fixture("me_viewer");
  await redraw();
  assert.deepEqual(items(), ["Home", "Connections", "Results", "Settings"]);
  assert.equal($('#pb-bar a[href="#/admin/users"]'), null);
});

test("models waiting for approval are a count on Results", async () => {
  let waiting = "2";
  chrome.addNavSlot("badge:approvals", { html: () => waiting });
  await redraw();
  const results = $('#pb-bar a.tn-item[href="#/results"]');
  assert.equal(results.querySelector(".count").textContent, "2");
  waiting = "";
  await redraw();
  assert.equal($('#pb-bar a.tn-item[href="#/results"] .count'), null, "no count when none wait");
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

test("the active place follows the route", async () => {
  const on = () => ($("#pb-bar .tn-item.on") || {}).textContent;
  const cases = [
    ["#/", "Home"],
    ["#/industry/banking", "Home"],
    ["#/uc/telco-churn", "Home"],
    ["#/uc/telco-churn/model/r1", "Home"],
    ["#/connections", "Connections"],
    ["#/generative/connection", "Connections"],
    ["#/results", "Results"],
    ["#/monitoring/runs", "Results"],
    ["#/campaign/telco-churn/r2", "Results"],
    ["#/pilot", "Results"],
    ["#/approvals", "Results"],
    ["#/settings", "Settings"],
    ["#/pilot/kit", "Settings"],
    ["#/uplift", "Settings"],
    ["#/monitoring/alerts", "Settings"],
    ["#/privacy/consent", "Settings"],
    ["#/admin/audit", "Settings"],
  ];
  state.me = fixture("me_admin");
  for (const [hash, label] of cases) {
    w.location.hash = hash;
    await settle(3);
    assert.equal((on() || "").trim().replace(/\s*\d+$/, ""), label, hash);
  }
  // A screen that knows more than its URL (a scoring run's Output) marks its own place; the goal names
  // screens used before Plan H ("campaigns") land on the place that holds them now.
  w.location.hash = "#/uc/telco-churn/output/r3";
  await settle(3);
  chrome.setActiveNav("campaigns");
  await settle(2);
  assert.equal(on().trim(), "Results");
  assert.equal($("#pb-bar .tn-item.on").getAttribute("aria-current"), "page");
});

// docs/UI_AUDIT.md §8.4 item 2: a run's screens are use-case routes (Home), but a run opened from
// Results is a Result - the bar used to jump to Home on the click.
test("a run opened from Results keeps Results marked, on all its pages and after a reload", async () => {
  const on = () => (($("#pb-bar .tn-item.on") || {}).textContent || "").trim().replace(/\s*\d+$/, "");
  const go = async (hash) => {
    w.location.hash = hash;
    await settle(3);
    return on();
  };
  assert.equal(await go("#/results"), "Results");
  assert.equal(await go("#/uc/telco-churn/run/r7"), "Results", "the row's link");
  assert.equal(await go("#/uc/telco-churn/model/r7"), "Results", "the run's Model page");
  assert.equal(await go("#/uc/telco-churn/output/r7"), "Results", "the run's Output page");
  assert.equal(await go("#/generative/rca/telco-churn/r7"), "Results", "the run's root-cause notes");
  assert.deepEqual(JSON.parse(w.sessionStorage.getItem("marketing-ai:run-opened-from")), ["telco-churn/r7"], "kept for a reload");
  // the use case itself is Home, and the same run opened from there is Home again
  assert.equal(await go("#/uc/telco-churn"), "Home");
  assert.equal(await go("#/uc/telco-churn/run/r7"), "Home", "opened from its use case");
  assert.deepEqual(JSON.parse(w.sessionStorage.getItem("marketing-ai:run-opened-from")), []);
  // from the campaign page and Waiting for approval (both under Results) too; a run opened from
  // Settings is not a Result
  await go("#/campaign/telco-churn/r8");
  assert.equal(await go("#/uc/telco-churn/output/r8"), "Results", "from the campaign page");
  await go("#/approvals");
  assert.equal(await go("#/uc/telco-churn/run/r9"), "Results", "from Waiting for approval");
  await go("#/monitoring/schedules");
  assert.equal(await go("#/uc/telco-churn/run/r10"), "Home", "from Settings");
  assert.equal(chrome.placeFor("#/uc/telco-churn/data/r9"), "results");
  assert.equal(chrome.navFor("#/uc/telco-churn/data/r9"), "home", "navFor itself still reads the route alone");
});

test("a menu (Help) opens with aria-expanded and closes on Escape, focus back on its trigger", async () => {
  chrome.addNavSlot("help", { html: () => `<button type="button">Take the tour</button>` });
  await redraw();
  const trigger = $('#pb-bar [data-menu="tn-help"]');
  trigger.click();
  assert.equal(trigger.getAttribute("aria-expanded"), "true");
  assert.equal($("#tn-help").hidden, false);
  w.document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape" }));
  assert.equal($("#tn-help").hidden, true);
  assert.equal($('#pb-bar [data-menu="tn-help"]').getAttribute("aria-expanded"), "false");
  assert.equal(w.document.activeElement, $('#pb-bar [data-menu="tn-help"]'));
});

test("with no access provider the four places are drawn (a deployment without sign-in)", async () => {
  chrome.setAccess(null);
  await redraw();
  assert.deepEqual(items(), ["Home", "Connections", "Results", "Settings"]);
});
