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
// This file is not imported by anything yet: `ui/index.html`'s Phase-3A markers are where the
// `<script type="module" src="./modules/generative/index.js"></script>` line belongs, and
// `docs/generative-ui-endpoints.md` states that line verbatim rather than this branch editing the
// shared file itself (the same reason `ui/modules/router.js` is not edited here either).

import { getIndustries, getUseCase } from "../../api.js";
import { backLink, errorBox, esc, pageHead } from "../../dom.js";
import { journeyFor } from "../../overview.js";
import { registerModule } from "../router.js";
import { aiNoticeHtml, needsAiNotice } from "../../availability.js";
import { assistantHtml, createAssistantController } from "./assistant.js";
import { connectionHtml, createConnectionController } from "./connection.js";
import { createCopyController, copyHtml } from "./copy.js";
import { createRcaController, rcaHtml } from "./rca.js";

const assistantControllers = new Map();
const rcaControllers = new Map();
const copyControllers = new Map();
let connectionController = null;

function paint(app, html, after) {
  app.innerHTML = html;
  if (after) after();
  window.scrollTo(0, 0);
}

function failure(app, error) {
  paint(
    app,
    `<main class="screen">${pageHead(
      `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">This screen could not be loaded</h1>`,
    )}${errorBox(error)}</main>`,
  );
}

/** The use case plus the journey its back link and breadcrumb return to, as `app.js` resolves it. */
async function useCase(useCaseId) {
  const [uc, payload] = await Promise.all([getUseCase(useCaseId), getIndustries()]);
  const journey = journeyFor(payload, uc.id);
  return journey ? { ...uc, journey } : uc;
}

async function renderAssistant(app, useCaseId, indexId) {
  const uc = await useCase(useCaseId);
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

async function renderRca(app, useCaseId, runId) {
  const uc = await useCase(useCaseId);
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
  paint(app, rcaHtml(uc, controller.state));
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

async function renderCopy(app, useCaseId, runId) {
  const uc = await useCase(useCaseId);
  const key = runId;
  if (!copyControllers.has(key)) {
    copyControllers.set(
      key,
      createCopyController(uc, runId, () => {
        const c = copyControllers.get(key);
        paint(app, copyHtml(uc, c.state), () => c.bind(app));
      }),
    );
  }
  const controller = copyControllers.get(key);
  paint(app, copyHtml(uc, controller.state));
  await controller.load();
  paint(app, copyHtml(uc, controller.state), () => controller.bind(app));
}

registerModule({
  name: "generative",
  routes: ["generative"],
  async render(app, parts) {
    // parts[0] is always "generative"; parts[1] picks the screen.
    const [, kind, useCaseId, thirdSegment] = parts;
    try {
      // In a demo with no AI service these screens would show the fake backend's stand-in text:
      // one notice instead (DEC-954). The connection screen stays, since it is how one connects.
      if (kind !== "connection" && useCaseId) {
        const uc = await useCase(useCaseId);
        if (await needsAiNotice(uc)) {
          paint(app, aiNoticeHtml(uc, backLink(uc)));
          document.title = `${uc.name} · Marketing AI`;
          return;
        }
      }
      if (kind === "connection") {
        await renderConnection(app);
        document.title = "AWS connection · Marketing AI";
        return;
      }
      if (kind === "assistant" && useCaseId) {
        await renderAssistant(app, useCaseId, thirdSegment || null);
        document.title = "AI Assistant · Marketing AI";
        return;
      }
      if (kind === "rca" && useCaseId && thirdSegment) {
        await renderRca(app, useCaseId, thirdSegment);
        document.title = "Root causes · Marketing AI";
        return;
      }
      if (kind === "copy" && useCaseId && thirdSegment) {
        await renderCopy(app, useCaseId, thirdSegment);
        document.title = "Campaign copy · Marketing AI";
        return;
      }
      failure(app, new Error(`No generative screen matches #/${parts.join("/")}`));
    } catch (error) {
      failure(app, error);
    }
  },
});
