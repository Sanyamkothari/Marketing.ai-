// Plan K M139 (DEC-1406): what the campaign planner shows, as pure functions of the server's answers.
//
// No DOM and no fetch here, so `tests/agentic/test_planner_ui.py` runs these with plain node against the
// real `POST /agentic/plans` answer. Every word and number drawn is the server's: the plan's payload, the
// policy's decisions and the narrative sentences the claim verifier rendered. Nothing is computed here.

const esc = (value) =>
  String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

/**
 * Whether the deployment offers the planner, from the `/auth/me` answer the page already has: the planner's
 * routes are declared (and so listed among the permissions) only while `agentic.enabled` is on. No answer, or
 * no such route listed: not offered, and nothing is asked of the server.
 */
export function plannerOffered(me) {
  const permissions = me && Array.isArray(me.permissions) ? me.permissions : [];
  return permissions.some((p) => p && p.method === "POST" && p.path === "/agentic/plans");
}

/** The card codes a campaign can answer (the server says the others plan nothing, and why). */
export const PLANNABLE = Object.freeze([
  "CAMPAIGN_NO_CONTROL",
  "PLAN_UNDERPOWERED",
  "CONTROL_GROUP_CONTACTED",
  "UPLIFT_NOT_BETTER_THAN_RISK",
  "EFFECT_FADING",
]);

/** The "Draft a plan" button under one warning card, or "" for a card no campaign answers. */
export function draftButtonHtml(card) {
  if (!card || !PLANNABLE.includes(card.code)) return "";
  const attrs = [`data-draft-plan="${esc(card.code)}"`];
  if (card.campaign_id) attrs.push(`data-plan-campaign="${esc(card.campaign_id)}"`);
  if (card.use_case_id) attrs.push(`data-plan-use-case="${esc(card.use_case_id)}"`);
  return ` <button type="button" class="btn-link" ${attrs.join(" ")}>Draft a plan</button>`;
}

/** The body `POST /agentic/plans` takes for a card the button names. */
export function cardRequest(code, campaignId, useCaseId) {
  const card = { code };
  if (campaignId) card.campaign_id = campaignId;
  if (useCaseId) card.use_case_id = useCaseId;
  return { card };
}

const RULE_WORDS = Object.freeze({
  "holdout.immutable": "The control group is kept",
  "budget.cap": "Within the budget",
  "consent.channel": "Only people who may be contacted",
  "frequency.cap": "Within the contact limits",
  "test.registered": "The test plan is fixed in advance",
  "role.separation": "Someone else approves",
});

/** One drafted plan: what would be done, the checks, the wording, and what a person can do now. */
export function planHtml(view, { canApprove = false } = {}) {
  if (!view || !view.plan) return "";
  const proposal = view.proposal;
  if (!proposal) return "";
  const p = proposal.payload || {};
  const plan = p.test_plan || {};
  const rows = [
    ["List", p.run_id],
    ["Channel", p.channel],
    ["People to contact (at most)", p.planned_contacts],
    ["Control group share", p.holdout && p.holdout.fraction],
    ["Result can be read on", plan.analysis_date],
    ["Smallest effect the test is planned to see (points)", plan.mde_pp],
  ]
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([label, value]) => `<li><span class="sp-label">${esc(label)}</span> <strong>${esc(value)}</strong></li>`)
    .join("");
  const checks = (proposal.policy || [])
    .map(
      (decision) =>
        `<li data-rule="${esc(decision.rule)}" class="${decision.allowed ? "ok" : "refused"}">${esc(
          RULE_WORDS[decision.rule] || decision.rule,
        )}: ${decision.allowed ? "yes" : esc((decision.detail && decision.detail.reason) || decision.code)}</li>`,
    )
    .join("");
  const sentences = ((proposal.narrative && proposal.narrative.sentences) || [])
    .map((sentence) => `<p class="sp-text" data-sentence>${esc(sentence.text)}</p>`)
    .join("");
  const pending = proposal.status === "pending";
  const actions =
    pending && canApprove
      ? `<div class="actions"><button type="button" class="btn" data-plan-approve="${esc(proposal.proposal_id)}">Approve</button> <button type="button" class="btn-link" data-plan-reject="${esc(proposal.proposal_id)}">Reject</button></div>`
      : pending
        ? `<p class="sp-text"><em>Waiting for an Approver.</em></p>`
        : `<p class="sp-text"><em>${esc(proposal.status === "refused" ? "The rules refuse this plan; nothing will be sent." : proposal.status)}</em></p>`;
  return `<section class="card sp-plan" data-plan="${esc(view.plan.plan_id)}" data-proposal="${esc(
    proposal.proposal_id,
  )}" data-status="${esc(proposal.status)}"><h4>Drafted plan</h4>${sentences}<ul class="sp-facts">${rows}</ul><ul class="sp-checks">${checks}</ul>${actions}</section>`;
}

/** A refusal from the planner, in the server's own words. */
export function refusalHtml(code, message) {
  return `<p class="sp-text" data-plan-refused="${esc(code)}">${esc(message)}</p>`;
}
