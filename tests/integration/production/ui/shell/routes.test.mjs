/* Plan M M174 (DEC-1604): one table of old and new addresses (`ui/layout.js` ROUTES), and the redirects it drives.

   Plan M §1 M5: every old hash keeps working - it redirects to its new home, or is kept and belongs to one of the
   five places. The table is checked against what the page really registers (every module's routes, read from the
   router after the whole page has loaded) plus the Phase 2 routes of the contract, so a route added later without
   a home fails here. The redirects are then followed on the whole page: only in the campaigns layout, only to a
   page this build has, replacing the history entry, and never bouncing. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { settle } from "../harness.mjs";
import { installShellPage, loadModules, me, untilStable, visit } from "./page.mjs";

const world = { me: me("me_off_classic"), approvals: null };
const { w, state } = installShellPage({ hash: "#/settings", world });
await loadModules();
await untilStable(state);
const session = await import("../../../../../ui/modules/production/session.js");
const layout = await import("../../../../../ui/layout.js");
const router = await import("../../../../../ui/modules/router.js");

/** Routes app.js draws itself: Home (`#/`), a use case and its pages, and the industry pages. */
const CORE = ["", "uc", "industry"];
/** Plan M Phase 2's routes (plan_m_phase2_contract.md), registered by M175-M179 on their branches. */
const PHASE_2 = ["overview", "campaigns", "reports", "data"];
const HOMES = new Set([...layout.PLACES.map((p) => p.id), layout.BELL, null]);

const registered = () => router.phaseModules().flatMap((m) => m.routes);

test("the page registers the routes this test expects to place (the whole page loaded)", () => {
  const routes = registered();
  for (const route of ["results", "settings", "connections", "pilot", "approvals", "monitoring", "doctor", "audit", "cost"]) {
    assert.ok(routes.includes(route), `${route} is registered`);
  }
});

test("every registered route, core route and Phase 2 route has a home in the table, at any depth", () => {
  const all = [...new Set([...CORE, ...registered(), ...PHASE_2])];
  const unplaced = [];
  for (const route of all) {
    for (const hash of route ? [`#/${route}`, `#/${route}/a`, `#/${route}/a/b/c`] : ["#/"]) {
      const hit = layout.routeEntry(hash);
      if (!hit) unplaced.push(hash);
      else assert.ok(HOMES.has(hit.row.home), `${hash}: unknown home ${hit.row.home}`);
    }
  }
  assert.deepEqual(unplaced, [], "add a row to ROUTES in ui/layout.js for each of these");
});

test("every row says why, has a home, and every redirect lands on a Phase 2 page", () => {
  for (const row of layout.ROUTES) {
    assert.ok(row.why && row.why.length > 8, `${row.from} says why`);
    assert.ok(HOMES.has(row.home), `${row.from}: home ${row.home}`);
    if (row.to) assert.ok(PHASE_2.includes(layout.partsOf(row.to)[0]), `${row.from} -> ${row.to}`);
  }
  const nowhere = layout.ROUTES.filter((row) => row.home === null).map((row) => row.from);
  assert.deepEqual(nowhere, ["signin/*", "account/*"], "only signing in and one's own account have no place");
});

test("the table says what Plan M §3 says (spec M174)", () => {
  const moved = {
    "#/": "#/overview",
    "#/industry/banking": "#/overview",
    "#/results": "#/campaigns",
    "#/connections": "#/data",
    "#/doctor": "#/data/doctor",
    "#/pilot": "#/reports",
    "#/audit": "#/reports/check",
    "#/campaigns/c_20261011_c9276210": "#/campaigns/c_20261011_c9276210/overview",
  };
  for (const [from, to] of Object.entries(moved)) assert.equal(layout.targetFor(from), to, from);
  const kept = {
    "#/approvals": "approvals",
    "#/connections/new/postgres": "data",
    "#/connections/conn_1": "data",
    "#/connections/ai/product": "settings",
    "#/generative/connection": "settings",
    "#/pilot/kit": "settings",
    "#/pilot/proof/c_20261011_c9276210": "reports",
    "#/pilot/view/results/hillstrom-email": "reports",
    "#/pilot/value/r_20261010_11100004": "reports",
    "#/monitoring/alerts": "settings",
    "#/monitoring/runs/r_20261010_11100004": "settings",
    "#/uplift": "settings",
    "#/uplift/win-back-campaign/model/r1": "settings",
    "#/cost": "settings",
    "#/privacy/consent": "settings",
    "#/admin": "settings",
    "#/admin/users": "settings",
    "#/settings": "settings",
    "#/uc/hillstrom-email": "campaigns",
    "#/uc/hillstrom-email/output/r_20261010_11100004": "campaigns",
    "#/campaign/hillstrom-email/r_20261010_11100004": "campaigns",
    "#/generative/assistant/onboarding-assistant": "campaigns",
    "#/campaigns": "campaigns",
    "#/campaigns/new": "campaigns",
    "#/campaigns/new/goal": "campaigns",
    "#/campaigns/c_1/results": "campaigns",
    "#/overview": "overview",
    "#/data/doctor": "data",
    "#/reports/check": "reports",
    "#/signin": null,
    "#/account": null,
  };
  for (const [hash, home] of Object.entries(kept)) {
    assert.equal(layout.targetFor(hash), null, `${hash} keeps its route`);
    assert.equal(layout.homeFor(hash), home, hash);
  }
});

test("in the classic layout nothing is redirected", async () => {
  assert.equal(layout.uiLayout(), "classic");
  for (const row of layout.ROUTES) {
    const hash = `#/${row.from.replace(/\/\*$/, "").replace(/:id/g, "x")}`;
    assert.equal(layout.layoutRedirect(hash), null, hash);
  }
  await visit(state, "#/connections");
  assert.equal(w.location.hash, "#/connections");
  await visit(state, "#/pilot");
  assert.equal(w.location.hash, "#/pilot");
});

test("in the campaigns layout an old address goes to its new home, once that page exists in this build", async () => {
  world.me = me("me_off_campaigns");
  await session.loadMe();
  await settle(4);
  assert.equal(layout.uiLayout(), "campaigns");
  // A Phase 2 page this build does not have yet is not a destination: the old page keeps drawing.
  if (!router.resolveRoute(["data"])) {
    await visit(state, "#/connections");
    assert.equal(w.location.hash, "#/connections", "no Data page in this build: Connections stays");
  }
  // The Phase 2 pages, as their branches will register them (unless they already have, after the merge).
  const drawn = [];
  for (const route of ["overview", "data", "reports"]) {
    if (router.resolveRoute([route])) continue;
    router.registerModule({
      name: `m174-test-${route}`,
      routes: [route],
      render: (app, parts) => {
        drawn.push(parts.join("/"));
        app.innerHTML = `<main class="screen" data-test-page="${parts.join("/")}"><h1 class="h1">${route}</h1></main>`;
      },
    });
  }
  const cases = [
    ["#/connections", "#/data"],
    ["#/doctor", "#/data/doctor"],
    ["#/pilot", "#/reports"],
    ["#/audit", "#/reports/check"],
    ["#/industry/banking", "#/overview"],
    ["#/", "#/overview"],
  ];
  for (const [from, to] of cases) {
    await visit(state, "#/settings");
    const before = w.history.length;
    await visit(state, from);
    assert.equal(w.location.hash, to, `${from} -> ${to}`);
    assert.equal(w.history.length, before + 1, `${from}: the redirect replaced the entry (Back skips the old address)`);
  }
  // kept addresses stay where they are
  for (const hash of ["#/connections/new/postgres", "#/approvals", "#/pilot/kit", "#/uc/hillstrom-email", "#/monitoring/alerts", "#/settings"]) {
    await visit(state, hash);
    assert.equal(w.location.hash, hash, `${hash} is kept`);
  }
});

test("a bare campaign address opens on its Overview tab", async () => {
  await visit(state, "#/campaigns/c_20261011_c9276210");
  assert.equal(w.location.hash, "#/campaigns/c_20261011_c9276210/overview");
});

test("a page that sends the browser straight back is drawn, not bounced between (#/results until M176)", async () => {
  await visit(state, "#/settings");
  const seen = [];
  const listener = () => seen.push(w.location.hash);
  w.addEventListener("hashchange", listener);
  await visit(state, "#/results");
  await settle(10);
  await untilStable(state);
  w.removeEventListener("hashchange", listener);
  assert.ok(["#/campaigns", "#/results"].includes(w.location.hash), w.location.hash);
  assert.ok(seen.length <= 4, `at most one round trip, saw ${seen.join(" ")}`);
  // drawn again on the same address (a module registering, a sign-in) it still does not bounce
  w.dispatchEvent(new w.Event("marketing-ai:modules-changed"));
  await settle(6);
  await untilStable(state);
  assert.ok(["#/campaigns", "#/results"].includes(w.location.hash), w.location.hash);
});

test("a redirect is decided by the table alone: the unit rules", () => {
  layout._resetLayoutForTests();
  assert.equal(layout.layoutRedirect("#/connections"), null, "classic after a reset");
  layout.setUiLayout("campaigns");
  assert.equal(layout.layoutRedirect("#/connections", { claimed: () => false }), null, "no page there: no redirect");
  assert.equal(layout.layoutRedirect("#/connections", { claimed: () => { throw new Error("x"); } }), null);
  assert.equal(layout.layoutRedirect("#/connections"), "#/data");
  assert.equal(layout.layoutRedirect("#/data"), null, "kept");
  assert.equal(layout.layoutRedirect("#/connections"), null, "sent straight back from #/data: drawn, not bounced");
  assert.equal(layout.layoutRedirect("#/settings"), null);
  assert.equal(layout.layoutRedirect("#/connections"), "#/data", "reached again from elsewhere: redirected again");
  assert.equal(layout.layoutRedirect("#/nothing-registers-this"), null, "an unknown address is left to app.js");
  assert.equal(layout.homeFor("#/nothing-registers-this"), null);
  layout.setUiLayout("anything else");
  assert.equal(layout.uiLayout(), "classic", "an unknown value is the classic layout");
});
