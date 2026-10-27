/* Proof: did it make you money, and how sure are we (PITCH.md screen 5, "Proof").

   Numbers first, explanations on hover (DESIGN.md section 6, "Words"). The page answers a business owner's
   questions in their order, each card being its title, its numbers and at most one short sentence:
     1. Did it make me extra money? A verdict in plain words comes before any number: "Real extra profit",
        "Not sure yet", "Losing money so far" or "Too early to tell". Then ONE sentence with the money and its
        range ("$4,300 extra profit (likely $900 to $7,700), measured against people we never contact"), the
        other figures (extra revenue, return on spend, spend) and a chart of the ranges. How this is measured
        (real extra profit, not "attributed revenue") is the description of "people we never contact".
     2. What should I do now? "Ready for Autopilot" when even the low end of the profit range is above zero
        and the goal is not on Autopilot yet; otherwise "stay in Shadow or Approve until the range is above
        zero", with a link to the Plan dial. One sentence; the rest in the title's description.
     3. Is the AI earning its keep? A verdict against the simple rule anyone could set up.
     4. What does it expect to earn? Its own forecast (GET /v2/expected), always called a forecast and never
        proof. In Shadow mode, where nothing is sent and so nothing can be proven, it comes straight after the
        verdict.
     5. What to expect, as general guidance about the product (3-10% typical), labelled as not being a result
        from this business, with its source in the label's description; a paragraph ready to share.
     6. One folded block at the bottom, "How this is measured": the three groups, the method and the API's own
        notes.

   Honesty rules this file keeps:
     - every number comes from the ProofSummary or the Expected forecast the API sends (or the mock files
       that mirror them); nothing is added up into a new figure here, and a verdict only compares a range's
       low and high with zero;
     - an estimate is always shown with its low and high, never a bare point, and "not sure yet" when its
       range includes zero; a missing estimate says "not enough data yet" instead of showing a zero;
     - general facts about the product (3-10%) are labelled as such and never shown as this shop's numbers;
     - a description holds words, never a number the page does not also have from the API.
   There are no pricing terms in the API, so nothing about "paid in results" is shown yet. */

import { info, pageHead, withInfo } from "../components.js";
import {
  DASH,
  call,
  count,
  day,
  ensureStyles,
  errText,
  h,
  money,
  notFound,
  notify,
  num,
  pct,
  present,
  range,
  resolveGoal,
  s,
} from "./live.js";

const NOT_ENOUGH = "Not enough data yet";

/** Where the "3-10% typical" guidance comes from. The docs are not served with the app, so it is named in words. */
export const EXPECT_DOC = "docs/research/v2/03_DECISION_SCIENCE.md";

/** Return on spend as a multiple, always one decimal: 90.1 reads "90.1×" (each unit spent brought 90.1 back). */
export const times = (v) => (present(v) ? `${num(v, 1)}×` : DASH);

/**
 * One precision for the whole estimate: when any of its numbers is 1,000 or more, all three are shown whole, so a
 * range never reads "$1,418 (range $766.92 to $2,068)". Rounding is for display only; the API's values are kept.
 */
export function estimateFormat(est, fmt) {
  const big = Boolean(est) && [est.point, est.low, est.high].some((v) => present(v) && Math.abs(Number(v)) >= 1000);
  return big ? (v) => fmt(present(v) ? Math.round(Number(v)) : v) : fmt;
}

/** True when an estimate's range runs through zero, so we cannot yet say which way it goes. */
export const straddlesZero = (est) => Boolean(est) && est.low <= 0 && est.high >= 0;

/** An estimate the page can show: point, low and high all there. */
const complete = (est) => Boolean(est) && present(est.point) && present(est.low) && present(est.high);

/**
 * "$4,300 (range $900 to $7,700)" in exactly the precision the figures on the page use (estimateFormat), so the
 * share text and the KPI never round the same number differently (90.1× in both).
 */
export function rangeText(est, fmt) {
  if (!complete(est)) return DASH;
  const f = estimateFormat(est, fmt);
  return `${f(est.point)} (range ${f(est.low)} to ${f(est.high)})`;
}

/** The short range line under a figure: "$900 to $7,700". */
const lowHigh = (est, fmt) => {
  const f = estimateFormat(est, fmt);
  return `${f(est.low)} to ${f(est.high)}`;
};

/**
 * Which way a measured range points, by comparing its low and high with zero (no new number is made):
 * "up" (the whole range is above zero), "unsure" (it includes zero), "down" (all below zero) or "early" (no range).
 */
export function direction(est) {
  if (!complete(est)) return "early";
  if (Number(est.low) > 0) return "up";
  if (Number(est.high) < 0) return "down";
  return "unsure";
}

/** The verdict on extra profit against the people we never contact, shown before any number. */
export function profitVerdict(est) {
  const way = direction(est);
  if (way === "up") return { key: "real", tone: "good", icon: "✓", title: "Real extra profit" };
  if (way === "down") return { key: "losing", tone: "bad", icon: "!", title: "Losing money so far" };
  if (way === "unsure") return { key: "unsure", tone: "warn", icon: "?", title: "Not sure yet" };
  return { key: "early", tone: "neutral", icon: "…", title: "Too early to tell" };
}

/** The verdict against the simple rule anyone could set up. */
export function ruleVerdict(est) {
  const way = direction(est);
  if (way === "up") return { key: "beats", tone: "good", icon: "✓", title: "Beats the simple rule" };
  if (way === "down") return { key: "behind", tone: "bad", icon: "!", title: "Behind the simple rule so far" };
  if (way === "unsure") return { key: "unsure", tone: "warn", icon: "?", title: "Not sure yet" };
  return { key: "early", tone: "neutral", icon: "…", title: "Too early to tell" };
}

const AUTONOMY_WORDS = { shadow: "Shadow", approve: "Approve each batch", autopilot: "Autopilot" };
export const autonomyName = (a) => AUTONOMY_WORDS[a] || (present(a) ? String(a) : DASH);

/**
 * What to do next with the autonomy dial, from the profit verdict and the goal's level (PITCH.md: "When the proof
 * page shows real extra profit: switch to Autopilot"). Returns null when there is no goal to advise on. `text` is
 * the one sentence on the card; `more` is the rest, in the description of the card's title.
 */
export function autopilotAdvice(goal, proof) {
  if (!goal) return null;
  const real = direction(proof?.incremental_profit) === "up";
  const level = goal.autonomy;
  if (goal.status === "done") return null;
  if (goal.status === "paused") {
    return {
      key: "paused",
      title: "Paused right now",
      text: "Nothing is being sent; if it paused itself, the Live screen and the inbox say why.",
      more: real
        ? "The proof shows real extra profit, but check why it paused before you resume it, or before you move the dial to Autopilot."
        : "Check why it paused before you resume it. Autopilot only makes sense once this page shows real extra profit, with the whole range above zero.",
      link: "Go to the Live screen to see why and resume",
      screen: "live",
    };
  }
  if (goal.status === "draft") {
    return {
      key: "draft",
      title: "The plan is not approved yet",
      text: "Nothing runs until you approve the plan.",
      more: "Start in Shadow: it decides and shows you what it would do, but sends nothing.",
      link: "Check and approve the plan",
    };
  }
  if (level === "autopilot" && direction(proof?.incremental_profit) === "down") {
    return {
      key: "autopilot-losing",
      title: "On Autopilot, and it is losing money so far",
      text: "The whole profit range is below zero.",
      more: "Move the dial back to Approve each batch on the Plan screen, or pause it with the button at the top, and look at who it contacts and which offers it uses.",
      link: "Change the autonomy level on the Plan screen",
    };
  }
  if (level === "autopilot") {
    return real
      ? {
          key: "autopilot-backed",
          title: "On Autopilot, and the proof backs it",
          text: "Even the low end of the profit range is above zero.",
          more: "It acts on its own inside the limits you set, and the Pause button at the top stops everything at once.",
          link: null,
        }
      : {
          key: "autopilot-unproven",
          title: "On Autopilot, but the proof is not there yet",
          text: "The profit range is not above zero yet.",
          more: "If you would rather check each batch until it is, move the dial back to Approve each batch on the Plan screen.",
          link: "Change the autonomy level on the Plan screen",
        };
  }
  if (real) {
    return {
      key: "ready",
      title: "Ready for Autopilot",
      text: `Even the low end of the profit range is above zero, and it is on ${autonomyName(level)} now.`,
      more: "On Autopilot it acts on its own inside the limits you set (budget, quiet hours, weekly limits). You can pause it at any time.",
      link: "Switch to Autopilot on the Plan screen",
      primary: true,
    };
  }
  if (direction(proof?.incremental_profit) === "down") {
    return {
      key: "losing",
      title: "Not ready for Autopilot: it is losing money so far",
      text: `Stay on ${autonomyName(level)}, or pause it with the button at the top, and look at the plan.`,
      more: "Look at who it contacts, which offers it may use and how often.",
      link: "Look at the plan",
    };
  }
  if (level === "shadow") {
    return {
      key: "shadow-next",
      title: "Not ready for Autopilot yet: stay in Shadow, then Approve",
      text: "When you like what it would do, switch to Approve each batch with a small budget.",
      more: "In Shadow mode it sends nothing, so there is nothing to prove yet. Switch to Autopilot only when this page shows real extra profit, with the whole range above zero.",
      link: "Change the autonomy level on the Plan screen",
    };
  }
  return {
    key: "stay",
    title: "Not ready for Autopilot yet",
    text: `Stay on ${autonomyName(level)} until the profit range is above zero.`,
    more: 'This page will say "Real extra profit" when it is.',
    link: null,
  };
}

/** The API's notes keep its own words; when they use "holdout" or "baseline", say first what those mean. */
export function notesGloss(notes) {
  const text = (notes || []).join(" ");
  const words = [];
  if (/\bholdout\b/i.test(text)) words.push('"holdout" means the people we never contact');
  if (/\bbaseline\b/i.test(text)) words.push('"baseline" means the simple rule');
  return words.length ? `In these notes, ${words.join(" and ")}.` : null;
}

/**
 * Two cautions that sit under the headline number, both worded by the API: the smallest gain this size of
 * comparison could see at all (so a flat result is not read as "no effect"), and a warning when the shop's
 * margin is not set (so the profit is overstated). Empty when the API sends neither.
 */
export function proofCaveats(proof) {
  const out = [];
  if (proof?.detectable?.sentence) out.push(h("p", { class: "pf-caveat pf-mde" }, String(proof.detectable.sentence)));
  if (proof?.margin_note) out.push(h("p", { class: "notice pf-caveat pf-margin" }, String(proof.margin_note)));
  return out;
}

/**
 * The smallest gain this comparison could see for one figure (`detectable.revenue` or
 * `detectable.lift_vs_baseline`), as a sentence for that figure's description; "" when the API sends none.
 */
export function detectableAbout(seen, m, what) {
  if (seen == null || !Number.isFinite(Number(seen))) return "";
  return ` With this many customers, ${what} below about ${m(Number(seen))} would not show up yet.`;
}

/**
 * The shareable paragraph. Plain text, every estimate with its range (in the same precision as the page), and
 * plain words where the data is not there yet. `goal` may be null (the whole programme), then money has no
 * currency sign.
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
    if (profit) bits.push(`${rangeText(profit, m)} in extra profit`);
    if (revenue) bits.push(`${rangeText(revenue, m)} in extra revenue`);
    out.push(`Compared with the people we never contact (a small random group left alone), the agent made ${bits.join(" and ")}.`);
    const way = direction(profit);
    if (way === "up") out.push("The whole profit range is above zero: real extra profit.");
    if (way === "unsure") out.push("The profit range includes zero, so the gain is not yet certain: not sure yet.");
    if (way === "down") out.push("The whole profit range is below zero: it is losing money so far.");
  } else {
    out.push("There is not enough data yet to compare with leaving customers alone (the people we never contact).");
  }
  const lift = proof.lift_vs_baseline;
  if (lift) {
    out.push(`Against the simple rule anyone could set up, it made ${rangeText(lift, m)} more profit per customer.`);
    const way = direction(lift);
    if (way === "up") out.push("It beats the simple rule.");
    if (way === "unsure") out.push("That range includes zero, so it has not yet clearly beaten the simple rule.");
    if (way === "down") out.push("The simple rule is doing better so far.");
  } else {
    out.push("There is not enough data yet to compare with the simple rule.");
  }
  out.push(
    `Groups: ${count(proof.agent_n)} customers decided by the agent, ${count(proof.baseline_n)} by the simple rule, and ${count(proof.holdout_n)} people we never contact.`,
  );
  const spend = `Spend so far: ${m(proof.spend)}`;
  out.push(complete(proof.roi) ? `${spend}; return on spend ${rangeText(proof.roi, times)}.` : `${spend}.`);
  if (proof.detectable?.sentence) out.push(String(proof.detectable.sentence));
  if (proof.margin_note) out.push(String(proof.margin_note));
  const gloss = notesGloss(proof.notes);
  if (gloss) out.push(gloss);
  for (const note of proof.notes || []) out.push(String(note));
  out.push("Ranges are 95% ranges: the real number is very likely inside them.");
  return out.join(" ");
}

/**
 * Horizontal bars with error bars, one row per estimate, on one shared axis that always includes 0. The bars
 * carry no numbers of their own (the figures beside the chart have them); the ranges are in the chart's label,
 * which is also its description on hover, focus or tap. Rows without an estimate say "Not enough data yet".
 * Returns null when no row has an estimate.
 */
export function estimateChart(rows, fmt, label) {
  const valid = rows.filter((r) => complete(r.est));
  if (!valid.length) return null;
  const lo = Math.min(0, ...valid.map((r) => Number(r.est.low)));
  const hi = Math.max(0, ...valid.map((r) => Number(r.est.high)));
  const span = hi - lo || 1;
  const W = 480;
  const L = 128;
  const R = 16;
  const rowH = 40;
  const top = 4;
  const H = top + rows.length * rowH + 20;
  const x = (v) => L + ((Number(v) - lo) / span) * (W - L - R);
  const x0 = x(0);
  const described = rows.map((r) => `${r.label}: ${complete(r.est) ? rangeText(r.est, fmt) : NOT_ENOUGH}`).join("; ");

  const parts = [s("title", null, `${label}. ${described}`)];
  rows.forEach((r, i) => {
    const cy = top + i * rowH + 18;
    parts.push(s("text", { x: 0, y: cy + 5, class: "ap-chart-label" }, r.label));
    if (!complete(r.est)) {
      parts.push(s("text", { x: L, y: cy + 5, class: "ap-chart-note pf-chart-note" }, NOT_ENOUGH));
      return;
    }
    const xp = x(r.est.point);
    parts.push(
      s("rect", { x: Math.min(x0, xp), y: cy - 9, width: Math.max(1, Math.abs(xp - x0)), height: 18, rx: 3,
        class: Number(r.est.point) < 0 ? "ap-bar ap-bar-neg" : "ap-bar" }),
      s("line", { x1: x0, x2: x0, y1: cy - 14, y2: cy + 14, class: "ap-zero" }),
      s("line", { x1: x(r.est.low), x2: x(r.est.high), y1: cy, y2: cy, class: "ap-err" }),
      s("line", { x1: x(r.est.low), x2: x(r.est.low), y1: cy - 7, y2: cy + 7, class: "ap-err" }),
      s("line", { x1: x(r.est.high), x2: x(r.est.high), y1: cy - 7, y2: cy + 7, class: "ap-err" }),
    );
  });
  parts.push(
    s("line", { x1: x0, x2: x0, y1: H - 20, y2: H - 15, class: "ap-zero" }),
    s("text", { x: x0, y: H - 2, class: "ap-chart-note", "text-anchor": "middle" }, "0"),
  );
  return s("svg", { viewBox: `0 0 ${W} ${H}`, class: "ap-chart", role: "img", "aria-label": `${label}. ${described}` }, ...parts);
}

// ---- the markup (DESIGN.md: hairline cards, one divided KPI strip per block, tabular figures, pale pills) -----

/** Inline size for a skeleton block or a KPI strip's column count, as a style attribute (h() sets attributes). */
const css = (rules) => Object.entries(rules).map(([k, v]) => `${k}: ${v}`).join("; ");

/** A label with its description (dotted underline; hover, focus or tap opens it). */
const described = (text, about) => withInfo(h("span", null, text), about);

/**
 * A KPI cell whose value is an estimate with its short range line ("$900 to $7,700"), or "not enough data yet"
 * in grey (never a zero). `label` may carry its own description.
 */
function estimateCell(label, est, baseFmt) {
  const fmt = estimateFormat(est, baseFmt);
  const ok = complete(est);
  return h(
    "div",
    { class: "kpi" },
    h("dt", { class: "kpi-label" }, label),
    h("dd", { class: ok ? "kpi-value" : "kpi-value is-missing" }, ok ? fmt(est.point) : NOT_ENOUGH),
    ok && h("dd", { class: "kpi-range tabular" }, lowHigh(est, baseFmt)),
    ok && straddlesZero(est) && h("dd", { class: "kpi-note pf-unsure" }, "Not sure yet: the range includes zero"),
  );
}

function plainCell(label, value, note) {
  const shown = present(value) && value !== DASH;
  return h(
    "div",
    { class: "kpi" },
    h("dt", { class: "kpi-label" }, label),
    h("dd", { class: shown ? "kpi-value" : "kpi-value is-missing" }, shown ? value : DASH),
    note && h("dd", { class: "kpi-note" }, note),
  );
}

/** One container split by hairlines (styles.css .kpis), as a description list. */
function kpis(cells, extra = "") {
  const list = cells.filter(Boolean);
  return h("dl", { class: `kpis ${extra}`.trim(), style: css({ "--kpi-count": String(Math.max(1, list.length)) }) }, ...list);
}

/** The verdict line: a small mark (hidden from screen readers) and the words, so it never relies on colour alone. */
function verdictLine(v, id, size = "lg") {
  return h(
    "p",
    { class: `pf-verdict pf-verdict-${size} pf-tone-${v.tone}`, id },
    h("span", { class: "pf-verdict-mark", "aria-hidden": "true" }),
    h("span", { class: "pf-verdict-title" }, v.title),
  );
}

/** How the result is measured: the description of "people we never contact" in the lead sentence. */
function methodWords(proof) {
  const holdout = proof && present(proof.holdout_n) ? ` (${count(proof.holdout_n)} so far)` : "";
  return `A small random group of your customers${holdout} is never contacted. Everyone else is compared with them, so this is real extra profit, after the cost of discounts and messages. It is not "attributed revenue", where a tool takes credit for every sale that follows a message.`;
}

const WHY_EARLY =
  "Proof needs the agent, the simple rule and the people we never contact to run side by side for a while, and each decision only counts once the days it is given to work have passed.";

/**
 * The one sentence under the profit verdict: the money with its range, measured against the people we never
 * contact (whose description says how). In Shadow, or without a range, it says why there is no number.
 */
function profitLede(v, est, fmt, goal, proof) {
  const never = described("people we never contact", methodWords(proof));
  if (v.key === "early") {
    if (goal?.autonomy === "shadow") {
      return h("p", { class: "pf-lede pf-lede-early" },
        "In Shadow mode it sends nothing, so there is nothing yet to measure against ", never, ".");
    }
    return h("p", { class: "pf-lede pf-lede-early" }, described(NOT_ENOUGH, WHY_EARLY), " to compare with ", never, ".");
  }
  const f = estimateFormat(est, fmt);
  const tail = {
    real: ".",
    unsure: "; the range includes zero, so it may not have made any yet.",
    losing: ": less than leaving these customers alone.",
  }[v.key];
  return h(
    "p",
    { class: "pf-lede" },
    h("span", { class: "pf-lede-num tabular" }, f(est.point)),
    " extra profit ",
    h("span", { class: "pf-lede-range tabular" }, `(likely ${lowHigh(est, fmt)})`),
    ", measured against ",
    never,
    tail,
  );
}

/**
 * The day the first contacts can count: `since` plus the days each contact is given to work, both as the API sent
 * them (GET /v2/expected's `since` and `horizon_days`). Null when either is missing or that day has already come,
 * so the page never shows a date in the past as something to wait for.
 */
export function firstCountsOn(since, horizonDays, asOf) {
  if (!present(since) || !present(horizonDays)) return null;
  const start = Date.parse(since);
  if (Number.isNaN(start)) return null;
  const days = Number(horizonDays);
  if (!Number.isFinite(days)) return null;
  const at = start + days * 86400000;
  const now = present(asOf) ? Date.parse(asOf) : Number.NaN;
  if (!Number.isNaN(now) && at <= now) return null;
  return new Date(at).toISOString();
}

/**
 * While it is too early to tell, the result card shows what the answer is waiting for, from the API's own numbers:
 * the three groups so far (the proof's counts), how many days each contact is given before it counts (the
 * forecast's horizon) and, when that is still ahead, the day the first ones count; then the spend. In Shadow the
 * days are left out: nothing is sent, so nothing will count.
 */
function earlyStrip(proof, forecast, shadow, m) {
  const expected = forecast && forecast.state === "ok" ? forecast.expected : null;
  const horizon = expected && present(expected.horizon_days) ? Number(expected.horizon_days) : null;
  const first = horizon !== null ? firstCountsOn(expected.since, horizon, proof.as_of) : null;
  const waits = horizon !== null && !shadow
    ? [
        `A contact counts once the ${count(horizon)} days it is given to work have passed, so the newest are not in yet.`,
        first ? `It has been deciding since ${day(expected.since)}, so the first contacts can count from ${day(first)}.` : "",
      ].join(" ").trim()
    : null;
  return kpis([
    plainCell(described("The agent decides", "Customers the agent decides for so far, picked at random."), count(proof.agent_n)),
    plainCell(described("The simple rule decides", "Customers a simple fixed rule decides for, picked at random, for comparison."), count(proof.baseline_n)),
    plainCell(described("People we never contact", methodWords(proof)), count(proof.holdout_n)),
    waits && plainCell(described("Each contact counts after", waits), `${count(horizon)} days`, first ? `First from ${day(first)}` : null),
    plainCell(described("Spend so far", "On discounts and messages, for this goal."), m(proof.spend)),
  ], "pf-hero-strip pf-early-strip");
}

/** The chart's rows in words ("Extra profit: $4,300 (range $900 to $7,700); …"), from its label after the title. */
const chartRows = (chart) => {
  const label = String(chart.getAttribute("aria-label") || "");
  const cut = label.indexOf(". ");
  return cut >= 0 ? `${label.slice(cut + 2)}.` : label;
};

/** The figures of a block next to their chart (stacked on a narrow screen). The chart is null without a range. */
function figure(strip, chart) {
  const about = "Each bar is the best estimate and the line across it its 95% range; the dashed line is zero.";
  return h(
    "div",
    { class: "pf-figure pf-figure-pair" },
    strip,
    chart && h("figure", { class: "pf-chart" }, withInfo(chart, `${about} ${chartRows(chart)}`.trim())),
  );
}

function planHref(goal, screen = "plan") {
  return goal?.goal_id ? `#/${screen}/${encodeURIComponent(goal.goal_id)}` : `#/${screen}`;
}

/** The block that failed: one sentence with the server's words, and a Retry that draws the page again. */
function failedBox(what, error, retry) {
  return h(
    "div",
    { class: "error-box", role: "alert" },
    h("p", { class: "empty-title" }, `Couldn't load ${what}: ${errText(error)}`),
    typeof retry === "function" && h("div", { class: "error-actions" }, h("button", { type: "button", class: "link-btn", onclick: retry }, "Retry")),
  );
}

function emptyBox(title, text) {
  return h("div", { class: "empty", role: "status" }, h("p", { class: "empty-title" }, title), text && h("p", null, text));
}

/** A card title with its (i): the card's explanation, out of the way until asked for. */
function cardTitle(id, text, about, label) {
  return h("h2", { class: "card-title pf-title", id }, text, about ? info(about, { label: label || `About ${text}` }) : null);
}

function adviceCard(advice, goal) {
  const tone = advice.key === "ready" || advice.key === "autopilot-backed" ? "good" : "neutral";
  return h(
    "section",
    { class: `card pf-advice pf-advice-${tone} span-12`, "aria-labelledby": "pf-advice-title" },
    h(
      "div",
      { class: "pf-advice-body" },
      h("p", { class: "pf-eyebrow" }, "Next step"),
      cardTitle("pf-advice-title", advice.title, advice.more, "Why"),
      h("p", { class: "pf-advice-text" }, advice.text),
    ),
    advice.link &&
      h("div", { class: "pf-advice-action" },
        h("a", { class: advice.primary ? "btn btn-primary" : "btn btn-secondary", href: planHref(goal, advice.screen || "plan") }, advice.link)),
  );
}

/** The forecast from GET /v2/expected, fetched alongside the proof: {state: "ok"|"none"|"error"|"skip", expected, error}. */
async function fetchForecast(ctx, goal) {
  if (!goal?.goal_id || typeof ctx.api?.getExpected !== "function") return { state: "skip" };
  try {
    const expected = await call(ctx.api, "getExpected", goal.goal_id);
    return expected ? { state: "ok", expected } : { state: "none" };
  } catch (error) {
    return { state: "error", error };
  }
}

const FORECAST_NOT_PROOF =
  "A forecast is what it thinks will happen, not proof. Only the comparison with the people we never contact shows what really happened.";

/** "What it expects to earn": the agent's own forecast, never proof. One sentence, then its figures. */
function forecastBody(expected, goal, currency) {
  const m = (v) => money(v, currency);
  const shadow = goal?.autonomy === "shadow";
  const est = expected.expected_net;
  const since = present(expected.since) ? ` since ${day(expected.since)}` : "";
  if (!complete(est)) return [emptyBox("No forecast yet", String(expected.words || "There is no forecast yet."))];
  const partial = present(expected.contacts) && present(expected.with_forecast) && Number(expected.contacts) !== Number(expected.with_forecast);
  const contactsAbout = [
    `${shadow ? "Contacts it would have made" : "Contacts it decided on"}${since}, to ${count(expected.customers)} customers.`,
    partial ? `Of ${count(expected.contacts)} contacts, ${count(expected.with_forecast)} carry a forecast of their own; only those are added up.` : "",
  ].join(" ").trim();
  return [
    h(
      "p",
      { class: "pf-lede-small" },
      shadow
        ? `Its forecast covers the ${count(expected.horizon_days)} days after each contact it would have made${since}; nothing was sent, because this goal is in Shadow mode.`
        : `Its forecast covers the ${count(expected.horizon_days)} days after each contact it decided on${since}.`,
    ),
    kpis([
      estimateCell(described("Expected extra profit", FORECAST_NOT_PROOF), est, m),
      plainCell(described(shadow ? "Contacts it would make" : "Contacts", contactsAbout), count(expected.with_forecast)),
      plainCell(described("Expected spend", "On discounts and messages, by its own forecast."), m(expected.expected_spend)),
    ]),
  ];
}

function forecastCard(result, goal, currency, { span, retry }) {
  let body;
  if (result.state === "error") body = [failedBox("the forecast", result.error, retry)];
  else if (result.state === "none") body = [emptyBox("No forecast yet", "No forecast to show yet: it has not decided on any contacts for this goal.")];
  else body = forecastBody(result.expected, goal, result.expected.currency || currency);
  return h(
    "section",
    { class: `card pf-forecast ${span}`, "aria-labelledby": "pf-forecast-title" },
    h(
      "div",
      { class: "card-head" },
      h("h2", { class: "card-title", id: "pf-forecast-title" }, "What it expects to earn "),
      h("span", { class: "pill pill-neutral pf-chip" }, "Its own forecast, not proof"),
    ),
    ...body,
  );
}

/** The 3-10% guidance, labelled as a fact about AI marketing in general, never as this shop's number. */
function expectFact(proof, measured, span) {
  const share = proof && complete(proof.incremental_profit_share) ? proof.incremental_profit_share : null;
  const yours = measured
    ? "Your own result is the one at the top of this page, in money, with its range. It is measured, not taken from this guidance."
    : "Your own result will show at the top of this page, in money, with its range, once there is enough data.";
  const label = withInfo(
    h("span", { class: "pill pill-neutral" }, "General guidance"),
    `General guidance about AI marketing, not a result from your business. Claims of much more are rarely measured against people who are never contacted. ${yours} Source: published results measured against an untouched group, summarised in ${EXPECT_DOC}.`,
  );
  return h(
    "aside",
    { class: `card pf-fact ${span}`, "aria-labelledby": "pf-fact-title" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title", id: "pf-fact-title" }, "What to expect"), label),
    h("p", null, "Well-run AI marketing, measured honestly, typically adds 3-10% in real extra profit."),
    share &&
      h("p", null, h("strong", null, "This goal so far: "), `${range(share, pct)} extra profit${straddlesZero(share) ? " (not sure yet: the range includes zero)" : ""}.`),
  );
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
  if (!fallbackField) return false;
  try {
    fallbackField.focus();
    fallbackField.select();
    return document.execCommand("copy");
  } catch {
    return false;
  }
}

function ruleCardFor(proof, m, span) {
  const rv = ruleVerdict(proof.lift_vs_baseline);
  const lift = proof.lift_vs_baseline;
  const sentence = {
    beats: "So the AI is earning its keep.",
    unsure: "The range includes zero, so it has not yet clearly beaten it.",
    behind: "The simple rule is doing better so far: worth a look at the plan.",
    early: `${NOT_ENOUGH} to compare with the simple rule.`,
  }[rv.key];
  const about = `A simple rule anyone could set up treats everyone in the audience the same way, like the usual "10% off to everyone who hasn't bought in 60 days" email. ${count(proof.baseline_n)} customers, picked at random, get the simple rule instead of the agent.`;
  return h(
    "section",
    { class: `card pf-rule ${span}`, "aria-labelledby": "pf-rule-title" },
    h("div", { class: "card-head" }, cardTitle("pf-rule-title", "Compared with a simple rule", about, "About the simple rule")),
    verdictLine(rv, "pf-rule-verdict", "sm"),
    h("p", { class: "pf-verdict-text" }, sentence),
    complete(lift) &&
      kpis([estimateCell(described("Extra profit per customer", `How much more profit each customer brought in with the agent than with the simple rule, with its 95% range.${detectableAbout(proof.detectable?.lift_vs_baseline, m, "a difference per customer")}`), lift, m)], "pf-single"),
  );
}

/**
 * "How this is measured": one folded block at the bottom, with the three groups, the method and the API's notes.
 * `groupsShown` is true when the result card already shows the three groups (too early to tell), so they are said once.
 */
function methodCard(proof, groupsShown = false) {
  const notes = (proof?.notes || []).map(String);
  const gloss = notesGloss(notes);
  return h(
    "section",
    { class: "card pf-method span-12", "aria-label": "How this is measured" },
    h(
      "details",
      { class: "pf-details" },
      h("summary", null, "How this is measured"),
      h(
        "div",
        { class: "pf-method-body" },
        proof &&
          !groupsShown &&
          kpis([
            plainCell("People we never contact", count(proof.holdout_n)),
            plainCell("The agent decides", count(proof.agent_n)),
            plainCell("The simple rule decides", count(proof.baseline_n)),
          ], "pf-groups"),
        h(
          "ul",
          { class: "pf-bullets" },
          h("li", null, "Each customer is put in one of three groups at random: the agent decides for most of them, a simple rule decides for some, and the people we never contact get nothing at all."),
          h("li", null, "Because the groups are picked at random, they would buy about the same if left alone. The difference in profit between them is what the messages and offers really added, after the cost of discounts and messages."),
          h("li", null, "A decision counts once the days it is given to work have passed, so the newest decisions are not in yet."),
          h("li", null, "Each range is a 95% range: the real number is very likely inside it. When the range includes zero, it says \"not sure yet\" rather than guess."),
        ),
        notes.length > 0 &&
          h(
            "div",
            { class: "pf-notes" },
            h("h3", { class: "pf-notes-title" }, "Notes from the measurement"),
            gloss && h("p", { class: "meta" }, gloss),
            h("ul", { class: "pf-bullets pf-note-list" }, ...notes.map((n) => h("li", null, n))),
          ),
      ),
    ),
  );
}

function shareCardFor(proof, goal, ctx, announcer, span) {
  const summary = proofSummaryText(proof, goal);
  const field = h("textarea", { class: "pf-summary", readonly: true, rows: 4, id: "ap-proof-summary" });
  field.value = summary;
  // The paragraph itself stays folded: the card is its title and its button, and the text is one tap away.
  const fold = h("details", { class: "pf-details pf-share-text" }, h("summary", null, "Show the text"), field);
  const copyBtn = h("button", { type: "button", class: "btn btn-secondary btn-sm" }, "Copy summary");
  copyBtn.addEventListener("click", async () => {
    let ok = await copyText(summary, null);
    if (!ok) {
      // No clipboard: open the fold so the text can be selected and copied by hand.
      fold.open = true;
      ok = await copyText(summary, field);
    }
    notify(ctx, ok ? "Summary copied." : "Couldn't copy; the text is selected, so press Ctrl+C (or Cmd+C).", ok ? "good" : "critical", announcer);
  });
  return h(
    "section",
    { class: `card pf-share ${span}` },
    h(
      "div",
      { class: "card-head" },
      h("h2", { class: "card-title pf-title" }, h("label", { for: "ap-proof-summary" }, "Ready to share"),
        info("Plain text with every figure and its range, for an email or a report.", { label: "About the summary" })),
      copyBtn,
    ),
    fold,
  );
}

const RANGES = "Every range on this page is a 95% range: the real number is very likely inside it. When a range includes zero, it says \"not sure yet\" rather than guess.";

/** The one page head: "Proof", the goal, and the date the proof is as of (a stamp, only when the API sent one). */
function head(sub, { asOf } = {}) {
  return pageHead({
    title: "Proof",
    sub,
    info: RANGES,
    stamp: present(asOf) ? h("span", { class: "stamp" }, `As of ${day(asOf)}`) : null,
  });
}

const block = (w, hgt, extra = "") => h("span", { class: `skeleton ${extra}`.trim(), "aria-hidden": "true", style: css({ width: w, height: hgt }) });

/** While the proof loads: the title, and the result block at its real size in grey (nothing shifts after). */
function loadingView() {
  return [
    pageHead({ title: "Proof", sub: block("320px", "20px", "skeleton-inline") }),
    h(
      "div",
      { class: "grid" },
      h(
        "section",
        { class: "card pf-hero span-12", role: "status", "aria-busy": "true" },
        h("span", { class: "visually-hidden" }, "Loading the proof…"),
        h("p", { class: "pf-verdict pf-verdict-lg" }, block("220px", "28px", "skeleton-inline")),
        h("p", { class: "pf-lede" }, block("min(520px, 100%)", "32px", "skeleton-inline")),
        h("div", { class: "pf-figure pf-figure-pair" },
          h("div", { class: "kpis", style: css({ "--kpi-count": "3" }) },
            ...["Extra revenue", "Return on spend", "Spend so far"].map((l) => h("div", { class: "kpi" }, h("p", { class: "kpi-label" }, l),
              h("p", { class: "kpi-value" }, block("110px", "28px", "skeleton-inline")), h("p", { class: "kpi-range" }, block("120px", "12px", "skeleton-inline"))))),
          block("100%", "100px")),
      ),
    ),
  ];
}

export async function render(root, ctx) {
  ensureStyles();
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-proof" });
  root.replaceChildren(page);
  page.append(...loadingView());
  const retry = () => render(root, ctx);

  let bundle = null;
  let goalError = null;
  try {
    bundle = await resolveGoal(ctx);
  } catch (e) {
    goalError = e;
  }
  const goal = bundle?.goal || null;
  const goalId = goal?.goal_id ?? (present(ctx.goalId) ? ctx.goalId : null);

  // No such goal: say so, rather than "not enough data yet" (the API answers null proof for an unknown goal).
  if (goalError && notFound(goalError)) {
    page.replaceChildren(head(`There is no goal ${ctx.goalId}.`),
      h("section", { class: "card" }, emptyBox("No such goal", "It may have been removed, or the link is mistyped. Pick a goal from the switcher at the top.")), announcer);
    return;
  }

  // The forecast loads alongside the proof; either failing leaves the other standing.
  const forecastP = fetchForecast(ctx, goal);
  let proof = null;
  let proofError = null;
  try {
    proof = await call(ctx.api, "getProof", goalId);
  } catch (e) {
    // Fall back to the proof inside the goal bundle only when it holds one: a null there must not turn a
    // failed request into "not enough data yet".
    if (bundle && bundle.proof) proof = bundle.proof;
    else proofError = e;
  }
  const forecast = await forecastP;

  const currency = goal?.currency;
  const m = (v) => money(v, currency);
  const shadow = goal?.autonomy === "shadow";

  // 1. The verdict first, then one sentence with the money against the people we never contact, then the figures.
  const resultCard = h("section", { class: "card pf-hero span-12", "aria-label": "Extra profit compared with people we never contact" });
  let early = false;
  if (proofError) {
    resultCard.append(h("h2", { class: "card-title" }, "Extra profit compared with people we never contact"), failedBox("proof", proofError, retry));
  } else {
    const verdict = profitVerdict(proof?.incremental_profit);
    resultCard.append(
      verdictLine(verdict, "pf-verdict"),
      profitLede(verdict, proof?.incremental_profit, m, goal, proof),
      ...proofCaveats(proof),
    );
    if (proof && verdict.key === "early") {
      early = true;
      resultCard.append(h("div", { class: "pf-figure" }, earlyStrip(proof, forecast, shadow, m)));
    } else if (proof) {
      // The figures that support the sentence: revenue next to profit, the return on spend once it has a range,
      // and the spend always (a measured $0 is a value). A figure with no range yet is left to the verdict.
      const measured = complete(proof.incremental_profit) || complete(proof.incremental_revenue);
      const chart = estimateChart(
        [{ label: "Extra profit", est: proof.incremental_profit }, { label: "Extra revenue", est: proof.incremental_revenue }], m,
        "Extra money compared with people we never contact, with 95% ranges");
      resultCard.append(figure(
        kpis([
          measured && estimateCell(described("Extra revenue", `Extra sales compared with people we never contact, before the cost of discounts and messages.${detectableAbout(proof.detectable?.revenue, m, "a real gain in revenue")}`), proof.incremental_revenue, m),
          complete(proof.roi) && estimateCell(described("Return on spend", `Extra profit for each ${m(1)} spent on discounts and messages.`), proof.roi, times),
          plainCell(described("Spend so far", "On discounts and messages, for this goal."), m(proof.spend)),
        ], "pf-hero-strip"),
        chart,
      ));
    }
  }

  // 2. What to do with the autonomy dial. 3. The simple rule. 4. Its own forecast (first in Shadow).
  const advice = proofError ? null : autopilotAdvice(goal, proof);
  const showForecast = !(forecast.state === "skip" || (forecast.state === "none" && !shadow));
  // Side by side with the simple rule (7 + 5) unless it leads in Shadow, where it is the main figure. While the
  // simple rule has no range yet its card is one sentence, so both go full width rather than leave a hole beside
  // the forecast.
  const ruleEarly = !complete(proof?.lift_vs_baseline);
  const forecastEl = showForecast
    ? forecastCard(forecast, goal, currency, { span: shadow || !proof || ruleEarly ? "span-12" : "span-5 pf-forecast-side", retry })
    : null;
  const ruleSpan = !shadow && !ruleEarly && forecastEl ? "span-7" : "span-12";

  // append() turns null into the text "null", so leave out what is not there.
  page.replaceChildren(
    ...[
      head(goal ? goal.text : "The whole programme", { asOf: proof?.as_of }),
      goalError && h("p", { class: "notice" }, `Couldn't load the goal (${errText(goalError)}); amounts are shown without a currency.`),
      h(
        "div",
        { class: "grid pf-grid" },
        ...[
          resultCard,
          shadow ? forecastEl : null,
          advice ? adviceCard(advice, goal) : null,
          proof ? ruleCardFor(proof, m, ruleSpan) : null,
          shadow ? null : forecastEl,
          expectFact(proof, direction(proof?.incremental_profit) !== "early", proof ? "span-5" : "span-12"),
          proof ? shareCardFor(proof, goal, ctx, announcer, "span-7") : null,
          proofError ? null : methodCard(proof, early),
        ].filter(Boolean),
      ),
      announcer,
    ].filter(Boolean),
  );
}
