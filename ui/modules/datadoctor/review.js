// The one approval screen of the Data Doctor (roadmap 2.5): `view.review` of `GET /cases/{case_id}/screen`
// (`engine/doctor/review.py`) drawn as one page, in the order a person reads it: the plan in plain words, what was
// decided or assumed (each with yes / change and, where an answer needs a value, the right input), the problems (a hard
// stop is drawn apart, with its reason and what would fix it), the swaps (old beside new) and ONE Approve button.
//
// Pure functions from the answer and a few ticks to HTML strings; no DOM, no fetch. The actions are the ones the
// screen already has (`data-rv-*` attributes are handled in `screen.js`): answers, decisions, confirmations,
// acknowledgements and the approval itself. A tick kept here is only the person's reading ("yes, that is right"); what
// is recorded is what the server records when Approve is clicked.

import { button, icon } from "../../components.js";
import { esc } from "../../dom.js";
import { namesOf, userTextIn as nameIn } from "./view.js";

const userTextIn = (text, names) => nameIn(text, names, { friendly: true });
const keyOf = (d) => d.question_id || d.item_id || d.title;

const KIND_LABEL = Object.freeze({
  customer_id: "Customer ID",
  outcome: "Outcome",
  date: "Dates",
  consent: "Consent",
  other: "Other",
});
const BY = Object.freeze({
  person: "You decided this.",
  rules: "Data Doctor assumed this.",
  agent: "The AI chose this.",
  earlier: "Your earlier answer, used again.",
});
const INPUT_TYPE = Object.freeze({ date: "date", number: "number", text: "text" });

/** The review of a screen, or null (an older server, or no files yet). */
export const reviewOf = (screen) => (screen && screen.view && screen.view.review) || null;

/**
 * What Approve waits for: the server's own reasons first, then the person's: an assumption not yet said yes to, a
 * problem not yet answered, a problem answered "no". Empty means Approve may be clicked.
 */
export function waitingFor(review, ctx) {
  if (!review || review.approve.stage !== "approve") return [];
  const out = [...review.approve.waiting];
  for (const d of review.decisions) {
    if (d.answered && d.needs_yes) out.push(`Say yes or change: ${d.title}`); // the server's own: a yes is saved there
  }
  for (const p of review.problems) {
    if (p.hard || !p.acknowledgeable || p.acknowledged) continue; // the server's own reasons above already say a rule is not met
    if (ctx.nos.has(p.key)) out.push(`You said no, so change the plan or the file: ${p.text}`);
    else if (!ctx.stops.has(p.key)) out.push(`Answer yes or no: ${p.text}`);
  }
  return out;
}

export const reviewAllowed = (review, ctx) => Boolean(review) && review.approve.stage === "approve" && waitingFor(review, ctx).length === 0;

// --- the parts -------------------------------------------------------------------------------------------------

function planHtml(review, names) {
  if (!review.plan.length) return `<p class="dds-why" data-rv-plan-empty>Nothing in the plan changes your data.</p>`;
  return `<ol class="dds-list" data-rv-plan>${review.plan
    .map(
      (l) =>
        `<li class="dds-opt" data-rv-step="${esc(l.item_id)}"><span class="dds-item-t">${userTextIn(l.text, names)}</span>${
          l.counts ? `<span class="dds-effect">${esc(l.counts)}</span>` : ""
        }</li>`,
    )
    .join("")}</ol>`;
}

function inputFor(d, o, busy) {
  const type = INPUT_TYPE[o.value_kind];
  const chosen = d.answer === o.id;
  return `<input type="${type}" class="dds-date" data-rv-value="${esc(o.id)}" data-rv-q="${esc(d.question_id)}" value="${esc(chosen ? d.answer_value || "" : "")}" aria-label="${esc(`${o.value_kind === "date" ? "Date" : o.value_kind === "number" ? "Number" : "Text"} for: ${o.label}`)}"${busy ? " disabled" : ""}>`;
}

function optionsHtml(d, names, busy) {
  return `<ul class="dds-opts" data-rv-options="${esc(keyOf(d))}">${d.options
    .map((o) => {
      const chosen = d.answer === o.id;
      return `<li class="dds-opt">${button({
        label: o.label,
        kind: chosen ? "primary" : "secondary",
        size: "sm",
        attrs: `data-rv-answer="${esc(o.id)}" data-rv-q="${esc(d.question_id)}" aria-pressed="${chosen}"`,
        disabled: busy || (chosen && !o.value_kind),
      })}${o.value_kind ? inputFor(d, o, busy) : ""}${o.effect ? `<span class="dds-effect">${userTextIn(o.effect, names)}</span>` : ""}${
        o.recommended ? `<span class="dds-effect">Recommended</span>` : ""
      }</li>`;
    })
    .join("")}</ul>`;
}

function decisionHtml(d, ctx, names) {
  const k = keyOf(d);
  const said = !d.needs_yes; // the yes is the server's record, so it survives a reload and shows in the approval
  const changing = ctx.changing.has(k);
  const open = !d.answered || changing;
  const label = KIND_LABEL[d.kind] || KIND_LABEL.other;
  const current = d.current
    ? `<p class="dds-state" data-rv-current>${icon(said || !d.needs_yes ? "check" : "info", { size: 14 })}<span>${userTextIn(d.current, names)}${
        d.decided_by ? ` <span class="dds-why">${esc(BY[d.decided_by] || "")}</span>` : ""
      }</span></p>`
    : `<p class="dds-state" data-rv-open>${icon("info", { size: 14 })}<span>Not answered yet.</span></p>`;
  const actions = [];
  if (d.answered && d.needs_yes) {
    actions.push(button({ label: "Yes, that is right", kind: "primary", size: "sm", attrs: `data-rv-yes="${esc(k)}"`, disabled: Boolean(ctx.busy) }));
  }
  if (d.answered && d.question_id) {
    actions.push(button({ label: changing ? "Keep it" : "Change", kind: "tertiary", size: "sm", attrs: `data-rv-change="${esc(k)}"` }));
  } else if (d.answered && d.item_id) {
    actions.push(button({ label: "Change", kind: "tertiary", size: "sm", attrs: `data-rv-undo="${esc(d.item_id)}"`, disabled: Boolean(ctx.busy) }));
  }
  return `<li class="dds-card" data-rv-decision="${esc(k)}" data-rv-kind="${esc(d.kind)}"><span class="dds-label">${esc(label)}</span><p class="dds-card-t">${userTextIn(
    d.title,
    names,
  )}</p>${d.why ? `<p class="dds-why">${userTextIn(d.why, names)}</p>` : ""}${current}${actions.length ? `<div class="dds-actions">${actions.join("")}</div>` : ""}${
    open && d.question_id ? optionsHtml(d, names, Boolean(ctx.busy)) : ""
  }</li>`;
}

function problemHtml(p, ctx, names) {
  const text = userTextIn(p.text, names);
  const detail = p.detail ? `<details class="ddc-detail"><summary>See details</summary><p class="dds-why">${userTextIn(p.detail, names)}</p></details>` : "";
  if (p.hard || !p.acknowledgeable) {
    return `<li class="dds-card is-hard" data-rv-problem="${esc(p.key)}" data-rv-hard role="group" aria-label="Rule not met"><span class="dds-label">A rule the data must meet</span><p class="dds-card-t">${text}</p><p class="dds-why" data-rv-reason>No one can say yes to this.</p><p class="dds-why" data-rv-fix><strong>What would fix it:</strong> ${userTextIn(
      p.fix || "Change the plan or the file.",
      names,
    )}</p>${detail}</li>`;
  }
  if (p.acknowledged) {
    return `<li class="dds-card" data-rv-problem="${esc(p.key)}" data-rv-done><p class="dds-state">${icon("check", { size: 14 })}<span>${text} <span class="dds-why">You said yes.</span></span></p></li>`;
  }
  const yes = ctx.stops.has(p.key);
  const no = ctx.nos.has(p.key);
  return `<li class="dds-card" data-rv-problem="${esc(p.key)}"><span class="dds-label">A safety check</span><p class="dds-card-t">${text}</p><p class="dds-why">It stops this plan until you say yes. ${
    p.fix ? `To fix it instead: ${userTextIn(p.fix, names)}.` : ""
  }</p>${detail}<div class="dds-actions" role="group" aria-label="Go on anyway?">${button({
    label: "Yes, go on",
    kind: yes ? "primary" : "secondary",
    size: "sm",
    attrs: `data-rv-stop-yes="${esc(p.key)}" aria-pressed="${yes}"`,
    disabled: Boolean(ctx.busy),
  })}${button({
    label: "No",
    kind: no ? "primary" : "secondary",
    size: "sm",
    attrs: `data-rv-stop-no="${esc(p.key)}" aria-pressed="${no}"`,
    disabled: Boolean(ctx.busy),
  })}</div></li>`;
}

function swapHtml(s, ctx, names) {
  const side = (title, label, counts, id) =>
    `<div class="dds-swap-side" data-rv-swap-${id}><span class="dds-label">${label}</span><p class="dds-card-t">${userTextIn(title, names)}</p>${counts ? `<p class="dds-effect">${esc(counts)}</p>` : ""}</div>`;
  const choose =
    s.state === "pending"
      ? `<div class="dds-actions" role="group" aria-label="Which step">${button({
          label: "Use the new step",
          kind: "primary",
          size: "sm",
          attrs: `data-rv-swap-use="${esc(s.new_item_id)}"`,
          disabled: Boolean(ctx.busy),
        })}${button({
          label: "Keep the old step",
          kind: "secondary",
          size: "sm",
          attrs: `data-rv-swap-keep="${esc(s.new_item_id)}"`,
          disabled: Boolean(ctx.busy),
        })}</div>`
      : `<p class="dds-why">Already swapped.</p>`;
  return `<li class="dds-card" data-rv-swap="${esc(s.new_item_id)}" data-rv-swap-state="${esc(s.state)}"><p class="dds-why">${esc(
    s.by === "agent" ? "The AI would replace a step you accepted." : "One step would replace another.",
  )}</p><div class="dds-swap">${side(s.old_title, "Old", s.old_counts, "old")}${side(s.new_title, "New", s.new_counts, "new")}</div>${choose}</li>`;
}

function agreementHtml(a, ctx, names) {
  const who = a.cannot_agree === "separation" ? "another person on your team" : "someone with the right role";
  if (!a.can_agree) {
    return `<li class="dds-state" data-rv-agreement="${esc(a.item_id)}" data-rv-ask-other>${icon("info", { size: 14 })}<span>${userTextIn(a.sentence, names)}<br>Ask ${esc(who)} to agree to it. ${esc(
      a.required ? "It is in the plan, so this waits for them." : "Until then it is left out.",
    )}</span></li>`;
  }
  if (a.required) {
    return `<li class="dds-state" data-rv-agreement="${esc(a.item_id)}" data-rv-agreed>${icon("check", { size: 14 })}<span>${userTextIn(a.sentence, names)}<br>Your yes is recorded when you click Approve.</span></li>`;
  }
  return `<li><label class="dds-tick" data-rv-agreement="${esc(a.item_id)}"><input type="checkbox" data-rv-confirm="${esc(a.item_id)}"${ctx.confirms.has(a.item_id) ? " checked" : ""}${ctx.busy ? " disabled" : ""}><span class="dds-item-body"><span class="dds-item-t">Also include: ${userTextIn(a.sentence, names)}</span><span class="dds-item-sub">Left out unless you tick it.</span></span></label></li>`;
}

const part = (title, id, inner, level) =>
  inner ? `<section class="dds-section" data-rv-part="${id}"><h${level} class="dds-section-t">${esc(title)}</h${level}>${inner}</section>` : "";

/** The whole approval screen. `ctx`: `{ yes, nos, stops, confirms, changing, busy, level, alertHtml }` (Sets). */
export function reviewHtml(screen, ctx) {
  const review = reviewOf(screen);
  if (!review) return "";
  const names = namesOf(screen.case);
  const level = ctx.level || 3;
  const sub = level + 1;
  const waiting = waitingFor(review, ctx);
  const ok = waiting.length === 0 && review.approve.stage === "approve";
  const list = (items, fn) => (items.length ? `<ul class="dds-list">${items.map(fn).join("")}</ul>` : "");
  const decisions = list(review.decisions, (d) => decisionHtml(d, ctx, names));
  const problems = list(review.problems, (p) => problemHtml(p, ctx, names));
  const swaps = list(review.swaps, (s) => swapHtml(s, ctx, names));
  const agreements = list(review.agreements, (a) => agreementHtml(a, ctx, names));
  const stage =
    review.approve.stage === "approve"
      ? ""
      : `<p class="dds-why" role="status" data-rv-stage="${esc(review.approve.stage)}">${esc(
          review.approve.stage === "start" ? "This is approved. Start it from the chat." : review.approve.stage === "blocked" ? "The last check found something to fix first." : "There is nothing to approve yet.",
        )}</p>`;
  const hold = waiting.length
    ? `<ul class="dds-why" data-rv-waiting>${waiting.map((w) => `<li>${userTextIn(w, names)}</li>`).join("")}</ul>`
    : "";
  return `<div class="dds" data-dds data-rv data-dds-case="${esc(screen.case.case_id)}"><div class="dds-actions">${button({
    label: "Back to the chat",
    kind: "secondary",
    attrs: "data-rv-back",
  })}</div><h${level} class="dds-section-t">Check everything, then approve</h${level}>${stage}${part("The plan", "plan", planHtml(review, names), sub)}${part(
    "What I assumed or decided",
    "decisions",
    decisions,
    sub,
  )}${part("Problems", "problems", problems, sub)}${part("Swapped steps", "swaps", swaps, sub)}${part("Needs your own yes", "agreements", agreements, sub)}${
    review.approve.stage === "approve"
      ? `<section class="ddc-ready${ok ? "" : " is-blocked"}" data-rv-approve-card aria-labelledby="rv-approve-t"><h${sub} class="dds-group-t" id="rv-approve-t">Approve</h${sub}><blockquote class="dds-sentence" id="rv-sentence" data-rv-sentence>${esc(
          review.approve.sentence,
        )}</blockquote>${hold}${ctx.alertHtml || ""}<div class="dds-actions">${button({
          label: "Approve",
          kind: "primary",
          attrs: `data-rv-approve aria-describedby="rv-sentence"`,
          disabled: Boolean(ctx.busy) || !ok,
          busy: ctx.busy === "approve",
        })}<span class="dds-why">The only approval. Your files are not changed.</span></div></section>`
      : ""
  }</div>`;
}
