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
import { RUNS_SHOWN } from "./api.js";

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

/** What the run did, in words. */
const modeWords = (run) => (run.mode === "score" ? "Scored new data" : "Trained a model");

/** The one outcome that matters for this run, or the em dash while it has none. */
export function outcomeText(run) {
  if (run.state === "done") {
    if (run.mode === "train") {
      const label = metricShortName(run.headline_metric, run.headline_metric_label);
      return present(run.headline_score) && label ? `${label} ${fmtMetric(run.headline_score)}` : "Model trained";
    }
    return present(run.row_count) ? `${fmtInt(run.row_count)} rows scored` : "Scored";
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
    text: "A newly trained model is used only after someone approves it.",
    action: { label: "Review", href: "#/approvals", kind: "primary" },
    attrs: "data-approvals",
  });
}

/**
 * Plan J M104: the Value Proof Packs that are ready, from `GET /pilot/proof` (`proofs`): each campaign's
 * name, the pack's one-sentence result and what its numbers can claim, all in the server's words, with a
 * link to the pack. Nothing is drawn when no pack is ready or the list could not be read.
 */
export function proofsCardHtml(proofs) {
  const ready = ((proofs && proofs.proofs) || []).filter((entry) => entry.status === "ready");
  if (!ready.length) return "";
  const rows = ready.map((entry) => [
    `<a href="#/pilot/proof/${esc(encodeURIComponent(entry.campaign_id))}" data-proof="${esc(entry.campaign_id)}">${esc(entry.name)}</a>`,
    esc(entry.headline || EM_DASH),
    esc(entry.claim_label || EM_DASH),
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
export function summaryCardHtml(summary) {
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
          const who = text(card.campaign_name);
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
          }><span class="sp-label">${esc(card.title)}</span>${who ? `<span class="sp-text">${esc(who)}</span>` : ""}<span class="sp-text">${esc(
            card.text,
          )}</span>${facts ? `<ul class="sp-facts">${facts}</ul>` : ""}<span class="sp-text"><em>${esc(card.next_step)}</em>${pack}</span></li>`;
        })
        .join("")}</ul></section>`
    : "";
  const line = (item, unit) =>
    `<li data-line="${esc(item.campaign_id)}">${esc(text(item.campaign_name))}: ${esc(text(item.lower_bound))}${unit ? ` ${esc(unit)}` : ""}</li>`;
  const sums = totals
    .map(
      (total) =>
        `<li class="sp-entry" data-total="${esc(total.unit)}"><span class="sp-label">${esc(total.label)}</span><ul class="sp-facts">${(
          total.campaigns || []
        )
          .map(line)
          .join("")}</ul></li>`,
    )
    .join("");
  const aside = apart
    .map(
      (item) =>
        `<li class="sp-entry" data-apart="${esc(item.campaign_id)}"><span class="sp-label">${esc(text(item.campaign_name))}</span><span class="sp-text">${esc(
          item.claim_label,
        )}. ${esc(item.reason)}${text(item.counted_as) ? ` <strong data-counted-as>${esc(text(item.counted_as))}</strong>` : ""}</span>${
          (item.lower_bounds || []).length
            ? `<ul class="sp-facts">${item.lower_bounds.map((bound) => line(bound, item.unit_label)).join("")}</ul>`
            : ""
        }</li>`,
    )
    .join("");
  const left = excluded
    .map(
      (item) =>
        `<li class="sp-entry" data-excluded="${esc(item.campaign_id)}"><span class="sp-label">${esc(text(item.campaign_name))}</span><span class="sp-text">${esc(
          item.reason,
        )}</span>${
          text(item.results_available_on) && item.results_available_label
            ? `<span class="sp-text" data-available>${esc(item.results_available_label)}: <strong>${esc(text(item.results_available_on))}</strong></span>`
            : ""
        }</li>`,
    )
    .join("");
  const unpricedList = unpriced
    .map(
      (item) =>
        `<li class="sp-entry" data-unpriced="${esc(item.campaign_id)}"><span class="sp-label">${esc(text(item.campaign_name))}</span><span class="sp-text">${esc(
          item.reason,
        )}</span></li>`,
    )
    .join("");
  const value =
    sums || aside || left
      ? `<section class="card sp-summary" data-proven><h3>Value proven to date</h3>${
          proven && proven.rule ? `<p class="sp-note">${esc(proven.rule)}</p>` : ""
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
} = {}) {
  const head = pageHead(
    `${crumbs([{ label: "Results" }])}<h1 class="h1">Results</h1><p class="desc">Every run, newest first. Open one to see its scores, reasons and next steps.</p>${headActions(
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
      title: "No runs yet",
      text: "Choose a use case on Home and add your data. Every run appears here.",
      action: { label: "Go to Home", href: "#/" },
    })}</section>`;
  } else {
    const rows = runs.map((run) => {
      const name = run.use_case_name || run.use_case_id;
      const started = run.created_at ? fmtStamp(run.created_at) : EM_DASH;
      return [
        `<a href="${esc(runHref(run))}" data-run="${esc(run.run_id)}">${esc(name)}</a>`,
        esc(modeWords(run)),
        statusPill(run.state),
        esc(outcomeText(run)),
        esc(started),
        // A second, visible way in: the row reads as something to open, not only as text.
        `<a class="sp-open" href="${esc(runHref(run))}" data-run-open aria-label="${esc(
          `Open the run: ${name}, ${modeWords(run).toLowerCase()}, started ${started}`,
        )}">Open ›</a>`,
      ];
    });
    const cols = [
      { label: "Use case" },
      { label: "Run" },
      { label: "Status" },
      { label: "Outcome" },
      { label: "Started" },
      { label: "", num: true },
    ];
    const more =
      runs.length >= RUNS_SHOWN
        ? `<p class="sp-note">Showing the latest ${fmtInt(RUNS_SHOWN)} runs. Older runs are on each use case's page.</p>`
        : "";
    body = `<section class="card" data-results><h3>Runs${sortNote("newest first")}</h3>${dataTable(cols, rows, {
      cls: "sp-runs",
    })}${more}</section>`;
  }
  const beside = [].concat(lists || []).filter(Boolean).join("");
  return `<main class="screen sp" data-module="simple">${head}${approvalsNotice(waiting)}${summaryCardHtml(summary)}${body}${beside}${proofsCardHtml(proofs)}</main>`;
}

// --- Settings ------------------------------------------------------------------------------------

/** One entry of a settings group: a link and one short sentence. */
const entry = ({ label, href, text, attrs = "" }) =>
  `<li class="sp-entry"><a href="${esc(href)}"${attrs ? ` ${attrs}` : ""}>${esc(label)}</a>${
    text ? `<span class="sp-text">${esc(text)}</span>` : ""
  }</li>`;

const group = (title, entries, attrs = "") =>
  entries.length ? `<section class="card sp-group"${attrs ? ` ${attrs}` : ""}><h3>${esc(title)}</h3><ul class="sp-list">${entries.join("")}</ul></section>` : "";

/**
 * The Settings page. `can(method, path)` says what this person may open; `status` is the sign-in
 * state ("off", "signed-in", ...); `version` the engine version, or null.
 */
export function settingsHtml({ can = () => true, status = null, version = null } = {}) {
  const head = pageHead(
    `${crumbs([{ label: "Settings" }])}<h1 class="h1">Settings</h1><p class="desc">Things you set up once. Your everyday work is on Home and Results.</p>`,
  );
  const main = [
    can("PUT", "/ai-service/{slot}")
      ? entry({ label: "AI service", href: "#/connections", text: "Connect the AI that helps your team in Guided setup and the AI behind what your customer gets. Both are on Connections, with your data." })
      : "",
    can("GET", "/privacy/purposes")
      ? entry({ label: "Privacy", href: "#/privacy/consent", text: "Consent, requests to erase or see a person's data, and how long data is kept." })
      : "",
    can("GET", "/schedules")
      ? entry({ label: "Schedules", href: "#/monitoring/schedules", text: "Score new data every week or month without anyone clicking Run." })
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
    entry({ label: "Uplift workbench", href: "#/uplift", text: "Train an uplift model by hand from a past campaign." }),
    entry({ label: "Data request kit", href: "#/pilot/kit", text: "What data to ask for, with templates, and data readiness reports." }),
    // Plain text, not a link: building from raw tables lives inside a use case's Manual setup, and
    // no single screen opens it (a link to Home did not open what it named).
    `<li class="sp-entry" data-raw-tables><span class="sp-label">Build from raw tables</span><span class="sp-text">${esc(
      "Open a use case, choose Manual setup, then Build from raw tables.",
    )}</span></li>`,
    can("GET", "/schedules")
      ? entry({ label: "Model health", href: "#/monitoring/alerts", text: "Alerts for every model: data that changed, results that dropped, missed runs." })
      : "",
    entry({
      label: "Document assistant",
      href: "#/uc/ai-onboarding-assistant",
      text: "Answers questions from your own documents. Needs the Deliverable AI.",
    }),
  ].filter(Boolean);
  const about = [
    `<p>Marketing AI${version ? `, version <span class="mono">${esc(version)}</span>` : ""}.</p>`,
    status === "off" ? `<p data-signin-off>Sign-in is off: this is a single-user tool, and whoever opens it can do everything.</p>` : "",
  ].join("");
  return `<main class="screen sp" data-module="simple">${head}<div class="sp-stack">
    ${group("Services and data", main, "data-settings-main")}
    ${group("Admin", admin, "data-settings-admin")}
    <section class="card sp-group" data-settings-advanced><details class="adv"><summary>Advanced tools</summary><ul class="sp-list">${advanced.join(
      "",
    )}</ul></details></section>
    <section class="card sp-group" data-settings-about><h3>About</h3><div class="sp-about">${about}</div></section>
  </div></main>`;
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
.sp details.adv>summary{padding:16px 20px;font-size:14px;font-weight:600;color:var(--ink);cursor:pointer}
.sp details.adv[open]>summary{border-bottom:1px solid var(--line)}
.sp .sp-sub{margin:0;padding:12px 20px 0;font-size:13px;font-weight:600;color:var(--ink2)}
.sp .sp-summary .sp-note{margin:0;padding:12px 20px 0}
.sp .sp-facts{list-style:none;margin:4px 0 0;padding:0;font-size:13px;color:var(--ink2)}
.sp .sp-card.warning{border-left:3px solid var(--bad,#b3261e)}
.sp .sp-about{padding:12px 20px;font-size:13px;color:var(--ink2);line-height:1.5}
.sp .sp-about p{margin:0 0 4px}
.sp .sp-runs a{color:var(--brand-blue);font-weight:600}
.sp .sp-runs a:hover{text-decoration:underline}
.sp .sp-runs a.sp-open{font-weight:500;white-space:nowrap}
.sp .card>details.adv{border-top:0;padding:0}
.sp .sp-note{margin:0;padding:12px 20px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.sp .notice-card{margin-bottom:16px}
.sp .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.uc-links{display:flex;flex-wrap:wrap;gap:8px 24px;margin:16px 0 0;font-size:13px}
.uc-links .uc-link{color:var(--brand-blue);font-weight:500}
.uc-links .uc-link:hover{text-decoration:underline}
`;

export function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("sp-styles")) return;
  const style = doc.createElement("style");
  style.id = "sp-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
