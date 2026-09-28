// The two pages Plan H adds (M82): Results (`#/results`) and Settings (`#/settings`).
//
// Results is every run, newest first, straight from `GET /runs`: the use case, what the run did
// (trained a model or scored a file), how it ended, its key outcome and a link to the run itself,
// where the campaign results, the report, Model health and the Schedule are one click away. When
// models wait for approval (`GET /approvals`), one calm notice says how many, with one way to them.
//
// Settings is where everything a new user does not need went: the AI service, Privacy, Schedules,
// Admin (only while sign-in is on) and, folded under "Advanced tools", the uplift workbench, the data
// request kit, building a dataset from raw tables, Model health and the document assistant. Every
// entry is a link to a screen that already exists; an entry the person's role may not open is left
// out, by the same access check the top bar used for these screens.
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
 * The Results page. `runs` is `GET /runs`'s list (null while loading), `error` its failure,
 * `waiting` how many models wait for approval (null or 0: no notice).
 */
export function resultsHtml({ runs = null, error = null, waiting = null } = {}) {
  const head = pageHead(
    `${crumbs([{ label: "Results" }])}<h1 class="h1">Results</h1><p class="desc">Every run, newest first. Open one to see its scores, reasons and next steps.</p>${headActions(
      { related: { label: "All reports", href: "#/pilot" } },
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
    const rows = runs.map((run) => [
      `<a href="${esc(runHref(run))}" data-run="${esc(run.run_id)}">${esc(run.use_case_name || run.use_case_id)}</a>`,
      esc(modeWords(run)),
      statusPill(run.state),
      esc(outcomeText(run)),
      esc(run.created_at ? fmtStamp(run.created_at) : EM_DASH),
    ]);
    const cols = [{ label: "Use case" }, { label: "Run" }, { label: "Status" }, { label: "Outcome" }, { label: "Started" }];
    const more =
      runs.length >= RUNS_SHOWN
        ? `<p class="sp-note">Showing the latest ${fmtInt(RUNS_SHOWN)} runs. Older runs are on each use case's page.</p>`
        : "";
    body = `<section class="card" data-results><h3>Runs${sortNote("newest first")}</h3>${dataTable(cols, rows, {
      cls: "sp-runs",
    })}${more}</section>`;
  }
  return `<main class="screen sp" data-module="simple">${head}${approvalsNotice(waiting)}${body}</main>`;
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
    can("POST", "/connection/aws/test")
      ? entry({ label: "AI service", href: "#/generative/connection", text: "Connect the AI service that writes campaign copy and answers questions." })
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
    entry({
      label: "Build from raw tables",
      href: "#/",
      text: "Open a use case, choose Manual setup, then Build from raw tables.",
      attrs: "data-raw-tables",
    }),
    can("GET", "/schedules")
      ? entry({ label: "Model health", href: "#/monitoring/alerts", text: "Alerts for every model: data that changed, results that dropped, missed runs." })
      : "",
    entry({
      label: "Document assistant",
      href: "#/uc/ai-onboarding-assistant",
      text: "Answers questions from your own documents. Needs the AI service.",
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
.sp .sp-text{font-size:13px;color:var(--muted);line-height:1.45}
.sp details.adv>summary{padding:16px 20px;font-size:14px;font-weight:600;color:var(--ink);cursor:pointer}
.sp details.adv[open]>summary{border-bottom:1px solid var(--line)}
.sp .sp-about{padding:12px 20px;font-size:13px;color:var(--ink2);line-height:1.5}
.sp .sp-about p{margin:0 0 4px}
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
