/* The uplift module's wiring (ui/modules/uplift/index.js), in node with a stubbed `window`,
   `document` and `fetch`: it registers its two routes with ui/modules/router.js, and a route
   render fetches exactly the contract's endpoints and paints what they returned, and the entry
   links it adds under a Phase 1 screen's header appear where they should and only once. No
   browser and no npm install: a separate file, so the globals stubbed here never leak into the
   view tests. */
import { test } from "node:test";
import assert from "node:assert/strict";

const calls = [];
const replies = new Map();

function reply(path, status, body) {
  replies.set(path, { status, body });
}

globalThis.window = {
  location: { hash: "#/", origin: "http://api.test" },
  addEventListener() {},
  scrollTo() {},
};
/** Just enough of an element for the entry-link code: children, attributes, `after`, `isConnected`. */
function element(tag) {
  return {
    tag,
    className: "",
    innerHTML: "",
    href: "",
    children: [],
    dataset: {},
    isConnected: true,
    setAttribute() {},
    appendChild(child) {
      this.children.push(child);
    },
  };
}
const documentApp = { main: null, querySelector: (sel) => (sel === "main.screen" ? documentApp.main : null) };
let observed = null;
globalThis.MutationObserver = class {
  constructor(callback) {
    observed = callback;
  }
  observe() {}
};
globalThis.document = {
  title: "",
  getElementById: (id) => (id === "app" ? documentApp : null),
  createElement: element,
  head: { appendChild() {} },
};
globalThis.fetch = async (url, options = {}) => {
  const path = String(url).replace("http://api.test", "");
  calls.push([options.method || "GET", path, options.body || null]);
  const found = replies.get(path) || { status: 404, body: { detail: { code: "NOT_FOUND", message: "no" } } };
  return {
    ok: found.status < 400,
    status: found.status,
    text: async () => (found.body === null ? "" : JSON.stringify(found.body)),
  };
};

const UI = new URL("../../../ui/", import.meta.url);
const router = await import(new URL("modules/router.js", UI));
await import(new URL("modules/uplift/index.js", UI));

const fakeApp = () => ({ innerHTML: "", querySelector: () => null, querySelectorAll: () => [] });

const industries = {
  industries: [
    {
      legend: [{ ai_type: "predictive", label: "Predictive" }],
      stages: [{ name: "Retention", use_cases: [{ id: "win-back", name: "Win-back" }] }],
    },
  ],
};
reply("/industries", 200, industries);
reply("/use-cases/win-back", 200, {
  id: "win-back",
  name: "Win-back",
  marker: "P",
  stars: "★",
  ai_type: "predictive",
  lifecycle_stage: "Retention",
});
reply("/runs/r1", 200, {
  run: { run_id: "r1", mode: "score", state: "done", created_at: "2026-09-01T00:00:00Z", file_name: "f.csv" },
  status: { stages: [] },
});
reply("/runs?use_case=win-back", 200, { runs: [] });

test("the module claims the uplift and campaign routes", () => {
  const mine = router.phaseModules().find((m) => m.name === "uplift");
  assert.ok(mine, "uplift is registered");
  assert.deepEqual(mine.routes, ["uplift", "campaign"]);
  assert.equal(router.resolveRoute(["campaign", "x", "y"]).name, "uplift");
  assert.equal(router.resolveRoute(["uc", "x"]), null, "Phase 1 routes stay with app.js");
});

test("#/uplift lists every use case from GET /industries", async () => {
  const app = fakeApp();
  window.location.hash = "#/uplift";
  await router.resolveRoute(["uplift"]).render(app, ["uplift"]);
  assert.match(app.innerHTML, /href="#\/uplift\/win-back"/);
});

test("the output page reads the run and its uplift artefacts, and 404s become em dashes", async () => {
  calls.length = 0;
  const app = fakeApp();
  const parts = ["uplift", "win-back", "output", "r1"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  const paths = calls.map(([, path]) => path);
  for (const name of ["uplift_validation.json", "segments.json", "policy_recommendation.json"]) {
    assert.ok(paths.includes(`/runs/r1/uplift/${name}`), name);
  }
  assert.match(app.innerHTML, /has not produced segments\.json yet/);
  assert.match(app.innerHTML, /href="http:\/\/api\.test\/runs\/r1\/scores\.csv"/);
  assert.ok(!/undefined|NaN/.test(app.innerHTML));
});

test("the campaign page asks GET /runs/{id}/campaign-results and renders the stored report", async () => {
  reply("/runs/r1/campaign-results", 200, {
    run_id: "r1",
    outcome_column: "y",
    outcome_window_days: 30,
    as_of: "2026-09-02T00:00:00Z",
    status: "immature",
    results_available_on: "2026-10-01",
    treated_rows: 0,
    treated_conversions: 0,
    treated_rate: null,
    control_rows: 0,
    control_conversions: 0,
    control_rate: null,
    absolute_lift: null,
    relative_lift: null,
    incremental_conversions: null,
    p_value: null,
    rows_immature: 10,
    rows_without_outcome: 0,
    rows_suppressed_or_untreated: 0,
    causal: true,
    summary: "",
    computed_at: "2026-09-02T00:00:00Z",
  });
  const app = fakeApp();
  const parts = ["campaign", "win-back", "r1"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  assert.match(app.innerHTML, /Results available on 01 Oct 2026/);
});

test("an unknown uplift URL renders an error screen, not a blank page", async () => {
  const app = fakeApp();
  const parts = ["uplift", "win-back", "nonsense", "r1"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  assert.match(app.innerHTML, /This screen could not be loaded/);
});

/** A Phase 1 screen as `app.js` paints it: a header rule the entry links go under. */
function phase1Screen() {
  const main = element("main");
  main.entry = null;
  const rule = { after: (node) => (main.entry = node) };
  main.querySelector = (sel) => (sel === ".rule" ? rule : sel === ".uentry" ? main.entry : null);
  return main;
}

test("a Phase 1 scoring run gets the uplift and campaign-results entry links, added by the module", async () => {
  window.location.hash = "#/uc/win-back/run/r1";
  documentApp.main = phase1Screen();
  observed();
  const nav = documentApp.main.entry;
  assert.ok(nav, "an entry nav was inserted under the header");
  assert.match(nav.innerHTML, /href="#\/uplift\/win-back"/);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(
    nav.children.map((a) => a.href),
    ["#/campaign/win-back/r1"],
  );
  observed();
  assert.equal(documentApp.main.entry, nav, "a second mutation does not insert a second nav");
});

test("the Overview gets one link to the uplift index; other modules' screens get none", () => {
  window.location.hash = "#/";
  documentApp.main = phase1Screen();
  observed();
  assert.match(documentApp.main.entry.innerHTML, /href="#\/uplift"/);
  window.location.hash = "#/generative/connection";
  documentApp.main = phase1Screen();
  observed();
  assert.equal(documentApp.main.entry, null);
});
