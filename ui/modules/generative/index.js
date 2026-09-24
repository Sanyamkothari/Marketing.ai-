// Phase 3a's entry point (PARALLEL_WORK_PROTOCOL.md §4): one module, one call to
// `registerModule`, so importing this file is the only wiring another branch has to do.
//
// It claims a single top-level route, `generative`, rather than one prefix per screen: `app.js`
// checks `resolveRoute` before its own `#/uc/...` handling runs, so a module that claimed `uc`, or
// any other prefix a later phase also reaches for, would silently break the screens that already
// work. One prefix is the whole risk surface, and everything past it - which screen, which use
// case, which run or index - is this module's own business, exactly like `#/uc/<id>/run/<runId>`
// already nests inside app.js's one `uc` claim.
//
// v1 UI: it also fills the router seams (docs/ui/FOUNDATION.md) - the Admin menu's "AI service"
// entry, and the "Write root-cause notes" / "Write campaign copy" actions on a run's results, offered
// only where AI writing is available (DEC-954: never into the demo's switched-off notice). Who may
// connect an AI service, and the approver's name, come from the production session.

import { ApiError, getIndustries, getUseCase } from "../../api.js";
import { backLink, crumbs, errorBox, esc, journeyBack, notFound, pageHead, skeleton } from "../../dom.js";
import { journeyFor } from "../../overview.js";
import { registerModule, registerNavSlot, registerRunAction } from "../router.js";
import {
  aiNoticeHtml,
  aiWritingAvailableNow,
  demoStatus,
  needsAiNotice,
  registerAiServiceAccess,
} from "../../availability.js";
import { can, currentMe, sessionStatus } from "../production/session.js";
import { assistantHtml, createAssistantController } from "./assistant.js";
import { connectionHtml, createConnectionController } from "./connection.js";
import { createCopyController, copyHtml } from "./copy.js";
import { createRcaController, rcaHtml } from "./rca.js";
import { CONNECTION_HREF } from "./gdom.js";

const assistantControllers = new Map();
const rcaControllers = new Map();
const copyControllers = new Map();
let connectionController = null;

/** Only an Admin may test (or change) the AWS connection (`api/access_policy.py`). */
const mayConnect = () => can("POST", "/connection/aws/test");
registerAiServiceAccess(mayConnect);

/** The signed-in person's name for the approval record; `null` with sign-in off or nobody known. */
function signedInName() {
  if (sessionStatus() !== "signed-in") return null;
  const me = currentMe();
  const principal = me && me.principal;
  return (principal && (principal.display_name || principal.username)) || null;
}

// --- painting --------------------------------------------------------------------------------------

let paintedHash = null;

/** Replace the screen; scroll to the top only when the route changed, never on a repaint. */
function paint(app, html, after) {
  app.innerHTML = html;
  if (after) after();
  const hash = window.location.hash;
  if (hash !== paintedHash) window.scrollTo(0, 0);
  paintedHash = hash;
}

/** Grey blocks at once on a route change; a repaint of the same route keeps what is on screen. */
function loading(app, kind, title) {
  if (window.location.hash === paintedHash) return;
  paint(app, skeleton(kind, { title }));
}

/**
 * A screen that could not be drawn. A run that is not there goes back to its use case, by name; a use
 * case that is not there goes Home. Anything else leads with a plain sentence and "Try again".
 */
function failure(app, error, uc) {
  const api = error instanceof ApiError ? error : new ApiError(0, "UI_ERROR", String(error), null);
  const back = uc ? { label: uc.name, href: `#/uc/${encodeURIComponent(uc.id)}` } : journeyBack(null);
  const trail = uc ? crumbs([uc.journey, back, { label: "Not found" }]) : crumbs([{ label: "Not found" }]);
  if (api.status === 404 || api.code === "UI_ROUTE") {
    const what = /RUN/.test(api.code) ? "run" : /INDEX/.test(api.code) ? "assistant" : uc ? "page" : "use case";
    paint(
      app,
      `<main class="screen">${pageHead(
        `${trail}<h1 class="h1">${esc(what[0].toUpperCase() + what.slice(1))} not found</h1>`,
      )}${notFound(what, back, api)}</main>`,
    );
    return;
  }
  paint(
    app,
    `<main class="screen">${pageHead(
      `${uc ? backLink(uc) : crumbs([])}<h1 class="h1">This screen could not be loaded</h1>`,
    )}${errorBox(api, { retry: true })}<div class="btn-row next"><a class="btn quiet" href="${esc(back.href)}">Back to ${esc(
      back.label,
    )}</a></div></main>`,
  );
}

/** The use case plus the journey its back link and breadcrumb return to, as `app.js` resolves it. */
async function useCase(useCaseId) {
  const [uc, payload] = await Promise.all([getUseCase(useCaseId), getIndustries()]);
  const journey = journeyFor(payload, uc.id);
  return journey ? { ...uc, journey } : uc;
}

// --- the screens -----------------------------------------------------------------------------------

async function renderAssistant(app, uc, indexId) {
  const useCaseId = uc.id;
  if (!assistantControllers.has(useCaseId)) {
    assistantControllers.set(
      useCaseId,
      createAssistantController(uc, () => {
        const c = assistantControllers.get(useCaseId);
        paint(app, assistantHtml(uc, c.state), () => c.bind(app));
      }),
    );
  }
  const controller = assistantControllers.get(useCaseId);
  controller.stop();
  await controller.refreshIndexes();
  if (indexId) {
    await controller.loadIndex(indexId);
  } else {
    controller.state.view = "setup";
  }
  paint(app, assistantHtml(uc, controller.state), () => controller.bind(app));
  if (controller.state.view === "running") controller.poll();
}

async function renderRca(app, uc, runId) {
  const key = runId;
  if (!rcaControllers.has(key)) {
    rcaControllers.set(
      key,
      createRcaController(uc, runId, () => {
        const c = rcaControllers.get(key);
        paint(app, rcaHtml(uc, c.state), () => c.bind(app));
      }),
    );
  }
  const controller = rcaControllers.get(key);
  if (controller.state.run) paint(app, rcaHtml(uc, controller.state), () => controller.bind(app));
  await controller.load();
  paint(app, rcaHtml(uc, controller.state), () => controller.bind(app));
}

async function renderConnection(app) {
  if (!connectionController) {
    connectionController = createConnectionController(() => {
      paint(app, connectionHtml(connectionController.state), () => connectionController.bind(app));
    });
  }
  const controller = connectionController;
  paint(app, connectionHtml(controller.state), () => controller.bind(app));
  await controller.load();
  paint(app, connectionHtml(controller.state), () => controller.bind(app));
}

async function renderCopy(app, uc, runId) {
  const key = runId;
  if (!copyControllers.has(key)) {
    copyControllers.set(
      key,
      createCopyController(
        uc,
        runId,
        () => {
          const c = copyControllers.get(key);
          paint(app, copyHtml(uc, c.state), () => c.bind(app));
        },
        { signedInName },
      ),
    );
  }
  const controller = copyControllers.get(key);
  if (controller.state.run) paint(app, copyHtml(uc, controller.state), () => controller.bind(app));
  await controller.load();
  paint(app, copyHtml(uc, controller.state), () => controller.bind(app));
}

const TITLES = { assistant: "AI assistant", rca: "Root-cause notes", copy: "Campaign copy" };

registerModule({
  name: "generative",
  routes: ["generative"],
  async render(app, parts) {
    // parts[0] is always "generative"; parts[1] picks the screen.
    const [, kind, useCaseId, thirdSegment] = parts;
    let uc = null;
    try {
      if (kind === "connection") {
        loading(app, "form", "AI service connection");
        await renderConnection(app);
        document.title = "AI service connection · Marketing AI";
        return;
      }
      const known = (kind === "assistant" && useCaseId) || ((kind === "rca" || kind === "copy") && useCaseId && thirdSegment);
      if (!known) throw new ApiError(404, "UI_ROUTE", `No screen matches #/${parts.join("/")}`, null);
      loading(app, kind === "assistant" ? "form" : "page", TITLES[kind]);
      uc = await useCase(useCaseId);
      // In a demo with no AI service these screens would show the fake backend's stand-in text:
      // one notice instead (DEC-954). The connection screen stays, since it is how one connects.
      if (await needsAiNotice(uc)) {
        paint(app, aiNoticeHtml(uc, backLink(uc)));
        document.title = `${uc.name} · Marketing AI`;
        return;
      }
      if (kind === "assistant") {
        await renderAssistant(app, uc, thirdSegment || null);
      } else if (kind === "rca") {
        await renderRca(app, uc, thirdSegment);
      } else {
        await renderCopy(app, uc, thirdSegment);
      }
      document.title = `${TITLES[kind]} · ${uc.name} · Marketing AI`;
    } catch (error) {
      failure(app, error, uc);
    }
  },
});

// --- the top bar and the run results (router seams, v1 UI) -------------------------------------------

registerNavSlot("admin", {
  html: () => (mayConnect() ? `<a href="${CONNECTION_HREF}">AI service</a>` : ""),
});

const kindOf = (uc) => uc && uc.config && uc.config.generative && uc.config.generative.kind;
const finished = (run) => Boolean(run) && (!run.state || run.state === "done");
const runHref = (screen, uc, run) =>
  `#/generative/${screen}/${encodeURIComponent(uc.id)}/${encodeURIComponent(run.run_id)}`;

// Registered once `GET /pilot/demo` has answered, so `applies` can decide at once and never offers an
// action that would open the demo's switched-off notice.
demoStatus().then(() => {
  registerRunAction({
    name: "generative.root-cause-notes",
    applies: (uc, run) => kindOf(uc) === "root_cause_summary" && finished(run) && aiWritingAvailableNow(uc),
    html: (uc, run) => `<a class="btn secondary" href="${runHref("rca", uc, run)}">Write root-cause notes</a>`,
  });
  registerRunAction({
    name: "generative.campaign-copy",
    applies: (uc, run) =>
      kindOf(uc) === "campaign_copy" && finished(run) && run.mode === "score" && aiWritingAvailableNow(uc),
    html: (uc, run) => `<a class="btn secondary" href="${runHref("copy", uc, run)}">Write campaign copy</a>`,
  });
});
