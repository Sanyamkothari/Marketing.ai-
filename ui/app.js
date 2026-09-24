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
import { backLink, errorBox, esc, pageHead } from "./dom.js";
import { bindOverview, journeyFor, overviewHtml } from "./overview.js";
import { isUplift, pageArtefacts, renderPage } from "./pages.js";
import { createController, useCaseHtml } from "./usecase.js";
import { MODULES_CHANGED, resolveRoute } from "./modules/router.js";
import { aiNoticeHtml, needsAiNotice } from "./availability.js";

const app = document.getElementById("app");
const PAGES = ["data", "model", "output"];

let industries = null;

async function allIndustries() {
  if (!industries) industries = await getIndustries();
  return industries;
}

/**
 * `GET /use-cases/{id}` carries the marker and the stars but not the wording beside them; the
 * industry legend is where that lives, so one cached `GET /industries` supplies it. The same
 * response names the journey the use case belongs to, which its back link and breadcrumb return to.
 */
async function useCase(id) {
  const [uc, payload] = await Promise.all([getUseCase(id), allIndustries()]);
  const legend = ((payload.industries || [])[0] || {}).legend || [];
  const entry = legend.find((l) => l.ai_type === uc.ai_type);
  const journey = journeyFor(payload, uc.id);
  return { ...uc, ...(entry ? { type_label: entry.label } : {}), ...(journey ? { journey } : {}) };
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

function paint(html, after) {
  app.style.animation = "none";
  void app.offsetWidth;
  app.style.animation = "";
  app.innerHTML = html;
  if (after) after();
  window.scrollTo(0, 0);
}

const screen = (inner) => `<main class="screen">${pageHead(inner)}</main>`;

/**
 * A use case that could not be loaded (a planned card's 404, say) still goes back to the journey it
 * was opened from; `parts` is the route that failed. When `GET /industries` is what failed, the link
 * is the bare overview.
 */
async function failure(error, parts) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  const payload = await allIndustries().catch(() => null);
  const journey = payload && journeyFor(payload, parts[0] === "uc" ? parts[1] : null);
  const back = backLink(journey ? { journey } : null);
  paint(
    screen(`${back}<h1 class="h1">This screen could not be loaded</h1>`).replace(
      "</main>",
      `${errorBox(api)}</main>`,
    ),
  );
}

function loading(title) {
  paint(screen(`<h1 class="h1">${esc(title)}</h1><p class="sub">Loading…</p>`));
}

/** `industryId` comes from `#/industry/<id>`; null opens the API's default journey. */
async function showOverview(industryId) {
  active = null;
  loading("Marketing AI");
  paint(overviewHtml(await allIndustries(), industryId), () => bindOverview(app));
}

async function showUseCase(id, runId) {
  const uc = await useCase(id);
  // A use case that is only AI-written text (the assistant) has nothing to show in a demo with no
  // AI service: one notice rather than a training form that does not apply to it (DEC-954).
  if (uc.ai_type === "generative" && (await needsAiNotice(uc))) {
    active = null;
    paint(aiNoticeHtml(uc, backLink(uc)));
    return;
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
}

/** The run a Data / Model / Output link should read when the URL names none: the newest finished one. */
async function latestRunId(useCaseId) {
  const { runs } = await getRuns(useCaseId);
  const finished = (runs || []).find((r) => r.state === "done");
  return finished ? finished.run_id : null;
}

async function showPage(id, kind, runId) {
  const uc = await useCase(id);
  active = null;
  const chosen = runId || (await latestRunId(id));
  if (!chosen) {
    paint(
      screen(
        `<a class="back" href="#/uc/${esc(id)}">‹&nbsp; ${esc(uc.name)}</a><h1 class="h1">${esc(
          uc.pages[kind],
        )}</h1><p class="desc">This use case has no finished run yet. Upload a dataset and run it to see this page.</p>`,
      ),
    );
    return;
  }
  loading(uc.pages[kind]);
  const detail = await getRun(chosen);
  const art = await getArtefacts(chosen, pageArtefacts(kind, detail.run));
  const extra = { ...(await lineageOf(kind, detail.run)), ...(await upliftChartsFor(kind, detail.run)) };
  paint(renderPage(kind, uc, detail.run, art, scoresUrl(chosen), extra));
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
    if (parts[2] === "run" && parts[3]) {
      await showUseCase(id, parts[3]);
    } else if (PAGES.includes(parts[2])) {
      await showPage(id, parts[2], parts[3]);
      document.title = `${parts[2][0].toUpperCase()}${parts[2].slice(1)} · Marketing AI`;
      return;
    } else {
      await showUseCase(id, null);
    }
    document.title = `${id} · Marketing AI`;
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
// like the client picked in the header - asks for the current route to be drawn again.
window.addEventListener(MODULES_CHANGED, render);
render();
