// "Add results from the campaign page" (Plan M M176, DEC-1606): the line the four older places that add a campaign's
// results draw, pointing to the one place Plan M keeps for it, the campaign's Results tab. Those places are not
// removed in this milestone (the campaigns layout redirects them in M174; the classic layout keeps them as they are).
//
// On Plan J's campaign page the campaign is known, so the line is a plain link (`campaignResultsLineHtml`). On a
// scoring run's pages the campaign made from the run's list is looked up once per run (`GET /campaigns?run_id=`) and
// the line is drawn only when there is one: a run whose list never became a campaign has no Results tab to send
// anyone to. The answer is kept per run, so a repaint draws the line at once, and a placeholder is filled in place
// when the answer arrives (no screen needs to repaint for it).

import { esc } from "../../dom.js";

// `./api.js` is imported when a line is first asked for, not with this file: the screens that import this file are
// also drawn by pure-view tests in node, where the API module (which reads `window` as it loads) has no page.
const campaignsForRun = (runId) => import("./api.js").then((api) => api.getCampaignsForRun(runId));

/** The campaign page's tab, `#/campaigns/<id>/<tab>`. */
export const campaignTabHref = (campaignId, tab = "overview") => `#/campaigns/${encodeURIComponent(campaignId)}/${tab}`;

export const RESULTS_LINE_LABEL = "Add results from the campaign page";

/** The line for a known campaign. */
export const campaignResultsLineHtml = (campaignId) =>
  `<p class="to-campaign" data-campaign-results-line><a href="${esc(campaignTabHref(campaignId, "results"))}">${esc(
    RESULTS_LINE_LABEL,
  )} ›</a></p>`;

const byRun = new Map(); // run id -> { campaignId: string | null, pending: Promise | null }

const holder = (runId) => `[data-campaign-results-for="${String(runId).replace(/["\\]/g, "\\$&")}"]`;

function fill(runId) {
  if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  const known = byRun.get(runId);
  if (!known || known.pending) return;
  for (const el of document.querySelectorAll(holder(runId))) {
    el.innerHTML = known.campaignId ? campaignResultsLineHtml(known.campaignId) : "";
  }
}

function lookUp(runId) {
  if (byRun.has(runId)) return;
  const entry = { campaignId: null, pending: null };
  byRun.set(runId, entry);
  entry.pending = campaignsForRun(runId)
    .then((body) => {
      const campaigns = (body && body.campaigns) || [];
      entry.campaignId = campaigns.length ? campaigns[0].campaign_id : null; // newest first
    })
    .catch(() => {
      entry.campaignId = null; // no line is better than a line to nowhere
    })
    .finally(() => {
      entry.pending = null;
      fill(runId);
    });
}

/**
 * The line for a scoring run's campaign: the link when its campaign is known, an empty placeholder (filled in
 * place) while it is being looked up, and nothing when the run's list is not a campaign.
 */
export function runResultsLineHtml(runId) {
  if (!runId) return "";
  lookUp(runId);
  const known = byRun.get(runId);
  const inner = known && !known.pending && known.campaignId ? campaignResultsLineHtml(known.campaignId) : "";
  return `<div data-campaign-results-for="${esc(runId)}">${inner}</div>`;
}

/** Forget what was looked up (tests, and after a campaign is recorded). */
export function resetRunResultsLines() {
  byRun.clear();
}
