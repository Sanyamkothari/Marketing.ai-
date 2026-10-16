// The campaign progress page's markup (`run_page.js` draws it and keeps it up to date). Pure: it draws from the
// answer of `GET /campaign-runs/{id}`, so the jsdom tests call it with recorded answers.

import { button, icon, pageHeader } from "../../components.js";
import { crumbs, errorBox, esc } from "../../dom.js";
import { etaText, progressMeterHtml, progressNoteHtml } from "../../run_progress.js";
import { RUN_WORDS, readyHref, restartLabel } from "./run_rules.js";
import { doctorHref } from "./summary_rules.js";
import { goalName } from "./status.js";

const STEP_ICONS = { done: "check", running: "clock", failed: "critical", skipped: "check", pending: "" };

const CSS = `
.cw .cw-run-steps{list-style:none;margin:var(--s4, 16px) 0 0;padding:0;display:flex;flex-direction:column;gap:var(--s2, 8px)}
.cw .cw-run-step{display:flex;align-items:center;gap:var(--s3, 12px);color:var(--muted);font-size:var(--fs-body, 14px)}
.cw .cw-run-step.is-running{color:var(--ink);font-weight:600}
.cw .cw-run-step.is-done{color:var(--ink2, var(--ink))}
.cw .cw-run-step .cw-run-mark{flex:none;display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;border:1px solid var(--line2, var(--line))}
.cw .cw-run-step.is-done .cw-run-mark{background:var(--brand-blue);border-color:var(--brand-blue);color:var(--btn-ink)}
.cw .cw-run-step.is-running .cw-run-mark{border-color:var(--brand-blue);color:var(--brand-blue)}
`;

export function injectRunStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById("cw-run-styles")) return;
  const style = doc.createElement("style");
  style.id = "cw-run-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}

function stepsHtml(steps) {
  const items = steps
    .map((s) => {
      const mark = STEP_ICONS[s.state] ? icon(STEP_ICONS[s.state], { size: 14 }) : "";
      const word = { done: "Done", skipped: "Not needed", running: "Now", failed: "Did not finish", pending: "Waiting" }[s.state] || "";
      return `<li class="cw-run-step is-${esc(s.state)}" data-run-step="${esc(s.key)}" data-state="${esc(s.state)}"><span class="cw-run-mark">${mark}</span><span>${esc(
        s.title,
      )}</span><span class="sr">${esc(word)}</span></li>`;
    })
    .join("");
  return `<ol class="cw-run-steps" data-run-steps>${items}</ol>`;
}

/** The page for one campaign run as `GET /campaign-runs/{id}` answered it. `error` is a refused restart. */
export function runPageHtml(view, { canRestart = true, busy = false, error = null, headline = "" } = {}) {
  const run = view.record;
  const goal = goalName(run.use_case_id);
  const title = run.state === "done" ? "Your campaign is ready" : "Getting your campaign ready";
  const head = pageHeader({
    title,
    subtitle: goal,
    crumbs: crumbs([{ label: "Campaigns", href: "#/campaigns" }, { label: goal }]),
  });
  let body;
  if (run.state === "done") {
    const href = readyHref(run);
    const campaign = run.campaign_id
      ? button({ label: "See the campaign", href: `#/campaigns/${encodeURIComponent(run.campaign_id)}/overview`, kind: "secondary", attrs: "data-run-campaign" })
      : "";
    const doctor = run.case_id
      ? button({ label: "What the Doctor did", href: doctorHref(run.case_id), kind: "tertiary", attrs: "data-run-doctor" })
      : "";
    body = `${headline ? `<p class="cw-sum-lead" data-run-headline>${esc(headline)}</p>` : ""}<p class="cw-p" data-run-summary>The contact list is built. ${
      run.campaign_id
        ? "Your results are ready: who to contact, why these customers, and how good the model is. Look them over, then approve and download."
        : "Open it to see who was picked."
    }</p>${stepsHtml(run.steps)}<div class="cw-actions">${button({
      label: run.campaign_id ? "Review and download the list" : "See the contact list",
      href,
      attrs: "data-run-ready",
    })}${campaign}${doctor}</div>`;
  } else if (run.state === "running") {
    const eta = view.eta_seconds === null || view.eta_seconds === undefined ? "" : etaText(view.eta_seconds);
    const reading = { pct: view.pct, estimated: true, finishing: false, eta };
    body = `<p class="cw-p" data-run-summary role="status">${esc(view.summary)}</p>${progressMeterHtml(reading, "cw-meter")}${progressNoteHtml(
      reading,
    )}${stepsHtml(run.steps)}<p class="cw-p" data-run-leave>You can close this page. The campaign carries on without it.</p>`;
  } else {
    const label = restartLabel(run);
    const check = run.error && run.error.code === "VALIDATION_FAILED" ? button({ label: "Check the data again", href: "#/data/check", kind: "secondary" }) : "";
    body = `<p class="cw-p" role="alert" data-run-summary><b>${esc(RUN_WORDS[run.state] || "Stopped")}.</b> ${esc(view.summary)}</p>${stepsHtml(
      run.steps,
    )}${error ? errorBox(error) : ""}<div class="cw-actions">${
      canRestart ? button({ label, kind: "primary", attrs: "data-run-restart", busy, disabled: busy }) : ""
    }${check}</div><p class="cw-p">Nothing was sent to any customer.</p>`;
  }
  return `<main class="screen cw" data-module="campaign-run" data-run-state="${esc(run.state)}">${head}<div class="cw-body"><section class="card cw-card"><div class="cw-card-body">${body}</div></section></div></main>`;
}

