// Phase 3b's entry point: the uplift screens (plan B §8), registered with `ui/modules/router.js`.
//
// HOW A USER REACHES THE UPLIFT SCREENS
//   * From the Overview (`#/`): a small "Uplift modelling" link this module adds under the page
//     header, leading to `#/uplift` - one link per use case.
//   * From any use-case screen (`#/uc/<id>`, its runs and its Data / Model / Output pages): an
//     "Uplift for this use case" link under the header, leading to that use case's uplift Setup
//     (`#/uplift/<id>`). On a Phase 1 *scoring* run a second link, "Campaign results for this
//     run", leads to `#/campaign/<id>/<run>`, because any scoring run with a control group can be
//     measured - not only uplift ones.
//   * Directly, by URL:
//       #/uplift                          every use case, one link each
//       #/uplift/<uc>                     Setup: upload, treatment column, Run (train or score)
//       #/uplift/<uc>/run/<run>           Running (polled) or Results: the pipeline blocks
//       #/uplift/<uc>/model/<run>         Model: Qini curve, AUUC with CI, uplift by decile, OPE
//       #/uplift/<uc>/output/<run>        Output: four segments, recommended contacts, treat list
//       #/campaign/<uc>/<run>             Campaign results: upload outcomes, incrementality report
//   Nothing in a Phase 1 file links here. Those files are not this branch's to edit, so the two
//   entry links are added to the page *after* `app.js` paints it: a MutationObserver on `#app`
//   notices a Phase 1 screen and inserts one `<nav class="uentry">` below its header. The Phase 1
//   markup and behaviour are untouched - remove this module and the links disappear with it.
//
// It claims two hash prefixes, `uplift` and `campaign`; `app.js` asks `resolveRoute` before its own
// `#/uc/...` routing, so claiming `uc` would break screens that work. `index.html` loads this file
// after `app.js`, whose first render has already run by then, so a deep link straight to an uplift
// URL is rendered here once registration is done; if `app.js`'s Overview paint lands on top of it,
// the same observer notices the screen is not this module's and renders it again (at most a few
// times per navigation).

import { ApiError, getIndustries, getRun, getUseCase } from "../../api.js";
import { errorBox, esc, pageHead } from "../../dom.js";
import { registerModule } from "../router.js";
import {
  createCampaignController,
  createModelController,
  createOutputController,
  createSetupController,
} from "./controller.js";
import { injectUpliftStyles } from "./styles.js";
import {
  UPLIFT_EXPLANATION,
  campaignPageHtml,
  modelPageHtml,
  outputPageHtml,
  routes,
  upliftIndexHtml,
  upliftScreenHtml,
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

/** `GET /use-cases/{id}` plus the legend label beside its marker, exactly as `app.js` does it. */
async function useCase(id) {
  const [uc, payload] = await Promise.all([getUseCase(id), allIndustries()]);
  const legend = ((payload.industries || [])[0] || {}).legend || [];
  const entry = legend.find((l) => l.ai_type === uc.ai_type);
  return entry ? { ...uc, type_label: entry.label } : uc;
}

function paint(app, html, after, scroll = true) {
  app.innerHTML = html;
  if (after) after();
  if (scroll) window.scrollTo(0, 0);
}

function failure(app, error) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  paint(
    app,
    `<main class="screen" data-module="uplift">${pageHead(
      `<a class="back" href="#/uplift">‹&nbsp; Uplift modelling</a><h1 class="h1">This screen could not be loaded</h1>`,
    )}${errorBox(api)}</main>`,
  );
}

const loading = (title) =>
  `<main class="screen" data-module="uplift">${pageHead(
    `<h1 class="h1">${esc(title)}</h1><p class="sub">Loading…</p>`,
  )}</main>`;

/** True while the screen a render started for is still the one the URL names. */
const current = (mine) => mine === token;

async function renderSetup(app, mine, ucId, runId) {
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
  else controller.showSetup();
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
  paint(app, html(), () => controller.bind(app));
  const title = { model: "Uplift model", output: "Uplift output", campaign: "Campaign results" }[kind];
  document.title = `${title} · Marketing AI`;
}

async function render(app, parts) {
  const mine = ++token;
  for (const c of setupControllers.values()) c.stop();
  activeSetup = null;
  try {
    const [root, ucId, third, fourth] = parts.map(decodeURIComponent);
    if (root === "campaign" && ucId && third) {
      paint(app, loading("Campaign results"));
      await renderPage(app, mine, "campaign", ucId, third);
      return;
    }
    if (root === "uplift" && !ucId) {
      paint(app, loading("Uplift modelling"));
      const payload = await allIndustries();
      if (!current(mine)) return;
      paint(app, upliftIndexHtml(payload));
      document.title = "Uplift · Marketing AI";
      return;
    }
    if (root === "uplift" && ucId && !third) {
      paint(app, loading("Uplift"));
      await renderSetup(app, mine, ucId, null);
      return;
    }
    if (root === "uplift" && third === "run" && fourth) {
      paint(app, loading("Uplift"));
      await renderSetup(app, mine, ucId, fourth);
      return;
    }
    if (root === "uplift" && (third === "model" || third === "output") && fourth) {
      paint(app, loading(third === "model" ? "Uplift model" : "Uplift output"));
      await renderPage(app, mine, third, ucId, fourth);
      return;
    }
    failure(app, new ApiError(404, "NOT_FOUND", `No uplift screen matches #/${parts.join("/")}.`, null));
  } catch (error) {
    if (current(mine)) failure(app, error);
  }
}

registerModule({ name: "uplift", routes: ROUTES, render });

// --- the entry links on Phase 1 screens --------------------------------------------------------

const scoringRuns = new Map();

/** Whether a run is a scoring run, asked once per run id; a failure just means "no link". */
async function isScoringRun(runId) {
  if (!scoringRuns.has(runId)) {
    scoringRuns.set(
      runId,
      getRun(runId).then(
        (detail) => detail.run.mode === "score",
        () => false,
      ),
    );
  }
  return scoringRuns.get(runId);
}

function insertEntry(app, html) {
  const main = app.querySelector("main.screen");
  if (!main || main.dataset.module || main.querySelector(".uentry")) return null;
  const rule = main.querySelector(".rule");
  if (!rule) return null;
  const nav = document.createElement("nav");
  nav.className = "uentry";
  nav.setAttribute("aria-label", "Uplift");
  nav.innerHTML = html;
  rule.after(nav);
  return nav;
}

function decorate(app) {
  const parts = hashParts();
  if (isOurs(parts)) {
    // `app.js` painted over one of our screens (its first render racing a deep link): draw ours again.
    const main = app.querySelector("main.screen");
    if (main && main.dataset.module !== "uplift" && repaints < MAX_REPAINTS) {
      repaints += 1;
      render(app, parts);
    }
    return;
  }
  if (parts.length === 0) {
    insertEntry(
      app,
      `<a href="${esc(routes.index())}"><b>Uplift modelling</b> ›</a><span>${esc(
        `Predicts who changes behaviour because of your action.`,
      )}</span>`,
    );
    return;
  }
  if (parts[0] !== "uc" || !parts[1]) return;
  const ucId = safeDecode(parts[1]);
  // The link never repeats the use-case name: a link named like the use case would make "the link
  // called <use case>" ambiguous on a screen whose breadcrumb already is one.
  const nav = insertEntry(
    app,
    `<a href="${esc(routes.setup(ucId))}">Uplift for <b>this use case</b> ›</a><span>${esc(
      UPLIFT_EXPLANATION,
    )}</span>`,
  );
  const runId = parts[2] === "run" ? parts[3] : ["data", "model", "output"].includes(parts[2]) ? parts[3] : null;
  if (!nav || !runId) return;
  isScoringRun(safeDecode(runId)).then((scoring) => {
    if (!scoring || !nav.isConnected) return;
    const link = document.createElement("a");
    link.href = routes.campaign(ucId, safeDecode(runId));
    link.innerHTML = "Campaign results for this run ›";
    nav.appendChild(link);
  });
}

const app = document.getElementById("app");
if (app && typeof MutationObserver !== "undefined") {
  new MutationObserver(() => decorate(app)).observe(app, { childList: true });
  window.addEventListener("hashchange", () => {
    repaints = 0;
    if (!isOurs(hashParts())) {
      token += 1;
      for (const c of setupControllers.values()) c.stop();
      activeSetup = null;
    }
  });
  // A deep link: `app.js` resolved this URL before the module existed. `decorate` re-renders it
  // when `app.js` has already painted a screen of its own; otherwise it is rendered here.
  decorate(app);
  const parts = hashParts();
  if (isOurs(parts) && repaints === 0) render(app, parts);
}
