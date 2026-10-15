// The campaign progress page, `#/campaigns/run/<id>`: where "Start" leaves a person. It asks the server how far the
// campaign run is (`GET /campaign-runs/{id}`) every few seconds and shows its steps in plain names with the time left.
// The work runs on the server, so closing the page loses nothing. When it is done the page leads to the campaign,
// where "Approve and download" stays the person's own decision; a run the server restarted under shows Restart.

import { canAccess } from "../router.js";
import { errorBox } from "../../dom.js";
import { getCampaignRun, restartCampaignRun } from "./wizard_api.js";
import { needsCostYes, runHref } from "./run_rules.js";
import { injectRunStyles, runPageHtml } from "./run_views.js";
import { injectWizardStyles } from "./wizard_styles.js";

export const RUN_POLL_MS = 3000;

const ui = { seq: 0, timer: null, app: null, id: null, view: null, busy: false, error: null };

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
const onPage = (id) => {
  const p = hashParts();
  return p[0] === "campaigns" && p[1] === "run" && decodeURIComponent(p[2] || "") === id;
};

function draw() {
  if (!ui.app || !ui.view || !onPage(ui.id)) return;
  ui.app.innerHTML = runPageHtml(ui.view, {
    canRestart: canAccess("POST", "/campaign-runs/{campaign_run_id}/restart"),
    busy: ui.busy,
    error: ui.error,
  });
}

async function poll(token) {
  clearTimeout(ui.timer);
  try {
    const view = await getCampaignRun(ui.id);
    if (token !== ui.seq || !onPage(ui.id)) return;
    ui.view = view;
    draw();
    if (view.record.state === "running") ui.timer = setTimeout(() => poll(token), RUN_POLL_MS);
  } catch (error) {
    if (token !== ui.seq || !onPage(ui.id)) return;
    if (ui.view) ui.timer = setTimeout(() => poll(token), RUN_POLL_MS); // a blip: keep what is shown, ask again
    else ui.app.innerHTML = errorBox(error, { retry: true });
  }
}

/** Draw the progress page for campaign run `id` and keep it up to date while it is on screen. */
export async function renderRun(app, id) {
  injectWizardStyles();
  injectRunStyles();
  document.title = "Your campaign · Marketing AI";
  clearTimeout(ui.timer);
  ui.seq += 1;
  Object.assign(ui, { app, id, view: ui.id === id ? ui.view : null, busy: false, error: null });
  if (ui.view) draw();
  else app.innerHTML = `<main class="screen cw" data-module="campaign-run"><p class="loading" role="status">Opening your campaign…</p></main>`;
  await poll(ui.seq);
}

/** Start a run that stopped again as a new one and move to its page. */
async function restart() {
  if (ui.busy || !ui.view) return;
  const run = ui.view.record;
  ui.busy = true;
  ui.error = null;
  draw();
  try {
    const created = await restartCampaignRun(run.campaign_run_id, {
      confirmCost: needsCostYes(run),
    });
    ui.busy = false;
    window.location.hash = runHref(created.record.campaign_run_id);
  } catch (error) {
    ui.busy = false;
    ui.error = error;
    draw();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("click", (event) => {
    const target = event.target && event.target.closest ? event.target.closest("[data-run-restart]") : null;
    if (target && ui.view && onPage(ui.id)) restart();
  });
  window.addEventListener("hashchange", () => {
    if (!onPage(ui.id)) {
      clearTimeout(ui.timer);
      ui.seq += 1;
    }
  });
}
