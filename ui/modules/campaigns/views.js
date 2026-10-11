// The Campaigns list and the campaign page (Plan M M176, DEC-1606; Plan M §3.2): every builder is pure, drawing
// only what its arguments hold (the API's own answers), so the node tests can call it directly.
//
// Page rules (Plan M §3.3): one H1, one sentence under it, one primary button per view. Words follow the glossary
// (`ui/labels.js`); anything technical sits in a `[data-technical]` section, which M180 hides by default.

import { EM_DASH, crumbs, dataTable, emptyState, errorBox, esc, fmtInt, headActions, pageHead } from "../../dom.js";
import { count, day, figureHtml, isValue, percent, rupees, rupeesExact } from "../../format.js";
import { term } from "../../labels.js";
import { PAGE_SIZE, pageOf, search, topReasons } from "./audience.js";
import { campaignTabHref } from "./links.js";
import {
  evidenceOf,
  goalName,
  isoDay,
  nextStep,
  resultsDue,
  runElsewhere,
  shortName,
  STATUS,
  wholeProgramme,
} from "./status.js";

export const TABS = Object.freeze(["overview", "audience", "results", "technical"]);

const TAB_LABEL = { overview: "Overview", audience: "Audience", results: "Results", technical: "Technical details" };

export const CAMPAIGNS_CRUMB = Object.freeze({ label: "Campaigns", href: "#/campaigns", place: true });

// --- formatting: ui/format.js (M172), the one number, money and day format ----------------------------------------

/** "12,862 customers" (one customer); the em dash when the count is missing. */
const customers = (n) => (isValue(n) ? `${count(n)} ${Number(n) === 1 ? "customer" : "customers"}` : EM_DASH);

// --- shared pieces --------------------------------------------------------------------------------------------

/** The status chip; `data-status` carries the rule's key for tests and styles. */
export const statusChip = (status) =>
  `<span class="chip cp-status cp-${esc(status.tone)}" data-status="${esc(status.key)}">${esc(status.label)}</span>`;

/** The marks beside a campaign's name: "Sent by another tool" or "Whole programme". */
function tagsHtml(campaign) {
  if (runElsewhere(campaign)) {
    return ` <span class="chip neutral cp-tag" data-run-elsewhere title="This campaign was sent by another tool and checked here.">Sent by another tool</span>`;
  }
  if (wholeProgramme(campaign)) return ` <span class="chip neutral cp-tag" data-programme>Whole programme</span>`;
  return "";
}

const counts = (campaign) => (campaign && campaign.counts) || {};

// --- the list -------------------------------------------------------------------------------------------------

/**
 * One row of the list: `entry` is `{ campaign, view, proof, goal, status }` - `view` is `GET /campaigns/{id}`
 * (null when it failed), `proof` that campaign's line of `GET /pilot/proof` (null when none), `goal` its goal's name.
 */
function listRow(entry) {
  const { campaign, view, proof, goal, status } = entry;
  const name = shortName(campaign, goal);
  const link = `<a class="cp-name" href="${esc(campaignTabHref(campaign.campaign_id))}" data-campaign-link="${esc(
    campaign.campaign_id,
  )}">${esc(name)}</a>${tagsHtml(campaign)}`;
  const subtitle = campaign.name && campaign.name !== name ? `<div class="cp-sub">${esc(campaign.name)}</div>` : "";
  let result;
  if (status.key === "measured") {
    const headline =
      (proof && proof.status === "ready" && proof.headline) || (view && view.verdict && view.verdict.headline) || "";
    result = headline ? `<span data-result>${esc(headline)}</span>` : `<span class="muted">Measured</span>`;
  } else {
    // A Draft has no results date yet: it is fixed with its test plan.
    const due = status.key === "draft" ? null : resultsDue(campaign, view && view.plan);
    result = due
      ? `<span class="muted" data-due>Results due ${esc(day(due))}</span>`
      : `<span class="muted">${EM_DASH}</span>`;
  }
  const goalText = runElsewhere(campaign) || wholeProgramme(campaign) || !campaign.use_case_id ? EM_DASH : goal;
  return [
    `<div class="cp-cell">${link}${subtitle}</div>`,
    esc(goalText),
    statusChip(status),
    esc(fmtInt(counts(campaign).intended_treated || 0)),
    result,
  ];
}

/**
 * The most campaigns `GET /campaigns` answers with (`CAMPAIGNS_SHOWN` in `api/routes/campaigns.py`): a list that
 * long may be cut, so the page says it shows the newest ones rather than every campaign.
 */
export const LIST_CAP = 100;

/** `#/campaigns`: every campaign, newest first, with "+ New campaign". `entries` as `listRow` reads them. */
export function campaignsListHtml({ entries = [], error = null, canCreate = true } = {}) {
  const action = canCreate ? { label: "+ New campaign", href: "#/campaigns/new", attrs: "data-new-campaign" } : null;
  const capped = entries.length >= LIST_CAP;
  const lead = capped
    ? `The newest ${fmtInt(LIST_CAP)} campaigns: where each stands, how many customers it reached and what it earned.`
    : "Every campaign: where it stands, how many customers it reached and what it earned.";
  const head = pageHead(
    `${crumbs([CAMPAIGNS_CRUMB])}<h1 class="h1">Campaigns</h1><p class="desc">${esc(lead)}</p>${
      entries.length ? headActions({ primary: action }) : ""
    }`,
  );
  let body;
  if (error) body = errorBox(error, { retry: true });
  else if (!entries.length) {
    body = `<section class="card" data-campaigns-empty>${emptyState({
      title: "No campaigns yet",
      text: "Start a campaign to choose who to contact, send the contact list and see what it earned.",
      action,
    })}</section>`;
  } else {
    const cols = [
      { label: "Campaign" },
      { label: "Goal" },
      { label: "Status" },
      { label: "Contacted", num: true },
      { label: "Result" },
    ];
    const title = capped ? `The newest ${fmtInt(LIST_CAP)} campaigns` : "All campaigns";
    const older = capped
      ? `<p class="cp-pad muted" data-campaigns-capped>Older campaigns are not listed here.</p>`
      : "";
    body = `<section class="card" data-campaigns-list><h3>${esc(title)} <span class="sort-note">(newest first)</span></h3>${older}${dataTable(
      cols,
      entries.map(listRow),
      { cls: "cp-table" },
    )}</section>`;
  }
  return `<main class="screen cp" data-module="campaigns" data-campaigns-page>${STYLE_TAG}${head}<div class="stack">${body}</div></main>`;
}

// --- the campaign page ----------------------------------------------------------------------------------------

function tabsHtml(campaignId, tab) {
  const links = TABS.map((t) => {
    const on = t === tab;
    const technical = t === "technical" ? " data-technical" : "";
    return `<a class="tab${on ? " on" : ""}" href="${esc(campaignTabHref(campaignId, t))}" data-tab="${t}"${
      on ? ' aria-current="page"' : ""
    }${technical}>${esc(TAB_LABEL[t])}</a>`;
  }).join("");
  return `<div class="tabs-bar"><nav class="tabs" aria-label="Campaign">${links}</nav></div>`;
}

/** A tile: label, value and an optional exact figure for the tooltip. */
const tile = (label, value, { exact = null, attrs = "", html = false } = {}) =>
  `<div class="kpi"${attrs ? ` ${attrs}` : ""}><div class="l">${esc(label)}</div><div class="v"${
    exact ? ` title="${esc(exact)}"` : ""
  }>${html ? value : esc(value)}</div></div>`;

/**
 * The net value line's label, as `engine/pilot/proof.py` writes it: "Net value", or, for a Pack whose claim is
 * `stated_random`, "If the groups were random as you said: net value" (`_conditional`). Nothing else matches.
 */
export const NET_VALUE_LABEL = /(^|: )net value$/i;

/**
 * The Proof Pack's net value range, as its two server figures: `{ low, high }` (each `{value, text, format}`) or null. The line is found
 * by its label only: no line is ever taken by its position, so a Pack without that line shows no range.
 */
export function netValueRange(proof) {
  const section = proof && (proof.sections || []).find((s) => s.key === "net_value");
  const lines = (section && section.lines) || [];
  const line = lines.find((l) => NET_VALUE_LABEL.test(String((l && l.label) || "").trim()));
  if (!line || !line.low || !line.high) return null;
  return { low: line.low, high: line.high };
}

function valueTile(model) {
  const range = netValueRange(model.proof);
  if (range) {
    // Each end drawn by ui/format.js (rupees in lakh and crore), the server's exact text in its tooltip.
    return tile("Net value, likely range", `${figureHtml(range.low)} to ${figureHtml(range.high)}`, {
      attrs: "data-tile-value",
      html: true,
    });
  }
  const summary = model.summary;
  if (summary && summary.net_value_total !== null && summary.net_value_total !== undefined) {
    return tile("Expected value (predicted)", rupees(summary.net_value_total), {
      exact: rupeesExact(summary.net_value_total),
      attrs: "data-tile-value",
    });
  }
  return "";
}

/** The Overview's one primary button, by status. */
export function primaryAction(model) {
  const { campaign, status, contactListHref } = model;
  const id = campaign.campaign_id;
  switch (status.key) {
    case "draft":
      return {
        label: "Plan the test",
        href: `#/campaigns/${encodeURIComponent(id)}/plan`,
        attrs: 'data-primary="plan"',
      };
    case "live":
      return contactListHref
        ? {
            label: "Download contact list",
            href: contactListHref,
            attrs: 'download data-download="contact-list" data-primary="download"',
          }
        : { label: "Add results", href: campaignTabHref(id, "results"), attrs: 'data-primary="results"' };
    case "waiting":
      return { label: "Add results", href: campaignTabHref(id, "results"), attrs: 'data-primary="results"' };
    case "measured":
      return {
        label: "Open the full report",
        href: `#/pilot/proof/${encodeURIComponent(id)}`,
        attrs: 'data-primary="report"',
      };
    default:
      return null;
  }
}

function actionLink(action, kind) {
  return action
    ? `<a class="btn ${kind}" href="${esc(action.href)}" ${action.attrs || ""}>${esc(action.label)}</a>`
    : "";
}

/** "Refresh the list": a secondary action with its confirmation, or what came of it. */
function refreshHtml(model) {
  const r = model.refresh || {};
  if (!model.canRefresh) return "";
  if (r.started) {
    return `<div class="cp-note" role="status" data-refresh-started>A new contact list is being made with the current model. <a href="${esc(
      r.startedHref,
    )}">See its progress ›</a></div>`;
  }
  if (r.asking || r.costMessage) {
    const cost = r.costMessage ? `<p class="cp-warn" data-refresh-cost>${esc(r.costMessage)}</p>` : "";
    const yes = r.costMessage ? "Start it anyway" : "Yes, refresh the list";
    const error = r.error ? errorBox(r.error) : "";
    return `<div class="cp-confirm" role="group" aria-label="Refresh the list" data-refresh-confirm>
      <p>Refresh the list? A new contact list is made from the same customers with the current model. This campaign keeps the list it sent.</p>
      ${cost}<div class="btn-row"><button type="button" class="btn secondary sm" data-refresh-yes${r.busy ? " disabled" : ""}>${
        r.busy ? "Starting…" : esc(yes)
      }</button><button type="button" class="btn quiet sm" data-refresh-no>Keep this list</button></div>${error}</div>`;
  }
  return `<button type="button" class="btn secondary" data-refresh-ask>Refresh the list</button>`;
}

function whatWasSent(model) {
  const { campaign, summary } = model;
  const rows = [];
  rows.push(["Sent on", day(campaign.treatment_start)]);
  const offers = Object.entries((summary && summary.offer_counts) || {});
  for (const [offer, n] of offers) rows.push([`Offer: ${offer}`, customers(n)]);
  const channels = Object.entries((summary && summary.channel_rows) || {});
  for (const [channel, n] of channels) rows.push([`Channel: ${channel}`, customers(n)]);
  if (summary && summary.treat_rows !== null && summary.treat_rows !== undefined) {
    rows.push([term("contactList"), customers(summary.treat_rows)]);
  }
  return `<section class="card" data-what-was-sent><h3>What was sent</h3><div class="cp-kv">${rows
    .map(([k, v]) => `<div class="cp-kv-row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join("")}</div></section>`;
}

function overviewHtml(model) {
  const { campaign, status } = model;
  const c = counts(campaign);
  const step = nextStep(model, day);
  const primary = primaryAction(model);
  const tiles = [
    tile("Contacted", customers(c.intended_treated), { attrs: "data-tile-contacted" }),
    tile(`${term("comparisonGroup")} (not contacted)`, customers(c.intended_holdout), {
      attrs: "data-tile-comparison",
    }),
    valueTile(model),
  ].join("");
  return `<section class="card" data-overview><h3>Where it stands</h3><div class="card-body cp-stand">
      <p class="cp-state">${statusChip(status)}${tagsHtml(campaign)}</p>
      <p class="cp-step" data-next-step>${esc(step)}</p>
      <div class="btn-row">${actionLink(primary, "primary")}${refreshHtml(model)}</div>
    </div></section>
    <div class="kpis cp-kpis">${tiles}</div>
    ${whatWasSent(model)}`;
}

// --- Audience -------------------------------------------------------------------------------------------------

/** The table and pager of the Audience tab: repainted alone while the person types in the search box. */
export function audienceTableHtml(audience) {
  const found = search(audience.list.rows, audience.query);
  const page = pageOf(found, audience.page, PAGE_SIZE);
  // A band is a priority (High, Medium, Low); a campaign-effect group is not, so it is called what it is.
  const group = audience.list.grouping === "segment" ? "Group" : "Priority";
  const cols = [
    { label: "Customer" },
    { label: group },
    { label: "Offer" },
    { label: "Channel" },
    { label: "Top reason" },
  ];
  const rows = page.rows.map((r) => [
    `<span class="mono">${esc(r.key)}</span>`,
    esc(r.priority || EM_DASH),
    esc(r.offer || EM_DASH),
    esc(r.channel || EM_DASH),
    esc(r.reason || EM_DASH),
  ]);
  const table = rows.length
    ? dataTable(cols, rows, { cls: "cp-table cp-audience" })
    : `<p class="cp-pad muted" data-audience-none>No customer on the list has an id containing “${esc(audience.query)}”.</p>`;
  const where = page.total
    ? `Showing ${fmtInt(page.from)} to ${fmtInt(page.to)} of ${customers(page.total)}`
    : "Showing no customers";
  const pager = `<div class="cp-pager" data-pager><button type="button" class="btn secondary sm" data-page="${
    page.page - 1
  }"${page.page <= 1 ? " disabled" : ""}>Previous</button><span data-page-of>${esc(where)} · page ${fmtInt(page.page)} of ${fmtInt(
    page.pages,
  )}</span><button type="button" class="btn secondary sm" data-page="${page.page + 1}"${
    page.page >= page.pages ? " disabled" : ""
  }>Next</button></div>`;
  return `${table}${pager}`;
}

function whyHtml(rows) {
  const top = topReasons(rows, 3);
  if (!top.length) return "";
  return `<section class="card" data-why><h3>Why these customers?</h3><div class="card-body"><p class="muted">The strongest reason behind each customer on the list, counted across the list.</p><ol class="cp-why">${top
    .map(
      (r) =>
        `<li data-reason><span>${esc(r.text)}</span> <span class="muted">for ${esc(fmtInt(r.customers))} of ${esc(
          customers(rows.length),
        )} (${esc(percent(r.share))})</span></li>`,
    )
    .join("")}</ol></div></section>`;
}

function audienceHtml(model) {
  const { audience, contactListHref } = model;
  if (!contactListHref) {
    return `<section class="card" data-audience>${emptyState({
      title: "No contact list here",
      text: "This campaign was not sent from a list made here, so there is no contact list to show.",
    })}</section>`;
  }
  const download = `<a class="btn primary" href="${esc(contactListHref)}" download data-download="contact-list">Download contact list</a>`;
  if (!audience || audience.loading) {
    return `<section class="card" data-audience><h3>${esc(term("contactList"))}</h3><div class="card-body"><p class="muted" role="status">Loading the contact list…</p><div class="btn-row">${download}</div></div></section>`;
  }
  if (audience.error) {
    const refused = Number(audience.error.status) === 403;
    const lead = refused
      ? "Your role can see the campaign but not the customers on its list. Ask an Analyst to download it."
      : null;
    return `<section class="card" data-audience><h3>${esc(term("contactList"))}</h3><div class="card-body">${
      lead ? `<p data-audience-refused>${esc(lead)}</p>` : errorBox(audience.error, { retry: true })
    }</div></section>`;
  }
  const { rows, total } = audience.list;
  // The counts are the list's own summary (`treat_list_summary.json`, the same file's counts) when it answered.
  const summary = model.summary || {};
  const known = (n) => n !== null && n !== undefined;
  const toContact = known(summary.treat_rows) ? summary.treat_rows : rows.length;
  const onList = known(summary.total_rows) ? summary.total_rows : total;
  const lead = `${customers(toContact)} to contact, out of ${customers(onList)} on the list.`;
  return `<section class="card" data-audience><h3>${esc(term("contactList"))}</h3><div class="card-body">
      <p data-audience-lead>${esc(lead)}</p>
      <div class="cp-search"><label for="cp-search">Search by customer id</label><input id="cp-search" type="search" autocomplete="off" data-audience-search value="${esc(
        audience.query || "",
      )}"></div>
    </div><div data-audience-table>${audienceTableHtml(audience)}</div><div class="card-body"><div class="btn-row">${download}</div></div></section>
    ${whyHtml(rows)}`;
}

// --- Results --------------------------------------------------------------------------------------------------

function measuredHtml(model) {
  const { campaign, proof, view } = model;
  const report = `#/pilot/proof/${encodeURIComponent(campaign.campaign_id)}`;
  const headline = (proof && proof.headline) || (view && view.verdict && view.verdict.headline) || "Measured.";
  const evidence = proof ? evidenceOf(proof.claim) : null;
  const label = evidence
    ? `<p class="cp-evidence" data-evidence="${esc(proof.claim)}"><span class="chip neutral">${esc(evidence.label)}</span> ${esc(
        evidence.text,
      )}</p>`
    : "";
  return `<section class="card" data-results-measured><h3>Result</h3><div class="card-body">
      <p class="cp-headline" data-headline>${esc(headline)}</p>${label}
      <p class="muted">Measured on ${esc(day(campaign.measured_at))}.</p>
      <div class="btn-row"><a class="btn primary" href="${esc(report)}" data-open-report>Open the full report</a></div>
    </div></section>`;
}

function uploadHtml(model) {
  const { campaign, plan, results = {}, canAdd, view } = model;
  if (!campaign.use_case_id) {
    return `<section class="card" data-results-none>${emptyState({
      title: "No results to add here",
      text: "This campaign's results come with the files it was checked with.",
    })}</section>`;
  }
  const early =
    view && view.report && view.report.early_look
      ? `<p class="cp-note" data-early-look>An early look was read before the results date. It is not a final result: the result is final on ${esc(
          day(plan && plan.analysis_date),
        )}.</p>`
      : "";
  const inFile = campaign.outcomes
    ? `<p data-results-file>A results file is in: <b>${esc(campaign.outcomes.file_name)}</b>. Read the result now, or add a newer file.</p>`
    : "";
  if (!canAdd) {
    return `<section class="card" data-results-upload><h3>Add results</h3><div class="card-body">${early}${inFile}<p data-results-refused>Adding results needs the Analyst role.</p></div></section>`;
  }
  const covariate =
    plan && plan.covariate_column && plan.covariate_source !== "run_score"
      ? `<label class="cp-field">Column with the date of ${esc(plan.covariate_column)}<input type="text" name="covariate_date_column" value="${esc(
          `${plan.covariate_column}_date`,
        )}" data-results-covariate-date></label>`
      : "";
  const busy = results.busy ? `<p role="status" class="cp-note" data-results-busy>${esc(results.busy)}</p>` : "";
  const error = results.error ? `<div data-results-error>${errorBox(results.error)}</div>` : "";
  const notice = results.notice
    ? `<p class="cp-warn" role="status" data-results-notice>${esc(results.notice)}</p>`
    : "";
  const read = campaign.outcomes
    ? `<button type="button" class="btn secondary" data-results-read${results.busy ? " disabled" : ""}>Read the result</button>`
    : "";
  const what = plan && plan.metric ? ` what they did (${esc(plan.metric)})` : " what they did after the campaign";
  return `<section class="card" data-results-upload><h3>Add results</h3><div class="card-body">
      ${early}${inFile}
      <form class="cp-upload" data-results-form novalidate>
        <p>Upload a CSV or Parquet file with each customer's id and${what}: one row per customer.</p>
        <label class="cp-field">Results file<input type="file" name="file" accept=".csv,.parquet" data-results-file-input required></label>
        ${covariate}
        <div class="btn-row"><button type="submit" class="btn primary" data-results-submit${results.busy ? " disabled" : ""}>Add results</button>${read}</div>
      </form>
      ${busy}${notice}${error}
    </div></section>`;
}

function resultsHtml(model) {
  return model.status.key === "measured" ? measuredHtml(model) : uploadHtml(model);
}

// --- Technical details ----------------------------------------------------------------------------------------

/** Links to today's pages behind the campaign. Hidden by default once M180 lands (`[data-technical]`). */
function technicalHtml(model) {
  const { campaign, run } = model;
  const uc = campaign.use_case_id || (run && run.use_case_id);
  const runLinks = (campaign.run_ids || [])
    .map((id) => {
      if (!uc) return `<li><span class="mono">${esc(id)}</span></li>`;
      const base = `#/uc/${encodeURIComponent(uc)}`;
      const r = encodeURIComponent(id);
      return `<li><span class="mono">${esc(id)}</span>: <a href="${base}/data/${r}">Data</a> · <a href="${base}/model/${r}">Model</a> · <a href="${base}/output/${r}">Output</a></li>`;
    })
    .join("");
  return `<section class="card" data-technical data-technical-tab><details class="tech" data-tech><summary>Technical details</summary><div class="card-body">
      <p>The pages behind this campaign, unchanged.</p>
      <ul class="cp-links">
        <li><a href="#/campaigns/${encodeURIComponent(campaign.campaign_id)}/plan" data-plan-page>The campaign's test plan and measurement</a></li>
        ${runLinks}
      </ul>
      <dl class="cp-facts"><dt>Campaign id</dt><dd class="mono">${esc(campaign.campaign_id)}</dd><dt>Kind</dt><dd>${esc(
        campaign.kind,
      )}</dd><dt>Goal id</dt><dd class="mono">${esc(uc || EM_DASH)}</dd></dl>
    </div></details></section>`;
}

const TAB_BODY = { overview: overviewHtml, audience: audienceHtml, results: resultsHtml, technical: technicalHtml };

/**
 * One campaign's page. `model` holds `campaign`, `view` (`GET /campaigns/{id}`), `plan`, `status`, `goal`, `tab`,
 * and what each tab needs: `summary` (the contact list's counts), `proof` (the Value Proof Pack as data), `run`,
 * `audience`, `results` and `refresh` (screen state), `contactListHref`, `canAdd`, `canRefresh`.
 */
export function campaignPageHtml(model) {
  const { campaign, goal, tab } = model;
  const name = shortName(campaign, goal);
  const sentence = runElsewhere(campaign)
    ? `A campaign another tool sent on ${day(campaign.treatment_start)}, checked here.`
    : wholeProgramme(campaign)
      ? `Every customer contacted, against the customers never contacted, from ${day(campaign.treatment_start)}.`
      : `${goal}: sent on ${day(campaign.treatment_start)} to ${customers(counts(campaign).intended_treated)}.`;
  const sub =
    campaign.name && campaign.name !== name ? `<p class="cp-sub" data-long-name>${esc(campaign.name)}</p>` : "";
  const head = pageHead(
    `${crumbs([CAMPAIGNS_CRUMB, { label: name }])}<h1 class="h1">${esc(name)}</h1><p class="desc">${esc(sentence)}</p>${sub}`,
  );
  const body = (TAB_BODY[tab] || overviewHtml)(model);
  return `<main class="screen cp" data-module="campaigns" data-campaign-view="${esc(campaign.campaign_id)}" data-tab-shown="${esc(
    tab,
  )}">${STYLE_TAG}${head}${tabsHtml(campaign.campaign_id, tab)}<div class="stack">${body}</div></main>`;
}

/** The page while it loads, or when the campaign could not be read. */
export function campaignShellHtml({ title = "Campaign", error = null } = {}) {
  const head = pageHead(
    `${crumbs([CAMPAIGNS_CRUMB, { label: title }])}<h1 class="h1">${esc(title)}</h1><p class="desc">${
      error ? "This campaign could not be shown." : "Loading the campaign…"
    }</p>`,
  );
  const body = error
    ? Number(error.status) === 404
      ? `<section class="card notice-card" role="status" data-not-found><h3>This campaign could not be found</h3><p>It may have been deleted, or the link is incomplete.</p><div class="notice-actions"><a class="btn secondary" href="#/campaigns">Back to Campaigns</a></div></section>`
      : errorBox(error, { retry: true })
    : `<p class="muted" role="status">Loading…</p>`;
  return `<main class="screen cp" data-module="campaigns" aria-busy="${error ? "false" : "true"}">${STYLE_TAG}${head}<div class="stack">${body}</div></main>`;
}

/** `#/campaigns/new` before M177's wizard is on this build: say so, and offer the goals. */
export function newCampaignPendingHtml() {
  const head = pageHead(
    `${crumbs([CAMPAIGNS_CRUMB, { label: "New campaign" }])}<h1 class="h1">New campaign</h1><p class="desc">Choose a goal to start a campaign.</p>`,
  );
  return `<main class="screen cp" data-module="campaigns" data-new-campaign-pending>${STYLE_TAG}${head}<div class="stack"><section class="card">${emptyState(
    {
      title: "Start from a goal",
      text: "The step-by-step New campaign screen is not part of this version yet. Every goal can be started from Home.",
      action: { label: "Choose a goal", href: "#/" },
    },
  )}</section></div></main>`;
}

export { STATUS, goalName };

const STYLE = `
.cp .cp-name{font-weight:600}
.cp .cp-sub{font-size:12px;color:var(--muted);margin-top:2px;max-width:520px}
.cp p.cp-sub{margin:4px 0 0}
.cp .cp-status{border:1px solid var(--line)}
.cp .cp-status.cp-neutral{background:var(--soft);color:var(--ink2)}
.cp .cp-status.cp-live{background:var(--ok-t,var(--soft));color:var(--ok,var(--ink))}
.cp .cp-status.cp-warn{background:var(--warn-t,var(--soft));color:var(--warn,var(--ink))}
.cp .cp-status.cp-ok{background:var(--btn);color:var(--btn-ink);border-color:var(--btn)}
.cp .cp-tag{margin-left:6px}
.cp .cp-stand>p{margin:0 0 12px}
.cp .cp-step{font-size:15px;color:var(--ink)}
.cp .cp-kpis{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.cp .cp-kv-row{display:flex;justify-content:space-between;gap:16px;padding:10px 20px;border-top:1px solid var(--line);font-size:13px}
.cp .cp-kv-row:first-child{border-top:0}
.cp .cp-kv-row .k{color:var(--muted)}
.cp .cp-confirm{border:1px solid var(--line2);border-radius:8px;padding:12px 16px;max-width:560px}
.cp .cp-confirm p{margin:0 0 8px;font-size:13px}
.cp .cp-note{font-size:13px;color:var(--ink2);background:var(--soft);border-radius:8px;padding:8px 12px}
.cp .cp-warn{font-size:13px;color:var(--ink);background:var(--soft);border-left:3px solid var(--brand-yellow);padding:8px 12px}
.cp .cp-search{display:flex;flex-direction:column;gap:4px;max-width:320px;font-size:13px;color:var(--ink2)}
.cp .cp-search input,.cp .cp-field input{height:36px;padding:0 10px;border:1px solid var(--line2);border-radius:8px;font:inherit}
.cp .cp-field{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--ink2);max-width:420px;margin:8px 0}
.cp .cp-pager{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 20px;font-size:13px;color:var(--ink2)}
.cp .cp-pad{padding:12px 20px}
.cp .cp-why{margin:8px 0 0;padding-left:20px;font-size:13px;line-height:1.7}
.cp .cp-headline{font-size:20px;font-weight:600;color:var(--ink);margin:0 0 8px}
.cp .cp-evidence{font-size:13px;color:var(--ink2)}
.cp .cp-links{margin:8px 0;padding-left:20px;font-size:13px;line-height:1.8}
.cp .cp-facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:12px;color:var(--ink2)}
.cp .cp-facts dd{margin:0}
.cp .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.cp .muted{color:var(--muted)}
@media (max-width:600px){.cp .cp-kpis{grid-template-columns:1fr}.cp .cp-pager{padding:12px}}
`;

const STYLE_TAG = `<style data-campaigns-style>${STYLE}</style>`;
