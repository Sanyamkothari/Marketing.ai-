// Marketing AI - UI screenshot script (Part 3 "before" / "after" shots; docs/UI_AUDIT.md).
//
//   node scripts/ui_screens.mjs                      # every screen into docs/ui/after/
//   ONLY=home,run-output SKIP_DARK=1 node scripts/ui_screens.mjs
//
// Re-runnable: point it at three servers and an output dir, and it writes
//   OUT/<screen>--<state>--desktop.png        1440x1000, full page
//   OUT/<screen>--<state>--mobile.png         390x844,  full page
//   OUT/<screen>--<state>--desktop-dark.png   for every "full" / "full-*" state
//
// Env:
//   BASE_DEMO   auth-off server on the seeded demo data dir      (default http://localhost:8791)
//   BASE_EMPTY  auth-off server on an empty data dir, no demo    (default http://localhost:8792)
//   BASE_AUTH   sign-in-on server; users demo-<role>, password demo-<role>-2026 (default http://localhost:8766)
//   OUT         output dir                                       (default docs/ui/after in this checkout)
//   ONLY        optional comma list of screen ids (or id--state) to shoot
//   SKIP_MOBILE=1 / SKIP_DARK=1   optional, to shoot faster
//   INVENTORY=1 print the screen inventory as JSON and exit (no browser)
//   FIXTURES    dir holding telco_history.csv and targeted_campaign.csv (default docs/ui/fixtures)
//   PLAYWRIGHT  module to import chromium from (default "playwright"; ES modules ignore NODE_PATH, so
//               either link a node_modules above the checkout or pass e.g.
//               PLAYWRIGHT=/opt/node22/lib/node_modules/playwright/index.mjs)
//
// States:
//   full     the seeded demo (BASE_DEMO) or a signed-in user (BASE_AUTH)
//   empty    BASE_EMPTY: fresh data dir, no demo, no runs, no clients
//   loading  the named API calls are held back for 25s with page.route; shot after 1.5s
//   error    a missing id in the URL, or an API 500 injected with page.route
//   other    in-page states reached by clicking (named in the state key)
//
// The only writes this script makes to a server: on BASE_DEMO, if no schedule exists, it creates one
// monthly drift-check schedule for the demo client and fires it once (so Monitoring has content).
// Uploads (a prepared CSV, an uplift campaign CSV) go to BASE_DEMO too. Nothing is written to
// BASE_AUTH except sign-ins (one per role); the "wrong password" state is a mocked 401, so no
// failed sign-in is ever counted against the lock-out.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT || "playwright");
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const strip = (u) => u.replace(/\/+$/, "");
const BASE = {
  demo: strip(process.env.BASE_DEMO || "http://localhost:8791"),
  empty: strip(process.env.BASE_EMPTY || "http://localhost:8792"),
  auth: strip(process.env.BASE_AUTH || "http://localhost:8766"),
};
const OUT = process.env.OUT || path.join(ROOT, "docs", "ui", "after");
const FIX = process.env.FIXTURES || path.join(ROOT, "docs", "ui", "fixtures");
const ONLY = (process.env.ONLY || "").split(",").map((s) => s.trim()).filter(Boolean);
const SKIP_MOBILE = process.env.SKIP_MOBILE === "1";
const SKIP_DARK = process.env.SKIP_DARK === "1";

const TOUR_KEY = "marketing-ai:pilot-tour-seen";
const CLIENT_KEY = "marketing-ai.client";
const AUTH_KEY = "marketing-ai.auth";
const SETTLE_MS = 700;
const VIEWPORTS = { desktop: { width: 1440, height: 1000 }, mobile: { width: 390, height: 844 } };

// --- helpers used by screen actions ------------------------------------------------------------

const settle = async (page, ms = SETTLE_MS) => {
  await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(ms);
};
const click = async (page, selector) => {
  await page.locator(selector).first().click({ timeout: 8000 });
  await settle(page);
};
const openDetails = async (page, selector) => {
  // <details> toggled via its summary; works at any width
  await page.locator(`${selector} > summary`).first().click({ timeout: 8000 });
  await settle(page, 400);
};
const upload = async (page, selector, file) => {
  await page.locator(selector).first().setInputFiles(path.join(FIX, file));
  await settle(page, 1500);
};
const trySelect = async (page, selector, value) => {
  const el = page.locator(selector).first();
  if (!(await el.count())) return;
  const current = await el.inputValue().catch(() => "");
  if (current) return;
  await el.selectOption(value).catch(() => {});
  await settle(page, 400);
};

/** GET /runs/<id> rewritten so the run looks in flight (or failed): the Running / failed views. */
const runAs = (runId, state) => ({
  match: new RegExp(`^/runs/${runId}$`),
  mutate: (body) => {
    const b = JSON.parse(body);
    b.run.state = state;
    if (b.status) {
      b.status.state = state;
      const n = (b.status.stages || []).length;
      (b.status.stages || []).forEach((st, i) => {
        if (state === "running") {
          st.state = i < Math.floor(n * 0.45) ? "done" : i === Math.floor(n * 0.45) ? "running" : "pending";
          if (st.state === "pending") st.detail = "";
        } else if (state === "failed") {
          st.state = i < Math.floor(n * 0.6) ? "done" : i === Math.floor(n * 0.6) ? "failed" : "pending";
          if (st.state === "failed")
            st.error = { code: "TRAINING_FAILED", message: "Every candidate model failed to train (UI audit: injected)." };
        }
      });
    }
    if (state === "failed") b.run.error = { code: "TRAINING_FAILED", message: "Every candidate model failed to train (UI audit: injected)." };
    return JSON.stringify(b);
  },
});

// --- the inventory -------------------------------------------------------------------------------
//
// Each screen: id, group, title, roles, files (ui sources), route(m) -> hash, states {name: spec}.
// A state spec: { base: demo|empty|auth, route?(m), as?: role, delay?: RegExp, fail?: RegExp,
//                 mock?: [{match, mutate}|{match,status,body}], act?(page, m), tour?: bool,
//                 client?: id, note }
// `m` is the demo manifest plus a few ids looked up at start (scheduleId).

const P1 = ["ui/app.js", "ui/usecase.js", "ui/settings.js", "ui/dom.js", "ui/index.html"];
const PAGES = ["ui/app.js", "ui/pages.js", "ui/dom.js"];
const PROD = (f) => ["ui/modules/production/index.js", `ui/modules/production/${f}`, "ui/modules/production/userbar.js"];
const LIST_DELAY = /^\/(industries|use-cases\/[^/]+)$/;
const ALL = "Any role (Viewer can read; Analyst runs)";

export const SCREENS = [
  // ---------------- chrome / home ----------------
  {
    id: "home", group: "chrome-home", title: "Overview (industry journey, use-case cards)",
    roles: "Any role", files: ["ui/overview.js", "ui/app.js", "ui/dom.js", "ui/modules/pilot/index.js", "ui/modules/uplift/index.js", "ui/modules/onboarding/clients.js", "ui/modules/production/userbar.js"],
    route: () => "#/",
    states: {
      full: { base: "demo" },
      empty: { base: "empty", note: "fresh data dir: no demo badge, same catalogue" },
      loading: { base: "demo", delay: /^\/industries$/ },
      error: { base: "demo", fail: /^\/industries$/ },
    },
  },
  {
    id: "home-industry", group: "chrome-home", title: "Overview for another industry (Banking)",
    roles: "Any role", files: ["ui/overview.js", "ui/app.js"],
    route: () => "#/industry/banking",
    states: { full: { base: "demo" } },
  },
  {
    id: "tour", group: "chrome-home", title: "Guided tour overlay (first visit in demo mode)",
    roles: "Any role (demo mode only)", files: ["ui/modules/pilot/tour.js", "ui/modules/pilot/styles.js"],
    route: () => "#/",
    states: {
      full: { base: "demo", tour: true, note: "step 1 of 6, opens by itself" },
      "full-step4": {
        base: "demo", tour: true, note: "step 4 of 6 (Output) after three Next clicks",
        act: async (page) => {
          for (let i = 0; i < 3; i += 1) await click(page, '[data-pe-tour="next"]');
        },
      },
    },
  },
  {
    id: "help-popover", group: "chrome-home", title: '"What does this mean?" help popover',
    roles: "Any role", files: ["ui/modules/pilot/help.js", "configs/pilot/help.yaml"],
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: {
        base: "demo", note: "Setup > Advanced settings, first ? opened",
        act: async (page) => {
          await openDetails(page, "#f-adv");
          if (!(await page.locator(".pe-q:visible").count())) {
            for (const d of await page.locator("#f-adv .stage-d > summary").all()) await d.click().catch(() => {});
            await settle(page, 400);
          }
          await page.locator(".pe-q:visible").first().click({ timeout: 8000 });
          await page.waitForTimeout(500);
        },
      },
    },
  },
  {
    id: "feedback", group: "chrome-home", title: "Feedback dialog (button on every screen)",
    roles: "Any role", files: ["ui/modules/pilot/feedback.js"],
    route: () => "#/",
    states: { full: { base: "demo", act: async (page) => click(page, "#pe-fb-btn") } },
  },
  {
    id: "ai-notice", group: "chrome-home", title: "AI service not connected notice (generative use case)",
    roles: "Any role", files: ["ui/availability.js", "ui/app.js", "ui/modules/generative/index.js"],
    route: () => "#/uc/ai-onboarding-assistant",
    states: { full: { base: "demo" } },
  },
  {
    id: "usecase-planned", group: "chrome-home", title: "A planned (not yet available) use case opened by URL",
    roles: "Any role", files: ["ui/app.js", "ui/dom.js"],
    route: () => "#/uc/criteo-uplift",
    states: { error: { base: "demo" } },
  },

  // ---------------- setup / onboarding ----------------
  {
    id: "usecase-setup-train", group: "setup-onboarding", title: "Use case Setup, Train mode (prepared file)",
    roles: ALL, files: P1,
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: { base: "demo" },
      empty: { base: "empty", note: "no runs, no clients" },
      loading: { base: "demo", delay: LIST_DELAY },
      error: { base: "demo", fail: /^\/use-cases\/[^/]+$/ },
    },
  },
  {
    id: "usecase-setup-file", group: "setup-onboarding", title: "Setup after a prepared file is uploaded (preview, columns, problem type)",
    roles: "Analyst", files: P1,
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: {
        base: "demo", note: "telco_history.csv uploaded; target auto-filled",
        act: async (page) => {
          await upload(page, "#f-file", "telco_history.csv");
          await trySelect(page, "#f-target", "Churn");
        },
      },
    },
  },
  {
    id: "usecase-setup-advanced", group: "setup-onboarding", title: "Setup > Advanced settings expanded",
    roles: "Analyst", files: [...P1, "ui/settings.js"],
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: {
        base: "demo",
        act: async (page) => {
          await openDetails(page, "#f-adv");
          for (const d of await page.locator("#f-adv .stage-d > summary").all()) await d.click().catch(() => {});
          await settle(page, 400);
        },
      },
    },
  },
  {
    id: "usecase-setup-score", group: "setup-onboarding", title: "Use case Setup, Score new data mode",
    roles: "Analyst", files: P1,
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: { base: "demo", act: async (page) => click(page, '.seg button[data-mode="score"]') },
      empty: { base: "empty", note: "no trained model yet", act: async (page) => click(page, '.seg button[data-mode="score"]') },
    },
  },
  {
    id: "client-new", group: "setup-onboarding", title: 'Header client picker: "+ New client" form',
    roles: "Analyst", files: ["ui/modules/onboarding/clients.js", "ui/dom.js"],
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: { base: "demo", act: async (page) => { await page.selectOption("#f-client", "__new__"); await settle(page); } },
      empty: { base: "empty", note: "no client exists yet" },
    },
  },
  ...[
    ["sources", 1, "Sources (upload raw tables, confirm roles)"],
    ["mapping", 2, "Mapping (source columns to our columns)"],
    ["features", 3, "Features & label"],
    ["build", 4, "Build & review (build report, future-data check)"],
  ].map(([step, n, label]) => ({
    id: `build-raw-${n}-${step}`, group: "setup-onboarding", title: `Build from raw tables, step ${n}: ${label}`,
    roles: "Analyst", files: ["ui/modules/onboarding/setup.js", "ui/modules/onboarding/panel.js", "ui/modules/onboarding/steps.js", "ui/usecase.js"],
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: {
        base: "demo", client: "c_demo_telecom_1", note: "client Demo Telecom",
        act: async (page) => {
          await click(page, '.pickcard[data-source]:not([data-source="file"])');
          const d = page.locator(`.stage-d[data-step="${step}"]`).first();
          if (!(await d.evaluate((el) => el.open).catch(() => false))) {
            await d.locator(":scope > summary").click({ timeout: 8000 });
            await settle(page, 500);
          }
        },
      },
      ...(n === 1
        ? {
            empty: {
              base: "empty", note: "no client yet",
              act: async (page) => click(page, '.pickcard[data-source]:not([data-source="file"])'),
            },
          }
        : {}),
    },
  })),
  {
    id: "build-raw-score", group: "setup-onboarding", title: "Score mode: Upload this month's tables (recipe replay)",
    roles: "Analyst", files: ["ui/modules/onboarding/setup.js", "ui/modules/onboarding/panel.js", "ui/usecase.js"],
    route: (m) => `#/uc/${m.use_case_id}`,
    states: {
      full: {
        base: "demo", client: "c_demo_telecom_1",
        act: async (page) => {
          await click(page, '.seg button[data-mode="score"]');
          await click(page, '.pickcard[data-source]:not([data-source="file"])');
        },
      },
    },
  },

  // ---------------- runs, pages, uplift ----------------
  {
    id: "usecase-running", group: "run-pages-uplift", title: "Use case: run in progress (Running…, Cancel)",
    roles: ALL, files: ["ui/usecase.js"],
    route: (m) => `#/uc/${m.use_case_id}/run/${m.train_run_id}`,
    states: { full: { base: "demo", mock: [], note: "GET /runs/<id> rewritten to state=running", mockFn: (m) => [runAs(m.train_run_id, "running")] } },
  },
  {
    id: "usecase-results", group: "run-pages-uplift", title: "Use case: training run results (Data > Model > Output blocks, previous runs)",
    roles: ALL, files: ["ui/usecase.js", "ui/modules/uplift/index.js"],
    route: (m) => `#/uc/${m.use_case_id}/run/${m.train_run_id}`,
    states: {
      full: { base: "demo" },
      loading: { base: "demo", delay: /^\/runs\/[^/]+$/ },
      error: { base: "demo", route: (m) => `#/uc/${m.use_case_id}/run/r_missing` },
      "error-failed-run": { base: "demo", note: "run rewritten to state=failed", mockFn: (m) => [runAs(m.train_run_id, "failed")] },
    },
  },
  {
    id: "usecase-results-score", group: "run-pages-uplift", title: "Use case: scoring run results",
    roles: ALL, files: ["ui/usecase.js", "ui/modules/uplift/index.js"],
    route: (m) => `#/uc/${m.use_case_id}/run/${m.score_run_id}`,
    states: { full: { base: "demo" } },
  },
  ...[
    ["data", "train_run_id", "Data page (quality, lineage, pipeline)"],
    ["model", "train_run_id", "Model page (leaderboard, metrics, importance)"],
    ["output", "score_run_id", "Output page of a scoring run (bands, actions, reasons, drift)"],
  ].map(([kind, runKey, label]) => ({
    id: `run-${kind}`, group: "run-pages-uplift", title: label,
    roles: ALL, files: [...PAGES, ...(kind === "model" ? ["ui/modules/uplift/charts.js"] : [])],
    route: (m) => `#/uc/${m.use_case_id}/${kind}/${m[runKey]}`,
    states: {
      full: { base: "demo" },
      empty: { base: "empty", route: (m) => `#/uc/${m.use_case_id}/${kind}`, note: "no finished run yet" },
      loading: { base: "demo", delay: /^\/runs\/[^/]+(\/artefacts\/.*)?$/ },
      error: { base: "demo", route: (m) => `#/uc/${m.use_case_id}/${kind}/r_missing` },
    },
  })),
  {
    id: "run-output-train", group: "run-pages-uplift", title: "Output page of a training run",
    roles: ALL, files: PAGES,
    route: (m) => `#/uc/${m.use_case_id}/output/${m.train_run_id}`,
    states: { full: { base: "demo" } },
  },
  {
    id: "uplift-index", group: "run-pages-uplift", title: "Uplift modelling: list of use cases",
    roles: ALL, files: ["ui/modules/uplift/index.js", "ui/modules/uplift/views.js"],
    route: () => "#/uplift",
    states: {
      full: { base: "demo" },
      loading: { base: "demo", delay: /^\/industries$/ },
      error: { base: "demo", fail: /^\/industries$/ },
    },
  },
  {
    id: "uplift-setup", group: "run-pages-uplift", title: "Uplift Setup (train uplift model)",
    roles: ALL, files: ["ui/modules/uplift/index.js", "ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}`,
    states: {
      full: { base: "demo" },
      empty: { base: "empty" },
      loading: { base: "demo", delay: LIST_DELAY },
      error: { base: "demo", fail: /^\/use-cases\/[^/]+$/ },
    },
  },
  {
    id: "uplift-setup-score", group: "run-pages-uplift", title: "Uplift Setup, score mode",
    roles: "Analyst", files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}`,
    states: { full: { base: "demo", act: async (page) => click(page, '[data-umode="score"]') } },
  },
  {
    id: "uplift-setup-file", group: "run-pages-uplift", title: "Uplift Setup after upload (treatment column, outcome)",
    roles: "Analyst", files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}`,
    states: {
      full: {
        base: "demo", note: "targeted_campaign.csv (a non-random campaign)",
        act: async (page) => {
          await upload(page, "#u-file", "targeted_campaign.csv");
          await trySelect(page, "#u-pk", "customer_id");
          await trySelect(page, "#u-treatment", "treatment");
          await trySelect(page, "#u-target", "reactivated_90d");
        },
      },
      "error-validation": {
        base: "demo", note: "Run clicked: TREATMENT_NOT_RANDOM refusal and validation list (no run is created)",
        act: async (page) => {
          await upload(page, "#u-file", "targeted_campaign.csv");
          await trySelect(page, "#u-pk", "customer_id");
          await trySelect(page, "#u-treatment", "treatment");
          await trySelect(page, "#u-target", "reactivated_90d");
          await click(page, "#u-run");
          await settle(page, 1500);
        },
      },
    },
  },
  {
    id: "uplift-running", group: "run-pages-uplift", title: "Uplift run in progress",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}/run/${m.uplift_run_id}`,
    states: { full: { base: "demo", mockFn: (m) => [runAs(m.uplift_run_id, "running")] } },
  },
  {
    id: "uplift-results", group: "run-pages-uplift", title: "Uplift run results (pipeline blocks)",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}/run/${m.uplift_run_id}`,
    states: {
      full: { base: "demo" },
      error: { base: "demo", route: (m) => `#/uplift/${m.uplift_use_case_id}/run/r_missing` },
    },
  },
  {
    id: "uplift-model", group: "run-pages-uplift", title: "Uplift Model page (Qini, AUUC, deciles, policy value)",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/charts.js", "ui/modules/uplift/format.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}/model/${m.uplift_run_id}`,
    states: {
      full: { base: "demo" },
      loading: { base: "demo", delay: /^\/runs\/[^/]+(\/artefacts\/.*)?$/ },
      error: { base: "demo", route: (m) => `#/uplift/${m.uplift_use_case_id}/model/r_missing` },
    },
  },
  {
    id: "uplift-output", group: "run-pages-uplift", title: "Uplift Output page (segments, treat list)",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/uplift/${m.uplift_use_case_id}/output/${m.uplift_score_run_id}`,
    states: {
      full: { base: "demo" },
      error: { base: "demo", route: (m) => `#/uplift/${m.uplift_use_case_id}/output/r_missing` },
    },
  },

  // ---------------- campaigns, reports, pilot, generative ----------------
  {
    id: "campaign-results", group: "campaigns-reports-pilot", title: "Campaign results (churn campaign incrementality)",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/campaign/${m.use_case_id}/${m.score_run_id}`,
    states: {
      full: { base: "demo" },
      loading: { base: "demo", delay: /^\/runs\/[^/]+(\/artefacts\/.*)?$/ },
      error: { base: "demo", route: (m) => `#/campaign/${m.use_case_id}/r_missing` },
    },
  },
  {
    id: "campaign-results-uplift", group: "campaigns-reports-pilot", title: "Campaign results (win-back uplift campaign)",
    roles: ALL, files: ["ui/modules/uplift/views.js", "ui/modules/uplift/controller.js"],
    route: (m) => `#/campaign/${m.uplift_use_case_id}/${m.uplift_score_run_id}`,
    states: { full: { base: "demo" } },
  },
  {
    id: "pilot", group: "campaigns-reports-pilot", title: "Pilot: data request kit, readiness, results reports, campaigns",
    roles: ALL, files: ["ui/modules/pilot/screen.js", "ui/modules/pilot/index.js"],
    route: () => "#/pilot",
    states: {
      full: { base: "demo" },
      empty: { base: "empty" },
      loading: { base: "demo", delay: /^\/(datasets|models|runs|pilot\/data-request)$/ },
      error: { base: "demo", fail: /^\/(datasets|models|runs|pilot\/data-request)$/ },
    },
  },
  {
    id: "pilot-readiness", group: "campaigns-reports-pilot", title: "Data readiness report (clean dataset)",
    roles: ALL, files: ["ui/modules/pilot/screen.js"],
    route: (m) => `#/pilot/view/readiness/${m.train_dataset_id}`,
    states: {
      full: { base: "demo" },
      "full-broken": { base: "demo", route: (m) => `#/pilot/view/readiness/${m.broken_dataset_id}`, note: "the planted-problem extract (Not ready)" },
      error: { base: "demo", route: () => "#/pilot/view/readiness/ds_missing" },
    },
  },
  {
    id: "pilot-results-report", group: "campaigns-reports-pilot", title: "Results report (one page for the marketing head)",
    roles: ALL, files: ["ui/modules/pilot/screen.js"],
    route: (m) => `#/pilot/view/results/${m.use_case_id}`,
    states: {
      full: { base: "demo" },
      error: { base: "demo", route: () => "#/pilot/view/results/rca", note: "use case with no champion" },
    },
  },
  {
    id: "pilot-value", group: "campaigns-reports-pilot", title: "Campaign value view (rupees, ROI inputs, PDF)",
    roles: ALL, files: ["ui/modules/pilot/screen.js"],
    route: (m) => `#/pilot/value/${m.score_run_id}`,
    states: {
      full: { base: "demo" },
      error: { base: "demo", route: () => "#/pilot/value/r_missing" },
    },
  },
  {
    id: "ai-connection", group: "admin-ops-auth-ai", title: "AWS / AI service connection",
    roles: "Admin", files: ["ui/modules/generative/connection.js", "ui/modules/generative/index.js"],
    route: () => "#/generative/connection",
    states: { full: { base: "demo" }, empty: { base: "empty" } },
  },
  {
    id: "ai-assistant", group: "admin-ops-auth-ai", title: "AI Onboarding Assistant (generative)",
    roles: ALL, files: ["ui/modules/generative/assistant.js", "ui/availability.js"],
    route: () => "#/generative/assistant/ai-onboarding-assistant",
    states: {
      full: { base: "demo", note: "demo mode, no AI service: the notice" },
      empty: { base: "empty", note: "no demo mode: the real screen on the fake backend" },
    },
  },
  {
    id: "ai-rca", group: "admin-ops-auth-ai", title: "Root-cause notes (RCA, generative on a run)",
    roles: ALL, files: ["ui/modules/generative/rca.js", "ui/availability.js"],
    route: (m) => `#/generative/rca/rca/${m.train_run_id}`,
    states: { full: { base: "demo", note: "demo: AI notice" }, empty: { base: "empty", note: "no demo: real screen, run missing" } },
  },
  {
    id: "ai-copy", group: "admin-ops-auth-ai", title: "Campaign copy (generative on a scoring run)",
    roles: ALL, files: ["ui/modules/generative/copy.js", "ui/availability.js"],
    route: (m) => `#/generative/copy/${m.uplift_use_case_id}/${m.uplift_score_run_id}`,
    states: { full: { base: "demo", note: "demo: AI notice" }, empty: { base: "empty", note: "no demo: real screen, run missing" } },
  },

  // ---------------- admin / ops / auth ----------------
  {
    id: "signin", group: "admin-ops-auth-ai", title: "Sign in",
    roles: "Signed out", files: PROD("signin.js").concat("ui/modules/production/session.js"),
    route: () => "#/signin",
    states: {
      full: { base: "auth" },
      error: {
        base: "auth", note: "mocked 401 BAD_CREDENTIALS (no real failed attempt)",
        mockFn: () => [{ match: /^\/auth\/login$/, status: 401, body: JSON.stringify({ detail: { code: "BAD_CREDENTIALS", message: "The username or password is not right.", path: null } }) }],
        act: async (page) => {
          await page.fill("#pb-username", "demo-analyst");
          await page.fill("#pb-password", "not-the-password");
          await click(page, "#pb-signin-submit");
        },
      },
      "signed-out-home": { base: "auth", route: () => "#/", note: "a signed-out visitor opening the app" },
    },
  },
  {
    id: "userbar", group: "admin-ops-auth-ai", title: "Home with the user bar, per role",
    roles: "Viewer / Analyst / Approver / Admin", files: ["ui/modules/production/userbar.js", "ui/modules/production/gate.js", "ui/overview.js"],
    route: () => "#/",
    states: {
      "full-viewer": { base: "auth", as: "viewer" },
      "full-analyst": { base: "auth", as: "analyst" },
      "full-approver": { base: "auth", as: "approver" },
      "full-admin": { base: "auth", as: "admin" },
      "full-authoff": { base: "demo", note: "auth off: 'Access control is off' bar" },
    },
  },
  {
    id: "usecase-viewer", group: "admin-ops-auth-ai", title: "Use case Setup as a Viewer (controls gated)",
    roles: "Viewer", files: ["ui/modules/production/gate.js", "ui/usecase.js"],
    route: () => "#/uc/telco-churn",
    states: { full: { base: "auth", as: "viewer" } },
  },
  {
    id: "account", group: "admin-ops-auth-ai", title: "Change your password",
    roles: "Any signed-in user", files: PROD("signin.js"),
    route: () => "#/account",
    states: {
      full: { base: "auth", as: "analyst" },
      error: {
        base: "auth", as: "analyst", note: "new passwords differ (client-side check, nothing sent)",
        act: async (page) => {
          await page.fill("#pb-current", "x");
          await page.fill("#pb-new", "a-new-password-1");
          await page.fill("#pb-again", "a-new-password-2");
          await click(page, "#pb-account-submit");
        },
      },
    },
  },
  {
    id: "admin-users", group: "admin-ops-auth-ai", title: "Admin: Users",
    roles: "Admin", files: PROD("users.js"),
    route: () => "#/admin/users",
    states: {
      full: { base: "auth", as: "admin" },
      empty: { base: "empty", note: "auth off: no users" },
      loading: { base: "auth", as: "admin", delay: /^\/users$/ },
      error: { base: "auth", as: "admin", fail: /^\/users$/ },
      "error-refused": { base: "auth", as: "viewer", note: "a Viewer opening the Admin screen" },
    },
  },
  {
    id: "admin-audit", group: "admin-ops-auth-ai", title: "Admin: Audit log",
    roles: "Admin", files: PROD("audit.js"),
    route: () => "#/admin/audit",
    states: {
      full: { base: "auth", as: "admin" },
      empty: { base: "empty" },
      loading: { base: "auth", as: "admin", delay: /^\/audit\/events$/ },
      error: { base: "auth", as: "admin", fail: /^\/audit\/events$/ },
    },
  },
  ...[
    ["consent", "Privacy: Consent (look up, import)", "privacy.js"],
    ["erasure", "Privacy: Erasure requests", "privacy.js"],
    ["access", "Privacy: Access requests", "privacy.js"],
    ["retention", "Privacy: Retention plan", "retention.js"],
  ].map(([sec, title, file]) => ({
    id: `privacy-${sec}`, group: "admin-ops-auth-ai", title,
    roles: "Admin", files: PROD(file),
    route: () => `#/privacy/${sec}`,
    states: {
      full: { base: "auth", as: "admin" },
      empty: { base: "empty" },
      ...(sec === "erasure" || sec === "retention"
        ? { error: { base: "auth", as: "admin", fail: /^\/(privacy|retention)\/.+$/ } }
        : {}),
    },
  })),
  {
    id: "monitoring-schedules", group: "admin-ops-auth-ai", title: "Monitoring: Schedules (list + create)",
    roles: "Viewer reads; Analyst changes", files: PROD("schedules.js"),
    route: () => "#/monitoring/schedules",
    states: {
      full: { base: "demo", note: "one seeded drift-check schedule" },
      empty: { base: "empty" },
      loading: { base: "demo", delay: /^\/schedules$/ },
      error: { base: "demo", fail: /^\/schedules$/ },
    },
  },
  {
    id: "monitoring-schedule", group: "admin-ops-auth-ai", title: "Monitoring: one schedule (details, firings, edit)",
    roles: "Viewer reads; Analyst changes", files: PROD("schedules.js"),
    route: (m) => `#/monitoring/schedules/${m.scheduleId || "sch_missing"}`,
    states: {
      full: { base: "demo" },
      error: { base: "demo", route: () => "#/monitoring/schedules/sch_missing" },
    },
  },
  {
    id: "monitoring-alerts", group: "admin-ops-auth-ai", title: "Monitoring: Alerts",
    roles: "Viewer reads; Analyst acknowledges", files: PROD("alerts.js"),
    route: () => "#/monitoring/alerts",
    states: { full: { base: "demo" }, empty: { base: "empty" }, error: { base: "demo", fail: /^\/monitoring\/alerts$/ } },
  },
  {
    id: "monitoring-missed", group: "admin-ops-auth-ai", title: "Monitoring: Missed runs",
    roles: "Viewer", files: PROD("alerts.js"),
    route: () => "#/monitoring/missed",
    states: { full: { base: "demo" }, empty: { base: "empty" } },
  },
  {
    id: "monitoring-runs", group: "admin-ops-auth-ai", title: "Monitoring: Outcomes - scoring runs list",
    roles: "Viewer", files: PROD("outcomes.js"),
    route: () => "#/monitoring/runs",
    states: { full: { base: "demo" }, empty: { base: "empty" }, loading: { base: "demo", delay: /^\/runs$/ } },
  },
  {
    id: "monitoring-run", group: "admin-ops-auth-ai", title: "Monitoring: one scoring run's outcomes (upload, report)",
    roles: "Viewer reads; Analyst uploads", files: PROD("outcomes.js"),
    route: (m) => `#/monitoring/runs/${m.score_run_id}`,
    states: { full: { base: "demo" }, error: { base: "demo", route: () => "#/monitoring/runs/r_missing" } },
  },
  {
    id: "approvals", group: "admin-ops-auth-ai", title: "Approvals (challengers waiting for an Approver)",
    roles: "Approver", files: PROD("approvals.js"),
    route: () => "#/approvals",
    states: {
      empty: { base: "demo", note: "queue empty in the seeded demo" },
      "full-approver": { base: "auth", as: "approver", note: "whatever the sign-in server's queue holds" },
      error: { base: "demo", fail: /^\/approvals$/ },
    },
  },
];

// --- runner --------------------------------------------------------------------------------------

function inventory() {
  return SCREENS.map((s) => ({
    id: s.id, group: s.group, title: s.title, roles: s.roles, files: s.files,
    route: s.route({ use_case_id: "<uc>", train_run_id: "<train_run>", score_run_id: "<score_run>", uplift_use_case_id: "<uplift_uc>", uplift_run_id: "<uplift_run>", uplift_score_run_id: "<uplift_score_run>", train_dataset_id: "<dataset>", broken_dataset_id: "<broken_dataset>", scheduleId: "<schedule>" }),
    states: Object.fromEntries(Object.entries(s.states).map(([k, v]) => [k, { base: v.base, as: v.as || null, note: v.note || null }])),
  }));
}

if (process.env.INVENTORY === "1") {
  console.log(JSON.stringify(inventory(), null, 2));
  process.exit(0);
}

async function getJson(url, init) {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${init && init.method ? init.method : "GET"} ${url} -> ${r.status}`);
  return r.json();
}

/** One monthly drift-check schedule on the demo server, fired once, so Monitoring is not empty. */
async function prepareDemo(m) {
  try {
    const { schedules } = await getJson(`${BASE.demo}/schedules`);
    if (schedules.length) return schedules[0].schedule_id;
    const s = await getJson(`${BASE.demo}/schedules`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ use_case_id: m.use_case_id, kind: "drift_check", cadence: "monthly", client_id: m.client_id, parameters: { dataset_id: m.score_dataset_id } }),
    });
    console.log(`prepared: schedule ${s.schedule_id} on ${BASE.demo}`);
    await fetch(`${BASE.demo}/schedules/${s.schedule_id}/fire`, { method: "POST" }).catch(() => {});
    return s.schedule_id;
  } catch (e) {
    console.log(`prepare: could not seed a schedule (${e.message})`);
    return null;
  }
}

const tokens = {};
async function tokenFor(role) {
  if (!tokens[role]) {
    const body = await getJson(`${BASE.auth}/auth/login`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: `demo-${role}`, password: `demo-${role}-2026` }),
    });
    tokens[role] = { token: body.token, expires_at: body.expires_at };
  }
  return tokens[role];
}

const apiPath = (url) => {
  const u = new URL(url);
  return u.pathname.startsWith("/ui/") || u.pathname === "/ui" ? null : u.pathname;
};

async function shoot(browser, screen, stateName, spec, m, variant) {
  const vp = VIEWPORTS[variant === "mobile" ? "mobile" : "desktop"];
  const context = await browser.newContext({
    viewport: vp,
    deviceScaleFactor: 1,
    colorScheme: variant === "desktop-dark" ? "dark" : "light",
    isMobile: variant === "mobile",
    hasTouch: variant === "mobile",
  });
  const init = { tourSeen: !spec.tour, client: spec.client || null, auth: spec.as ? await tokenFor(spec.as) : null, keys: { TOUR_KEY, CLIENT_KEY, AUTH_KEY } };
  await context.addInitScript((o) => {
    try {
      if (o.tourSeen) localStorage.setItem(o.keys.TOUR_KEY, "1");
      else localStorage.removeItem(o.keys.TOUR_KEY);
      if (o.client) localStorage.setItem(o.keys.CLIENT_KEY, o.client);
      if (o.auth) sessionStorage.setItem(o.keys.AUTH_KEY, JSON.stringify(o.auth));
    } catch {}
  }, init);
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  const mocks = spec.mockFn ? spec.mockFn(m) : [];
  if (spec.delay || spec.fail || mocks.length) {
    await page.route("**/*", async (route) => {
      const p = apiPath(route.request().url());
      if (p === null) return route.continue();
      if (spec.fail && spec.fail.test(p)) return route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" });
      const mock = mocks.find((x) => x.match.test(p));
      if (mock && mock.status) return route.fulfill({ status: mock.status, contentType: "application/json", body: mock.body });
      if (mock && mock.mutate) {
        const res = await route.fetch();
        return route.fulfill({ response: res, body: mock.mutate(await res.text()) });
      }
      if (spec.delay && spec.delay.test(p)) {
        await new Promise((r) => setTimeout(r, 25000));
        return route.continue().catch(() => {});
      }
      return route.continue();
    });
  }

  const hash = (spec.route || screen.route)(m);
  const url = `${BASE[spec.base]}/ui/${hash}`;
  const file = path.join(OUT, `${screen.id}--${stateName}--${variant}.png`);
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (stateName === "loading") {
      await page.waitForTimeout(1500);
    } else {
      await settle(page);
      if (spec.act) await spec.act(page, m);
      await settle(page);
    }
    await page.screenshot({ path: file, fullPage: true, timeout: 30000 });
    console.log(`wrote ${file}${pageErrors.length ? `  (page errors: ${pageErrors.slice(0, 2).join(" | ")})` : ""}`);
    return null;
  } catch (e) {
    return `${path.basename(file)}: ${String(e.message || e).split("\n")[0]}`;
  } finally {
    await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
    await context.close().catch(() => {});
  }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const demo = await getJson(`${BASE.demo}/pilot/demo`);
  if (!demo.seeded || !demo.manifest) throw new Error(`${BASE.demo} is not a seeded demo server`);
  const m = { ...demo.manifest };
  m.scheduleId = await prepareDemo(m);

  const browser = await chromium.launch();
  const failures = [];
  let written = 0;
  const t0 = Date.now();
  for (const screen of SCREENS) {
    for (const [stateName, spec] of Object.entries(screen.states)) {
      if (ONLY.length && !ONLY.includes(screen.id) && !ONLY.includes(`${screen.id}--${stateName}`)) continue;
      const variants = ["desktop"];
      if (!SKIP_MOBILE) variants.push("mobile");
      if (!SKIP_DARK && (stateName === "full" || stateName.startsWith("full-"))) variants.push("desktop-dark");
      for (const variant of variants) {
        let err;
        try {
          err = await shoot(browser, screen, stateName, spec, m, variant);
        } catch (e) {
          err = `${screen.id}--${stateName}--${variant}: ${String(e.message || e).split("\n")[0]}`;
        }
        if (err) {
          failures.push(err);
          console.log(`FAILED ${err}`);
        } else written += 1;
      }
    }
  }
  await browser.close();
  console.log(`\n${written} screenshots written to ${OUT} in ${Math.round((Date.now() - t0) / 1000)}s`);
  if (failures.length) {
    console.log(`${failures.length} failed:`);
    for (const f of failures) console.log(`  - ${f}`);
  } else console.log("no failures");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
