// The router. Every screen is built from a response the API just gave us; nothing is cached
// across a reload and nothing is rendered from a value the API did not send.

import {
  ApiError,
  getArtefacts,
  getDatasetLineage,
  getIndustries,
  getRun,
  getRuns,
  scoresUrl,
  getUseCase,
} from "./api.js";
import {
  backLink,
  crumbs,
  emptyState,
  errorBox,
  esc,
  journeyBack,
  noticeCard,
  notFound,
  pageHead,
  skeleton,
} from "./dom.js";
import { mountChrome } from "./chrome.js";
import { bindOverview, journeyFor, overviewHtml } from "./overview.js";
import { isUplift, pageArtefacts, renderPage } from "./pages.js";
import { createController, useCaseHtml } from "./usecase.js";
import { MODULES_CHANGED, resolveRoute } from "./modules/router.js";
import { aiNoticeHtml, needsAiNotice } from "./availability.js";

const app = document.getElementById("app");
const PAGES = ["data", "model", "output"];
const PAGE_TITLES = { data: "Data", model: "Model", output: "Output" };

// The one top bar (v1): mounted here, after `modules/router.js` - and through it Phase 4b's boot, whose
// older user bar it adopts - has finished evaluating.
mountChrome(document);

let industries = null;

async function allIndustries() {
  if (!industries) industries = await getIndustries();
  return industries;
}

/**
 * `GET /use-cases/{id}` carries the marker and the stars but not the wording beside them; the
 * industry legend is where that lives, so one cached `GET /industries` supplies it. The same
 * response names the journey the use case belongs to, which its breadcrumb returns to.
 */
async function useCase(id) {
  const [uc, payload] = await Promise.all([getUseCase(id), allIndustries()]);
  const legend = ((payload.industries || [])[0] || {}).legend || [];
  const entry = legend.find((l) => l.ai_type === uc.ai_type);
  const journey = journeyFor(payload, uc.id);
  return { ...uc, ...(entry ? { type_label: entry.label } : {}), ...(journey ? { journey } : {}) };
}

/** The catalogue card for a use case id, available or planned, from the cached `GET /industries`. */
function cardFor(payload, id) {
  for (const industry of (payload && payload.industries) || []) {
    for (const stage of industry.stages || []) {
      const card = (stage.use_cases || []).find((u) => u.id === id);
      if (card) return card;
    }
  }
  return null;
}

const controllers = new Map();
let active = null;

function controllerFor(uc) {
  if (!controllers.has(uc.id)) {
    controllers.set(
      uc.id,
      createController(uc, () => {
        if (active !== uc.id) return;
        paint(useCaseHtml(uc, controllers.get(uc.id).state), () => controllers.get(uc.id).bind(app));
      }),
    );
  }
  return controllers.get(uc.id);
}

// What `#app` shows: the route it was painted for and the element painted, so a repaint of the same
// screen (a module registering, the client changing) never flashes a skeleton, while a new route
// never keeps the previous screen on show while its data loads.
const shown = { hash: null, node: null };

function paint(html, after) {
  app.style.animation = "none";
  void app.offsetWidth;
  app.style.animation = "";
  app.innerHTML = html;
  shown.hash = window.location.hash;
  shown.node = app.firstElementChild;
  if (after) after();
  window.scrollTo(0, 0);
}

/** Grey blocks shaped like the page, at once, unless this route is already on screen. */
function loading(kind = "page", title = null) {
  const onScreen = shown.hash === window.location.hash && shown.node && app.firstElementChild === shown.node;
  if (onScreen) return;
  paint(skeleton(kind, { title }));
}

const screen = (inner, body = "") => `<main class="screen">${pageHead(inner)}${body}</main>`;

/**
 * A screen that could not be drawn. A planned use case is not an error: it gets the calm "Coming
 * soon" notice under its own name. A use case or run that is not there gets one card and one way
 * back. Anything else leads with a plain sentence and "Try again", the code under Details. Every one
 * goes back to the journey it was opened from; when `GET /industries` is what failed, that is Home.
 */
async function failure(error, parts) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  const payload = await allIndustries().catch(() => null);
  const ucId = parts[0] === "uc" ? parts[1] : null;
  const journey = payload && journeyFor(payload, ucId);
  const back = journeyBack(journey ? { journey } : null);
  const trail = backLink(journey ? { journey } : null);
  if (api.code === "USE_CASE_PLANNED") {
    const card = cardFor(payload, ucId) || {};
    const name = card.name || "This use case";
    paint(
      screen(
        `${crumbs([journey, { label: name }])}<h1 class="h1">${esc(name)}</h1>${
          card.description ? `<p class="desc">${esc(card.description)}</p>` : ""
        }`,
        noticeCard({
          title: "Coming soon",
          text: "This use case is planned but not available yet. The other use cases in this journey work today.",
          action: { label: `Back to ${back.label}`, href: back.href },
          attrs: "data-planned",
        }),
      ),
    );
    document.title = `${name} · Marketing AI`;
    return;
  }
  if (api.status === 404) {
    // A run that is not there, inside a use case that is: back to that use case, by name.
    const card = /RUN/.test(api.code) ? cardFor(payload, ucId) : null;
    const home = card ? { label: card.name, href: `#/uc/${encodeURIComponent(ucId)}` } : back;
    const what = /RUN/.test(api.code) ? "run" : ucId ? "use case" : "page";
    paint(
      screen(
        `${crumbs([journey, card ? home : null, { label: "Not found" }])}<h1 class="h1">${esc(
          what[0].toUpperCase() + what.slice(1),
        )} not found</h1>`,
        notFound(what, home, api),
      ),
    );
    return;
  }
  paint(
    screen(
      `${trail}<h1 class="h1">This screen could not be loaded</h1>`,
      `${errorBox(api, { retry: true })}<div class="btn-row next"><a class="btn quiet" href="${esc(back.href)}">Back to ${esc(
        back.label,
      )}</a></div>`,
    ),
  );
}

/** `industryId` comes from `#/industry/<id>`; null opens the API's default journey. */
async function showOverview(industryId) {
  active = null;
  loading("list", "Marketing AI");
  paint(overviewHtml(await allIndustries(), industryId), () => bindOverview(app));
}

async function showUseCase(id, runId) {
  loading("form");
  const uc = await useCase(id);
  // A use case that is only AI-written text (the assistant) has nothing to show in a demo with no
  // AI service: one notice rather than a training form that does not apply to it (DEC-954).
  if (uc.ai_type === "generative" && (await needsAiNotice(uc))) {
    active = null;
    paint(aiNoticeHtml(uc, backLink(uc)));
    return uc;
  }
  active = uc.id;
  const controller = controllerFor(uc);
  controller.stop();
  await controller.refreshLists();
  controller.sync();
  if (runId) {
    await controller.loadRun(runId);
  }
  paint(useCaseHtml(uc, controller.state), () => controller.bind(app));
  if (controller.state.view === "running") controller.poll();
  return uc;
}

/** The run a Data / Model / Output link should read when the URL names none: the newest finished one. */
async function latestRunId(useCaseId) {
  const { runs } = await getRuns(useCaseId);
  const finished = (runs || []).find((r) => r.state === "done");
  return finished ? finished.run_id : null;
}

async function showPage(id, kind, runId) {
  loading("page");
  const uc = await useCase(id);
  active = null;
  const chosen = runId || (await latestRunId(id));
  if (!chosen) {
    paint(
      screen(
        `${crumbs([uc.journey, { label: uc.name, href: `#/uc/${id}` }, { label: PAGE_TITLES[kind] }])}<h1 class="h1">${esc(
          uc.pages[kind],
        )}</h1>`,
        `<section class="card">${emptyState({
          title: "No finished run yet",
          text: "Upload a dataset and run it on the Setup screen to see this page.",
          action: { label: "Go to Setup", href: `#/uc/${id}` },
        })}</section>`,
      ),
    );
    return uc;
  }
  const detail = await getRun(chosen);
  const art = await getArtefacts(chosen, pageArtefacts(kind, detail.run));
  const extra = { ...(await lineageOf(kind, detail.run)), ...(await upliftChartsFor(kind, detail.run)) };
  paint(renderPage(kind, uc, detail.run, art, scoresUrl(chosen), extra));
  return uc;
}

/**
 * An uplift run's Model page draws its Qini curve with the uplift module's own chart, so the two
 * screens show one picture. Loaded on demand: a Phase 1 run never asks, and without that module the
 * page lists the curve's points instead (M53).
 */
async function upliftChartsFor(kind, run) {
  if (kind !== "model" || !isUplift(run)) return {};
  try {
    const charts = await import("./modules/uplift/charts.js");
    return { qiniChart: charts.qiniChart };
  } catch {
    return {};
  }
}

/**
 * The Data page's lineage block (sources -> mapping -> recipe -> dataset -> run) for a run that read
 * a built dataset; nothing for a run that read an uploaded file, which has no such history. A
 * lineage that cannot be read is shown as its error, not as a page that failed to load.
 */
async function lineageOf(kind, run) {
  if (kind !== "data" || !run.dataset_id) return {};
  try {
    return { lineage: await getDatasetLineage(run.dataset_id) };
  } catch (error) {
    if (error instanceof ApiError) return { lineageError: error };
    throw error;
  }
}

async function renderNow() {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    // A phase branch registers whole screens of its own through `modules/router.js`; nothing is
    // registered on this branch, so this resolves to null and the Phase 1 routing below runs.
    const claimed = resolveRoute(parts);
    if (claimed) {
      active = null;
      await claimed.render(app, parts);
      return;
    }
    if (parts[0] !== "uc" || !parts[1]) {
      await showOverview(parts[0] === "industry" && parts[1] ? decodeURIComponent(parts[1]) : null);
      document.title = "Marketing AI · Minfy";
      return;
    }
    const id = parts[1];
    let uc;
    if (parts[2] === "run" && parts[3]) {
      uc = await showUseCase(id, parts[3]);
    } else if (PAGES.includes(parts[2])) {
      uc = await showPage(id, parts[2], parts[3]);
      document.title = `${PAGE_TITLES[parts[2]]} · ${uc.name} · Marketing AI`;
      return;
    } else {
      uc = await showUseCase(id, null);
    }
    document.title = `${uc.name} · Marketing AI`;
  } catch (error) {
    await failure(error, parts);
  }
}

let rendering = null;
let renderAgain = false;

/**
 * Draw the current route - one draw at a time, and once more if anything asked meanwhile.
 *
 * Draws used to overlap freely, which was harmless while only a hash change started one. Phase
 * modules now ask for a redraw too (`MODULES_CHANGED`, below), and two overlapping draws of two
 * different routes can finish out of order: the overview a picker change asked for landing on top of
 * the use case the user had just clicked into. Every draw reads the hash when it starts, so the one
 * extra draw after a busy spell is always of the route the user is actually on.
 */
function render() {
  // A phase module's own screen is drawn at once, never queued behind a Phase 1 draw that may be
  // waiting on a slow request: the module guards its screen against a late Phase 1 paint itself
  // (the uplift module's repaint, Phase 4b's deep-link guard), and a hung `GET /industries` must not
  // keep someone reloading on an admin screen from reaching it (DEC-801).
  if (resolveRoute(window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean))) {
    return renderNow();
  }
  if (rendering) {
    renderAgain = true;
    return rendering;
  }
  rendering = (async () => {
    do {
      renderAgain = false;
      await renderNow();
    } while (renderAgain);
    rendering = null;
  })();
  return rendering;
}

window.addEventListener("hashchange", render);
// A phase module that registers after the first paint - or whose state the current screen shows,
// like the client picked in the top bar - asks for the current route to be drawn again.
window.addEventListener(MODULES_CHANGED, render);
// "Try again" on any screen's error (`errorBox(error, { retry: true })`) draws the route again.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (target && typeof target.closest === "function" && target.closest("[data-retry]")) {
    event.preventDefault();
    shown.hash = null; // the failed screen goes; the skeleton shows the new attempt
    render();
  }
});
render();
