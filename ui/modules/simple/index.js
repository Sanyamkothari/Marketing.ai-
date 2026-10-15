// Plan H's entry point (M82; PARALLEL_WORK_PROTOCOL.md §4, the PLAN-G block Plan H shares): importing
// this file is the only wiring the four-page product needs, and `ui/index.html`'s PLAN-G block is
// where it is imported.
//
// It registers two screens - Results (`#/results`) and Settings (`#/settings`), drawn by `pages.js` -
// and the links under a run's results (`registerResultLink`): Model health, the Schedule and the
// report, which used to be top-level menu items. The top bar's four places are `ui/chrome.js`'s.
// Plan M M173 adds the Help pages (`#/help/glossary`, `#/help/whats-new`, drawn by `help.js`) and their
// two entries in the Help (?) menu.
// Plan M M192 (DEC-1622) adds Settings › AI service (`#/settings/ai`, drawn by `ai_service.js`): the two AI services in
// plain words, each with a direct button to its key form.

import { accessStatus } from "../../chrome.js";
import { skeleton } from "../../dom.js";
import {
  announceModulesChanged,
  canAccess,
  registerModule,
  registerNavSlot,
  registerResultLink,
  resultsListsHtml,
} from "../router.js";
import {
  engineVersion,
  getAiServiceState,
  getAllRuns,
  getCampaigns,
  getSummary,
  waitingForApproval,
} from "./api.js";
import { aiServiceHtml } from "./ai_service.js";
import { glossaryEntry, glossaryHtml, loadWhatsNew, whatsNewEntry, whatsNewHtml } from "./help.js";
import { injectStyles, resultsHtml, settingsHtml } from "./pages.js";

export const ROUTES = ["results", "settings", "help"];

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

let paintSeq = 0;

/** Paint only while the route still names this screen and no newer paint has started. */
function painter(app, parts) {
  const seq = (paintSeq += 1);
  return (html) => {
    if (seq !== paintSeq || hashParts()[0] !== parts[0]) return;
    app.innerHTML = html;
  };
}

async function renderResults(app, parts) {
  const paint = painter(app, parts);
  if (!app.querySelector('[data-module="simple"]')) paint(skeleton("list", { title: "Results" }));
  const [answer, waiting, lists, summary, campaigns] = await Promise.all([
    getAllRuns().then(
      (body) => ({ runs: (body && body.runs) || [] }),
      (error) => ({ error }),
    ),
    waitingForApproval(),
    resultsListsHtml(), // Plan J M94: the campaigns beside the runs
    getSummary(), // Plan J M105: what needs attention and the value proven to date
    getCampaigns(), // Plan M M172: the campaigns' short names (goal and month)
  ]);
  // Plan J M103: a person who may audit a campaign another tool ran gets the way to it, beside the runs.
  const auditHref = canAccess("POST", "/campaigns/audit") ? "#/audit" : null;
  paint(resultsHtml({ ...answer, waiting, lists, auditHref, summary, campaigns }));
  document.title = "Results · Marketing AI";
}

async function renderSettings(app, parts) {
  const paint = painter(app, parts);
  const draw = (version) => settingsHtml({ can: canAccess, status: accessStatus(), version });
  paint(draw(null));
  document.title = "Settings · Marketing AI";
  // Drawn again once the version is in: by then a deep link's sign-in state has settled too.
  paint(draw(await engineVersion()));
}

/**
 * Plan M M192 (DEC-1622): `#/settings/ai`, Settings › AI service. Drawn at once ("Loading…"), then from
 * `GET /ai-service`; a failed call is the error with "Try again"; a person who may not read the AI service gets the
 * page's notice without a call.
 */
async function renderAiSettings(app, parts) {
  const paint = painter(app, parts);
  const can = canAccess;
  document.title = "AI service · Marketing AI";
  paint(aiServiceHtml({ can }));
  if (!can("GET", "/ai-service")) return;
  try {
    paint(aiServiceHtml({ state: await getAiServiceState(), can }));
  } catch (error) {
    paint(aiServiceHtml({ error, can }));
  }
}

/** `#/help/glossary` (also bare `#/help`) and `#/help/whats-new`: the Help menu's two pages (M173). */
async function renderHelp(app, parts) {
  const paint = painter(app, parts);
  if (parts[1] === "whats-new") {
    paint(whatsNewHtml(await loadWhatsNew()));
    document.title = "What's new · Marketing AI";
    return;
  }
  paint(glossaryHtml());
  document.title = "Glossary · Marketing AI";
}

registerModule({
  name: "simple",
  routes: ROUTES,
  render: async (app, parts) => {
    injectStyles();
    if (parts[0] === "settings" && parts[1] === "ai") await renderAiSettings(app, parts); // Plan M M192
    else if (parts[0] === "settings") await renderSettings(app, parts);
    else if (parts[0] === "help") await renderHelp(app, parts);
    else await renderResults(app, parts);
  },
});

// The Help (?) menu's Glossary and What's new, between the tour and feedback that `modules/pilot` adds.
registerNavSlot("help", { html: glossaryEntry, order: 20 });
registerNavSlot("help", { html: whatsNewEntry, order: 40 });

// --- under a run's results: the screens that used to be menu items ---------------------------------

const finished = (run) => Boolean(run) && run.state === "done";

registerResultLink({
  name: "model-health",
  applies: (uc, run) => finished(run) && canAccess("GET", "/schedules"),
  // Plan M M180 (DEC-1610): a model's drift and health alerts are for analysts: hidden with "Show technical details" off.
  link: () => ({ label: "Model health", href: "#/monitoring/alerts", technical: true }),
});

registerResultLink({
  name: "schedule",
  applies: (uc, run) => finished(run) && canAccess("GET", "/schedules"),
  link: () => ({ label: "Schedule", href: "#/monitoring/schedules" }),
});

// The campaign value report (`#/pilot/value/<run>`) is frozen (v1 plan, stage 0): a scored run gets no link to it, so
// no page says "The campaign worked" while Reports and the Overview say nothing is proven. The address still opens.
registerResultLink({
  name: "report",
  applies: (uc, run) => finished(run) && run.mode !== "score" && Boolean(uc && uc.id) && uc.ai_type !== "generative",
  // Plan M M180 (DEC-1610): the model results report is technical.
  link: (uc) => ({ label: "Results report", href: `#/pilot/view/results/${encodeURIComponent(uc.id)}`, technical: true }),
});

injectStyles();
// A deep link to one of these pages was resolved by `app.js` before this module existed: draw it now.
if (ROUTES.includes(hashParts()[0])) announceModulesChanged();
