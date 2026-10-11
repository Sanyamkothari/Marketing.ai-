// Plan H's entry point (M82; PARALLEL_WORK_PROTOCOL.md §4, the PLAN-G block Plan H shares): importing
// this file is the only wiring the four-page product needs, and `ui/index.html`'s PLAN-G block is
// where it is imported.
//
// It registers two screens - Results (`#/results`) and Settings (`#/settings`), drawn by `pages.js` -
// and the links under a run's results (`registerResultLink`): Model health, the Schedule and the
// report, which used to be top-level menu items. The top bar's four places are `ui/chrome.js`'s.
// Plan M M173 adds the Help pages (`#/help/glossary`, `#/help/whats-new`, drawn by `help.js`) and their
// two entries in the Help (?) menu.

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
import { engineVersion, getAllRuns, getCampaigns, getProofs, getSummary, waitingForApproval } from "./api.js";
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
  const [answer, waiting, lists, proofs, summary, campaigns] = await Promise.all([
    getAllRuns().then(
      (body) => ({ runs: (body && body.runs) || [] }),
      (error) => ({ error }),
    ),
    waitingForApproval(),
    resultsListsHtml(), // Plan J M94: the campaigns beside the runs
    getProofs(), // Plan J M104: the Value Proof Packs that are ready
    getSummary(), // Plan J M105: what needs attention and the value proven to date
    getCampaigns(), // Plan M M172: the campaigns' short names (goal and month)
  ]);
  // Plan J M103: a person who may audit a campaign another tool ran gets the way to it, beside the runs.
  const auditHref = canAccess("POST", "/campaigns/audit") ? "#/audit" : null;
  paint(resultsHtml({ ...answer, waiting, lists, auditHref, proofs, summary, campaigns }));
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
    if (parts[0] === "settings") await renderSettings(app, parts);
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

registerResultLink({
  name: "report",
  applies: (uc, run) => finished(run) && Boolean(uc && uc.id) && uc.ai_type !== "generative",
  link: (uc, run) =>
    run.mode === "score"
      ? { label: "Campaign value report", href: `#/pilot/value/${encodeURIComponent(run.run_id)}` }
      : // Plan M M180 (DEC-1610): the model results report is technical; the campaign value report is not.
        { label: "Results report", href: `#/pilot/view/results/${encodeURIComponent(uc.id)}`, technical: true },
});

injectStyles();
// A deep link to one of these pages was resolved by `app.js` before this module existed: draw it now.
if (ROUTES.includes(hashParts()[0])) announceModulesChanged();
