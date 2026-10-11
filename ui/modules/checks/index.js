// Plan L follow-up (DEC-1599): the entry point of "Things we checked", loaded by `ui/index.html`'s PLAN-L block.
// It adds a page panel to a propensity training run's Model page (`registerPagePanel`, the router's seam), so
// no Phase 1 file imports it: the panel draws an empty placeholder, asks `GET /runs/{id}/checks` once, and
// fills itself in place. With this module absent, or the request failing, the page is drawn as before.

import { API_BASE } from "../../api.js";
import { registerPagePanel } from "../router.js";
import { checksPanelHtml } from "./panel.js";

const views = new Map();

function repaint(runId) {
  if (typeof document === "undefined") return;
  const el = document.querySelector(`[data-checks-panel="${encodeURIComponent(runId)}"]`);
  if (el) el.innerHTML = checksPanelHtml(views.get(runId));
}

async function load(runId) {
  if (views.has(runId)) return;
  views.set(runId, null);
  try {
    const response = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/checks`);
    views.set(runId, response.ok ? await response.json() : null);
  } catch {
    views.set(runId, null); // no card is better than a card that says nothing
  }
  repaint(runId);
}

function panelHtml(kind, uc, run) {
  load(run.run_id);
  return `<div data-checks-panel="${encodeURIComponent(run.run_id)}">${checksPanelHtml(views.get(run.run_id))}</div>`;
}

registerPagePanel({
  name: "things_checked",
  applies: (kind, uc, run) =>
    kind === "model" && !!run && !!run.run_id && run.mode === "train" && run.problem_type !== "uplift",
  html: panelHtml,
});
