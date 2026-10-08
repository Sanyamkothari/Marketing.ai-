// The campaign screens (Plan J M94, DEC-1304 (k)): the campaigns list Results draws beside its runs,
// and one campaign's page - its measured result and the "Plan the test" card.
//
// A result read before the test plan's analysis date is an *early look*: it is labelled so, with the
// report's own sentence, and with no verdict - the server sends none, and nothing here makes one up.
// Every builder is pure: what it draws comes from its arguments (the API's own answers).

import { crumbs, dataTable, EM_DASH, esc, fmtDate, fmtInt, fmtMoney, pageHead, RESULTS_CRUMB, sortNote } from "../../dom.js";
import { planCardHtml } from "./plan.js";

/** A campaign's own page. */
export const campaignHref = (campaign) => `#/campaigns/${encodeURIComponent(campaign.campaign_id)}`;

const STATUS = { live: "Waiting for outcomes", measured: "Measured" };

/** What a campaign's status means, in words. */
export const statusText = (campaign) => STATUS[campaign.status] || String(campaign.status || EM_DASH);

/** The list Results draws beside its runs; `""` when there is no campaign (nothing is drawn). */
export function campaignsListHtml(body) {
  const campaigns = (body && body.campaigns) || [];
  if (!campaigns.length) return "";
  const rows = campaigns.map((campaign) => [
    `<a href="${esc(campaignHref(campaign))}" data-campaign="${esc(campaign.campaign_id)}">${esc(campaign.name)}</a>`,
    esc(fmtDate(campaign.treatment_start)),
    esc(statusText(campaign)),
    esc(fmtInt((campaign.counts && campaign.counts.intended) || 0)),
  ]);
  const cols = [{ label: "Campaign" }, { label: "Sent" }, { label: "Status" }, { label: "Customers measured", num: true }];
  return `<section class="card" data-campaigns><h3>Campaigns${sortNote("newest first")}</h3>${dataTable(cols, rows, {
    cls: "sp-runs",
  })}</section>`;
}

/** The measured result, an early look, or what is still missing. */
function resultCard(view, { canMeasure, busy, error }) {
  const { campaign, report, verdict, plan } = view;
  const measure =
    canMeasure && campaign.outcomes
      ? `<div class="dc-actions"><button type="button" class="btn secondary" data-measure${busy ? " disabled" : ""}>${
          busy ? "Measuring…" : "Measure now"
        }</button></div>`
      : "";
  const problem = error ? `<p class="dc-error" role="alert" data-measure-error>${esc(error.message || String(error))}</p>` : "";
  let body;
  if (!report) {
    const missing = campaign.outcomes
      ? `The outcomes file (${esc(campaign.outcomes.file_name)}) is in; the campaign has not been measured yet.`
      : "Not measured yet: the campaign's outcomes have not been added.";
    body = `<p class="dc-text" data-result-none>${missing}</p>`;
  } else if (report.early_look) {
    const when = plan ? ` The planned analysis date is ${esc(fmtDate(plan.analysis_date))}.` : "";
    body = `<p><span class="chip neutral" data-early-look>Early look</span> Not a final result.${when}</p><p class="dc-text">${esc(
      report.summary,
    )}</p>`;
  } else {
    const headline = verdict ? `<p class="dc-headline" data-verdict="${esc(verdict.kind)}">${esc(verdict.headline)}</p>` : "";
    const detail = verdict ? `<p class="dc-text">${esc(verdict.detail)}</p>` : "";
    body = `${headline}${detail}<p class="dc-text" data-result-summary>${esc(report.summary)}</p>`;
  }
  return `<section class="card dc-card" data-campaign-result><h3>Result</h3>${body}${problem}${measure}</section>`;
}

/**
 * One campaign's page. `view` is `GET /campaigns/{id}`, `plans` is `GET /campaigns/{id}/plan`,
 * `can(method, path)` what this person may do, `busy` / `measureError` / `planError` the screen's state,
 * `preview` / `previewIndex` the plan preview (`GET /campaigns/{id}/plan-preview`) and its slider's stop.
 */
export function campaignPageHtml({
  view,
  plans = null,
  can = () => true,
  busy = false,
  measureError = null,
  planError = null,
  preview = null,
  previewIndex = null,
}) {
  const { campaign } = view;
  const counts = campaign.counts || {};
  const run = (campaign.run_ids || [])[0];
  const head = pageHead(
    `${crumbs([RESULTS_CRUMB, { label: campaign.name }])}<h1 class="h1">${esc(campaign.name)}</h1><p class="desc">Sent on ${esc(
      fmtDate(campaign.treatment_start),
    )}${run ? ` from the list of run <span class="mono">${esc(run)}</span>` : ""}. ${esc(fmtInt(counts.intended_treated || 0))} contacted and ${esc(
      fmtInt(counts.intended_holdout || 0),
    )} held back are compared.</p>`,
  );
  const plan = view.plan || (plans && plans.plan) || null;
  const card = planCardHtml({
    campaign,
    plan,
    versions: (plans && plans.versions) || [],
    canRegister: can("POST", "/campaigns/{campaign_id}/plan"),
    error: planError,
    preview,
    previewIndex,
  });
  const result = resultCard({ ...view, plan }, { canMeasure: can("POST", "/campaigns/{campaign_id}/measure"), busy, error: measureError });
  return `<main class="screen dc" data-module="decide" data-campaign-page="${esc(campaign.campaign_id)}">${head}<div class="dc-stack">${result}${card}</div></main>`;
}

/**
 * The treat list card on a scoring run's Output page (Plan J M98, DEC-1308): the counts of
 * `treat_list_summary.json` in the server's own words, the one-line instruction, and the download.
 * `error` is what the summary request answered when it failed (an ApiError or any `{ message }`): the
 * card then says so in the server's words instead of drawing nothing. `summary` null with no error is
 * the moment before the answer arrives.
 *
 * The file this card downloads is the treat list. The Output page's own "Download contact list" is the
 * scored list (`scores.csv`), which still holds the held-back customers and the ones not to contact.
 */
export function treatListCardHtml(summary, { treatListHref = "", error = null } = {}) {
  if (error) {
    const said = error.message ? esc(error.message) : "The server gave no reason.";
    return `<div class="dc"><section class="card dc-card" data-treat-list-card data-treat-list-error>
      <h3>Treat list</h3>
      <p class="dc-error" role="alert">The treat list could not be loaded. ${said}</p>
    </section></div>`;
  }
  if (!summary) return "";
  const count = (n) => (n === null || n === undefined ? EM_DASH : fmtInt(n));
  const money = (n) => (n === null || n === undefined ? null : fmtMoney(n));
  const row = (label, value) =>
    `<div class="dc-kv"><span class="dc-k">${esc(label)}</span><span class="dc-v">${esc(value)}</span></div>`;
  const netValue = money(summary.net_value_total);
  const grossValue = money(summary.expected_gross_value_total);
  const notes = [summary.holdout_note, summary.net_value_note, summary.expected_gross_value_note].filter(Boolean);
  const notesHtml = notes.length
    ? `<div class="dc-note" data-treat-list-notes>${notes.map((n) => `<p>${esc(n)}</p>`).join("")}</div>`
    : "";
  const downloadBtn = treatListHref
    ? `<div class="dc-actions">
         <a class="btn primary" href="${esc(treatListHref)}" download data-download="treat-list">Download treat list (CSV)</a>
       </div>`
    : "";
  return `<div class="dc"><section class="card dc-card" data-treat-list-card>
    <h3>Treat list</h3>
    <p class="dc-text">The file to give your sending tool. Include treat = 1, exclude holdout = 1. The contact list above is the scored list before that choice.</p>
    <div class="dc-kvs">
      ${row("Customers", count(summary.total_rows))}
      ${row("To treat (treat = 1)", count(summary.treat_rows))}
      ${row("Held back (holdout = 1)", count(summary.holdout_rows))}
      ${row("Explore (explore = 1)", count(summary.explore_rows))}
      ${row("Not to be contacted", count(summary.suppressed_rows))}
      ${netValue === null ? "" : row("Predicted net value of those treated", netValue)}
      ${grossValue === null ? "" : row("Expected gross value of those treated (not incremental)", grossValue)}
    </div>
    ${notesHtml}
    ${downloadBtn}
  </section></div>`;
}

const CSS = `
.dc .dc-stack{display:flex;flex-direction:column;gap:16px}
.dc .dc-card h3{margin:0}
.dc .dc-card>p,.dc .dc-card>div,.dc .dc-card>form{padding:0 20px}
.dc .dc-card>p:first-of-type{margin-top:12px}
.dc .dc-card>:last-child{padding-bottom:16px}
.dc .dc-headline{font-size:20px;font-weight:600;color:var(--ink);margin:12px 0 4px}
.dc .dc-text{font-size:13px;color:var(--ink2);line-height:1.5}
.dc .dc-note{font-size:12px;color:var(--muted)}
.dc .dc-warn{font-size:13px;color:var(--ink);background:var(--soft);border-left:3px solid var(--brand-yellow);padding:8px 12px}
.dc .dc-error{font-size:13px;color:var(--bad, var(--ink))}
.dc .dc-kvs{display:grid;grid-template-columns:minmax(0,1fr);gap:0;margin-top:8px}
.dc .dc-kv{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-top:1px solid var(--line);font-size:13px}
.dc .dc-kv:first-child{border-top:0}
.dc .dc-k{color:var(--muted)}
.dc .dc-v{color:var(--ink);text-align:right}
.dc .dc-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:8px}
.dc .dc-form label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--ink2)}
.dc .dc-actions{display:flex;gap:8px;margin-top:8px}
.dc .dc-preview{margin-top:12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.dc .dc-preview input[type=range]{width:100%;margin:8px 0}
.dc .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("dc-styles")) return;
  const style = doc.createElement("style");
  style.id = "dc-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
