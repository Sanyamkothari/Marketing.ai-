/* Proof: did the agent make money, and how sure are we (plan-v2.md section 9, screen 5).

   Two comparisons, each with its range: money made against the no-contact holdout, and lift against
   the simple baseline rule. Then the group sizes, the spend and the return on spend. A small inline
   SVG draws each estimate as a bar with its error bar, and "Copy summary" puts a plain paragraph on
   the clipboard to paste into an email.

   Every number here comes from the ProofSummary the API sends. Nothing is computed into a new
   figure, every estimate is shown with its low and high (never a bare point), and a missing
   estimate says "not enough data yet" instead of showing a zero. */

import {
  DASH,
  call,
  count,
  day,
  empty,
  ensureStyles,
  errText,
  failed,
  h,
  money,
  notify,
  num,
  pageHead,
  present,
  range,
  resolveGoal,
  s,
} from "./live.js";

const NOT_ENOUGH = "Not enough data yet";

/** Return on spend as a multiple: 2.3 reads "2.3×" (each unit spent brought 2.3 back in extra profit). */
export const times = (v) => (present(v) ? `${num(v, 1)}×` : DASH);

/** True when an estimate's range runs through zero, so we cannot yet say which way it goes. */
export const straddlesZero = (est) => Boolean(est) && est.low <= 0 && est.high >= 0;

/**
 * The shareable paragraph. Plain text, every estimate with its range, and plain words where the data
 * is not there yet. `goal` may be null (the whole programme), then money has no currency sign.
 */
export function proofSummaryText(proof, goal) {
  const currency = goal?.currency;
  const m = (v) => money(v, currency);
  const subject = goal?.text ? `"${goal.text}"` : "the whole programme";
  if (!proof) return `There is not enough data yet to show proof for ${subject}.`;

  const out = [`Proof for ${subject}, as of ${day(proof.as_of)}.`];
  const profit = proof.incremental_profit;
  const revenue = proof.incremental_revenue;
  if (profit || revenue) {
    const bits = [];
    if (profit) bits.push(`${range(profit, m)} in extra profit`);
    if (revenue) bits.push(`${range(revenue, m)} in extra revenue`);
    out.push(`Compared with leaving customers alone, the agent made ${bits.join(" and ")}.`);
    if (profit && straddlesZero(profit)) out.push("The profit range includes zero, so the gain is not yet certain.");
  } else {
    out.push("There is not enough data yet to compare with leaving customers alone.");
  }
  const lift = proof.lift_vs_baseline;
  if (lift) {
    out.push(`Against the simple rule, it made ${range(lift, m)} more profit per customer.`);
    if (straddlesZero(lift)) out.push("That range includes zero, so it has not yet clearly beaten the simple rule.");
  } else {
    out.push("There is not enough data yet to compare with the simple rule.");
  }
  out.push(
    `Groups: ${count(proof.agent_n)} customers decided by the agent, ${count(proof.baseline_n)} by the simple rule, and ${count(proof.holdout_n)} never contacted.`,
  );
  const spend = `Spend so far: ${m(proof.spend)}`;
  out.push(proof.roi ? `${spend}; return on spend ${range(proof.roi, times)}.` : `${spend}.`);
  for (const note of proof.notes || []) out.push(String(note));
  out.push("Ranges are 95% intervals.");
  return out.join(" ");
}

/**
 * Horizontal bars with error bars, one row per estimate, on one shared axis that always includes 0.
 * Rows without an estimate say "Not enough data yet". Returns null when no row has an estimate.
 */
export function estimateChart(rows, fmt, label) {
  const valid = rows.filter((r) => r.est);
  if (!valid.length) return null;
  const lo = Math.min(0, ...valid.map((r) => r.est.low));
  const hi = Math.max(0, ...valid.map((r) => r.est.high));
  const span = hi - lo || 1;
  const W = 480;
  const L = 128;
  const R = 16;
  const rowH = 60;
  const top = 8;
  const H = top + rows.length * rowH + 20;
  const x = (v) => L + ((v - lo) / span) * (W - L - R);
  const x0 = x(0);
  const described = rows.map((r) => `${r.label}: ${r.est ? range(r.est, fmt) : NOT_ENOUGH}`).join("; ");

  const parts = [s("title", null, `${label}. ${described}`)];
  rows.forEach((r, i) => {
    const cy = top + i * rowH + 22;
    parts.push(s("text", { x: 0, y: cy + 5, class: "ap-chart-label" }, r.label));
    if (!r.est) {
      parts.push(s("text", { x: L, y: cy + 5, class: "ap-chart-note" }, NOT_ENOUGH));
      return;
    }
    const xp = x(r.est.point);
    parts.push(
      s("rect", { x: Math.min(x0, xp), y: cy - 9, width: Math.max(1, Math.abs(xp - x0)), height: 18, rx: 3,
        class: r.est.point < 0 ? "ap-bar ap-bar-neg" : "ap-bar" }),
      s("line", { x1: x(r.est.low), x2: x(r.est.high), y1: cy, y2: cy, class: "ap-err" }),
      s("line", { x1: x(r.est.low), x2: x(r.est.low), y1: cy - 7, y2: cy + 7, class: "ap-err" }),
      s("line", { x1: x(r.est.high), x2: x(r.est.high), y1: cy - 7, y2: cy + 7, class: "ap-err" }),
      s("text", { x: L, y: cy + 27, class: "ap-chart-note" }, range(r.est, fmt)),
    );
  });
  parts.push(
    s("line", { x1: x0, x2: x0, y1: top, y2: H - 18, class: "ap-zero" }),
    s("text", { x: x0, y: H - 4, class: "ap-chart-note", "text-anchor": "middle" }, "0"),
  );
  return s("svg", { viewBox: `0 0 ${W} ${H}`, class: "ap-chart", role: "img", "aria-label": `${label}. ${described}` }, ...parts);
}

/** A stat tile whose value is an estimate with its range, or "not enough data yet". */
function estimateTile(title, est, fmt, hint) {
  return h(
    "div",
    { class: "ap-tile" },
    h("dt", null, title),
    h("dd", { class: "ap-tile-value" }, est ? fmt(est.point) : NOT_ENOUGH),
    est && h("dd", { class: "ap-small" }, `Range ${fmt(est.low)} to ${fmt(est.high)}`),
    est && straddlesZero(est) && h("dd", { class: "ap-small ap-warn-text" }, "Includes zero: not yet certain"),
    hint && h("dd", { class: "ap-muted ap-small" }, hint),
  );
}

function plainTile(title, value, hint) {
  return h("div", { class: "ap-tile" }, h("dt", null, title), h("dd", { class: "ap-tile-value" }, value),
    hint && h("dd", { class: "ap-muted ap-small" }, hint));
}

async function copyText(text, fallbackField) {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Clipboard refused (no permission, not a secure page): fall back to selecting the text.
  }
  try {
    fallbackField.focus();
    fallbackField.select();
    return document.execCommand("copy");
  } catch {
    return false;
  }
}

export async function render(root, ctx) {
  ensureStyles();
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-proof" });
  root.replaceChildren(page);

  let bundle = null;
  let goalError = null;
  try {
    bundle = await resolveGoal(ctx);
  } catch (e) {
    goalError = e;
  }
  const goal = bundle?.goal || null;
  const goalId = goal?.goal_id ?? (present(ctx.goalId) ? ctx.goalId : null);

  page.append(pageHead("Proof", goal ? goal.text : "The whole programme"));
  if (goalError) page.append(h("p", { class: "ap-muted ap-small" }, `Couldn't load the goal (${errText(goalError)}); amounts are shown without a currency.`));

  let proof;
  try {
    proof = await call(ctx.api, "getProof", goalId);
  } catch (e) {
    // Fall back to the proof inside the goal bundle only when it holds one: a null there must not turn a
    // failed request into "not enough data yet".
    if (bundle && bundle.proof) proof = bundle.proof;
    else {
      page.append(h("div", { class: "ap-card" }, failed("proof", e)), announcer);
      return;
    }
  }

  const currency = goal?.currency;
  const m = (v) => money(v, currency);
  const summary = proofSummaryText(proof, goal);

  if (!proof) {
    page.append(
      h("div", { class: "ap-card" },
        empty("Not enough data yet. Proof needs the agent, the simple rule and the no-contact holdout to run side by side for a while; it usually starts to show after a couple of weeks.")),
      announcer,
    );
    return;
  }

  const holdoutCard = h(
    "section",
    { class: "ap-card" },
    h("h2", { class: "ap-h2" }, "Compared with contacting nobody"),
    h("p", { class: "ap-muted" }, "The agent's customers against the holdout, a group we never message, so this is money that would not have come in otherwise."),
    h("dl", { class: "ap-tiles" },
      estimateTile("Extra profit", proof.incremental_profit, m),
      estimateTile("Extra revenue", proof.incremental_revenue, m)),
    estimateChart([{ label: "Extra profit", est: proof.incremental_profit }, { label: "Extra revenue", est: proof.incremental_revenue }], m,
      "Extra money compared with contacting nobody, with 95% ranges"),
  );

  const baselineCard = h(
    "section",
    { class: "ap-card" },
    h("h2", { class: "ap-h2" }, "Compared with the simple rule"),
    h("p", { class: "ap-muted" }, "The agent against a plain rule anyone could set up (contact everyone in the audience the same way)."),
    h("dl", { class: "ap-tiles" }, estimateTile("Extra profit per customer", proof.lift_vs_baseline, m)),
    estimateChart([{ label: "Per customer", est: proof.lift_vs_baseline }], m, "Extra profit per customer compared with the simple rule, with its 95% range"),
  );

  const groupsCard = h(
    "section",
    { class: "ap-card" },
    h("h2", { class: "ap-h2" }, "Groups and spend"),
    h("dl", { class: "ap-tiles" },
      plainTile("Agent decides", count(proof.agent_n), "customers"),
      plainTile("Simple rule", count(proof.baseline_n), "customers"),
      plainTile("Never contacted", count(proof.holdout_n), "customers (holdout)"),
      plainTile("Spend so far", m(proof.spend), "offers and messages"),
      estimateTile("Return on spend", proof.roi, times, "Extra profit for each unit spent")),
  );

  const notes = (proof.notes || []).length
    ? h("section", { class: "ap-card" }, h("h2", { class: "ap-h2" }, "Notes"), h("ul", { class: "ap-plain" }, ...proof.notes.map((n) => h("li", null, String(n)))))
    : null;

  const field = h("textarea", { class: "ap-summary", readonly: true, rows: 6, id: "ap-proof-summary" });
  field.value = summary;
  const copyBtn = h("button", { type: "button", class: "ap-btn ap-btn-primary" }, "Copy summary");
  copyBtn.addEventListener("click", async () => {
    const ok = await copyText(summary, field);
    notify(ctx, ok ? "Summary copied." : "Couldn't copy; the text is selected, so press Ctrl+C (or Cmd+C).", ok ? "good" : "critical", announcer);
  });
  const shareCard = h(
    "section",
    { class: "ap-card" },
    h("h2", { class: "ap-h2" }, h("label", { for: "ap-proof-summary" }, "Ready to share")),
    field,
    h("div", { class: "ap-actions" }, copyBtn),
  );

  // append() turns null into the text "null", so leave out the notes card when there are no notes.
  page.append(
    ...[
      h("p", { class: "ap-muted ap-small" }, `As of ${day(proof.as_of)} · ranges are 95% intervals`),
      holdoutCard,
      baselineCard,
      groupsCard,
      notes,
      shareCard,
      announcer,
    ].filter(Boolean),
  );
}
