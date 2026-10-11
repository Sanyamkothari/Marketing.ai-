/* Plan M M174 (DEC-1604): the campaigns layout's shell, on the whole page.

   `GET /auth/me` says `ui_layout: "campaigns"` (real answers, per role, from tests/integration/test_shell_ui.py);
   everything else is the recorded Hillstrom demo. What a person gets: a left sidebar with the five places of Plan M
   §3 for every role, the route's place marked; top right the approvals bell (its count for an Approver only, from
   the same source and role rule as the classic bar's badge), Help (?) and the profile; the wordmark and the logo;
   below 900 px a Menu button that opens the sidebar. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { installShellPage, loadModules, me, untilStable, visit } from "./page.mjs";

const world = { me: me("me_off_campaigns"), approvals: null };
const { w, state } = installShellPage({ hash: "#/settings", world });
await loadModules();
const session = await import("../../../../../ui/modules/production/session.js");
const layout = await import("../../../../../ui/layout.js");
await until(() => $("#pb-bar .sh-side"), 5000, "the sidebar");
await untilStable(state);

const PLACES = ["Overview", "Campaigns", "Data", "Reports", "Settings"];
const places = () => $$("#pb-bar nav.sh-side[aria-label='Main'] a.sh-item").map((a) => a.textContent.trim());
const marked = () => $$("#pb-bar .sh-side a[aria-current='page']").map((a) => a.textContent.trim());

async function signInAs(name, approvals = null) {
  world.me = me(name);
  world.approvals = approvals;
  await session.loadMe();
  await settle(4);
  await untilStable(state);
}

test("GET /auth/me's ui_layout turns the shell on: the page says so, and the classic places are gone", () => {
  assert.equal(layout.uiLayout(), "campaigns");
  assert.equal(layout.isCampaignLayout(), true);
  assert.equal(document.documentElement.getAttribute("data-ui-layout"), "campaigns");
  assert.ok($("#sh-styles"), "the shell's styles are in the page");
  const bar = $("#pb-bar");
  assert.equal(bar.tagName, "HEADER");
  assert.equal(bar.nextElementSibling, $("#app"), "still one header before #app");
  assert.ok(bar.classList.contains("sh-on") && bar.classList.contains("sh-side-on"));
  assert.equal($("#pb-bar .topnav"), null, "the four classic places are not drawn");
  assert.equal($$("#pb-bar nav[aria-label='Main']").length, 1, "one main navigation");
});

test("the sidebar is the five places, in order, each a link to its page", () => {
  assert.deepEqual(places(), PLACES);
  assert.deepEqual(
    $$("#pb-bar .sh-side a.sh-item").map((a) => a.getAttribute("href")),
    ["#/overview", "#/campaigns", "#/data", "#/reports", "#/settings"],
  );
  assert.ok($('#pb-bar a.wordmark[href="#/overview"]'), "the wordmark stays, and opens the Overview itself");
  assert.equal($("#pb-bar a.tb-logo").getAttribute("href"), "#/overview", "so does the logo (not '#/', which redirects)");
  assert.equal($("#pb-bar .tb-logo svg.logo").getAttribute("aria-label"), "Minfy", "the Minfy logo stays");
});

test("every role sees the same five places (sign-in on), and so does sign-in off", async () => {
  for (const name of ["me_viewer", "me_analyst", "me_approver", "me_admin", "me_off_campaigns"]) {
    await signInAs(name);
    assert.equal(session.sessionStatus(), name === "me_off_campaigns" ? "off" : "signed-in", name);
    assert.deepEqual(places(), PLACES, name);
  }
});

test("the bell: an Approver sees how many models wait for them; a Viewer sees the bell with no count", async () => {
  const items = ["m1", "m2", "m3"].map((id) => ({ model_id: id, can_decide: true }));
  await signInAs("me_approver", { items, separation_enforced: false });
  await until(() => $("#pb-bar .sh-bell .count"), 3000, "the count on the bell");
  const bell = $("#pb-bar a.sh-bell");
  assert.equal(bell.getAttribute("href"), "#/approvals");
  assert.equal(bell.querySelector(".count").textContent, "3");
  assert.equal(bell.getAttribute("aria-label"), "Waiting for approval: 3");
  // a model this person may not decide is not counted, as on the classic badge
  await signInAs("me_admin", { items, separation_enforced: false });
  await signInAs("me_approver", { items: [...items, { model_id: "m4", can_decide: false }], separation_enforced: false });
  await until(() => ($("#pb-bar .sh-bell .count") || {}).textContent === "3", 3000, "three, not four");
  await signInAs("me_viewer", { items, separation_enforced: false });
  assert.ok($("#pb-bar a.sh-bell"), "the bell is there for everyone");
  assert.equal($("#pb-bar a.sh-bell .count"), null, "a Viewer may not decide, so no count is read for them");
  assert.equal($("#pb-bar a.sh-bell").getAttribute("aria-label"), "Waiting for approval");
  await signInAs("me_off_campaigns");
});

test("the place marked follows the route; the approvals page marks the bell", async () => {
  const cases = [
    ["#/settings", "Settings"],
    ["#/monitoring/alerts", "Settings"],
    ["#/privacy/consent", "Settings"],
    ["#/cost", "Settings"],
    ["#/uplift", "Settings"],
    ["#/pilot/kit", "Settings"],
    ["#/connections/new/postgres", "Data"],
    ["#/uc/hillstrom-email", "Campaigns"],
    ["#/campaign/hillstrom-email/r_20261010_11100004", "Campaigns"],
    ["#/pilot/proof/c_20261011_c9276210", "Reports"],
  ];
  for (const [hash, place] of cases) {
    await visit(state, hash);
    assert.equal(w.location.hash, hash, `${hash} is kept`);
    assert.deepEqual(marked(), [place], hash);
  }
  await visit(state, "#/approvals");
  assert.deepEqual(marked(), [], "no sidebar place for the approvals page");
  assert.equal($("#pb-bar a.sh-bell").getAttribute("aria-current"), "page", "the bell is marked instead");
  assert.ok($("#pb-bar a.sh-bell").classList.contains("on"));
  await visit(state, "#/settings");
  assert.equal($("#pb-bar a.sh-bell").getAttribute("aria-current"), null);
});

test("a screen naming its classic place (setActiveNav) does not move the sidebar's mark", async () => {
  const router = await import("../../../../../ui/modules/router.js");
  await visit(state, "#/uc/hillstrom-email/output/r_20261010_11100004");
  router.setActiveNav("results");
  await settle(3);
  assert.deepEqual(marked(), ["Campaigns"]);
});

test("the keyboard path: wordmark, Menu, the five places, the bell, Help, the logo; nothing taken out of the tab order", async () => {
  await visit(state, "#/settings");
  const focusable = $$("#pb-bar a[href], #pb-bar button").filter((el) => !el.closest("[hidden]"));
  const names = focusable.map(
    (el) => el.getAttribute("aria-label") || el.textContent.trim() || el.getAttribute("href"),
  );
  assert.deepEqual(names, [
    "Marketing AI",
    "Menu",
    ...PLACES,
    "Waiting for approval",
    "Help",
    "Minfy, home",
  ]);
  assert.equal(focusable.filter((el) => el.getAttribute("tabindex") === "-1").length, 0);
  // the place on screen says so to a screen reader, and the focus ring is drawn for keyboard focus only
  assert.deepEqual(marked(), ["Settings"]);
  assert.match($("#sh-styles").textContent, /\.sh-item:focus-visible[^{]*\{outline:2px solid var\(--brand-blue\)/);
  // a place is a link: focus it and Enter follows it, as any link does
  const reports = $('#pb-bar .sh-side a[href="#/reports"]');
  reports.focus();
  assert.equal(document.activeElement, reports);
});

test("Help (?) opens the help entries as a menu, and Escape closes it and returns focus", async () => {
  await visit(state, "#/settings");
  const button = $('#pb-bar button[data-menu="sh-help"]');
  assert.ok(button, "Help is drawn");
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal($("#sh-help").hidden, true);
  button.click();
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal($("#sh-help").hidden, false);
  assert.ok($("#sh-help #pe-fb-btn"), "Send feedback is one of its entries");
  assert.match($("#sh-help").textContent, /Send feedback/);
  document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle(2);
  assert.equal($('#pb-bar button[data-menu="sh-help"]').getAttribute("aria-expanded"), "false");
  assert.equal($("#sh-help").hidden, true);
  assert.equal(document.activeElement, $('#pb-bar button[data-menu="sh-help"]'), "focus back on Help");
});

test("below 900 px the Menu button opens the sidebar; choosing a place or Escape folds it again", async () => {
  await visit(state, "#/settings");
  const bar = $("#pb-bar");
  const menu = () => $("#pb-bar [data-tb-menu]");
  assert.equal(menu().getAttribute("aria-controls"), "sh-side");
  assert.ok($("#sh-side"), "the sidebar it controls");
  assert.equal(menu().getAttribute("aria-expanded"), "false");
  menu().click();
  assert.ok(bar.classList.contains("open"));
  assert.equal(menu().getAttribute("aria-expanded"), "true");
  // the place already on screen: no route change, the panel still folds
  $('#pb-bar .sh-side a[href="#/settings"]').click();
  await settle(2);
  assert.equal(bar.classList.contains("open"), false, "choosing a place folds the sidebar");
  assert.equal(menu().getAttribute("aria-expanded"), "false", "and the Menu button says so");
  menu().click();
  assert.ok(bar.classList.contains("open"));
  document.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await settle(2);
  assert.equal(bar.classList.contains("open"), false);
  assert.equal(document.activeElement, menu(), "focus back on Menu");
  menu().click();
  await visit(state, "#/monitoring/alerts");
  assert.equal(bar.classList.contains("open"), false, "a route change folds it");
  assert.equal(menu().getAttribute("aria-expanded"), "false");
  // the folding itself is CSS: the sidebar hides and the button shows below 900 px
  const css = $("#sh-styles").textContent;
  assert.match(css, /@media \(max-width:900px\)\{[^@]*\.sh-menu-btn\{display:inline-flex\}[^@]*\.sh-side\{display:none\}[^@]*\.topbar\.open \.sh-side\{display:block/);
});

test("signed out: the wordmark, Sign in and the logo; no places, no bell, no Help", async () => {
  await signInAs("me_viewer");
  world.me = null;
  session.forgetSignIn();
  await settle(4);
  assert.equal($("#pb-bar .sh-side"), null);
  assert.equal($("#pb-bar .sh-bell"), null);
  assert.equal($('#pb-bar [data-menu="sh-help"]'), null);
  assert.equal($("#pb-bar [data-tb-menu]"), null);
  assert.ok($("#pb-bar .wordmark"));
  assert.ok($("#pb-bar .tb-logo"));
  assert.ok($("#pb-bar").textContent.includes("Sign in"), "the user slot offers Sign in");
  assert.equal($("#pb-bar").classList.contains("sh-side-on"), false, "#app takes the full width");
  assert.equal(layout.uiLayout(), "campaigns", "a signed-out answer keeps the layout that was known");
  await signInAs("me_off_campaigns");
  await visit(state, "#/settings");
  assert.deepEqual(places(), PLACES);
});

test("back to classic: today's bar, and nothing of the shell left on the page", async () => {
  await signInAs("me_off_classic");
  assert.equal(layout.uiLayout(), "classic");
  assert.equal(document.documentElement.hasAttribute("data-ui-layout"), false);
  assert.equal($("#pb-bar .sh-side"), null);
  assert.equal($("#pb-bar").className, "topbar");
  assert.deepEqual(
    $$("#pb-bar nav[aria-label='Main'] > ul > li").map((li) => li.querySelector(".tn-item").textContent.trim()),
    ["Home", "Connections", "Results", "Settings"],
  );
});
