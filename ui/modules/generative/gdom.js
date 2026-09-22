// Render helpers specific to the three generative screens.
//
// `ui/dom.js` stays untouched: its helpers are generic across every screen in the app, where the
// ones here read shapes only this package's contracts produce (a `Citation`, a `GuardrailCheck`, an
// `LlmUsageReport`). They exist because the three rules of `engine/generative` - grounded or
// nothing, guardrails before storage, cost is an artefact - are not abstract here: a citation with
// no quote, a guardrail outcome nobody rendered, or a cost line silently dropped would each let one
// of those rules go unobserved on the one screen a reviewer actually reads.

import { EM_DASH, dash, esc, fmtInt, fmtNum, fmtStamp, present } from "../../dom.js";
import { injectGenerativeStyles } from "./styles.js";

injectGenerativeStyles();

/**
 * The backend indicator every generative screen carries above its content (plan §13.3): a fake
 * backend must be as obvious as a watermark, because nothing it renders should be mistaken for a
 * real answer, a real root cause or copy fit to send. `llm` is the resolved `generative.llm` block
 * from a run's `run_config.json`, or the equivalent snapshot an index build was made with.
 *
 * It is a link, always to the AWS connection screen: which identity answered is exactly the
 * question that screen exists to answer, so the badge that names a backend is also how a person
 * finds where that backend's credentials come from.
 */
export function backendBadge(llm) {
  const href = `href="#/generative/connection"`;
  if (!llm) return `<a class="gbackend g-unknown" ${href}>Backend ${EM_DASH}</a>`;
  if (llm.backend === "fake") {
    return `<a class="gbackend g-fake" ${href}><b>Fake backend</b><span>Deterministic stand-in · no model was called anywhere on this screen · $0 could ever be spent</span></a>`;
  }
  const model = present(llm.generation_model_id) ? llm.generation_model_id : EM_DASH;
  const region = present(llm.region) ? ` · ${esc(llm.region)}` : "";
  return `<a class="gbackend g-live" ${href}><b>Bedrock</b><span>${esc(model)}${region}</span></a>`;
}

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
  return `<div class="gusage"><b>${fmtInt(calls)}</b> call${calls === 1 ? "" : "s"} · <b>${fmtInt(
    tokens,
  )}</b> tokens · <b>${dash(cost, (v) => `$${fmtNum(v, 4)}`)}</b>${
    usage.cache_hits ? ` · ${fmtInt(usage.cache_hits)} cache hit${usage.cache_hits === 1 ? "" : "s"}` : ""
  }${priceUnknown ? `<span class="gwarn"> price unknown for one model - cost is a partial total</span>` : ""}</div>`;
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

/** One `Citation`: document, section and the exact quote, because a hidden source is a guess. */
export function citationCard(citation) {
  return `<figure class="gcite"><figcaption>${esc(citation.document)} · ${esc(
    citation.section,
  )} <span class="gsim">sim ${fmtNum(citation.similarity, 2)}</span></figcaption><blockquote>&ldquo;${esc(
    citation.quote,
  )}&rdquo;</blockquote></figure>`;
}

/** One evidence-pack reference (`EvidenceReason` or `RedactedComplaint`), by its shared `id`. */
export function evidenceChip(item) {
  if (!item) return "";
  return "feature" in item
    ? `<span class="gref" title="${esc(item.share_pct.toFixed(1))}% of segment contribution"><code>${esc(
        item.feature,
      )}</code> ${item.direction === "up" ? "↑" : item.direction === "down" ? "↓" : ""}</span>`
    : `<span class="gref gref-quote">&ldquo;${esc(item.text)}&rdquo;</span>`;
}

export const confidencePill = (level) =>
  `<span class="pill ${level === "high" ? "ok" : level === "medium" ? "warn" : "bad"}">${esc(level)} confidence</span>`;

export const copyStatusPill = (status) =>
  `<span class="pill ${status === "approved" ? "ok" : status === "blocked" ? "bad" : "warn"}">${
    status === "pending_review" ? "Pending review" : status === "approved" ? "Approved" : "Blocked"
  }</span>`;

/** `stamp`-or-dash, next to the job kind, for the small print under a generative status header. */
export const jobMeta = (jobId, updatedAt) => `${esc(jobId)} · ${esc(fmtStamp(updatedAt))}`;
