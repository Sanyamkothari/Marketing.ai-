// The two pages Plan H adds (M82): Results (`#/results`) and Settings (`#/settings`).
//
// Results is every run, newest first, straight from `GET /runs`: the use case, what the run did
// (trained a model or scored a file), how it ended, its key outcome and a link to the run itself,
// where the campaign results, the report, Model health and the Schedule are one click away. When
// models wait for approval (`GET /approvals`), one calm notice says how many, with one way to them.
// Plan J M104: the Value Proof Packs that are ready (`GET /pilot/proof`) follow, each with the server's own
// sentence and a link to the pack.
// Plan J M105: what needs attention and the value proven to date (`GET /campaigns/summary`) come first, drawn only
// from the server's answer: a card appears only when the server sent it, and no number is made here.
//
// Settings is where everything a new user does not need went: the AI service, Privacy, Schedules,
// Admin (only while sign-in is on) and, folded under "Advanced tools", the uplift workbench, the data
// request kit, building a dataset from raw tables, Model health and the document assistant. Every
// entry is a link to a screen that already exists, except "Build from raw tables", which lives inside
// a use case's Manual setup and so is one plain sentence saying how to get there; an entry the
// person's role may not open is left out, by the same access check the top bar used for these screens.
//
// Both builders are pure (`resultsHtml`, `settingsHtml`): what they draw comes from their arguments,
// so the jsdom tests can call them with stubbed answers.

import {
  crumbs,
  dataTable,
  EM_DASH,
  emptyState,
  errorBox,
  esc,
  fmtInt,
  fmtMetric,
  fmtStamp,
  glossaryCode,
  headActions,
  metricShortName,
  noticeCard,
  pageHead,
  present,
  sortNote,
} from "../../dom.js";
import { icon, pageHeader } from "../../components.js"; // Plan M M188 (DEC-1618): the look of Settings
import { AI_SETTINGS_HREF } from "../../availability.js"; // Plan M M192 (DEC-1622): Settings › AI service
import { AI_SERVICE_CSS } from "./ai_service.js";
import { technicalSwitchHtml } from "../../technical.js"; // Plan M M180 (DEC-1610)
import { RUNS_SHOWN } from "./api.js";
import { campaignNameHtml, campaignNames, figureHtml } from "../../format.js"; // Plan M M172 (DEC-1602)
import { goalName, goalShortName, plainText, term, userText } from "../../labels.js";

/**
 * Plan M M172: each campaign's two names (`GET /campaigns`), by id - the short one (goal and month) for lists and
 * the label recorded with it as the subtitle. Empty when the list could not be read: a campaign then shows its label.
 */
export function campaignNameIndex(body) {
  const index = {};
  for (const campaign of (body && body.campaigns) || []) {
    if (campaign && campaign.campaign_id) index[campaign.campaign_id] = campaignNames(campaign, goalShortName);
  }
  return index;
}

/** A campaign's name on a list: its short name and subtitle when known, else its recorded label (the person's words). */
function campaignCell(names, id, label, { link = "", attrs = "" } = {}) {
  if (id && names && names[id]) return campaignNameHtml(names[id], { link, attrs });
  const shown = label ? userText(label) : esc(EM_DASH);
  return link ? `<a href="${esc(link)}"${attrs ? ` ${attrs}` : ""}>${shown}</a>` : shown;
}

// --- Results -------------------------------------------------------------------------------------

const STATUS = {
  pending: ["Queued", "neutral"],
  running: ["Running", "neutral"],
  done: ["Finished", "ok"],
  failed: ["Failed", "bad"],
  cancelled: ["Cancelled", "neutral"],
};

/** A run's state as a short word in a pill. */
function statusPill(state) {
  const [label, tone] = STATUS[state] || [String(state || EM_DASH), "neutral"];
  return tone === "neutral" ? `<span class="chip neutral">${esc(label)}</span>` : `<span class="pill ${tone}">${esc(label)}</span>`;
}

/** What the run did, in words (Plan M §4.1: a run is a model built or a contact list made). */
const modeWords = (run) => (run.mode === "score" ? "Made a contact list" : "Built a model");

/** The one outcome that matters for this run, or the em dash while it has none. */
export function outcomeText(run) {
  if (run.state === "done") {
    if (run.mode === "train") {
      const label = metricShortName(run.headline_metric, run.headline_metric_label);
      return present(run.headline_score) && label ? `${label} ${fmtMetric(run.headline_score)}` : "Model built";
    }
    return present(run.row_count) ? `${fmtInt(run.row_count)} rows ranked` : "Ranked";
  }
  if (run.state === "failed") {
    const error = run.error || {};
    const entry = error.code ? glossaryCode(error.code) : null;
    return (entry && entry.title) || "Did not finish";
  }
  return EM_DASH;
}

/** The run's own page: its use case's screen, open on this run. */
export const runHref = (run) => `#/uc/${encodeURIComponent(run.use_case_id)}/run/${encodeURIComponent(run.run_id)}`;

function approvalsNotice(waiting) {
  if (!waiting) return "";
  return noticeCard({
    title: waiting === 1 ? "1 model is waiting for approval" : `${fmtInt(waiting)} models are waiting for approval`,
    text: "A newly built model is used only after someone approves it.",
    action: { label: "Review", href: "#/approvals", kind: "primary" },
    attrs: "data-approvals",
  });
}

/**
 * Plan J M104: the Value Proof Packs that are ready, from `GET /pilot/proof` (`proofs`): each campaign's
 * name, the pack's one-sentence result and what its numbers can claim, all in the server's words, with a
 * link to the pack. Nothing is drawn when no pack is ready or the list could not be read.
 */
export function proofsCardHtml(proofs, names = {}) {
  const ready = ((proofs && proofs.proofs) || []).filter((entry) => entry.status === "ready");
  if (!ready.length) return "";
  const rows = ready.map((entry) => [
    campaignCell(names, entry.campaign_id, entry.name, {
      link: `#/pilot/proof/${encodeURIComponent(entry.campaign_id)}`,
      attrs: `data-proof="${esc(entry.campaign_id)}"`,
    }),
    esc(plainText(entry.headline || EM_DASH)),
    esc(plainText(entry.claim_label || EM_DASH)),
  ]);
  return `<section class="card" data-proofs><h3>Value Proof Packs${sortNote("newest first")}</h3>${dataTable(
    [{ label: "Campaign" }, { label: "What it shows" }, { label: "What it can claim" }],
    rows,
    { cls: "sp-runs" },
  )}</section>`;
}

/**
 * Plan J M105: what needs attention and the value proven to date, from `GET /campaigns/summary` (`summary`).
 * Every word and number is the server's (a figure's `text`); nothing is computed or filled in here, so with no
 * answer (null: a role without it, or an API without the route) or an answer with nothing in it, nothing is drawn.
 */
export function summaryCardHtml(summary, names = {}) {
  if (!summary || typeof summary !== "object") return "";
  const cards = Array.isArray(summary.cards) ? summary.cards : [];
  const proven = summary.proven && typeof summary.proven === "object" ? summary.proven : null;
  const totals = (proven && proven.totals) || [];
  const apart = (proven && proven.apart) || [];
  const excluded = (proven && proven.excluded) || [];
  const unpriced = (proven && proven.unpriced) || [];
  const text = (figure) => (figure && figure.text != null ? String(figure.text) : "");
  const attention = cards.length
    ? `<section class="card sp-summary" data-attention><h3>Needs your attention</h3><ul class="sp-list">${cards
        .map((card) => {
          const who = text(card.campaign_name) || (names[card.campaign_id] ? names[card.campaign_id].long : "");
          const facts = (card.facts || [])
            .filter((fact) => text(fact.value))
            .map((fact) => `<li>${esc(fact.label)}: <strong>${esc(text(fact.value))}</strong></li>`)
            .join("");
          const pack =
            card.kind === "backfire" && card.campaign_id
              ? ` <a href="#/pilot/proof/${esc(encodeURIComponent(card.campaign_id))}" data-card-pack>Open the Value Proof Pack</a>`
              : "";
          return `<li class="sp-entry sp-card ${esc(card.severity || "info")}" data-card="${esc(card.code)}"${
            card.campaign_id ? ` data-card-campaign="${esc(card.campaign_id)}"` : ""
          }${
            // PLAN-K (M139): the card's use case, so "Draft a plan" (ui/modules/agentic) can name the card.
            card.use_case_id ? ` data-card-use-case="${esc(card.use_case_id)}"` : ""
          }><span class="sp-label">${esc(plainText(card.title))}</span>${
            who ? `<span class="sp-text">${campaignCell(names, card.campaign_id, who)}</span>` : ""
          }<span class="sp-text">${esc(plainText(card.text))}</span>${
            facts ? `<ul class="sp-facts">${facts}</ul>` : ""
          }<span class="sp-text"><em>${esc(plainText(card.next_step))}</em>${pack}</span></li>`;
        })
        .join("")}</ul></section>`
    : "";
  // Plan M M172: the campaign's short name, and each lower bound in the one number format (exact in the tooltip).
  const line = (item, unit) =>
    `<li data-line="${esc(item.campaign_id)}">${campaignCell(names, item.campaign_id, text(item.campaign_name))}: ${figureHtml(
      item.lower_bound,
    )}${unit ? ` ${esc(unit)}` : ""}</li>`;
  // The total's own figure in the label ("at least <total> ...") is drawn by the one number format too, so the
  // headline reads as the lines under it ("Rs 4.18 lakh", exact in the tooltip). Found by the figure's text, never
  // by position; a label that does not carry that text is shown as the server wrote it.
  const totalLabel = (total) => {
    const label = plainText(total.label || "");
    const figure = total.total;
    const at = figure && figure.text ? label.indexOf(String(figure.text)) : -1;
    return at < 0
      ? esc(label)
      : `${esc(label.slice(0, at))}${figureHtml(figure)}${esc(label.slice(at + String(figure.text).length))}`;
  };
  const sums = totals
    .map(
      (total) =>
        `<li class="sp-entry" data-total="${esc(total.unit)}"><span class="sp-label">${totalLabel(total)}</span><ul class="sp-facts">${(
          total.campaigns || []
        )
          .map((c) => line(c))
          .join("")}</ul></li>`,
    )
    .join("");
  const aside = apart
    .map(
      (item) =>
        `<li class="sp-entry" data-apart="${esc(item.campaign_id)}"><span class="sp-label">${campaignCell(
          names,
          item.campaign_id,
          text(item.campaign_name),
        )}</span><span class="sp-text">${esc(plainText(item.claim_label))}. ${esc(plainText(item.reason))}${text(item.counted_as) ? ` <strong data-counted-as>${esc(text(item.counted_as))}</strong>` : ""}</span>${
          (item.lower_bounds || []).length
            ? `<ul class="sp-facts">${item.lower_bounds.map((bound) => line(bound, item.unit_label)).join("")}</ul>`
            : ""
        }</li>`,
    )
    .join("");
  const left = excluded
    .map(
      (item) =>
        `<li class="sp-entry" data-excluded="${esc(item.campaign_id)}"><span class="sp-label">${campaignCell(
          names,
          item.campaign_id,
          text(item.campaign_name),
        )}</span><span class="sp-text">${esc(plainText(item.reason))}</span>${
          text(item.results_available_on) && item.results_available_label
            ? `<span class="sp-text" data-available>${esc(item.results_available_label)}: <strong>${esc(text(item.results_available_on))}</strong></span>`
            : ""
        }</li>`,
    )
    .join("");
  const unpricedList = unpriced
    .map(
      (item) =>
        `<li class="sp-entry" data-unpriced="${esc(item.campaign_id)}"><span class="sp-label">${campaignCell(
          names,
          item.campaign_id,
          text(item.campaign_name),
        )}</span><span class="sp-text">${esc(plainText(item.reason))}</span></li>`,
    )
    .join("");
  const value =
    sums || aside || left
      ? `<section class="card sp-summary" data-proven><h3>Value proven to date</h3>${
          proven && proven.rule ? `<p class="sp-note">${esc(plainText(proven.rule))}</p>` : ""
        }<ul class="sp-list">${sums}</ul>${
          sums && unpricedList ? `<h4 class="sp-sub">In the totals above but not in rupees</h4><ul class="sp-list" data-unpriced-list>${unpricedList}</ul>` : ""
        }${
          aside ? `<h4 class="sp-sub">Listed apart, never added</h4><ul class="sp-list" data-apart-list>${aside}</ul>` : ""
        }${left ? `<h4 class="sp-sub">Not counted</h4><ul class="sp-list" data-excluded-list>${left}</ul>` : ""}</section>`
      : "";
  return `${attention}${value}`;
}

/**
 * The Results page. `runs` is `GET /runs`'s list (null while loading), `error` its failure,
 * `waiting` how many models wait for approval (null or 0: no notice). `lists` are the drawn results
 * lists other modules register beside the runs (Plan J M94: campaigns; `registerResultsList`).
 * `auditHref` (Plan J M103) is where "Audit a campaign" goes, for a person who may audit one; null: no link.
 * `proofs` (Plan J M104) is `GET /pilot/proof`'s answer, or null: the ready Value Proof Packs.
 * `summary` (Plan J M105) is `GET /campaigns/summary`'s answer, or null: what needs attention and the value proven.
 */
export function resultsHtml({
  runs = null,
  error = null,
  waiting = null,
  lists = [],
  auditHref = null,
  proofs = null,
  summary = null,
  campaigns = null,
} = {}) {
  const names = campaignNameIndex(campaigns);
  const head = pageHead(
    `${crumbs([{ label: "Results" }])}<h1 class="h1">Results</h1><p class="desc">Every model and contact list, newest first. Open one to see who to contact, why, and what to do next.</p>${headActions(
      {
        secondary: auditHref ? [{ label: "Audit a campaign", href: auditHref, attrs: "data-audit-link" }] : [],
        related: { label: "All reports", href: "#/pilot" },
      },
    )}`,
  );
  let body;
  if (error) {
    body = errorBox(error, { retry: true });
  } else if (!runs) {
    body = `<p class="loading" role="status">Loading…</p>`;
  } else if (!runs.length) {
    body = `<section class="card" data-results-empty>${emptyState({
      title: "Nothing here yet",
      text: "Choose a goal on Home and add your data. Every model and contact list appears here.",
      action: { label: "Go to Home", href: "#/" },
    })}</section>`;
  } else {
    const rows = runs.map((run) => {
      const name = goalName(run.use_case_id, run.use_case_name || run.use_case_id);
      const started = run.created_at ? fmtStamp(run.created_at) : EM_DASH;
      return [
        `<a href="${esc(runHref(run))}" data-run="${esc(run.run_id)}">${esc(name)}</a>`,
        esc(modeWords(run)),
        statusPill(run.state),
        esc(outcomeText(run)),
        esc(started),
        // A second, visible way in: the row reads as something to open, not only as text.
        `<a class="sp-open" href="${esc(runHref(run))}" data-run-open aria-label="${esc(
          `Open: ${name}, ${modeWords(run).toLowerCase()}, started ${started}`,
        )}">Open ›</a>`,
      ];
    });
    const cols = [
      { label: term("goal") },
      { label: "What it made" },
      { label: "Status" },
      { label: "Outcome" },
      { label: "Started" },
      { label: "", num: true },
    ];
    const more =
      runs.length >= RUNS_SHOWN
        ? `<p class="sp-note">Showing the latest ${fmtInt(RUNS_SHOWN)}. Older ones are on each goal's page.</p>`
        : "";
    body = `<section class="card" data-results><h3>Models and contact lists${sortNote("newest first")}</h3>${dataTable(cols, rows, {
      cls: "sp-runs",
    })}${more}</section>`;
  }
  const beside = [].concat(lists || []).filter(Boolean).join("");
  return `<main class="screen sp" data-module="simple">${head}${approvalsNotice(waiting)}${summaryCardHtml(summary, names)}${body}${beside}${proofsCardHtml(proofs, names)}</main>`;
}

// --- Settings ------------------------------------------------------------------------------------

/**
 * One entry of a settings group, drawn as a row (M188): the label is the link and the whole row is its target, one short
 * sentence under it in muted text, a chevron at the right. `sp-entry` stays on it: it is what the page's tests and the
 * other lists of this module know a row by.
 */
const entry = ({ label, href, text, attrs = "" }) =>
  `<li class="sp-entry st-row"><a class="st-label" href="${esc(href)}"${attrs ? ` ${attrs}` : ""}>${esc(label)}</a>${
    text ? `<span class="sp-text st-desc">${esc(text)}</span>` : ""
  }<span class="st-go">${icon("arrowRight", { size: 16 })}</span></li>`;

const group = (title, entries, attrs = "") =>
  entries.length
    ? `<section class="card sp-group st-group"${attrs ? ` ${attrs}` : ""}><h3>${esc(title)}</h3><ul class="sp-list">${entries.join("")}</ul></section>`
    : "";

/**
 * The Settings page. `can(method, path)` says what this person may open; `status` is the sign-in
 * state ("off", "signed-in", ...); `version` the engine version, or null.
 */
export function settingsHtml({ can = () => true, status = null, version = null } = {}) {
  // Settings is a top-level place: no breadcrumb (visual direction 3).
  const head = pageHeader({ title: "Settings", subtitle: "Things you set up once. Your everyday work is on Home and Results." });
  const main = [
    // Plan M M192 (DEC-1622): its own page, Settings › AI service, for anyone who may read the AI service; that page
    // says who may connect one. It used to open Connections, which the campaigns layout sends to the Data page.
    can("GET", "/ai-service")
      ? entry({
          label: "AI service",
          href: AI_SETTINGS_HREF,
          text: "Connect the AI that helps your team and the AI that writes for your customers.",
          attrs: "data-settings-ai",
        })
      : "",
    can("GET", "/privacy/purposes")
      ? entry({ label: "Privacy", href: "#/privacy/consent", text: "Consent, requests to erase or see a person's data, and how long data is kept." })
      : "",
    can("GET", "/schedules")
      ? entry({ label: "Schedules", href: "#/monitoring/schedules", text: "Refresh contact lists every week or month without anyone starting them." })
      : "",
    // Plan M M173: `#/cost` had no way in. What runs have cost, and the rate rupee amounts are worked out with.
    can("GET", "/cost/spend")
      ? entry({ label: "Costs", href: "#/cost", text: "What building and refreshing contact lists has cost each month, and the exchange rate that rupee amounts use." })
      : "",
  ].filter(Boolean);
  // Admin only while sign-in is on: with it off there is one person, who needs no user list.
  const admin =
    status === "signed-in"
      ? [
          can("GET", "/users") ? entry({ label: "Users", href: "#/admin/users", text: "Who can sign in, and what each person may do." }) : "",
          can("GET", "/audit/events") ? entry({ label: "Audit log", href: "#/admin/audit", text: "Who did what, and when." }) : "",
        ].filter(Boolean)
      : [];
  const advanced = [
    entry({
      label: "Campaign effect workbench",
      href: "#/uplift",
      text: "Build a model of the effect of contacting customers, by hand, from a past campaign.",
    }),
    entry({ label: "Data request kit", href: "#/pilot/kit", text: "What data to ask for, with templates, and data readiness reports." }),
    // Plain text, not a link: building from raw tables lives inside a use case's Manual setup, and
    // no single screen opens it (a link to Home did not open what it named).
    `<li class="sp-entry st-row st-plain" data-raw-tables><span class="sp-label st-label">Build from raw tables</span><span class="sp-text st-desc">${esc(
      "Open a goal, choose Manual setup, then Build from raw tables.",
    )}</span></li>`,
    can("GET", "/schedules")
      ? entry({ label: "Model health", href: "#/monitoring/alerts", text: "Alerts for every model: data that changed, results that dropped, missed schedules." })
      : "",
    // Plan M M173: `#/approvals` was reached only from a notice that exists while a model waits.
    can("GET", "/approvals")
      ? entry({ label: "Waiting for approval", href: "#/approvals", text: "New models that need an approver before they are used." })
      : "",
    // Plan M M173: a second way in to three screens that had one each (Home, Results, a run's links).
    can("POST", "/doctor/diagnosis")
      ? entry({ label: "Data Doctor", href: "#/doctor", text: "Drop your files and see what each one is, what it can power and what to fix first." })
      : "",
    can("POST", "/campaigns/audit")
      ? entry({ label: "Check a past campaign", href: "#/audit", text: "A campaign another tool ran: upload who was contacted and what happened, and see what it changed." })
      : "",
    // Plan M M178 (DEC-1608): Reports (`#/reports`) is in the campaigns layout's sidebar (M174); in the classic
    // layout this is its way in, so no registered route is an orphan (the M173 crawler).
    can("GET", "/pilot/proof")
      ? entry({ label: "Reports", href: "#/reports", text: "Value Proof Packs, the value proven to date and a check of a past campaign, on one page." })
      : "",
    // Plan M M175 (DEC-1605) and M179 (DEC-1609): the Overview and the Data page are in the campaigns layout's sidebar
    // (M174); in the classic layout these are their ways in, as for Reports above.
    can("GET", "/overview")
      ? entry({ label: "Overview", href: "#/overview", text: "What needs your attention, the value proven to date and the campaigns under way, on one page." })
      : "",
    can("GET", "/uploads")
      ? entry({ label: "Data", href: "#/data", text: "Where your customer data comes from: your files and saved connections, and a check of your data." })
      : "",
    can("GET", "/runs/{run_id}/outcomes")
      ? entry({ label: "Campaigns waiting for results", href: "#/monitoring/runs", text: "Every contact list you made, and where to add what happened to those customers." })
      : "",
    entry({
      label: "Document assistant",
      href: "#/uc/ai-onboarding-assistant",
      // Plan M M192 (DEC-1622): the AI service's plain name, as Settings › AI service calls it.
      text: "Answers questions from your own documents. Needs the AI that writes for your customers.",
    }),
  ].filter(Boolean);
  const about = [
    `<p>Marketing AI${version ? `, version <span class="mono">${esc(version)}</span>` : ""}.</p>`,
    status === "off" ? `<p data-signin-off>Sign-in is off: this is a single-user tool, and whoever opens it can do everything.</p>` : "",
  ].join("");
  return `<main class="screen sp st" data-module="simple">${head}<div class="sp-stack">
    ${group("Services and data", main, "data-settings-main")}
    ${group("Admin", admin, "data-settings-admin")}
    <section class="card sp-group st-group st-quiet" data-settings-advanced><details class="adv"><summary>Advanced tools</summary><ul class="sp-list">${advanced.join(
      "",
    )}</ul></details></section>
    ${viewHtml()}
    <section class="card sp-group st-group" data-settings-about><h3>About</h3><div class="sp-about">${about}</div></section>
  </div></main>`;
}

/** Plan M M180 (DEC-1610): the person's own "Show technical details" switch, remembered for them. */
export const TECHNICAL_HINT =
  "Shows the Data, Model and Output pages, the model reports and the settings analysts use. It changes what you see, not what you are allowed to do.";

function viewHtml() {
  return `<section class="card sp-group st-group" data-settings-view><h3>What you see</h3><div class="sp-view st-view">${technicalSwitchHtml({
    hint: TECHNICAL_HINT,
    id: "sp-tech-switch",
  })}</div></section>`;
}

// --- styles: tokens only, so both themes apply -----------------------------------------------------

const CSS = `
.sp .sp-stack{display:flex;flex-direction:column;gap:16px}
.sp .sp-group h3{margin:0}
.sp .sp-list{list-style:none;margin:0;padding:0}
.sp .sp-entry{display:flex;flex-direction:column;gap:2px;padding:12px 20px;border-top:1px solid var(--line)}
.sp .sp-entry:first-child{border-top:0}
.sp .sp-entry a{font-weight:600;color:var(--brand-blue)}
.sp .sp-entry a:hover{text-decoration:underline}
.sp .sp-entry .sp-label{font-weight:600;color:var(--ink)}
.sp .sp-text{font-size:13px;color:var(--muted);line-height:1.45}
.sp .sp-when{font-size:12px;color:var(--muted)}
.sp details.adv>summary{padding:16px 20px;font-size:14px;font-weight:600;color:var(--ink);cursor:pointer}
.sp details.adv[open]>summary{border-bottom:1px solid var(--line)}
.sp .sp-sub{margin:0;padding:12px 20px 0;font-size:13px;font-weight:600;color:var(--ink2)}
.sp .sp-summary .sp-note{margin:0;padding:12px 20px 0}
.sp .sp-facts{list-style:none;margin:4px 0 0;padding:0;font-size:13px;color:var(--ink2)}
.sp .sp-card.warning{border-left:3px solid var(--bad,#b3261e)}
.sp .sp-about{padding:12px 20px;font-size:13px;color:var(--ink2);line-height:1.5}
.sp .sp-about p{margin:0 0 4px}
.sp .sp-view{padding:12px 20px}
.sp .sp-runs a{color:var(--brand-blue);font-weight:600}
.sp .sp-runs a:hover{text-decoration:underline}
.sp .sp-runs a.sp-open{font-weight:500;white-space:nowrap}
.sp .card>details.adv{border-top:0;padding:0}
.sp .sp-note{margin:0;padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.sp .notice-card{margin-bottom:16px}
.sp .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
/* Settings and Help (M188): grouped cards of rows, a proper toggle, a quiet advanced list, the glossary and the dated news */
.st .st-row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:var(--s4);row-gap:2px;align-items:center;min-height:64px;padding:var(--s3) 20px;transition:background-color var(--dur) var(--ease)}
.st .st-row .st-label{grid-column:1;font-size:var(--fs-body);line-height:var(--lh-body);font-weight:600;color:var(--brand-blue)}
.st .st-row .st-desc{grid-column:1;font-size:var(--fs-small);line-height:var(--lh-small);color:var(--muted)}
.st .st-row .st-go{grid-column:2;grid-row:1 / span 2;display:inline-flex;color:var(--muted);transition:transform var(--dur) var(--ease),color var(--dur) var(--ease)}
.st .st-row:not(.st-plain):hover{background:var(--soft)}
.st .st-row:not(.st-plain):hover .st-go{color:var(--brand-blue);transform:translateX(2px)}
.st .st-row a.st-label::after{content:"";position:absolute;inset:0}
.st .st-row:has(a:focus-visible){outline:2px solid var(--brand-blue);outline-offset:-2px}
.st .st-row a.st-label:focus-visible{outline:none}
.st .st-row.st-plain .st-go{display:none}
.st .st-row.st-plain .st-label{color:var(--ink)}
.st .st-quiet .st-row{min-height:0;padding:10px 20px}
.st .st-quiet .st-row .st-label{font-size:var(--fs-small);line-height:var(--lh-small);font-weight:500}
.st .st-quiet .st-row .st-desc{font-size:var(--fs-caption);line-height:var(--lh-caption)}
.st .st-quiet details.adv>summary{display:flex;align-items:center;min-height:56px;padding:0 20px;color:var(--ink2)}
.st .st-view{padding:var(--s4) 20px}
.st .tech-switch{display:flex;flex-direction:row-reverse;align-items:center;justify-content:space-between;gap:var(--s5);width:100%;min-height:40px}
.st .tech-switch input[type=checkbox]{-webkit-appearance:none;appearance:none;position:relative;flex:none;width:44px;height:24px;margin:0;border:0;border-radius:var(--r-pill);background:var(--muted);cursor:pointer;transition:background-color var(--dur) var(--ease)}
.st .tech-switch input[type=checkbox]::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(16,24,40,.3);transition:transform var(--dur) var(--ease)}
.st .tech-switch input[type=checkbox]:checked{background:var(--brand-blue)}
.st .tech-switch input[type=checkbox]:checked::after{transform:translateX(20px)}
.st .tech-switch input[type=checkbox]:focus-visible{outline:2px solid var(--brand-blue);outline-offset:2px}
.st .tech-switch .ts-label{font-size:var(--fs-body);line-height:var(--lh-body)}
@media (forced-colors:active){.st .tech-switch input[type=checkbox]{border:1px solid CanvasText}.st .tech-switch input[type=checkbox]::after{background:CanvasText}.st .tech-switch input[type=checkbox]:checked{background:Highlight}}
.st .sp-about{padding:var(--s4) 20px;font-size:var(--fs-small);line-height:var(--lh-small)}
.st .sp-about p{margin:0}
.st .sp-about p+p{margin-top:var(--s1);color:var(--muted)}
.gl-search{position:relative;display:block;padding:var(--s4) 20px;border-bottom:1px solid var(--line)}
.gl-search svg{position:absolute;left:32px;top:50%;margin-top:-8px;color:var(--muted);pointer-events:none}
.gl-search input{width:100%;height:40px;padding:0 var(--s3) 0 36px;border:1px solid var(--line2);border-radius:var(--r);background:var(--surface);color:var(--ink);font:inherit}
.gl-search input:focus{outline:2px solid var(--brand-blue);outline-offset:1px;border-color:var(--brand-blue)}
.gl-none{margin:0;padding:var(--s5) 20px;font-size:var(--fs-small);color:var(--muted)}
.gl-none[hidden],.sp-glossary tr[hidden]{display:none!important}
.sp-glossary td:first-child{width:30%;color:var(--ink)}
.sp-glossary td:first-child strong{font-weight:600}
.sp-glossary td:last-child{color:var(--ink2)}
.sp-glossary td{padding-top:var(--s4);padding-bottom:var(--s4);vertical-align:top}
[data-glossary] .tbl-wrap{border:0;border-radius:0}
@media (max-width:700px){
.sp-glossary tr{padding:var(--s3) 0}
.sp-glossary td{display:block;width:auto!important;padding:2px var(--s4);text-align:left}
.sp-glossary td::before{display:none}
}
.st .st-news{padding:0}
.st .st-entry{display:grid;grid-template-columns:150px minmax(0,1fr);column-gap:var(--s5);row-gap:2px;align-items:baseline;padding:var(--s4) 20px}
.st .st-entry .sp-when{grid-row:1 / span 2;font-size:var(--fs-small);line-height:var(--lh-body);font-variant-numeric:tabular-nums}
.st .st-entry strong{grid-column:2;font-size:var(--fs-card);line-height:var(--lh-card);font-weight:600;color:var(--ink)}
.st .st-entry .sp-text{grid-column:2;font-size:var(--fs-body);line-height:var(--lh-body);color:var(--ink2)}
@media (max-width:700px){
.st .st-entry{grid-template-columns:minmax(0,1fr)}
.st .st-entry .sp-when{grid-row:auto}
.st .st-entry strong,.st .st-entry .sp-text{grid-column:1}
}
.uc-links{display:flex;flex-wrap:wrap;gap:8px 24px;margin:16px 0 0;font-size:13px}
.uc-links .uc-link{color:var(--brand-blue);font-weight:500}
.uc-links .uc-link:hover{text-decoration:underline}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("sp-styles")) return;
  const style = doc.createElement("style");
  style.id = "sp-styles";
  style.textContent = CSS + AI_SERVICE_CSS; // Plan M M192: Settings › AI service's cards
  doc.head.appendChild(style);
}
