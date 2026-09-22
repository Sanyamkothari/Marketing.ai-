// The win-back campaign-copy screen (prototype 14, light and dark): generated messages per band and
// channel, waiting for a person before anything counts as usable.
//
// `CopyBatch.require_human_review` is not a UI convenience - `DEC-205` sets it true by default
// because this text is written to be sent to a customer, so the screen's one job beyond showing the
// copy is to make the review real: every template's guardrail outcome is on screen rather than
// folded into a pass/fail dot, a blocked template is shown blocked rather than hidden (a card that
// silently disappeared would read as "nothing to review" instead of "a rule refused this"), and the
// control holdout - the customers this campaign deliberately does not write to - is a number here,
// not a fact left to the CSV. Nothing this screen does sends anything: approving a template records
// that a named person accepted it (DEC-055's unverified-claim pattern, same as model approval), and
// the actual export is the CSV download, same as `scores.csv` is for a predictive run.

import { ApiError, getArtefacts, getRun } from "../../api.js";
import { dash, errorBox, esc, fmtInt, fmtNum, fmtStamp, kpis, pageHead, stageChip, typeChip } from "../../dom.js";
import { backendBadge, copyStatusPill, guardrailCounts, guardrailList, usageLine } from "./gdom.js";
import { copyMessagesUrl, postApproveTemplate, postCampaignCopy, postRegenerateTemplate } from "./api.js";
import { readPath } from "../../settings.js";

const POLL_MS = 2000;
const STATE = new Map();

const ARTEFACTS = [
  "scoring_summary.json",
  "run_config.json",
  "copy_status.json",
  "copy_batch.json",
  "guardrail_report.json",
  "llm_usage.json",
];

function stateFor(runId) {
  if (!STATE.has(runId)) {
    STATE.set(runId, {
      runId,
      run: null,
      art: {},
      generating: false,
      generateError: null,
      approverName: "",
      busy: {},
      actionError: null,
    });
  }
  return STATE.get(runId);
}

// --- context tiles -----------------------------------------------------------------------------

function kpiTiles(uc, s) {
  const summary = s.art["scoring_summary.json"];
  const batch = s.art["copy_batch.json"];
  return kpis([
    [uc.output.kpi.label, dash(summary && summary.kpi && summary.kpi.display)],
    ["Eligible for copy", dash(batch && batch.audience.rows, fmtInt)],
    ["Control holdout", dash(batch && batch.holdout.control_rows, fmtInt)],
    ["Suppressed", dash(batch && batch.holdout.suppressed_rows, fmtInt)],
  ]);
}

// --- one template --------------------------------------------------------------------------------

const JUDGE_LABEL = (purpose) => purpose.replace(/^judge_/, "").replace(/^./, (c) => c.toUpperCase());

function judgeLine(template) {
  const scores = template.judge_scores.map((j) => `${JUDGE_LABEL(j.purpose)} ${fmtNum(j.score, 2)}`);
  scores.push(`${template.text.length} characters`);
  return scores.join(" · ");
}

function templateCard(runId, s, template) {
  const busy = s.busy[template.template_id];
  const canApprove = template.status === "pending_review" && s.approverName.trim() && !busy;
  const canRegenerate = !busy;
  return `<div class="gtpl${template.status === "blocked" ? " blocked" : ""}">
    <div class="gtpl-head"><span>${esc(template.channel.toUpperCase())} · Variant ${esc(
      template.variant,
    )}</span>${copyStatusPill(template.status)}</div>
    ${template.subject ? `<div class="gtpl-subj">${esc(template.subject)}</div>` : ""}
    <div class="gtpl-body">${esc(template.text)}</div>
    <div class="gtpl-meta">${esc(judgeLine(template))}</div>
    ${template.block_reason ? `<div class="gtpl-block-reason">${esc(template.block_reason)}</div>` : ""}
    ${guardrailList(template.guardrails)}
    ${
      template.status === "approved"
        ? `<div class="gtpl-meta">Approved by ${esc(template.approved_by)} · ${esc(
            fmtStamp(template.approved_at),
          )}</div>`
        : ""
    }
    <div class="gtpl-actions">
      ${
        template.status === "approved"
          ? ""
          : `<button type="button" class="g-approve" data-approve="${esc(
              template.template_id,
            )}"${canApprove ? "" : " disabled"}>${busy === "approve" ? "Approving…" : "Approve"}</button>`
      }
      <button type="button" class="g-regen" data-regen="${esc(template.template_id)}"${
        canRegenerate ? "" : " disabled"
      }>${busy === "regenerate" ? "Regenerating…" : "Regenerate"}</button>
    </div>
  </div>`;
}

function bandSection(runId, s, band, templates) {
  return `<div class="gband-h">${esc(band)} band</div><div class="ggrid">${templates
    .map((t) => templateCard(runId, s, t))
    .join("")}</div>`;
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

function copyCard(uc, s) {
  const batch = s.art["copy_batch.json"];
  const status = s.art["copy_status.json"];
  const running = status && (status.state === "pending" || status.state === "running");
  if (batch) {
    const approved = batch.templates.filter((t) => t.status === "approved").length;
    const pending = batch.templates.filter((t) => t.status === "pending_review").length;
    const blocked = batch.templates.filter((t) => t.status === "blocked").length;
    const bands = new Set(batch.templates.map((t) => t.band)).size;
    const channels = new Set(batch.templates.map((t) => t.channel)).size;
    const canDownload = batch.messages_rendered > 0;
    return `<section class="card"><h3>Campaign copy</h3>
      <div style="padding:16px 20px 0">
        <div class="gnotice">Nothing is sent from here. ${
          batch.require_human_review ? "Approved messages are exported for your campaign tool." : "Every template is usable as generated; human review is off for this use case."
        }</div>
        <p class="gholdout-note">Holds back <b>${fmtInt(batch.holdout.control_rows)}</b> customers as a control group and suppresses <b>${fmtInt(
          batch.holdout.suppressed_rows,
        )}</b> more by consent or recent contact${
          batch.holdout.out_of_band_rows
            ? ` (${fmtInt(batch.holdout.out_of_band_rows)} fell outside a band this batch writes for)`
            : ""
        }.</p>
        <div class="gseg-row">
          <span>${fmtInt(batch.templates.length)} templates · ${bands} band${
            bands === 1 ? "" : "s"
          } × ${channels} channel${channels === 1 ? "" : "s"}</span>
          <span>${approved} approved · ${pending} pending review · ${blocked} blocked</span>
          <a class="linkbtn" href="${esc(copyMessagesUrl(s.runId))}"${
            canDownload ? " download" : ' aria-disabled="true" style="pointer-events:none;opacity:.5"'
          }>Download messages</a>
        </div>
        <div class="field sm" style="margin:14px 0 4px"><span class="sub">Your name, for the approval record</span><div class="control"><input type="text" id="g-approver" value="${esc(
          s.approverName,
        )}" placeholder="Required to approve"></div></div>
        ${s.actionError ? errorBox(s.actionError) : ""}
      </div>
      <div style="padding:6px 20px 20px">${templatesByBand(batch)
        .map(([band, templates]) => bandSection(s.runId, s, band, templates))
        .join("")}</div>
      <p class="caption">Scores come from the copy judges. A blocked message is never exported, whatever its scores.</p>
    </section>`;
  }
  if (running) {
    const stages = status.stages || [];
    const rows = stages.length
      ? stages
          .map(
            (st, i) =>
              `<li class="${st.state === "running" ? "active" : st.state === "done" ? "done" : st.state === "failed" ? "failed" : ""}"><span class="dot">${
                i + 1
              }</span><div><div class="pt">${esc(st.title)}</div><div class="pd">${esc(st.detail)}</div></div></li>`,
          )
          .join("")
      : `<li class="active"><span class="dot">1</span><div><div class="pt">Starting…</div><div class="pd"></div></div></li>`;
    return `<section class="card"><h3>Campaign copy</h3><ol class="progress">${rows}</ol></section>`;
  }
  if (status && status.state === "failed") {
    return `<section class="card"><h3>Campaign copy</h3><div class="empty">${esc(
      status.error_message || "The last attempt failed.",
    )}</div><div style="padding:0 20px 20px">${generateButton(s)}</div></section>`;
  }
  return `<section class="card"><h3>Campaign copy</h3><div style="padding:18px 20px">
    <p class="desc" style="margin:0 0 14px">Copy has not been written for this run yet.</p>
    <p class="desc" style="margin:0 0 14px;color:var(--muted)">We write templates per band and channel from the fields this use case whitelists, check them against the guardrails, and hold every one for review. Nothing is sent to anyone.</p>
    ${s.generateError ? errorBox(s.generateError) : ""}
    ${generateButton(s)}
  </div></section>`;
}

const generateButton = (s) =>
  `<button type="button" class="run" id="g-generate-copy"${s.generating ? " disabled" : ""}>${
    s.generating ? "Starting…" : "Generate campaign copy"
  }</button>`;

function usageCard(s) {
  const usage = s.art["llm_usage.json"];
  const guardrails = s.art["guardrail_report.json"];
  if (!usage && !guardrails) return "";
  return `<section class="card"><h3>Cost &amp; guardrails</h3><div style="padding:16px 20px">${usageLine(
    usage,
  )}${
    guardrails
      ? `<div style="margin-top:6px">${guardrailCounts(guardrails.checks)} <span style="font-size:12px;color:var(--muted)">across ${fmtInt(
          guardrails.summary.checked,
        )} generated text${guardrails.summary.checked === 1 ? "" : "s"}</span></div>`
      : ""
  }</div></section>`;
}

// --- shell -------------------------------------------------------------------------------------

export function copyHtml(uc, s) {
  const run = s.run;
  if (!run) return `<main class="screen t-${esc(uc.marker)}">${pageHead(`<h1 class="h1">${esc(uc.name)}</h1>`)}<p class="loading">Loading the run…</p></main>`;
  const config = s.art["run_config.json"] && s.art["run_config.json"].config;
  const llm = config && readPath(config, "generative.llm");
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(
      `<nav class="crumbs" aria-label="Breadcrumb"><a href="#/">Customer Lifecycle</a><span class="sep">›</span><a href="#/uc/${esc(
        uc.id,
      )}">${esc(uc.name)}</a><span class="sep">›</span><span class="cur">Campaign copy</span></nav>
      <span class="over" style="color:var(--c)">Personalised offers</span><h1 class="h1">${esc(
        uc.pages.output,
      )}</h1><div class="chips">${stageChip(uc.lifecycle_stage)}${typeChip(uc)}</div>`,
    )}
    ${backendBadge(llm)}
    <p class="note" style="margin:-8px 0 16px">Scoring run ${esc(run.run_id)} · ${esc(fmtStamp(run.created_at))}</p>
    <div class="stack">
      ${kpiTiles(uc, s)}
      ${copyCard(uc, s)}
      ${usageCard(s)}
    </div>
  </main>`;
}

// --- controller ----------------------------------------------------------------------------------

export function createCopyController(uc, runId, rerender) {
  const s = stateFor(runId);
  let timer = null;
  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  async function load() {
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

  function bind(root) {
    const generateBtn = root.querySelector("#g-generate-copy");
    if (generateBtn) generateBtn.addEventListener("click", () => generate());
    const approver = root.querySelector("#g-approver");
    if (approver) approver.addEventListener("input", (event) => (s.approverName = event.target.value));
    root.querySelectorAll("[data-approve]").forEach((button) =>
      button.addEventListener("click", () => approve(button.dataset.approve)),
    );
    root.querySelectorAll("[data-regen]").forEach((button) =>
      button.addEventListener("click", () => regenerate(button.dataset.regen)),
    );
  }

  return { state: s, load, bind, stop };
}
