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

// Plan M M173 (DEC-1603): the Help menu is drawn again. DEC-1123 had hidden it (the Minfy logo took its place),
// which left the tour and "Send feedback" with no trigger. It is a "?" button beside the user menu, filled
// from the `help` slot; the logo stays the last thing in the bar.
test("with nothing in the help slot no Help menu is drawn", async () => {
  await redraw();
  assert.equal($('#pb-bar [data-menu="tn-help"]'), null, "no entries, no button");
  assert.equal($("#tn-help"), null);
});

test("the Help menu is a ? button top right, filled from the help slot in `order`, and the Minfy logo is still last", async () => {
  // Registered out of order on purpose: the entries are sorted by `order`, then by registration.
  chrome.addNavSlot("help", { html: () => `<button type="button" id="h-feedback">Send feedback</button>`, order: 30 });
  chrome.addNavSlot("help", { html: () => `<button type="button" id="h-tour">Take the tour</button>`, order: 10 });
  chrome.addNavSlot("help", { html: () => `<a href="#/help/whats-new" id="h-new">What's new</a>`, order: 40 });
  chrome.addNavSlot("help", { html: () => `<a href="#/help/glossary" id="h-gloss">Glossary</a>`, order: 20 });
  await redraw();
  const button = $('#pb-bar [data-menu="tn-help"]');
  assert.ok(button, "Help is drawn");
  assert.equal(button.getAttribute("aria-label"), "Help");
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.getAttribute("aria-controls"), "tn-help");
  assert.equal($("#tn-help").hidden, true, "closed until asked for");
  assert.deepEqual(
    $$("#tn-help a, #tn-help button").map((el) => el.textContent.trim()),
    ["Take the tour", "Glossary", "Send feedback", "What's new"],
  );
  assert.ok(button.closest(".tb-utils"), "it sits with the user menu, not among the four places");
  assert.equal($("#pb-bar nav[aria-label='Main'] [data-menu]"), null, "and the four places still have no menu");
  const logo = $("#pb-bar .tb-row > .tb-logo");
  assert.ok(logo, "the logo is a direct child of the bar's row");
  assert.equal(logo.querySelector("svg.logo").getAttribute("aria-label"), "Minfy");
  const row = $("#pb-bar .tb-row");
  const order = [...row.children].filter((el) => el.classList.contains("tb-logo") || el.classList.contains("tb-utils"));
  assert.equal(order[order.length - 1], logo, "the logo comes after the Help and user menus");
});

const key = (target, name) => {
  const event = new w.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
};

test("the Help menu works from the keyboard: Enter opens, arrows move, Escape closes and returns to the button", async () => {
  const button = $('#pb-bar [data-menu="tn-help"]');
  button.focus();
  button.click(); // what Enter or Space on a focused button does
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal($("#tn-help").hidden, false);
  assert.equal(key(button, "ArrowDown").defaultPrevented, true);
  assert.equal(w.document.activeElement.id, "h-tour", "Down from the button lands on the first entry");
  key(w.document.activeElement, "ArrowDown");
  assert.equal(w.document.activeElement.id, "h-gloss");
  key(w.document.activeElement, "ArrowDown");
  key(w.document.activeElement, "ArrowDown");
  assert.equal(w.document.activeElement.id, "h-new");
  key(w.document.activeElement, "ArrowDown");
  assert.equal(w.document.activeElement.id, "h-tour", "Down from the last wraps to the first");
  key(w.document.activeElement, "ArrowUp");
  assert.equal(w.document.activeElement.id, "h-new", "Up from the first wraps to the last");
  key(w.document.activeElement, "Home");
  assert.equal(w.document.activeElement.id, "h-tour");
  key(w.document.activeElement, "End");
  assert.equal(w.document.activeElement.id, "h-new");
  key(w.document.activeElement, "Escape");
  assert.equal($("#tn-help").hidden, true, "Escape closes it");
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(w.document.activeElement, button, "and focus goes back to the button");
});

test("Down on a closed Help button opens it and lands on the first entry", async () => {
  const button = $('#pb-bar [data-menu="tn-help"]');
  button.focus();
  key(button, "ArrowDown");
  assert.equal($("#tn-help").hidden, false);
  assert.equal(w.document.activeElement.id, "h-tour");
  key(w.document.activeElement, "Escape");
});

test("tabbing out of the open Help menu closes it", async () => {
  const button = $('#pb-bar [data-menu="tn-help"]');
  button.click();
  assert.equal($("#tn-help").hidden, false);
  $('#pb-bar a.wordmark').focus(); // focus moves outside the menu
  assert.equal($("#tn-help").hidden, true);
});

test("a redraw while someone is arrowing through the Help menu keeps their place", async () => {
  const button = $('#pb-bar [data-menu="tn-help"]');
  button.click();
  key(button, "ArrowDown");
  key(w.document.activeElement, "ArrowDown");
  assert.equal(w.document.activeElement.id, "h-gloss");
  await redraw(); // a module registering late redraws the whole row
  chrome.addNavSlot("badge:approvals", { html: () => "" });
  await redraw();
  assert.equal($("#tn-help").hidden, false, "still open");
  assert.equal(w.document.activeElement.id, "h-gloss", "and still on the same entry");
  key(w.document.activeElement, "Escape");
});

test("signed out, the bar draws no Help menu (the sign-in screen is all there is)", async () => {
  state.status = "signed-out";
  await redraw();
  assert.equal($('#pb-bar [data-menu="tn-help"]'), null);
  state.status = "signed-in";
  await redraw();
  assert.ok($('#pb-bar [data-menu="tn-help"]'));
});

test("with no access provider the four places are drawn (a deployment without sign-in)", async () => {
  chrome.setAccess(null);
  await redraw();
  assert.deepEqual(items(), ["Home", "Connections", "Results", "Settings"]);
});
