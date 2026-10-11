// "Add results from the campaign page" (Plan M M176, DEC-1606): the line the four older places that add a campaign's
// results draw, pointing to the one place Plan M keeps for it, the campaign's Results tab. Those places are not
// removed in this milestone: they are kept in both layouts, and M174 keeps their addresses.
//
// On Plan J's campaign page the campaign is known, so the line is a plain link (`campaignLineHtml`). On a
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

export const RESULT_LINE_LABEL = "See the result on the campaign page";

/** The line for a known campaign. `label` is what the Results tab offers there (see `campaignLineHtml`). */
export const campaignResultsLineHtml = (campaignId, label = RESULTS_LINE_LABEL) =>
  `<p class="to-campaign" data-campaign-results-line><a href="${esc(campaignTabHref(campaignId, "results"))}">${esc(
    label,
  )} ›</a></p>`;

/**
 * The line for a campaign record, worded for what its Results tab holds: "Add results ..." while results can be
 * added there (our own campaign, not yet measured), "See the result ..." once it is measured, and nothing for a
 * campaign whose Results tab adds nothing (another tool's, or a programme readout: no goal of ours).
 */
export function campaignLineHtml(campaign) {
  if (!campaign || !campaign.campaign_id) return "";
  if (campaign.status === "measured") return campaignResultsLineHtml(campaign.campaign_id, RESULT_LINE_LABEL);
  return campaign.use_case_id ? campaignResultsLineHtml(campaign.campaign_id) : "";
}

const byRun = new Map(); // run id -> { campaign: the newest campaign record | null, pending: Promise | null }

const holder = (runId) => `[data-campaign-results-for="${String(runId).replace(/["\\]/g, "\\$&")}"]`;

function fill(runId) {
  if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  const known = byRun.get(runId);
  if (!known || known.pending) return;
  for (const el of document.querySelectorAll(holder(runId))) {
    el.innerHTML = campaignLineHtml(known.campaign);
  }
}

function lookUp(runId) {
  if (byRun.has(runId)) return;
  const entry = { campaign: null, pending: null };
  byRun.set(runId, entry);
  entry.pending = campaignsForRun(runId)
    .then((body) => {
      const campaigns = (body && body.campaigns) || [];
      entry.campaign = campaigns.length ? campaigns[0] : null; // newest first
    })
    .catch(() => {
      entry.campaign = null; // no line is better than a line to nowhere
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
  const inner = known && !known.pending ? campaignLineHtml(known.campaign) : "";
  return `<div data-campaign-results-for="${esc(runId)}">${inner}</div>`;
}

/** Forget what was looked up (tests, and after a campaign is recorded). */
export function resetRunResultsLines() {
  byRun.clear();
}
