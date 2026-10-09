// Plan H's entry point (M82; PARALLEL_WORK_PROTOCOL.md §4, the PLAN-G block Plan H shares): importing
// this file is the only wiring the four-page product needs, and `ui/index.html`'s PLAN-G block is
// where it is imported.
//
// It registers two screens - Results (`#/results`) and Settings (`#/settings`), drawn by `pages.js` -
// and the links under a run's results (`registerResultLink`): Model health, the Schedule and the
// report, which used to be top-level menu items. The top bar's four places are `ui/chrome.js`'s.

import { accessStatus } from "../../chrome.js";
import { skeleton } from "../../dom.js";
import { announceModulesChanged, canAccess, registerModule, registerResultLink, resultsListsHtml } from "../router.js";
import { engineVersion, getAllRuns, getProofs, waitingForApproval } from "./api.js";
import { injectStyles, resultsHtml, settingsHtml } from "./pages.js";

export const ROUTES = ["results", "settings"];

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
  const [answer, waiting, lists, proofs] = await Promise.all([
    getAllRuns().then(
      (body) => ({ runs: (body && body.runs) || [] }),
      (error) => ({ error }),
    ),
    waitingForApproval(),
    resultsListsHtml(), // Plan J M94: the campaigns beside the runs
    getProofs(), // Plan J M104: the Value Proof Packs that are ready
  ]);
  // Plan J M103: a person who may audit a campaign another tool ran gets the way to it, beside the runs.
  const auditHref = canAccess("POST", "/campaigns/audit") ? "#/audit" : null;
  paint(resultsHtml({ ...answer, waiting, lists, auditHref, proofs }));
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

registerModule({
  name: "simple",
  routes: ROUTES,
  render: async (app, parts) => {
    injectStyles();
    if (parts[0] === "settings") await renderSettings(app, parts);
    else await renderResults(app, parts);
  },
});

// --- under a run's results: the screens that used to be menu items ---------------------------------

const finished = (run) => Boolean(run) && run.state === "done";

registerResultLink({
  name: "model-health",
  applies: (uc, run) => finished(run) && canAccess("GET", "/schedules"),
  link: () => ({ label: "Model health", href: "#/monitoring/alerts" }),
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
      : { label: "Results report", href: `#/pilot/view/results/${encodeURIComponent(uc.id)}` },
});

injectStyles();
// A deep link to one of these pages was resolved by `app.js` before this module existed: draw it now.
if (ROUTES.includes(hashParts()[0])) announceModulesChanged();
