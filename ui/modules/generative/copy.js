// The win-back campaign-copy screen (prototype 14, light and dark): generated messages per band and
// channel, waiting for a person before anything counts as usable.
//
// `CopyBatch.require_human_review` is not a UI convenience - `DEC-205` sets it true by default
// because this text is written to be sent to a customer, so the screen's one job beyond showing the
// copy is to make the review real: a template's failed or warned guardrail is on screen rather than
// folded into a pass/fail dot, a blocked template is shown blocked rather than hidden (a card that
// silently disappeared would read as "nothing to review" instead of "a rule refused this"), and the
// control holdout - the customers this campaign deliberately does not write to - is a number here,
// not a fact left to the CSV. Nothing this screen does sends anything: approving a template records
// that a named person accepted it (DEC-055's unverified-claim pattern, same as model approval), and
// the actual export is the CSV download, same as `scores.csv` is for a predictive run.
//
// v1 UI: one primary action - "Generate campaign copy" until copy exists, then "Download messages (CSV)"
// (every message that was not blocked; the screen says to send only the approved versions).The approver's name is the signed-in person's, read-only; it is typed only when sign-in
// is off. Judge scores, every passed check and the cost sit under closed disclosures.

import { ApiError, getArtefacts, getRun } from "../../api.js";
import {
  emptyState,
  errorBox,
  esc,
  fmtInt,
  fmtNum,
  fmtStamp,
  headActions,
  journeyCrumb,
  pageHead,
  skeleton,
  techDetails,
} from "../../dom.js";
import {
  backendBadge,
  copyStatusPill,
  costAndChecks,
  flaggedChecks,
  gTypeChip,
  guardrailList,
  progressList,
  termTip,
} from "./gdom.js";
import { copyMessagesUrl, postApproveTemplate, postCampaignCopy, postRegenerateTemplate } from "./api.js";
import { readPath } from "../../settings.js";

const POLL_MS = 2000;
const STATE = new Map();

const ARTEFACTS = ["run_config.json", "copy_status.json", "copy_batch.json", "guardrail_report.json", "llm_usage.json"];

// The wording `configs/pilot/help.yaml` gives these terms, for when the catalogue is not registered.
const BAND_TIP = "A group of customers with similar scores (High, Medium, Low) that share one suggested action.";
const CONTROL_TIP =
  "Customers chosen at random and deliberately not contacted, so the campaign's effect can be measured against them.";

function stateFor(runId) {
  if (!STATE.has(runId)) {
    STATE.set(runId, {
      runId,
      run: null,
      art: {},
      generating: false,
      generateError: null,
      approverName: "",
      approverLocked: false,
      busy: {},
      actionError: null,
    });
  }
  return STATE.get(runId);
}

// --- one template --------------------------------------------------------------------------------

const JUDGE_LABEL = (purpose) => purpose.replace(/^judge_/, "").replace(/^./, (c) => c.toUpperCase());

function judgeLine(template) {
  const scores = template.judge_scores.map((j) => `${JUDGE_LABEL(j.purpose)} ${fmtNum(j.score, 2)}`);
  scores.push(`${template.text.length} characters`);
  return scores.join(" · ");
}

function templateCard(s, template) {
  const busy = s.busy[template.template_id];
  const canApprove = template.status === "pending_review" && s.approverName.trim() && !busy;
  const flagged = flaggedChecks(template.guardrails);
  return `<div class="gtpl${template.status === "blocked" ? " blocked" : ""}">
    <div class="gtpl-head"><span>${esc(template.channel.toUpperCase())} · Version ${esc(
      template.variant,
    )}</span>${copyStatusPill(template.status)}</div>
    ${template.subject ? `<div class="gtpl-subj">${esc(template.subject)}</div>` : ""}
    <div class="gtpl-body">${esc(template.text)}</div>
    ${template.block_reason ? `<div class="gtpl-block-reason">${esc(template.block_reason)}</div>` : ""}
    ${guardrailList(flagged)}
    ${
      template.status === "approved"
        ? `<div class="gtpl-meta">Approved by ${esc(template.approved_by)} · ${esc(fmtStamp(template.approved_at))}</div>`
        : ""
    }
    <div class="gtpl-actions">
      ${
        template.status === "approved"
          ? ""
          : `<button type="button" class="btn secondary sm g-approve" data-approve="${esc(template.template_id)}"${
              canApprove ? "" : " disabled"
            }>${busy === "approve" ? "Approving…" : "Approve"}</button>`
      }
      <button type="button" class="btn quiet sm g-regen" data-regen="${esc(template.template_id)}"${
        busy ? " disabled" : ""
      }>${busy === "regenerate" ? "Regenerating…" : "Regenerate"}</button>
    </div>
    <details class="tech"><summary>Scores and checks</summary><p>${esc(judgeLine(template))}</p>${guardrailList(
      template.guardrails,
    )}</details>
  </div>`;
}

function bandSection(s, band, templates) {
  return `<section><h4 class="gband-h">${esc(band)} band ${termTip("band", BAND_TIP)}</h4><div class="ggrid">${templates
    .map((t) => templateCard(s, t))
    .join("")}</div></section>`;
}

function templatesByBand(batch) {
  const order = [];
  const grouped = new Map();
  for (const template of batch.templates) {
    if (!grouped.has(template.band)) {
      grouped.set(template.band, []);
      order.push(template.band);
    }
    grouped.get(template.band).push(template);
  }
  return order.map((band) => [band, grouped.get(band)]);
}

function approverField(s) {
  const locked = s.approverLocked;
  const missing = !s.approverName.trim();
  return `<div class="gapprover"><label for="g-approver">Approved by</label><div class="control"><input type="text" id="g-approver" value="${esc(
    s.approverName,
  )}"${locked ? " readonly" : ' placeholder="Your name"'} aria-describedby="g-approver-help" autocomplete="name"></div><p class="fhint" id="g-approver-help">${
    locked
      ? "Your name is recorded with each message you approve."
      : missing
        ? "Type your name to approve messages. It is recorded with each one."
        : "Recorded with each message you approve."
  }</p></div>`;
}

function summaryHtml(s, batch) {
  const approved = batch.templates.filter((t) => t.status === "approved").length;
  const pending = batch.templates.filter((t) => t.status === "pending_review").length;
  const blocked = batch.templates.filter((t) => t.status === "blocked").length;
  const bands = new Set(batch.templates.map((t) => t.band)).size;
  const channels = new Set(batch.templates.map((t) => t.channel)).size;
  const total = batch.templates.length;
  const verdict = total
    ? `<p class="gverdict">${fmtInt(approved)} of ${fmtInt(total)} messages approved</p><p class="gverdict-sub">${fmtInt(
        pending,
      )} waiting for review · ${fmtInt(blocked)} blocked · ${bands} band${bands === 1 ? "" : "s"} × ${channels} channel${
        channels === 1 ? "" : "s"
      }</p>`
    : `<p class="gverdict">No messages were written for this run</p><p class="gverdict-sub">None of the customers it scored fell in a band this use case writes for. Score a newer file, or check the bands in the use case's settings.</p>`;
  return `<div class="gsummary">
    <div>${verdict}</div>
    <p>Nothing is sent from here. ${
      batch.require_human_review
        ? "The download holds every message that was not blocked: send only the versions you approved."
        : "Human review is off for this use case, so every message can be used as written."
    }</p>
    <p class="tnum">Holds back <b>${fmtInt(batch.holdout.control_rows)}</b> customers as a control group ${termTip(
      "control group",
      CONTROL_TIP,
    )} and skips <b>${fmtInt(batch.holdout.suppressed_rows)}</b> more because of consent or recent contact${
      batch.holdout.out_of_band_rows
        ? ` (${fmtInt(batch.holdout.out_of_band_rows)} fell outside the bands this batch writes for)`
        : ""
    }.</p>
    ${total ? approverField(s) : ""}
    ${s.actionError ? errorBox(s.actionError) : ""}
  </div>`;
}

// Written out, not built by `emptyState`: the gate (`production/gate.js`) and its test find the id.
const generateAction = (s) =>
  `<button type="button" class="btn primary run" id="g-generate-copy"${s.generating ? " disabled" : ""}>${
    s.generating ? "Starting…" : "Generate campaign copy"
  }</button>`;

const generateErrorHtml = (s) => (s.generateError ? `<div class="gbody">${errorBox(s.generateError)}</div>` : "");

function copyCard(s) {
  const batch = s.art["copy_batch.json"];
  const status = s.art["copy_status.json"];
  const running = status && (status.state === "pending" || status.state === "running");
  const details = costAndChecks(s.art["llm_usage.json"], s.art["guardrail_report.json"]);
  if (batch) {
    const bands = templatesByBand(batch);
    return `<section class="card"><h3>Messages to review</h3>${summaryHtml(s, batch)}
      ${
        bands.length
          ? `<div class="gbands">${bands
              .map(([band, templates]) => bandSection(s, band, templates))
              .join("")}</div><p class="caption">A blocked message is never in the download, whatever its scores.</p>`
          : ""
      }
      ${details}
    </section>`;
  }
  if (running) {
    return `<section class="card"><h3>Writing campaign copy…</h3>${progressList(status)}</section>`;
  }
  if (status && status.state === "failed") {
    return `<section class="card"><h3>Campaign copy</h3>${emptyState({
      title: "The last attempt did not finish.",
      text: status.error_message || "",
      action: generateAction(s),
    })}${generateErrorHtml(s)}${details}</section>`;
  }
  return `<section class="card"><h3>Campaign copy</h3>${emptyState({
    title: "No campaign copy has been written for this run yet.",
    text: "We write messages for each band and channel from the customer details this use case allows, check them against the safety rules, and hold every one for your review. Nothing is sent to anyone.",
    action: generateAction(s),
  })}${generateErrorHtml(s)}</section>`;
}

function downloadAction(s) {
  const batch = s.art["copy_batch.json"];
  if (!batch) return null;
  // The file is every message that was not blocked, as written (`copy_messages.csv`); approving
  // records a decision and does not rewrite it, so the label says "messages", not "approved".
  return batch.messages_rendered > 0
    ? { label: "Download messages (CSV)", href: copyMessagesUrl(s.runId), attrs: 'download id="g-download-copy"' }
    : `<span class="btn primary" aria-disabled="true" id="g-download-copy">Download messages (CSV)</span><span class="reason">No message could be written for these customers.</span>`;
}

// --- shell -------------------------------------------------------------------------------------

function crumbTrail(uc) {
  return `<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}<span class="sep" aria-hidden="true">›</span><a href="#/uc/${esc(
    uc.id,
  )}">${esc(uc.name)}</a><span class="sep" aria-hidden="true">›</span><span class="cur" aria-current="page">Campaign copy</span></nav>`;
}

export function copyHtml(uc, s) {
  const run = s.run;
  if (!run) return skeleton("page", { title: "Campaign copy" });
  const config = s.art["run_config.json"] && s.art["run_config.json"].config;
  const llm = config && readPath(config, "generative.llm");
  const contacts = `#/uc/${encodeURIComponent(uc.id)}/output/${encodeURIComponent(run.run_id)}`;
  return `<main class="screen gscreen t-${esc(uc.marker)}">
    ${pageHead(
      `${crumbTrail(uc)}<h1 class="h1">Campaign copy</h1><p class="desc">Messages written for the customers this scoring run picked, held for your review before anyone downloads them.</p><div class="chips">${gTypeChip(
        uc,
      )}</div>${headActions({ primary: downloadAction(s), related: { label: "See who to contact", href: contacts } })}`,
    )}
    ${backendBadge(llm)}
    <div class="stack">
      ${copyCard(s)}
      ${techDetails([
        ["Scoring run", run.run_id],
        ["Run created", fmtStamp(run.created_at)],
      ])}
    </div>
  </main>`;
}

// --- controller ----------------------------------------------------------------------------------

/**
 * `options.signedInName()` is the signed-in person's display name, or `null` when nobody is signed
 * in (sign-in off): then the approver types a name, as before.
 */
export function createCopyController(uc, runId, rerender, options = {}) {
  const s = stateFor(runId);
  let timer = null;
  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  function adoptSignedInName() {
    let name = null;
    try {
      name = typeof options.signedInName === "function" ? options.signedInName() : null;
    } catch {
      name = null;
    }
    if (name) {
      s.approverName = name;
      s.approverLocked = true;
    } else if (s.approverLocked) {
      s.approverName = "";
      s.approverLocked = false;
    }
  }

  async function load() {
    adoptSignedInName();
    const [detail, art] = await Promise.all([getRun(runId), getArtefacts(runId, ARTEFACTS)]);
    s.run = detail.run;
    s.art = art;
    const status = art["copy_status.json"];
    if (status && (status.state === "pending" || status.state === "running")) poll();
  }

  function poll() {
    stop();
    timer = setInterval(async () => {
      try {
        s.art = await getArtefacts(runId, ARTEFACTS);
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        return stop();
      }
      const status = s.art["copy_status.json"];
      if (status && (status.state === "pending" || status.state === "running")) return rerender();
      stop();
      rerender();
    }, POLL_MS);
  }

  async function generate() {
    s.generating = true;
    s.generateError = null;
    rerender();
    try {
      await postCampaignCopy(runId);
      s.art = await getArtefacts(runId, ARTEFACTS);
      s.generating = false;
      rerender();
      poll();
    } catch (error) {
      s.generating = false;
      s.generateError = error;
      rerender();
    }
  }

  function replaceTemplate(updated) {
    const batch = s.art["copy_batch.json"];
    if (!batch) return;
    s.art = {
      ...s.art,
      "copy_batch.json": {
        ...batch,
        templates: batch.templates.map((t) => (t.template_id === updated.template_id ? updated : t)),
      },
    };
  }

  async function approve(templateId) {
    s.busy = { ...s.busy, [templateId]: "approve" };
    s.actionError = null;
    rerender();
    try {
      const updated = await postApproveTemplate(runId, templateId, s.approverName.trim());
      replaceTemplate(updated);
    } catch (error) {
      s.actionError = error;
    }
    s.busy = { ...s.busy, [templateId]: null };
    rerender();
  }

  async function regenerate(templateId) {
    s.busy = { ...s.busy, [templateId]: "regenerate" };
    s.actionError = null;
    rerender();
    try {
      const updated = await postRegenerateTemplate(runId, templateId);
      replaceTemplate(updated);
    } catch (error) {
      s.actionError = error;
    }
    s.busy = { ...s.busy, [templateId]: null };
    rerender();
  }

  /** Enable or disable each Approve as the typed name appears or goes, without redrawing the field. */
  function syncApproveButtons(root) {
    const batch = s.art["copy_batch.json"];
    if (!batch) return;
    const named = Boolean(s.approverName.trim());
    for (const button of root.querySelectorAll("[data-approve]")) {
      const template = batch.templates.find((t) => t.template_id === button.dataset.approve);
      const ok = named && template && template.status === "pending_review" && !s.busy[template.template_id];
      if ("pbGate" in button.dataset) continue; // a role that may not approve stays as the gate left it
      button.disabled = !ok;
    }
    const help = root.querySelector("#g-approver-help");
    if (help && !s.approverLocked) {
      help.textContent = named
        ? "Recorded with each message you approve."
        : "Type your name to approve messages. It is recorded with each one.";
    }
  }

  function bind(root) {
    const generateBtn = root.querySelector("#g-generate-copy");
    if (generateBtn) generateBtn.addEventListener("click", () => generate());
    const approver = root.querySelector("#g-approver");
    if (approver && !s.approverLocked) {
      approver.addEventListener("input", (event) => {
        s.approverName = event.target.value;
        syncApproveButtons(root);
      });
    }
    root.querySelectorAll("[data-approve]").forEach((button) =>
      button.addEventListener("click", () => approve(button.dataset.approve)),
    );
    root.querySelectorAll("[data-regen]").forEach((button) =>
      button.addEventListener("click", () => regenerate(button.dataset.regen)),
    );
  }

  return { state: s, load, bind, stop };
}
