/* The uplift module's wiring (ui/modules/uplift/index.js), in node with a stubbed `window`,
   `document` and `fetch`: it registers its two routes with ui/modules/router.js, and a route
   render fetches exactly the contract's endpoints and paints what they returned, and the two run
   actions it offers other screens (the use case's "Also: target with uplift" link, a scoring run's
   "Campaign results" block) apply where they should. No browser and no npm install: a separate
   file, so the globals stubbed here never leak into the view tests. */
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
  // `registerRunAction` announces a change of modules (`router.js` MODULES_CHANGED).
  dispatchEvent() {},
  scrollTo() {},
  // `router.js` loads Phase 4b's `boot.js` for its side effects, which wraps `window.fetch` and asks
  // `GET /auth/me` through it. A 404 there means "no sign-in on this deployment" (DEC-790); it is a
  // separate stub from the recording `fetch` below, so the calls this file asserts are unchanged.
  fetch: async () => ({ ok: false, status: 404, json: async () => ({}), text: async () => "" }),
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
    addEventListener() {},
    appendChild(child) {
      this.children.push(child);
    },
  };
}
const documentApp = {
  main: null,
  querySelector: (sel) => (sel === "main.screen" ? documentApp.main : null),
  // What Phase 4b's `boot.js` touches on the app element: the user bar is inserted before it, and the
  // role gates query it for controls (it has none here).
  querySelectorAll: () => [],
  parentNode: { insertBefore() {} },
};
let observed = null;
globalThis.MutationObserver = class {
  constructor(callback) {
    observed = callback;
  }
  observe() {}
  disconnect() {}
};
window.MutationObserver = globalThis.MutationObserver;
globalThis.Event = globalThis.Event || class {
  constructor(type) {
    this.type = type;
  }
};
globalThis.document = {
  title: "",
  addEventListener() {},
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
  assert.match(app.innerHTML, /Available after training\./);
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

test("an unknown uplift URL renders a calm not-found card with one way back, not a blank page", async () => {
  const app = fakeApp();
  const parts = ["uplift", "win-back", "nonsense", "r1"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  assert.match(app.innerHTML, /data-not-found/);
  assert.match(app.innerHTML, /This page could not be found/);
  assert.match(app.innerHTML, /href="#\/uplift\/win-back">Back to uplift Setup/);
  assert.ok(!app.innerHTML.includes("‹&nbsp; Uplift modelling"), "no hard-coded back link");
});

test("a campaign that is not there says so and leads back to all campaigns", async () => {
  const app = fakeApp();
  const parts = ["campaign", "win-back", "r_missing"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  assert.match(app.innerHTML, /This campaign could not be found/);
  assert.match(app.innerHTML, /href="#\/monitoring\/runs">Back to all campaigns/);
});

test("a server fault renders a plain sentence with Try again, the code under Details", async () => {
  reply("/runs/r500", 500, { detail: { code: "INTERNAL", message: "boom" } });
  const app = fakeApp();
  const parts = ["uplift", "win-back", "model", "r500"];
  window.location.hash = `#/${parts.join("/")}`;
  await router.resolveRoute(parts).render(app, parts);
  assert.match(app.innerHTML, /This screen could not be loaded/);
  assert.match(app.innerHTML, /data-retry/);
  assert.match(app.innerHTML, /<details class="tech"><summary>Details<\/summary>/);
});

test("the module paints no entry pill on other screens: the links are run actions instead", () => {
  window.location.hash = "#/uc/win-back/run/r1";
  const main = { dataset: {}, querySelector: () => null };
  documentApp.main = main;
  observed();
  assert.equal(documentApp.main, main);
  assert.ok(!("entry" in main), "nothing is inserted under a Phase 1 header");
});

test("a use case's header gets the related link 'Also: target with uplift', never for AI-written text", () => {
  const winBack = { id: "win-back", name: "Win-back", ai_type: "predictive", status: "available" };
  assert.equal(
    router.runActionsHtml(winBack, null),
    '<a class="related" href="#/uplift/win-back">Also: target with uplift ›</a>',
  );
  assert.equal(router.runActionsHtml({ id: "assistant", ai_type: "generative" }, null), "");
  assert.equal(router.runActionsHtml({ id: "soon", ai_type: "predictive", status: "planned" }, null), "");
});

test("a finished scoring run gets the Campaign results flow block; a training run does not", () => {
  const winBack = { id: "win-back", name: "Win-back", ai_type: "predictive" };
  const scoring = { run_id: "r1", mode: "score", state: "done" };
  const html = router.runActionsHtml(winBack, scoring);
  assert.match(html, /<a class="block" href="#\/campaign\/win-back\/r1" data-action="campaign-results">/);
  assert.match(html, /<span>Campaign results<\/span><span class="bstate waiting">After the campaign<\/span>/);
  assert.ok(!html.includes("Also: target with uplift"), "the related link is for the use case header only");
  assert.equal(router.runActionsHtml(winBack, { ...scoring, mode: "train" }), "");
  assert.equal(router.runActionsHtml(winBack, { ...scoring, state: "running" }), "");
});

/** The 409 `POST /uplift/runs` really sends (api/routes/uplift.py `_validation_conflict`): the two
    reports at the top level, beside an error envelope that is always an object. */
const REFUSED = {
  detail: {
    code: "UPLIFT_VALIDATION_FAILED",
    message: "This file cannot be used as an experiment yet: TREATMENT_NOT_RANDOM.",
    path: null,
  },
  validation: {
    schema_version: 1,
    upload_id: "u9",
    passed: true,
    error_count: 0,
    warning_count: 1,
    checks: [
      {
        code: "LOW_POSITIVE_RATE",
        severity: "warning",
        message: "Only a few customers converted.",
        suggestion: "Check the outcome column.",
        details: {},
        acknowledged: false,
      },
    ],
  },
  uplift_validation: {
    schema_version: 1,
    upload_id: "u9",
    treatment_column: "treatment",
    passed: false,
    causal: false,
    randomness_auc: 0.8821,
    checks: [
      {
        code: "TREATMENT_NOT_RANDOM",
        severity: "error",
        message: "Who was treated can be predicted from the customer data.",
        suggestion: "Use a campaign whose treatment was assigned at random.",
        details: { auc: 0.8821 },
        acknowledgeable: true,
        acknowledged: false,
      },
    ],
  },
};

test("a real 409 from POST /uplift/runs renders both reports and the acknowledge control", async () => {
  const controller = await import(new URL("modules/uplift/controller.js", UI));
  const { ApiError } = await import(new URL("api.js", UI));
  const { upliftScreenHtml } = await import(new URL("modules/uplift/views.js", UI));

  // The parser, on the exact body shape.
  const parsed = controller.refusal(new ApiError(409, "UPLIFT_VALIDATION_FAILED", "no", REFUSED));
  assert.deepEqual(parsed, { validation: REFUSED.validation, upliftValidation: REFUSED.uplift_validation });
  // The nested fallback still works, and anything that is not a 409 with a report is not a refusal.
  const nested = { detail: { validation: REFUSED.validation, uplift_validation: REFUSED.uplift_validation } };
  assert.deepEqual(controller.refusal(new ApiError(409, "X", "no", nested)), parsed);
  assert.equal(controller.refusal(new ApiError(409, "X", "no", { detail: REFUSED.detail })), null);
  assert.equal(controller.refusal(new ApiError(422, "X", "no", REFUSED)), null);

  // End to end through the Setup controller: submit, get the 409, render what the state holds.
  reply("/uplift/runs", 409, REFUSED);
  const uc = {
    id: "win-back",
    name: "Win-back",
    entity: "customer",
    marker: "P",
    stars: "★",
    ai_type: "predictive",
    lifecycle_stage: "Retention",
  };
  let renders = 0;
  const setup = controller.createSetupController(uc, () => (renders += 1));
  Object.assign(setup.state, {
    mode: "train",
    upload: {
      upload_id: "u9",
      profile: {
        file_name: "c.csv",
        row_count: 10,
        column_count: 4,
        file_size_bytes: 100,
        columns: ["customer_id", "treatment", "converted", "visits"].map((name) => ({ name })),
      },
    },
    pk: "customer_id",
    target: "converted",
    treatment: "treatment",
  });
  const listeners = {};
  const form = { addEventListener: (event, fn) => (listeners[event] = fn) };
  setup.bind({ querySelector: (sel) => (sel === "#u-setup" ? form : null), querySelectorAll: () => [] });
  calls.length = 0;
  listeners.submit({ preventDefault() {} });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(
    calls.map(([method, path]) => [method, path]),
    [["POST", "/uplift/runs"]],
  );
  const s = setup.state;
  assert.equal(s.submitError, null, "a refusal is not a generic error");
  assert.deepEqual(s.upliftValidation, REFUSED.uplift_validation);
  assert.deepEqual(s.validation, REFUSED.validation);
  assert.ok(renders > 0);
  const html = upliftScreenHtml(uc, s);
  assert.match(html, /data-uack="TREATMENT_NOT_RANDOM"/);
  assert.match(html, /Who was treated can be predicted from the customer data/);
  assert.match(html, /Only a few customers converted/);
  // The randomness measure is merged into the refusal and red, with the limit the check used.
  assert.match(html, /<span class="pill bad" data-code="RANDOMNESS">Not random<\/span>/);
  assert.match(html, /Randomness measured 0\.882 \(0\.5 = random\)/);
  assert.ok(!/undefined|NaN/.test(html));
});
