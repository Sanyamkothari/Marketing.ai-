// Phase 3b's entry point: the uplift screens (plan B §8), registered with `ui/modules/router.js`.
//
// HOW A USER REACHES THE UPLIFT SCREENS
//   * From the top bar: Models › "Uplift models" (`ui/chrome.js`) opens `#/uplift`.
//   * From a use case's Setup (`#/uc/<id>`): the quiet related link "Also: target with uplift ›" in
//     its header, which this module offers through the router's run-action seam as the action for
//     "no run" (`runActionsHtml(uc, null)`), and only for a use case that can have one (not an
//     AI-written-text use case; planned, notice and error screens never draw a use case).
//   * From a finished scoring run (`#/uc/<id>/run/<run>`): the "Campaign results" flow block, the
//     same seam's action for a scoring run (`runActionsHtml(uc, run)`), because any scoring run with a
//     control group can be measured - not only uplift ones.
//   * From Campaigns (`#/monitoring/runs`) and the value view (`#/pilot/value/<run>`), which link here.
//   * Directly, by URL:
//       #/uplift                          every use case, one row each, with its uplift status
//       #/uplift/<uc>                     Setup: upload, treatment column, Run (train or score)
//       #/uplift/<uc>/score               the same Setup, opened in score mode
//       #/uplift/<uc>/run/<run>           Running (polled) or Results: the verdict and the pipeline
//       #/uplift/<uc>/model/<run>         Model: the finding, Qini curve, gain by tenth, "What if…"
//       #/uplift/<uc>/output/<run>        Contact list: who to contact, four groups, the download
//       #/campaign/<uc>/<run>             Campaign results: the verdict, upload outcomes
//
// It claims two hash prefixes, `uplift` and `campaign`; `app.js` asks `resolveRoute` before its own
// `#/uc/...` routing, so claiming `uc` would break screens that work. `index.html` loads this file
// after `app.js`, whose first render has already run by then, so a deep link straight to an uplift
// URL is rendered here once registration is done; if `app.js`'s paint lands on top of it, a
// MutationObserver on `#app` notices the screen is not this module's and renders it again (at most a
// few times per navigation). The observer adds nothing to other modules' screens.

import { ApiError, getIndustries, getUseCase } from "../../api.js";
import { crumbs, errorBox, esc, notFound, pageHead, skeleton } from "../../dom.js";
import { journeyFor } from "../../overview.js";
import { registerModule, registerRunAction, setActiveNav } from "../router.js";
import {
  createCampaignController,
  createModelController,
  createOutputController,
  createSetupController,
  loadIndexStatuses,
} from "./controller.js";
import { injectUpliftStyles } from "./styles.js";
import {
  UPLIFT_EXPLANATION,
  campaignPageHtml,
  campaignTitle,
  indexStatusHtml,
  modelPageHtml,
  outputPageHtml,
  routes,
  upliftIndexHtml,
  upliftScreenHtml,
  upliftUnavailable,
} from "./views.js";

injectUpliftStyles();

const ROUTES = ["uplift", "campaign"];
const MAX_REPAINTS = 3;

const setupControllers = new Map();
let industries = null;
let token = 0;
let repaints = 0;
let activeSetup = null;

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
const safeDecode = (part) => {
  try {
    return decodeURIComponent(part || "");
  } catch {
    return part;
  }
};
const isOurs = (parts) => ROUTES.includes(parts[0]);

async function allIndustries() {
  if (!industries) industries = await getIndustries();
  return industries;
}

/** `GET /use-cases/{id}` plus the legend label and the journey, exactly as `app.js` does it. */
async function useCase(id) {
  const [uc, payload] = await Promise.all([getUseCase(id), allIndustries()]);
  const legend = ((payload.industries || [])[0] || {}).legend || [];
  const entry = legend.find((l) => l.ai_type === uc.ai_type);
  const journey = journeyFor(payload, uc.id);
  return { ...uc, ...(entry ? { type_label: entry.label } : {}), ...(journey ? { journey } : {}) };
}

function paint(app, html, after, scroll = true) {
  app.innerHTML = html;
  if (after) after();
  if (scroll) window.scrollTo(0, 0);
}

/** Grey blocks in the page's shape at once, marked as this module's so the repaint guard leaves it. */
const loading = (kind, title) =>
  skeleton(kind, { title }).replace('<main class="screen skel-screen"', '<main class="screen skel-screen" data-module="uplift"');

/**
 * A screen that could not be drawn. A campaign or run that is not there gets one calm card with one
 * way back (all campaigns; the use case's uplift Setup; the uplift list); anything else a plain
 * sentence, "Try again" and the code under Details.
 */
function failure(app, error, parts) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  const [root, ucId] = parts.map(safeDecode);
  const campaign = root === "campaign";
  const back = campaign
    ? { href: routes.campaigns(), label: "all campaigns" }
    : ucId && !/USE_CASE/.test(api.code)
      ? { href: routes.setup(ucId), label: "uplift Setup" }
      : { href: routes.index(), label: "Uplift modelling" };
  const trail = campaign
    ? crumbs([{ label: "Campaigns", href: routes.campaigns() }, { label: "Not found" }])
    : crumbs([{ label: "Uplift modelling", href: routes.index() }, { label: api.status === 404 ? "Not found" : "Error" }]);
  if (api.status === 404) {
    const what = campaign ? "campaign" : /RUN/.test(api.code) ? "run" : /USE_CASE/.test(api.code) ? "use case" : "page";
    paint(
      app,
      `<main class="screen" data-module="uplift">${pageHead(
        `${trail}<h1 class="h1">${esc(`${what[0].toUpperCase()}${what.slice(1)} not found`)}</h1>`,
      )}${notFound(what, back, api)}</main>`,
    );
    return;
  }
  paint(
    app,
    `<main class="screen" data-module="uplift">${pageHead(
      `${trail}<h1 class="h1">This screen could not be loaded</h1>`,
    )}${errorBox(api, { retry: true })}<div class="btn-row next"><a class="btn quiet" href="${esc(back.href)}">Back to ${esc(
      back.label,
    )}</a></div></main>`,
  );
}

/** True while the screen a render started for is still the one the URL names. */
const current = (mine) => mine === token;

async function renderSetup(app, mine, ucId, runId, mode = null) {
  const uc = await useCase(ucId);
  if (!setupControllers.has(uc.id)) {
    setupControllers.set(
      uc.id,
      createSetupController(uc, () => {
        const parts = hashParts();
        if (activeSetup !== uc.id || parts[0] !== "uplift" || safeDecode(parts[1]) !== uc.id) return;
        const c = setupControllers.get(uc.id);
        paint(app, upliftScreenHtml(uc, c.state), () => c.bind(app), false);
      }),
    );
  }
  const controller = setupControllers.get(uc.id);
  controller.stop();
  await controller.refreshLists();
  if (runId) await controller.loadRun(runId);
  else controller.showSetup(mode);
  if (!current(mine)) return;
  activeSetup = uc.id;
  paint(app, upliftScreenHtml(uc, controller.state), () => controller.bind(app));
  if (controller.state.view === "running") controller.poll();
  document.title = `${uc.name} · Uplift · Marketing AI`;
}

async function renderPage(app, mine, kind, ucId, runId) {
  const uc = await useCase(ucId);
  let controller;
  const rerender = () => {
    if (!current(mine)) return;
    paint(app, html(), () => controller.bind(app), false);
  };
  const html = () => {
    const s = controller.state;
    if (kind === "model") return modelPageHtml(uc, s.run, s.art, s.ope);
    if (kind === "output") return outputPageHtml(uc, s.run, s.art, s);
    return campaignPageHtml(uc, s.run, s);
  };
  controller =
    kind === "model"
      ? createModelController(runId, rerender)
      : kind === "output"
        ? createOutputController(uc, runId)
        : createCampaignController(uc, runId, rerender);
  await controller.load();
  if (!current(mine)) return;
  // A scoring run's contact list and its campaign results belong to Campaigns in the top bar.
  if (kind === "campaign" || (kind === "output" && controller.state.run.mode === "score")) setActiveNav("campaigns");
  paint(app, html(), () => controller.bind(app));
  const title =
    kind === "campaign"
      ? campaignTitle(uc, controller.state.run, controller.state)
      : `${uc.name} · ${kind === "model" ? "Uplift model" : "Contact list"}`;
  document.title = `${title} · Marketing AI`;
}

async function renderIndex(app, mine) {
  const payload = await allIndustries();
  if (!current(mine)) return;
  const statuses = {};
  paint(app, upliftIndexHtml(payload, statuses));
  document.title = "Uplift · Marketing AI";
  // Each row's status arrives on its own; the list is usable before any has.
  await loadIndexStatuses(payload, (id, status) => {
    statuses[id] = status;
    if (!current(mine)) return;
    const rows = app.querySelectorAll ? [...app.querySelectorAll("[data-uc]")] : [];
    const row = rows.find((el) => el.dataset.uc === id);
    const cell = row && row.querySelector("[data-ustatus]");
    if (cell) cell.outerHTML = indexStatusHtml(status);
  });
}

async function render(app, parts) {
  const mine = ++token;
  for (const c of setupControllers.values()) c.stop();
  activeSetup = null;
  try {
    const [root, ucId, third, fourth] = parts.map(safeDecode);
    if (root === "campaign" && ucId && third) {
      paint(app, loading("page", "Campaign results"));
      await renderPage(app, mine, "campaign", ucId, third);
      return;
    }
    if (root === "uplift" && !ucId) {
      paint(app, loading("list", "Measure what a campaign changes (uplift)"));
      await renderIndex(app, mine);
      return;
    }
    if (root === "uplift" && ucId && (!third || (third === "score" && !fourth))) {
      paint(app, loading("form"));
      await renderSetup(app, mine, ucId, null, third === "score" ? "score" : null);
      return;
    }
    if (root === "uplift" && third === "run" && fourth) {
      paint(app, loading("page"));
      await renderSetup(app, mine, ucId, fourth);
      return;
    }
    if (root === "uplift" && (third === "model" || third === "output") && fourth) {
      paint(app, loading("page", third === "model" ? "How well the model finds the persuadable" : "Who to contact"));
      await renderPage(app, mine, third, ucId, fourth);
      return;
    }
    failure(app, new ApiError(404, "NOT_FOUND", `No uplift screen matches #/${parts.join("/")}.`, null), parts);
  } catch (error) {
    if (current(mine)) failure(app, error, parts);
  }
}

registerModule({ name: "uplift", routes: ROUTES, render });

// --- what other screens offer: the related link and the Campaign results block -----------------

/**
 * On a use case's own header (no run): "Also: target with uplift ›", as `headActions`' quiet related
 * link. Never for a use case uplift cannot serve (AI-written text, planned).
 */
registerRunAction({
  name: "uplift-related",
  applies: (uc, run) => !run && !!uc && !!uc.id && !upliftUnavailable(uc),
  html: (uc) => `<a class="related" href="${esc(routes.setup(uc.id))}">Also: target with uplift ›</a>`,
});

/**
 * On a finished scoring run's Results: the fourth flow block, "Campaign results". It never says
 * "Done": whether outcomes were uploaded is not in run.json, so it says when it applies instead.
 */
registerRunAction({
  name: "campaign-results",
  applies: (uc, run) => !!uc && !!run && run.mode === "score" && run.state === "done",
  html: (uc, run) =>
    `<div class="arrow" aria-hidden="true">→</div><a class="block" href="${esc(
      routes.campaign(uc.id, run.run_id),
    )}" data-action="campaign-results"><div><div class="lab"><span>Campaign results</span><span class="bstate waiting">After the campaign</span></div><div class="val">Contacted vs not contacted</div><div class="meta">Upload outcomes once the campaign has run</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`,
});

// --- the deep-link repaint guard ----------------------------------------------------------------

function guard(app) {
  const parts = hashParts();
  if (!isOurs(parts)) return;
  // `app.js` painted over one of our screens (its first render racing a deep link): draw ours again.
  const main = app.querySelector("main.screen");
  if (main && main.dataset.module !== "uplift" && repaints < MAX_REPAINTS) {
    repaints += 1;
    render(app, parts);
  }
}

const app = document.getElementById("app");
if (app && typeof MutationObserver !== "undefined") {
  new MutationObserver(() => guard(app)).observe(app, { childList: true });
  window.addEventListener("hashchange", () => {
    repaints = 0;
    if (!isOurs(hashParts())) {
      token += 1;
      for (const c of setupControllers.values()) c.stop();
      activeSetup = null;
    }
  });
  // A deep link: `app.js` resolved this URL before the module existed. `guard` re-renders it when
  // `app.js` has already painted a screen of its own; otherwise it is rendered here.
  guard(app);
  const parts = hashParts();
  if (isOurs(parts) && repaints === 0) render(app, parts);
}
