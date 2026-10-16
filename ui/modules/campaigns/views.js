// The Campaigns list and the campaign page (Plan M M176, DEC-1606; Plan M §3.2): every builder is pure, drawing
// only what its arguments hold (the API's own answers), so the node tests can call it directly.
//
// Page rules (Plan M §3.3): one H1, one sentence under it, one primary button per view. Words follow the glossary
// (`ui/labels.js`); anything technical sits in a `[data-technical]` section, which M180 hides by default.
//
// The look (Plan M M186, DEC-1616) is ui/theme.css's, drawn with ui/components.js: status chips (`statusChip`, the
// one mapping), tag chips for goals, underline tabs (`tabs`), stat tiles (`kpiRow`/`statTile`), value ranges
// (`valueRange`) where the Value Proof Pack gives both ends of a figure, empty states with an icon, and buttons. The
// campaign page's header carries the name, the status chip and the view's one primary action; the list is a table on
// a wide screen and stacked row cards on a phone. This file's own style tag holds only the page's layout.

import { button, emptyState, icon, kpiRow, statTile, statusChip, tabs, tagChip, valueRange } from "../../components.js";
import { EM_DASH, crumbs, dataTable, errorBox, esc, fmtInt, headActions, pageHead } from "../../dom.js";
import { count, day, isValue, money, percent, rupees, rupeesExact } from "../../format.js";
import { term, USER_TEXT_ATTRIBUTE, userText } from "../../labels.js";
import { PAGE_SIZE, pageOf, search, topReasons } from "./audience.js";
import { campaignTabHref } from "./links.js";
import {
  evidenceOf,
  goalName,
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

/**
 * A server figure (`{value, text, format}`, engine/pilot/proof.py) as plain text in ui/format.js's one format, for the
 * helpers that take text (a stat tile's value, a value range's words): rupees in lakh and crore, counts and amounts
 * grouped, signed where the server signs them. Any other format is the server's own text.
 */
export function figureText(figure) {
  if (!figure || typeof figure !== "object") return EM_DASH;
  const value = figure.value;
  if (!isValue(value)) return figure.text ? String(figure.text) : EM_DASH;
  switch (figure.format) {
    case "inr":
    case "inr_unit":
      return rupees(value);
    case "amount":
      return money(value, null);
    case "signed_amount":
      return money(value, null, { signed: true });
    case "count":
      return count(value);
    case "signed_count":
      return count(value, { signed: true });
    case "share":
      return percent(value);
    default:
      return figure.text ? String(figure.text) : EM_DASH;
  }
}

/** A figure's likely range as `valueRange`'s options: the bar from the two ends, the dot at the figure, the words. */
const rangeOf = (line) => ({
  low: line.low.value,
  high: line.high.value,
  estimate: line.value ? line.value.value : null,
  text: `Likely ${figureText(line.low)} to ${figureText(line.high)}`,
});

// --- shared pieces --------------------------------------------------------------------------------------------

/**
 * A campaign's short name as HTML. Ours (a goal and a month) is our wording; a campaign with no goal of ours (sent by
 * another tool, a programme readout) is named by its own recorded label, the person's words, so it is marked as
 * theirs (`ui/labels.js` USER_TEXT_ATTRIBUTE, M172).
 */
function nameHtml(campaign, name) {
  const theirs = runElsewhere(campaign) || wholeProgramme(campaign) || !campaign.use_case_id;
  return theirs ? `<span ${USER_TEXT_ATTRIBUTE}>${esc(name)}</span>` : esc(name);
}

/** The status chip of the one rule (status.js `campaignStatus`), drawn by components.js: icon + word + tone. */
export const campaignChip = (status) => statusChip("campaign", status.key, { word: status.label });

/** The marks beside a campaign's name: "Sent by another tool" or "Whole programme", as quiet tags. */
function tagsHtml(campaign) {
  if (runElsewhere(campaign)) {
    return tagChip("Sent by another tool", {
      attrs: 'data-run-elsewhere title="This campaign was sent by another tool and checked here."',
    });
  }
  if (wholeProgramme(campaign)) return tagChip("Whole programme", { attrs: "data-programme" });
  return "";
}

const counts = (campaign) => (campaign && campaign.counts) || {};

/**
 * How many customers the campaign contacts: the contact list's own count when the list says it (the file a person
 * downloads and sends), else the campaign's planned count. The two differ for a list that picks only its best
 * customers, and the page must not give two numbers for one list.
 */
const contacted = (model) => {
  const n = model.summary && model.summary.treat_rows;
  return Number.isInteger(n) && n >= 0 ? n : counts(model.campaign).intended_treated;
};

/**
 * The net value line's label, as `engine/pilot/proof.py` writes it: "Net value", or, for a Pack whose claim is
 * `stated_random`, "If the groups were random as you said: net value" (`_conditional`). Nothing else matches.
 */
export const NET_VALUE_LABEL = /(^|: )net value$/i;

/** The Proof Pack's net value line (`{label, value, low, high}`), found by its label only; null without a range. */
export function netValueLine(proof) {
  const section = proof && (proof.sections || []).find((s) => s.key === "net_value");
  const lines = (section && section.lines) || [];
  const line = lines.find((l) => NET_VALUE_LABEL.test(String((l && l.label) || "").trim()));
  return line && line.low && line.high ? line : null;
}

/**
 * The Proof Pack's net value range, as its two server figures: `{ low, high }` (each `{value, text, format}`) or null. The line is found
 * by its label only: no line is ever taken by its position, so a Pack without that line shows no range.
 */
export function netValueRange(proof) {
  const line = netValueLine(proof);
  return line ? { low: line.low, high: line.high } : null;
}

/** What the campaign changed, with its range: the Pack's "Extra ... because of the campaign" line, by its label. */
export const CHANGED_LABEL = /^extra .+ because of the campaign$/i;

export function changedLine(proof) {
  const section = proof && (proof.sections || []).find((s) => s.key === "incremental");
  const line = ((section && section.lines) || []).find((l) => CHANGED_LABEL.test(String((l && l.label) || "").trim()));
  return line && line.value && line.low && line.high ? line : null;
}

/** What a changed-outcome line counts, in two words for a list cell: "extra outcomes", "extra amount". */
export const changedUnit = (line) =>
  String((line && line.label) || "")
    .trim()
    .replace(/\s+because of the campaign$/i, "")
    .toLowerCase();

/** The net value line's label for a tile: "Net value", or the Pack's own conditional wording, first letter up. */
const netLabel = (line) => {
  const label = String(line.label || "Net value").trim();
  return `${label.charAt(0).toUpperCase()}${label.slice(1)}`;
};

// --- the list -------------------------------------------------------------------------------------------------

/** A figure and its range for the result column: the number, what it counts, the bar and the words. */
const resultFigure = (line, unit, kind) =>
  `<div class="cp-result" data-result="${kind}"><p class="cp-result-value"><span class="num" title="${esc(
    line.value.text || "",
  )}">${esc(figureText(line.value))}</span> <span class="cp-result-unit">${esc(unit)}</span></p>${valueRange(rangeOf(line))}</div>`;

/**
 * The result column: once measured, the net value and its likely range; a Pack with no rupee value (no value per
 * outcome entered) gives what the campaign changed instead ("+26 extra outcomes", with its range). The verdict's short
 * sentence only when there is no Pack to read. Before that, the day results are due.
 */
function resultCell(entry) {
  const { campaign, view, proof, status } = entry;
  if (status.key === "measured") {
    const net = netValueLine(proof);
    if (net && net.value) return resultFigure(net, "net value", "net");
    const changed = changedLine(proof);
    if (changed) return resultFigure(changed, changedUnit(changed), "changed");
    const verdict = view && view.verdict && view.verdict.headline;
    return verdict ? `<span class="cp-result-note" data-result>${esc(verdict)}</span>` : `<span class="muted">Measured</span>`;
  }
  // A Draft has no results date yet: it is fixed with its test plan.
  const due = status.key === "draft" ? null : resultsDue(campaign, view && view.plan);
  return due ? `<span class="muted" data-due>Results due ${esc(day(due))}</span>` : `<span class="muted">${EM_DASH}</span>`;
}

/**
 * One row of the list: `entry` is `{ campaign, view, proof, goal, status }` - `view` is `GET /campaigns/{id}`
 * (null when it failed), `proof` that campaign's Value Proof Pack (`GET /pilot/proof/{id}`, read once it is measured;
 * null otherwise), `goal` its goal's name.
 */
function listRow(entry) {
  const { campaign, goal, status } = entry;
  const name = shortName(campaign, goal);
  const link = `<a class="cp-name" href="${esc(campaignTabHref(campaign.campaign_id))}" data-campaign-link="${esc(
    campaign.campaign_id,
  )}">${nameHtml(campaign, name)}</a>${tagsHtml(campaign)}`;
  // The long recorded name is a secondary line, cut to one line; the whole of it is the tooltip.
  const subtitle =
    campaign.name && campaign.name !== name
      ? `<span class="td-sub cp-sub" ${USER_TEXT_ATTRIBUTE} title="${esc(campaign.name)}">${esc(campaign.name)}</span>`
      : "";
  const ours = !(runElsewhere(campaign) || wholeProgramme(campaign) || !campaign.use_case_id);
  return [
    `<div class="cp-cell">${link}${subtitle}</div>`,
    ours ? tagChip(goal, { attrs: `title="${esc(goal)}"` }) : `<span class="muted">${EM_DASH}</span>`,
    campaignChip(status),
    esc(fmtInt(counts(campaign).intended_treated || 0)),
    resultCell(entry),
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
  // A top-level place: no breadcrumb (visual_common §3).
  const head = pageHead(
    `<h1 class="h1">Campaigns</h1><p class="desc">${esc(lead)}</p>${entries.length ? headActions({ primary: action }) : ""}`,
  );
  let body;
  if (error) body = errorBox(error, { retry: true });
  else if (!entries.length) {
    body = `<section class="card" data-campaigns-empty>${emptyState({
      icon: "campaign",
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
      { cls: "cp-table cp-list" },
    )}</section>`;
  }
  return `<main class="screen cp" data-module="campaigns" data-campaigns-page>${STYLE_TAG}${head}<div class="stack">${body}</div></main>`;
}

// --- the campaign page ----------------------------------------------------------------------------------------

/** Underline tabs (components.js `tabs`): one address per tab; the Technical tab is marked for M180's switch. */
function tabsHtml(campaignId, tab) {
  return tabs({
    label: "Campaign",
    current: tab,
    items: TABS.map((t) => ({
      id: t,
      label: TAB_LABEL[t],
      href: campaignTabHref(campaignId, t),
      attrs: `id="cp-tab-${t}" data-tab="${t}"${t === "technical" ? " data-technical" : ""}`,
    })),
  });
}

/**
 * The third tile: the net value with its likely range once measured; a Pack with no rupee value gives what the
 * campaign changed, with its range; before a result, the list's predicted value; else none. A measured campaign never
 * shows a prediction beside its measured figures.
 */
function valueTile(model) {
  const line = netValueLine(model.proof);
  if (line && line.value) {
    return statTile({
      label: netLabel(line),
      value: figureText(line.value),
      range: rangeOf(line),
      caption: "After the cost of contacts and offers.",
      attrs: `data-tile-value title="${esc(line.value.text || "")}"`,
    });
  }
  const changed = changedLine(model.proof);
  if (changed) {
    return statTile({
      label: changed.label,
      value: figureText(changed.value),
      range: rangeOf(changed),
      caption: "No value in rupees was entered for this campaign.",
      attrs: `data-tile-value data-tile-changed title="${esc(changed.value.text || "")}"`,
    });
  }
  if (model.status && model.status.key === "measured") return "";
  const summary = model.summary;
  if (summary && summary.net_value_total !== null && summary.net_value_total !== undefined) {
    return statTile({
      label: "Expected value (predicted)",
      value: rupees(summary.net_value_total),
      caption: "What the contact list is expected to earn.",
      attrs: `data-tile-value title="${esc(rupeesExact(summary.net_value_total))}"`,
    });
  }
  return "";
}

/**
 * A contact list whose model is not approved (M181 part C review, DEC-1611; `listHold` is `pending`, `refused`,
 * `superseded` or `unknown`): what the page says instead of offering the download or the plan.
 */
export const LIST_HOLD = Object.freeze({
  pending:
    "This contact list was made with a new model that an Approver has not approved yet, so it cannot be downloaded or planned. An Approver finds it under Waiting for approval.",
  refused: "An Approver turned down the new model this contact list was made with, so it cannot be downloaded. Start a new campaign for this goal.",
  superseded:
    "Another model came into use before the new model this contact list was made with was approved, so it cannot be downloaded. Start a new campaign for this goal.",
  unknown: "Whether the model behind this contact list is approved could not be checked, so it cannot be downloaded right now. Try again in a moment.",
});

/** The one primary action for a held list: Waiting for approval, a new campaign, or none while it is unknown. */
function holdAction(model) {
  const hold = model.listHold;
  if (hold === "pending") return { label: "Open Waiting for approval", href: "#/approvals", attrs: 'data-primary="approvals"' };
  if (hold === "refused" || hold === "superseded") {
    return {
      label: "Start a new campaign",
      href: `#/campaigns/new/customers/${encodeURIComponent(model.campaign.use_case_id)}`,
      attrs: 'data-primary="restart"',
    };
  }
  return null;
}

/** A held list's state while the campaign is not measured yet (a measured one keeps its report), else null. */
const heldNow = (model) => (model.listHold && model.status.key !== "measured" ? model.listHold : null);

/** The Overview's one primary button, by status. A campaign whose list is held offers no plan, download or results. */
export function primaryAction(model) {
  const { campaign, status, contactListHref } = model;
  const id = campaign.campaign_id;
  if (heldNow(model)) return holdAction(model);
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
      // No button: the Value Proof Pack it used to open is frozen (v1 plan, stage 0), and the Results tab is beside it.
      return null;
    default:
      return null;
  }
}

/**
 * The view's ONE primary action, in the page header: the Overview's by status; on Audience the download (unless the
 * list is held); none on Results (the Value Proof Pack's "Open the full report" is frozen, v1 plan stage 0) or on
 * Technical details.
 */
export function tabPrimary(model) {
  const { tab, contactListHref } = model;
  if (tab === "overview") return primaryAction(model);
  if (tab === "audience") {
    return contactListHref && !model.listHold
      ? { label: "Download contact list", href: contactListHref, attrs: 'download data-download="contact-list"' }
      : null;
  }
  return null;
}

/** "Refresh the list": a secondary action with its confirmation, or what came of it. */
function refreshHtml(model) {
  const r = model.refresh || {};
  if (!model.canRefresh) return { button: "", panel: "" };
  if (r.started) {
    return {
      button: "",
      panel: `<div class="cp-panel-note" role="status" data-refresh-started>A new contact list is being made with the current model. <a href="${esc(
        r.startedHref,
      )}">See its progress ›</a></div>`,
    };
  }
  if (r.asking || r.costMessage) {
    const cost = r.costMessage ? `<p class="cp-warn" data-refresh-cost>${esc(r.costMessage)}</p>` : "";
    const yes = r.costMessage ? "Start it anyway" : "Yes, refresh the list";
    const error = r.error ? errorBox(r.error) : "";
    return {
      button: "",
      panel: `<div class="cp-confirm" role="group" aria-label="Refresh the list" data-refresh-confirm>
      <p>Refresh the list? A new contact list is made from the same customers with the current model. This campaign keeps the list it sent.</p>
      ${cost}<div class="btn-row">${button({
        label: r.busy ? "Starting…" : yes,
        kind: "secondary",
        size: "sm",
        attrs: "data-refresh-yes",
        disabled: !!r.busy,
      })}${button({ label: "Keep this list", kind: "tertiary", size: "sm", attrs: "data-refresh-no" })}</div>${error}</div>`,
    };
  }
  return { button: button({ label: "Refresh the list", kind: "secondary", size: "sm", attrs: "data-refresh-ask" }), panel: "" };
}

const isDraft = (model) => Boolean(model.status && model.status.key === "draft");

function whatWasSent(model) {
  const { campaign, summary } = model;
  const rows = [];
  // A Draft has not been sent: no "sent" date, only what is planned.
  const draft = isDraft(model);
  if (!draft) rows.push(["Sent on", day(campaign.treatment_start)]);
  const offers = Object.entries((summary && summary.offer_counts) || {});
  // Offer and channel names are the person's own (their configuration and data): marked as theirs (M172).
  for (const [offer, n] of offers) rows.push([`Offer: ${userText(offer)}`, customers(n), true]);
  const channels = Object.entries((summary && summary.channel_rows) || {});
  for (const [channel, n] of channels) rows.push([`Channel: ${userText(channel)}`, customers(n), true]);
  if (summary && summary.treat_rows !== null && summary.treat_rows !== undefined) {
    rows.push([term("contactList"), customers(summary.treat_rows)]);
  }
  return `<section class="card" data-what-was-sent><h3>${draft ? "What will be sent" : "What was sent"}</h3><dl class="cp-dl">${rows
    .map(([k, v, html]) => `<div class="cp-kv-row"><dt>${html ? k : esc(k)}</dt><dd class="num">${esc(v)}</dd></div>`)
    .join("")}</dl></section>`;
}

/** The status line's icon: the status mapping's own (a held list is a warning). */
const STAND_ICON = { draft: "draft", live: "dot", waiting: "clock", measured: "check" };
const STAND_TONE = { draft: "neutral", live: "info", waiting: "warning", measured: "good" };

function overviewHtml(model) {
  const { campaign, status } = model;
  const c = counts(campaign);
  const held = heldNow(model);
  const step = held ? LIST_HOLD[held] : nextStep(model, day);
  const tone = held ? "warning" : STAND_TONE[status.key] || "neutral";
  const refresh = refreshHtml(model);
  const tiles = [
    statTile({
      label: isDraft(model) ? "To contact" : "Contacted",
      value: count(contacted(model)),
      caption: isDraft(model) ? "Customers the campaign is planned for." : "Customers sent the campaign.",
      attrs: "data-tile-contacted",
    }),
    statTile({
      label: term("comparisonGroup"),
      value: count(c.intended_holdout),
      caption: "Customers held back, not contacted, to compare against.",
      attrs: "data-tile-comparison",
    }),
    valueTile(model),
  ].filter(Boolean);
  return `<section class="card cp-stand" data-overview aria-label="Where it stands"><div class="cp-stand-row"><span class="cp-stand-icon tone-${tone}">${icon(
    held ? "alert" : STAND_ICON[status.key] || "info",
    { size: 18 },
  )}</span><div class="cp-stand-text"><p class="cp-stand-label">Where it stands</p><p class="cp-step" data-next-step${
    held ? ` data-list-held="${esc(held)}"` : ""
  }>${esc(step)}</p></div>${refresh.button ? `<div class="cp-stand-action">${refresh.button}</div>` : ""}</div>${refresh.panel}</section>
    ${kpiRow(tiles, { attrs: "data-kpis" })}
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
    // The customer's id and the offer and channel names are the person's data (M172 userText); the priority is ours.
    `<span class="mono" ${USER_TEXT_ATTRIBUTE}>${esc(r.key)}</span>`,
    r.priority ? tagChip(r.priority) : EM_DASH,
    r.offer ? userText(r.offer) : EM_DASH,
    r.channel ? userText(r.channel) : EM_DASH,
    `<span class="cp-reason">${esc(r.reason || EM_DASH)}</span>`,
  ]);
  const table = rows.length
    ? dataTable(cols, rows, { cls: "cp-table cp-audience" })
    : `<p class="cp-pad muted" data-audience-none>No customer on the list has an id containing “${esc(audience.query)}”.</p>`;
  const where = page.total
    ? `Showing ${fmtInt(page.from)} to ${fmtInt(page.to)} of ${customers(page.total)}`
    : "Showing no customers";
  const pager = `<div class="cp-pager" data-pager><span data-page-of>${esc(where)} · page ${fmtInt(page.page)} of ${fmtInt(
    page.pages,
  )}</span><span class="cp-pager-buttons">${button({
    label: "Previous",
    kind: "secondary",
    size: "sm",
    attrs: `data-page="${page.page - 1}"`,
    disabled: page.page <= 1,
  })}${button({ label: "Next", kind: "secondary", size: "sm", attrs: `data-page="${page.page + 1}"`, disabled: page.page >= page.pages })}</span></div>`;
  return `${table}${pager}`;
}

function whyHtml(rows) {
  const top = topReasons(rows, 3);
  if (!top.length) return "";
  return `<section class="card" data-why><h3>Why these customers?</h3><div class="card-body"><p class="cp-help">The strongest reason behind each customer on the list, counted across the list.</p><ol class="cp-why">${top
    .map(
      (r) =>
        `<li data-reason><p class="cp-why-text"><span>${r.html}</span> <span class="muted">for ${esc(fmtInt(r.customers))} of ${esc(
          customers(rows.length),
        )} (${esc(percent(r.share))})</span></p><span class="cp-why-bar" aria-hidden="true"><span style="width:${Math.max(
          1,
          Math.min(100, Number(r.share) * 100),
        ).toFixed(1)}%"></span></span></li>`,
    )
    .join("")}</ol></div></section>`;
}

function audienceHtml(model) {
  const { audience, contactListHref } = model;
  if (!contactListHref) {
    return `<section class="card" data-audience>${emptyState({
      icon: "inbox",
      title: "No contact list here",
      text: "This campaign was not sent from a list made here, so there is no contact list to show.",
    })}</section>`;
  }
  // The download is the view's primary action, in the header (`tabPrimary`); a held list says why it is not offered.
  const held = model.listHold
    ? `<p class="cp-warn" data-list-held="${esc(model.listHold)}">${esc(LIST_HOLD[model.listHold] || LIST_HOLD.unknown)}</p>`
    : "";
  const title = `<h3>${esc(term("contactList"))}</h3>`;
  if (!audience || audience.loading) {
    return `<section class="card" data-audience>${title}<div class="card-body">${held}<p class="muted" role="status">Loading the contact list…</p></div></section>`;
  }
  if (audience.error) {
    const refused = Number(audience.error.status) === 403;
    const lead = refused
      ? "Your role can see the campaign but not the customers on its list. Ask an Analyst to download it."
      : null;
    return `<section class="card" data-audience>${title}<div class="card-body">${
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
  return `<section class="card" data-audience>${title}<div class="card-body cp-audience-top">
      ${held}<p class="cp-lead" data-audience-lead>${esc(lead)}</p>
      <div class="cp-search"><label class="form-label" for="cp-search">Search by customer id</label><input id="cp-search" class="input" type="search" autocomplete="off" data-audience-search value="${esc(audience.query || "")}"></div>
    </div><div data-audience-table>${audienceTableHtml(audience)}</div></section>
    ${whyHtml(rows)}`;
}

// --- Results --------------------------------------------------------------------------------------------------

/** The Pack's `claim` as the evidence status (components.js `statusOf("evidence")`): Proven, Estimated, Not yet proven. */
const EVIDENCE_KEY = { proven: "proven", stated_random: "estimated", descriptive: "not_yet_proven" };

function measuredHtml(model) {
  const { campaign, proof, view } = model;
  const headline = (proof && proof.headline) || (view && view.verdict && view.verdict.headline) || "Measured.";
  const evidence = proof ? evidenceOf(proof.claim) : null;
  const label = evidence
    ? `<p class="cp-evidence" data-evidence="${esc(proof.claim)}">${statusChip("evidence", EVIDENCE_KEY[proof.claim] || "not_yet_proven", {
        word: evidence.label,
      })} <span>${esc(evidence.text)}</span></p>`
    : "";
  const changed = changedLine(proof);
  const net = netValueLine(proof);
  const tiles = [
    changed
      ? statTile({
          label: changed.label,
          value: figureText(changed.value),
          range: rangeOf(changed),
          attrs: `data-tile-changed title="${esc(changed.value.text || "")}"`,
        })
      : "",
    net && net.value
      ? statTile({
          label: netLabel(net),
          value: figureText(net.value),
          range: rangeOf(net),
          caption: "After the cost of contacts and offers.",
          attrs: `data-tile-value title="${esc(net.value.text || "")}"`,
        })
      : "",
  ].filter(Boolean);
  // With figures to show, they lead (a KPI row) and the Pack's sentence is the card's secondary text; without, the
  // sentence is the result, drawn as the card's headline. Either way it stays the Pack's own words ([data-headline]).
  const sentence = tiles.length
    ? `<p class="cp-help" data-headline>${esc(headline)}</p>`
    : `<p class="cp-headline" data-headline>${esc(headline)}</p>`;
  const card = `<section class="card"><h3>Result</h3><div class="card-body cp-result-body">
      ${label}${sentence}
      <p class="cp-help">Measured on ${esc(day(campaign.measured_at))}.</p>
    </div></section>`;
  return `<div class="cp-measured" data-results-measured>${tiles.length ? kpiRow(tiles, { attrs: "data-result-tiles" }) : ""}${card}</div>`;
}

function uploadHtml(model) {
  const { campaign, plan, results = {}, canAdd, view } = model;
  if (!campaign.use_case_id) {
    return `<section class="card" data-results-none>${emptyState({
      icon: "report",
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
    ? `<p class="cp-file-in" data-results-file>${icon("checkCircle")}<span>A results file is in: <b ${USER_TEXT_ATTRIBUTE}>${esc(
        campaign.outcomes.file_name,
      )}</b>. Read the result now, or add a newer file.</span></p>`
    : "";
  if (!canAdd) {
    return `<section class="card" data-results-upload><h3>Add results</h3><div class="card-body">${early}${inFile}<p data-results-refused>Adding results needs the Analyst role.</p></div></section>`;
  }
  const covariate =
    plan && plan.covariate_column && plan.covariate_source !== "run_score"
      ? `<div class="form-field cp-field"><label class="form-label" for="cp-covariate-date">Column with the date of ${esc(
          plan.covariate_column,
        )}</label><input id="cp-covariate-date" class="input" type="text" name="covariate_date_column" value="${esc(
          `${plan.covariate_column}_date`,
        )}" data-results-covariate-date></div>`
      : "";
  const busy = results.busy ? `<p role="status" class="cp-note" data-results-busy>${esc(results.busy)}</p>` : "";
  const error = results.error ? `<div data-results-error>${errorBox(results.error)}</div>` : "";
  const notice = results.notice
    ? `<p class="cp-warn" role="status" data-results-notice>${esc(results.notice)}</p>`
    : "";
  const read = campaign.outcomes
    ? button({ label: "Read the result", kind: "secondary", attrs: "data-results-read", disabled: !!results.busy })
    : "";
  const what = plan && plan.metric ? ` what they did (${esc(plan.metric)})` : " what they did after the campaign";
  // The drop zone is the file input's label: a click (or Enter on the focused input) opens the file chooser, and a
  // file dropped on it is handed to the same input (index.js), so the form sends exactly what it sent before.
  return `<section class="card" data-results-upload><h3>Add results</h3><div class="card-body">
      ${early}${inFile}
      <form class="cp-upload" data-results-form novalidate>
        <p class="cp-help">Upload a CSV or Parquet file with each customer's id and${what}: one row per customer.</p>
        <label class="cp-drop" for="cp-results-file" data-results-drop>
          <span class="cp-drop-icon">${icon("upload", { size: 22 })}</span>
          <span class="cp-drop-title">Drop the results file here, or <span class="cp-drop-choose">choose a file</span></span>
          <span class="cp-drop-help">CSV or Parquet, one row per customer</span>
          <span class="cp-drop-chosen" data-results-chosen hidden></span>
          <input id="cp-results-file" class="cp-drop-input" type="file" name="file" accept=".csv,.parquet" data-results-file-input required>
        </label>
        ${covariate}
        <div class="btn-row">${button({
          label: "Add results",
          kind: "primary",
          type: "submit",
          attrs: "data-results-submit",
          disabled: !!results.busy,
        })}${read}</div>
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
 * `audience`, `results` and `refresh` (screen state), `contactListHref`, `listHold` (the list's model is not
 * approved: no download, `LIST_HOLD`), `canAdd`, `canRefresh`.
 */
export function campaignPageHtml(model) {
  const { campaign, goal, tab } = model;
  const name = shortName(campaign, goal);
  const sentence = runElsewhere(campaign)
    ? `A campaign another tool sent on ${day(campaign.treatment_start)}, checked here.`
    : wholeProgramme(campaign)
      ? `Every customer contacted, against the customers never contacted, from ${day(campaign.treatment_start)}.`
      : isDraft(model)
        ? `${goal}: not sent yet, planned for ${customers(contacted(model))}.`
        : `${goal}: sent on ${day(campaign.treatment_start)} to ${customers(contacted(model))}.`;
  const sub =
    campaign.name && campaign.name !== name
      ? `<p class="cp-sub cp-long-name" data-long-name ${USER_TEXT_ATTRIBUTE} title="${esc(campaign.name)}">${esc(campaign.name)}</p>`
      : "";
  const primary = tabPrimary({ ...model, tab: TAB_BODY[tab] ? tab : "overview" });
  const head = pageHead(
    `${crumbs([CAMPAIGNS_CRUMB, { label: name }])}<div class="cp-title"><h1 class="h1">${nameHtml(campaign, name)}</h1>${
      model.status ? campaignChip(model.status) : ""
    }${tagsHtml(campaign)}</div><p class="desc">${esc(sentence)}</p>${sub}${primary ? headActions({ primary }) : ""}`,
  );
  const body = (TAB_BODY[tab] || overviewHtml)(model);
  const shown = TAB_BODY[tab] ? tab : "overview";
  return `<main class="screen cp" data-module="campaigns" data-campaign-view="${esc(campaign.campaign_id)}" data-tab-shown="${esc(
    tab,
  )}">${STYLE_TAG}${head}${tabsHtml(campaign.campaign_id, tab)}<div class="stack cp-tabpanel" role="tabpanel" aria-labelledby="cp-tab-${esc(
    shown,
  )}">${body}</div></main>`;
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
      ? `<section class="card notice-card" role="status" data-not-found>${emptyState({
          icon: "inbox",
          title: "This campaign could not be found",
          text: "It may have been deleted, or the link is incomplete.",
          action: { label: "Back to Campaigns", href: "#/campaigns", kind: "secondary" },
        })}</section>`
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
      icon: "campaign",
      title: "Start from a goal",
      text: "The step-by-step New campaign screen is not part of this version yet. Every goal can be started from Home.",
      action: { label: "Choose a goal", href: "#/" },
    },
  )}</section></div></main>`;
}

export { STATUS, goalName };

// --- styles: the pages' own layout; the components are ui/theme.css's ----------------------------------------------
// Scoped to `.cp`. Text colours are tokens on the card's white, the soft panel or the canvas, which the contrast test
// checks. On a phone (700px and less) the list's table becomes stacked row cards: the name and status on top, the
// goal under them, then the figures with their labels.

const STYLE = `
.cp .cp-title{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px}
.cp .cp-title .h1{min-width:0}
.cp .head .titles>.cp-title+.desc{margin-top:4px}
.cp .cp-name{font-size:14px;line-height:20px;font-weight:600;color:var(--ink);text-decoration:none}
.cp .cp-name:hover{color:var(--brand-blue);text-decoration:underline;text-underline-offset:3px}
.cp .cp-cell{display:flex;flex-direction:column;gap:2px;min-width:0}
.cp .cp-cell .tag{align-self:flex-start;margin-top:4px}
.cp .cp-sub{display:block;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;line-height:16px;color:var(--muted)}
.cp p.cp-long-name{margin:4px 0 0;max-width:min(720px,100%);min-width:0}
.cp .cp-list td{vertical-align:middle}
.cp .cp-list .tag{display:inline-block;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:22px;vertical-align:middle}
.cp .cp-list th:nth-child(4),.cp .cp-list td:nth-child(4){width:1%;white-space:nowrap}
.cp .cp-list td:nth-child(5){min-width:260px}
.cp .cp-result{display:flex;flex-direction:column;gap:4px}
.cp .cp-result-value{margin:0;font-size:15px;line-height:22px;font-weight:600;color:var(--ink)}
.cp .cp-result-unit{font-size:12px;font-weight:500;color:var(--muted)}
.cp .cp-result .vr-track{flex-basis:96px}
.cp .cp-result .vr-text{font-size:12px;line-height:16px}
.cp .cp-result-note{font-size:13px;color:var(--ink2)}
.cp .cp-pad{margin:0;padding:12px 20px}
.cp .muted{color:var(--muted)}
.cp .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.cp .card{overflow:hidden}
.cp .cp-stand-row{display:flex;align-items:center;gap:16px;padding:16px 20px}
.cp .cp-stand-icon{flex:none;display:inline-grid;place-items:center;width:40px;height:40px;border-radius:var(--r-lg,12px)}
.cp .cp-stand-icon.tone-good{background:var(--good-t);color:var(--good)}
.cp .cp-stand-icon.tone-warning{background:var(--warning-t);color:var(--warning)}
.cp .cp-stand-icon.tone-info{background:var(--brand-50);color:var(--brand-blue)}
.cp .cp-stand-icon.tone-neutral{background:var(--neutral-t);color:var(--neutral)}
.cp .cp-stand-text{flex:1 1 auto;min-width:0}
.cp .cp-stand-label{margin:0;font-size:12px;line-height:16px;font-weight:600;color:var(--muted)}
.cp .cp-step{margin:2px 0 0;font-size:15px;line-height:22px;font-weight:500;color:var(--ink)}
.cp .cp-stand-action{flex:none}
.cp .cp-confirm,.cp .cp-panel-note{margin:0;padding:16px 20px;border-top:1px solid var(--line);background:var(--soft);font-size:13px;color:var(--ink2)}
.cp .cp-confirm p{margin:0 0 8px;max-width:640px;color:var(--ink2)}
.cp .cp-confirm .btn-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cp .cp-panel-note a{color:var(--brand-blue);font-weight:600}
.cp .stat .stat-caption{font-size:12px;line-height:16px}
.cp .cp-dl{margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:20px 32px;padding:20px}
.cp .cp-kv-row{display:flex;flex-direction:column;gap:2px;min-width:0}
.cp .cp-kv-row dt{font-size:13px;line-height:20px;color:var(--muted)}
.cp .cp-kv-row dd{margin:0;font-size:15px;line-height:22px;font-weight:600;color:var(--ink)}
.cp .cp-note{margin:0 0 12px;font-size:13px;color:var(--ink2);background:var(--soft);border-radius:var(--r,8px);padding:8px 12px}
.cp .cp-warn{margin:0 0 12px;font-size:13px;color:var(--ink);background:var(--warning-t);border-radius:var(--r,8px);padding:10px 12px}
.cp .cp-help{margin:0;font-size:13px;line-height:20px;color:var(--ink2)}
.cp .cp-audience-top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px}
.cp .cp-audience-top .cp-warn{flex-basis:100%}
.cp .cp-lead{margin:0;font-size:15px;line-height:22px;font-weight:500;color:var(--ink)}
.cp .cp-search{display:flex;flex-direction:column;gap:6px;width:min(320px,100%)}
.cp .cp-audience td{vertical-align:middle}
.cp .cp-reason{display:block;max-width:360px;font-size:12px;line-height:16px;color:var(--ink2)}
.cp .cp-pager{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 20px;border-top:1px solid var(--line);font-size:13px;color:var(--ink2);font-variant-numeric:tabular-nums}
.cp .cp-pager-buttons{display:flex;gap:8px}
.cp .cp-why{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:12px}
.cp .cp-why li{display:flex;flex-direction:column;gap:6px}
.cp .cp-why-text{margin:0;font-size:13px;line-height:20px;color:var(--ink)}
.cp .cp-why-bar{display:block;height:6px;max-width:480px;border-radius:3px;background:var(--neutral-t);overflow:hidden}
.cp .cp-why-bar>span{display:block;height:100%;border-radius:3px;background:var(--brand-blue)}
.cp .cp-result-body{display:flex;flex-direction:column;gap:12px}
.cp .cp-measured{display:flex;flex-direction:column;gap:24px}
.cp .cp-evidence{margin:0;display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:13px;line-height:20px;color:var(--ink2)}
.cp .cp-headline{margin:0;font-size:18px;line-height:26px;font-weight:600;letter-spacing:-.01em;color:var(--ink);max-width:880px}
.cp .cp-file-in{display:flex;align-items:flex-start;gap:8px;margin:0 0 12px;font-size:13px;color:var(--ink2)}
.cp .cp-file-in svg{flex:none;margin-top:2px;color:var(--good)}
.cp .cp-upload{display:flex;flex-direction:column;gap:16px;max-width:640px}
.cp .cp-drop{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px;padding:28px 20px;border:1.5px dashed var(--line2);border-radius:var(--r-lg,12px);background:var(--soft);text-align:center;cursor:pointer;transition:border-color var(--dur,140ms) var(--ease,ease),background-color var(--dur,140ms) var(--ease,ease)}
.cp .cp-drop:hover,.cp .cp-drop.is-over{border-color:var(--brand-blue);background:var(--brand-50)}
.cp .cp-drop:focus-within{outline:2px solid var(--brand-blue);outline-offset:2px;border-color:var(--brand-blue)}
.cp .cp-drop-icon{display:inline-grid;place-items:center;width:40px;height:40px;margin-bottom:4px;border-radius:var(--r-lg,12px);background:var(--surface);color:var(--brand-blue);box-shadow:0 1px 2px rgba(16,24,40,.08)}
.cp .cp-drop-title{font-size:14px;line-height:22px;font-weight:600;color:var(--ink)}
.cp .cp-drop-choose{color:var(--brand-blue);text-decoration:underline;text-underline-offset:3px}
.cp .cp-drop-help{font-size:12px;line-height:16px;color:var(--muted)}
.cp .cp-drop-chosen{margin-top:4px;font-size:13px;font-weight:600;color:var(--ink)}
.cp .cp-drop-input{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden}
.cp .cp-field{max-width:420px}
.cp .btn-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cp .cp-links{margin:8px 0;padding-left:20px;font-size:13px;line-height:1.8}
.cp .cp-facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:12px;color:var(--ink2)}
.cp .cp-facts dd{margin:0}
@media (max-width:700px),(pointer:coarse){
  .cp .btn.sm,.cp .btn.tertiary,.cp .btn.quiet{min-height:40px}
  .cp .btn.sm:not(.tertiary):not(.quiet){height:40px}
  .cp .cp-panel-note a,.cp .cp-links a{display:inline-flex;align-items:center;min-height:40px}
}
@media (max-width:700px){
  .cp .cp-stand-row{display:grid;grid-template-columns:40px minmax(0,1fr);align-items:start;gap:12px 16px;padding:16px}
  .cp .cp-stand-action{grid-column:2}
  .cp .cp-dl{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:16px}
  .cp [data-kpis]>[data-tile-value],.cp [data-result-tiles]>.stat{grid-column:1 / -1}
  .cp .cp-audience-top{flex-direction:column;align-items:stretch}
  .cp .cp-search{width:100%}
  .cp .cp-pager{padding:12px 16px}
  .cp .cp-list tr{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 12px;padding:16px}
  .cp .cp-list td,.cp .cp-list td:nth-child(4){display:block;width:auto;padding:0;text-align:left;white-space:normal}
  .cp .cp-list td::before{display:none}
  .cp .cp-list td:nth-child(1){grid-column:1;grid-row:1}
  .cp .cp-list td:nth-child(3){grid-column:2;grid-row:1;justify-self:end}
  .cp .cp-list td:nth-child(2){grid-column:1 / -1;grid-row:2}
  .cp .cp-list td:nth-child(4),.cp .cp-list td:nth-child(5){grid-column:1 / -1;min-width:0}
  .cp .cp-list td:nth-child(4)::before,.cp .cp-list td:nth-child(5)::before{display:block;content:attr(data-label);margin-bottom:2px;font-size:12px;line-height:16px;font-weight:500;color:var(--muted)}
  .cp .cp-list td:nth-child(4){font-size:15px;font-weight:600;color:var(--ink)}
  .cp .cp-sub{max-width:100%}
  .cp .cp-list .tag{max-width:100%}
  .cp .cp-audience td:first-child{font-size:13px}
}
`;

const STYLE_TAG = `<style data-campaigns-style>${STYLE}</style>`;
