// Render helpers specific to the three generative screens.
//
// `ui/dom.js` stays untouched: its helpers are generic across every screen in the app, where the
// ones here read shapes only this package's contracts produce (a `Citation`, a `GuardrailCheck`, an
// `LlmUsageReport`). They exist because the three rules of `engine/generative` - grounded or
// nothing, guardrails before storage, cost is an artefact - are not abstract here: a citation with
// no quote, a guardrail outcome nobody rendered, or a cost line silently dropped would each let one
// of those rules go unobserved on the one screen a reviewer actually reads. v1 moves the cost and
// the passed checks behind a closed disclosure; a warning or a block stays on screen.

import { EM_DASH, dash, esc, fmtInt, fmtNum, fmtStamp, glossaryTerm, present, toggletip, typeChip } from "../../dom.js";
import { injectGenerativeStyles } from "./styles.js";

injectGenerativeStyles();

export const CONNECTION_HREF = "#/generative/connection";

/**
 * The line every generative screen carries above its content (plan §13.3): with the fake backend,
 * nothing on the screen came from a real AI service, and that must be as plain as a watermark - but
 * calm, one sentence. `llm` is the resolved `generative.llm` block from a run's `run_config.json`, or
 * the snapshot an index build was made with.
 *
 * It is a link, always to the AI service connection screen: which service answered is exactly the
 * question that screen answers, so the line that names it is also how a person finds where it is set.
 */
export function backendBadge(llm) {
  const href = `href="${CONNECTION_HREF}"`;
  if (!llm) return `<a class="gbackend g-unknown" ${href}>AI service ${EM_DASH}</a>`;
  if (llm.backend === "fake") {
    return `<a class="gbackend g-fake" ${href}>Practice mode: answers are sample text, not from a real AI service.</a>`;
  }
  const model = present(llm.generation_model_id) ? llm.generation_model_id : "";
  const region = present(llm.region) ? ` · ${llm.region}` : "";
  return `<a class="gbackend g-live" ${href}${
    model ? ` title="${esc(`${model}${region}`)}"` : ""
  }>Written by your company's AI service.</a>`;
}

/** The type chip with its words, not only its stars ("★★ Generative AI"). */
const TYPE_LABEL = { predictive: "Predictive AI", generative: "Generative AI", hybrid: "Hybrid" };
export const gTypeChip = (uc) => typeChip({ ...uc, label: TYPE_LABEL[uc.ai_type] || uc.label || "" });

/** One line of calls / tokens / cost, so cost reads as an artefact rather than an afterthought. */
export function usageLine(usage) {
  if (!usage) return `<div class="gusage">Usage ${EM_DASH}</div>`;
  const calls = usage.by_purpose.reduce((n, p) => n + p.calls, 0) || usage.totals.calls;
  const tokens =
    usage.by_purpose.reduce((n, p) => n + p.input_tokens + p.output_tokens, 0) ||
    usage.totals.input_tokens + usage.totals.output_tokens;
  const known = usage.by_purpose.every((p) => present(p.cost_estimate_usd));
  const cost = known
    ? usage.by_purpose.reduce((n, p) => n + (p.cost_estimate_usd || 0), 0)
    : usage.totals.cost_estimate_usd;
  const priceUnknown = (usage.warnings || []).some((w) => w.startsWith("PRICE_UNKNOWN"));
  return `<div class="gusage"><b>${fmtInt(calls)}</b> AI call${calls === 1 ? "" : "s"} · <b>${fmtInt(
    tokens,
  )}</b> tokens · <b>${dash(cost, (v) => `$${fmtNum(v, 4)}`)}</b> estimated cost${
    usage.cache_hits
      ? ` · ${fmtInt(usage.cache_hits)} answer${usage.cache_hits === 1 ? "" : "s"} reused`
      : ""
  }${priceUnknown ? `<span class="gwarn"> The price of one AI model is not known, so the cost is a partial total.</span>` : ""}</div>`;
}

/** A `GuardrailSummary`, or a plain sentence when only a list of `GuardrailCheck` is on hand. */
export function guardrailCounts(checks) {
  const passed = checks.filter((c) => c.outcome === "passed").length;
  const warned = checks.filter((c) => c.outcome === "warned").length;
  const blocked = checks.filter((c) => c.outcome === "blocked").length;
  return `<span class="pill ok">${passed} passed</span>${
    warned ? `<span class="pill warn">${warned} warned</span>` : ""
  }${blocked ? `<span class="pill bad">${blocked} blocked</span>` : ""}`;
}

/** Every `GuardrailCheck` of one generated item, rendered so a warning or a block is never silent. */
export function guardrailList(checks) {
  if (!checks || !checks.length) return "";
  return `<ul class="gchecks">${checks
    .map(
      (c) =>
        `<li class="${c.outcome}"><span class="pill ${
          c.outcome === "passed" ? "ok" : c.outcome === "warned" ? "warn" : "bad"
        }">${esc(c.rule)}</span><span>${esc(c.detail)}</span></li>`,
    )
    .join("")}</ul>`;
}

/** The checks that did not pass, on screen; `[]` when every check passed. */
export const flaggedChecks = (checks) => (checks || []).filter((c) => c.outcome !== "passed");

/** "Cost and safety checks": the usage line and the guardrail totals, in one closed disclosure. */
export function costAndChecks(usage, guardrails) {
  if (!usage && !guardrails) return "";
  return `<details class="tech gdetails"><summary>Cost and safety checks</summary><div class="gdetails-body">${usageLine(
    usage,
  )}${
    guardrails
      ? `<div class="gcounts">${guardrailCounts(guardrails.checks)}<span class="note">across ${fmtInt(
          guardrails.summary.checked,
        )} written text${guardrails.summary.checked === 1 ? "" : "s"}</span></div>`
      : ""
  }</div></details>`;
}

/** One `Citation`: document, section and the exact quote, because a hidden source is a guess. */
export function citationCard(citation) {
  return `<figure class="gcite"><figcaption>${esc(citation.document)} · ${esc(
    citation.section,
  )} <span class="gsim">match ${fmtNum(citation.similarity, 2)}</span></figcaption><blockquote>&ldquo;${esc(
    citation.quote,
  )}&rdquo;</blockquote></figure>`;
}

/** One evidence-pack reference (`EvidenceReason` or `RedactedComplaint`), by its shared `id`. */
export function evidenceChip(item) {
  if (!item) return "";
  return "feature" in item
    ? `<span class="gref" title="${esc(item.share_pct.toFixed(1))}% of this group's score"><code>${esc(
        item.feature,
      )}</code> ${item.direction === "up" ? "↑" : item.direction === "down" ? "↓" : ""}</span>`
    : `<span class="gref gref-quote">&ldquo;${esc(item.text)}&rdquo;</span>`;
}

export const confidencePill = (level) =>
  `<span class="pill ${level === "high" ? "ok" : level === "medium" ? "warn" : "bad"}">${esc(level)} confidence</span>`;

export const copyStatusPill = (status) =>
  `<span class="pill ${status === "approved" ? "ok" : status === "blocked" ? "bad" : "warn"}">${
    status === "pending_review" ? "Waiting for review" : status === "approved" ? "Approved" : "Blocked"
  }</span>`;

/** A "?" beside a term, worded by `configs/pilot/help.yaml` once the catalogue is registered. */
export function termTip(term, fallback) {
  const text = glossaryTerm(term) || fallback;
  return text ? toggletip(text, term) : "";
}

/** A job's progress list (root causes, campaign copy): one row per stage the status names. */
export function progressList(status) {
  const stages = (status && status.stages) || [];
  const rows = stages.length
    ? stages
        .map(
          (st, i) =>
            `<li class="${
              st.state === "running" ? "active" : st.state === "done" ? "done" : st.state === "failed" ? "failed" : ""
            }"><span class="dot">${i + 1}</span><div><div class="pt">${esc(st.title)}</div><div class="pd">${esc(
              st.detail,
            )}</div></div></li>`,
        )
        .join("")
    : `<li class="active"><span class="dot">1</span><div><div class="pt">Starting…</div><div class="pd"></div></div></li>`;
  return `<ol class="progress">${rows}</ol>`;
}

/** `stamp`-or-dash, next to the job kind, for the small print under a generative status header. */
export const jobMeta = (jobId, updatedAt) => `${esc(jobId)} · ${esc(fmtStamp(updatedAt))}`;
