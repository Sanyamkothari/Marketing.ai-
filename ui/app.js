// The router. Every screen is built from a response the API just gave us; nothing is cached
// across a reload and nothing is rendered from a value the API did not send.

import { ApiError, getArtefacts, getIndustries, getRun, getRuns, scoresUrl, getUseCase } from "./api.js";
import { errorBox, esc, pageHead } from "./dom.js";
import { overviewHtml } from "./overview.js";
import { PAGE_ARTEFACTS, renderPage } from "./pages.js";
import { createController, useCaseHtml } from "./usecase.js";
import { resolveRoute } from "./modules/router.js";

const app = document.getElementById("app");
const PAGES = ["data", "model", "output"];

let industries = null;

async function allIndustries() {
  if (!industries) industries = await getIndustries();
  return industries;
}

/**
 * `GET /use-cases/{id}` carries the marker and the stars but not the wording beside them; the
 * industry legend is where that lives, so one cached `GET /industries` supplies it.
 */
async function useCase(id) {
  const [uc, payload] = await Promise.all([getUseCase(id), allIndustries()]);
  const legend = ((payload.industries || [])[0] || {}).legend || [];
  const entry = legend.find((l) => l.ai_type === uc.ai_type);
  return entry ? { ...uc, type_label: entry.label } : uc;
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

function failure(error) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  paint(
    screen(
      `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">This screen could not be loaded</h1>`,
    ).replace("</main>", `${errorBox(api)}</main>`),
  );
}

function loading(title) {
  paint(screen(`<h1 class="h1">${esc(title)}</h1><p class="sub">Loading…</p>`));
}

async function showOverview() {
  active = null;
  loading("Marketing AI");
  paint(overviewHtml(await allIndustries()));
}

async function showUseCase(id, runId) {
  const uc = await useCase(id);
  active = uc.id;
  const controller = controllerFor(uc);
  controller.stop();
  await controller.refreshLists();
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
  const art = await getArtefacts(chosen, PAGE_ARTEFACTS[kind]);
  paint(renderPage(kind, uc, detail.run, art, scoresUrl(chosen)));
}

async function render() {
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
      await showOverview();
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
    failure(error);
  }
}

window.addEventListener("hashchange", render);
render();
