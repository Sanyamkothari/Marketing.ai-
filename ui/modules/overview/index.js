// Plan M M175 (DEC-1605): the Overview page, `#/overview`. Importing this file is the only wiring it needs;
// `ui/index.html`'s PLAN-M block is where it is imported.
//
// It claims one route, `overview`, in both layouts: nothing links to it in the classic shell (it is an extra route
// nobody navigates to yet), and the campaign-centred shell (M174) makes it the home. The page asks `GET /overview` once
// and draws what comes back (`page.js`). It reads no layout setting, so it merges in any order with M174.
//
// Where its links lead is resolved when the page is drawn: `#/data` and `#/reports` (M179, M178) when a module has
// claimed them, else the screens that do the same job today (`#/connections`, `#/results`). "+ New campaign" and the
// checklist's "Start your first campaign" lead to `#/campaigns/new`, the wizard of M177: until M177 is merged that
// address reaches Plan J's campaign page, which answers "not found". The page does not hide this; the integrator
// merges M177 with it (DEC-1605).

import { skeleton } from "../../dom.js";
import { announceModulesChanged, phaseModules, registerModule } from "../router.js";
import { getOverview } from "./api.js";
import { DEFAULT_PLACES, injectStyles, overviewHtml } from "./page.js";

export const ROUTES = ["overview"];

const HIDDEN_KEY = "marketing-ai.overview.checklist-hidden";

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

// Only used while storage cannot be read or written: the list is then hidden for this visit and shows again next time.
let hiddenThisVisit = false;

/** Remembered in this browser only; a private window or blocked storage just means the list shows again next visit. */
export function checklistHidden() {
  try {
    return window.localStorage.getItem(HIDDEN_KEY) === "1";
  } catch {
    return hiddenThisVisit;
  }
}

function hideChecklist() {
  try {
    window.localStorage.setItem(HIDDEN_KEY, "1");
  } catch {
    hiddenThisVisit = true;
  }
}

/** Where the page's links lead: the new screens when a module has claimed them, else today's equivalents. */
export function placesNow() {
  const claimed = (route) => phaseModules().some((m) => m.routes.includes(route));
  return {
    ...DEFAULT_PLACES,
    data: claimed("data") ? DEFAULT_PLACES.data : "#/connections",
    reports: claimed("reports") ? DEFAULT_PLACES.reports : "#/results",
  };
}

let paintSeq = 0;
let shown = null; // the last answer drawn, so "Hide this list" repaints without asking again
const wired = new WeakSet();

function paintInto(app, state) {
  app.innerHTML = overviewHtml({ ...state, places: placesNow(), checklistHidden: checklistHidden() });
}

/** One click handler on the page element, which survives repaints. */
function wire(app) {
  if (wired.has(app)) return;
  wired.add(app);
  app.addEventListener("click", (event) => {
    const target = event.target && typeof event.target.closest === "function" ? event.target.closest("[data-checklist-hide]") : null;
    if (!target || !shown || hashParts()[0] !== "overview") return;
    hideChecklist();
    paintInto(app, { overview: shown });
    document.title = "Overview · Marketing AI";
  });
}

async function renderOverview(app) {
  const seq = (paintSeq += 1);
  wire(app);
  if (!app.querySelector('[data-module="overview"]')) app.innerHTML = skeleton("page", { title: "Overview" });
  let state;
  try {
    state = { overview: await getOverview() };
    shown = state.overview;
  } catch (error) {
    shown = null;
    state = { error };
  }
  // Paint only while the route still names this page and no newer paint has started.
  if (seq !== paintSeq || hashParts()[0] !== "overview") return;
  paintInto(app, state);
  document.title = "Overview · Marketing AI";
}

registerModule({
  name: "overview",
  routes: ROUTES,
  render: async (app) => {
    injectStyles();
    await renderOverview(app);
  },
});

// A deep link to this page was resolved by `app.js` before this module existed: draw it now.
if (ROUTES.includes(hashParts()[0])) announceModulesChanged();
