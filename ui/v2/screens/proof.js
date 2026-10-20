/* Proof: did it make you money, and how sure are we (PITCH.md screen 5, "Proof").

   The page answers a business owner's questions in their order:
     1. Did it make me extra money? A verdict in plain words comes before any number: "Real extra profit",
        "Not sure yet", "Losing money so far" or "Too early to tell". Then the extra profit and revenue
        compared with the people we never contact (a small random group left alone), each with its range,
        and one line on how this is measured (real extra profit, not "attributed revenue").
     2. What should I do now? "Ready for Autopilot" when even the low end of the profit range is above zero
        and the goal is not on Autopilot yet; otherwise "stay in Shadow or Approve until the range is above
        zero", with a link to the Plan dial.
     3. Is the AI earning its keep? A verdict against the simple rule anyone could set up.
     4. What does it expect to earn? Its own forecast (GET /v2/expected), always called a forecast and never
        proof. In Shadow mode, where nothing is sent and so nothing can be proven, it comes straight after the
        verdict.
     5. Spend, return on spend and the three groups; the API's own notes; a paragraph ready to share.
     6. What to expect, as general guidance about the product (3-10% typical), labelled as not being a result
        from this business, with its source.

   Honesty rules this file keeps:
     - every number comes from the ProofSummary or the Expected forecast the API sends (or the mock files
       that mirror them); nothing is added up into a new figure here, and a verdict only compares a range's
       low and high with zero;
     - an estimate is always shown with its low and high, never a bare point, and "not sure yet" when its
       range includes zero; a missing estimate says "not enough data yet" instead of showing a zero;
     - general facts about the product (3-10%) are labelled as such and never shown as this shop's numbers.
   There are no pricing terms in the API, so nothing about "paid in results" is shown yet. */

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

/** Return on spend as a multiple: 2.3 reads "2.3×" (each unit spent brought 2.3 back in extra profit). */
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
 * page shows real extra profit: switch to Autopilot"). Returns null when there is no goal to advise on.
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
      text: real
        ? "The proof shows real extra profit, but this goal is paused, so nothing is being sent. If it paused itself, the Live screen and the inbox say why. Check that before you resume it, or before you move the dial to Autopilot."
        : "This goal is paused, so nothing is being sent. If it paused itself, the Live screen and the inbox say why. Check that before you resume it. Autopilot only makes sense once this page shows real extra profit, with the whole range above zero.",
      link: "Go to the Live screen to see why and resume",
      screen: "live",
    };
  }
  if (goal.status === "draft") {
    return {
      key: "draft",
      title: "The plan is not approved yet",
      text: "Nothing runs until you approve the plan. Start in Shadow: it decides and shows you what it would do, but sends nothing.",
      link: "Check and approve the plan",
    };
  }
  if (level === "autopilot" && direction(proof?.incremental_profit) === "down") {
    return {
      key: "autopilot-losing",
      title: "On Autopilot, and it is losing money so far",
      text: "The whole profit range is below zero. Move the dial back to Approve each batch on the Plan screen, or pause it with the button at the top, and look at who it contacts and which offers it uses.",
      link: "Change the autonomy level on the Plan screen",
    };
  }
  if (level === "autopilot") {
    return real
      ? {
          key: "autopilot-backed",
          title: "On Autopilot, and the proof backs it",
          text: "Even the low end of the profit range is above zero. It acts on its own inside the limits you set, and the Pause button at the top stops everything at once.",
          link: null,
        }
      : {
          key: "autopilot-unproven",
          title: "On Autopilot, but the proof is not there yet",
          text: "The profit range is not above zero yet. If you would rather check each batch until it is, move the dial back to Approve each batch on the Plan screen.",
          link: "Change the autonomy level on the Plan screen",
        };
  }
  if (real) {
    return {
      key: "ready",
      title: "Ready for Autopilot",
      text: `The proof shows real extra profit: even the low end of the range is above zero. It is on ${autonomyName(level)} now. If you want, let it act on its own inside the limits you set (budget, quiet hours, weekly limits). You can pause it at any time.`,
      link: "Switch to Autopilot on the Plan screen",
      primary: true,
    };
  }
  if (direction(proof?.incremental_profit) === "down") {
    return {
      key: "losing",
      title: "Not ready for Autopilot: it is losing money so far",
      text: `Stay on ${autonomyName(level)}, or pause it with the button at the top, and look at the plan: who it contacts, which offers it may use and how often.`,
      link: "Look at the plan",
    };
  }
  if (level === "shadow") {
    return {
      key: "shadow-next",
      title: "Not ready for Autopilot yet: stay in Shadow, then Approve",
      text: "In Shadow mode it sends nothing, so there is nothing to prove yet. When you like what it would do, switch to Approve each batch with a small budget. Switch to Autopilot only when this page shows real extra profit, with the whole range above zero.",
      link: "Change the autonomy level on the Plan screen",
    };
  }
  return {
    key: "stay",
    title: "Not ready for Autopilot yet",
    text: `Stay on ${autonomyName(level)} until the profit range is above zero. This page will say "Real extra profit" when it is.`,
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
    if (profit) bits.push(`${range(profit, estimateFormat(profit, m))} in extra profit`);
    if (revenue) bits.push(`${range(revenue, estimateFormat(revenue, m))} in extra revenue`);
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
    out.push(`Against the simple rule anyone could set up, it made ${range(lift, estimateFormat(lift, m))} more profit per customer.`);
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
  out.push(proof.roi ? `${spend}; return on spend ${range(proof.roi, estimateFormat(proof.roi, times))}.` : `${spend}.`);
  const gloss = notesGloss(proof.notes);
  if (gloss) out.push(gloss);
  for (const note of proof.notes || []) out.push(String(note));
  out.push("Ranges are 95% ranges: the real number is very likely inside them.");
  return out.join(" ");
}

/**
 * Horizontal bars with error bars, one row per estimate, on one shared axis that always includes 0.
 * Rows without an estimate say "Not enough data yet". Returns null when no row has an estimate.
 * The zero line is drawn through each bar only, so the value written under a bar never crosses it.
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
  const rowH = 60;
  const top = 8;
  const H = top + rows.length * rowH + 20;
  const x = (v) => L + ((Number(v) - lo) / span) * (W - L - R);
  const x0 = x(0);
  const described = rows.map((r) => `${r.label}: ${complete(r.est) ? range(r.est, estimateFormat(r.est, fmt)) : NOT_ENOUGH}`).join("; ");

  const parts = [s("title", null, `${label}. ${described}`)];
  rows.forEach((r, i) => {
    const cy = top + i * rowH + 22;
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
      s("text", { x: L, y: cy + 29, class: "ap-chart-note pf-chart-note" }, range(r.est, estimateFormat(r.est, fmt))),
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

/** A KPI cell whose value is an estimate with its range, or "not enough data yet" in grey (never a zero). */
function estimateCell(title, est, baseFmt, hint) {
  const fmt = estimateFormat(est, baseFmt);
  const ok = complete(est);
  return h(
    "div",
    { class: "kpi" },
    h("dt", { class: "kpi-label" }, title),
    h("dd", { class: ok ? "kpi-value" : "kpi-value is-missing" }, ok ? fmt(est.point) : NOT_ENOUGH),
    ok && h("dd", { class: "kpi-range tabular" }, `Range ${fmt(est.low)} to ${fmt(est.high)}`),
    ok && straddlesZero(est) && h("dd", { class: "kpi-note pf-unsure" }, "Not sure yet: the range includes zero"),
    hint && h("dd", { class: "kpi-note" }, hint),
  );
}

function plainCell(title, value, hint) {
  const shown = present(value) && value !== DASH;
  return h(
    "div",
    { class: "kpi" },
    h("dt", { class: "kpi-label" }, title),
    h("dd", { class: shown ? "kpi-value" : "kpi-value is-missing" }, shown ? value : DASH),
    hint && h("dd", { class: "kpi-note" }, hint),
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

/** The sentence under the profit verdict, from the range's own numbers. */
function profitSentence(v, est, fmt, goal, hasNotes) {
  const r = complete(est) ? range(est, estimateFormat(est, fmt)) : null;
  if (v.key === "real") {
    return `Compared with the people we never contact, it brought in ${r} more profit. The whole range is above zero, so it very likely made money that would not have come in without it.`;
  }
  if (v.key === "unsure") {
    return `Best estimate so far: ${r} more profit than the people we never contact. The range includes zero, so it may not have made any extra profit yet. It needs more time, or more customers, before we can say.`;
  }
  if (v.key === "losing") {
    return `Compared with the people we never contact, it made ${r}: less profit than leaving these customers alone. Look at the plan, and pause it with the button at the top if you want it to stop.`;
  }
  if (goal?.autonomy === "shadow") {
    return `${NOT_ENOUGH}. In Shadow mode it sends nothing, so there is nothing to measure yet. What it expects to earn is just below, as its own forecast.`;
  }
  const why = hasNotes ? " The notes further down say more." : "";
  return `${NOT_ENOUGH}. Proof needs the agent, the simple rule and the people we never contact to run side by side for a while, and each decision only counts once the days it is given to work have passed.${why}`;
}

/** The figures of a block next to their chart (stacked on a narrow screen). The chart is null without a range. */
function figure(strip, chart, kind = "pair") {
  return h(
    "div",
    { class: `pf-figure pf-figure-${kind}` },
    strip,
    chart && h("figure", { class: "pf-chart" }, chart,
      h("figcaption", { class: "meta" }, "Each bar is the best estimate; the line across it is its 95% range.")),
  );
}

/** "How this is measured": one plain line, with the detail folded away. */
function methodBlock(proof) {
  const holdout = proof && present(proof.holdout_n) ? ` (${count(proof.holdout_n)} so far)` : "";
  return h(
    "div",
    { class: "pf-method", role: "note", "aria-label": "How this is measured" },
    h(
      "p",
      null,
      h("strong", null, "How this is measured: "),
      `a small random group of your customers${holdout} is never contacted. Everyone it looks after is compared with them, so this is real extra profit, after the cost of discounts and messages. It is not "attributed revenue", where a tool takes credit for every sale that follows a message.`,
    ),
    h(
      "details",
      { class: "pf-details" },
      h("summary", null, "More about the method"),
      h(
        "ul",
        { class: "pf-bullets" },
        h("li", null, "Each customer is put in one of three groups at random: the agent decides for most of them, a simple rule decides for some, and the people we never contact get nothing at all."),
        h("li", null, "Because the groups are picked at random, they would buy about the same if left alone. The difference in profit between them is what the messages and offers really added."),
        h("li", null, "A decision counts once the days it is given to work have passed, so the newest decisions are not in yet."),
        h("li", null, "Each range is a 95% range: the real number is very likely inside it. When the range includes zero, it says \"not sure yet\" rather than guess."),
      ),
    ),
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

function adviceCard(advice, goal) {
  const tone = advice.key === "ready" || advice.key === "autopilot-backed" ? "good" : "neutral";
  return h(
    "section",
    { class: `card pf-advice pf-advice-${tone} span-12`, "aria-labelledby": "pf-advice-title" },
    h(
      "div",
      { class: "pf-advice-body" },
      h("p", { class: "pf-eyebrow" }, "Next step"),
      h("h2", { class: "card-title", id: "pf-advice-title" }, advice.title),
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

/** "What it expects to earn": the agent's own forecast, never proof. */
function forecastBody(expected, goal, currency) {
  const m = (v) => money(v, currency);
  const shadow = goal?.autonomy === "shadow";
  const est = expected.expected_net;
  const since = present(expected.since) ? ` since ${day(expected.since)}` : "";
  if (!complete(est)) return [emptyBox("No forecast yet", String(expected.words || "There is no forecast yet."))];
  const kids = [
    h(
      "p",
      { class: "pf-lede" },
      shadow
        ? `For the contacts it would have made${since} (nothing was sent, because this goal is in Shadow mode), it expects this much extra profit over the ${count(expected.horizon_days)} days after each contact:`
        : `For the contacts it decided on${since}, it expects this much extra profit over the ${count(expected.horizon_days)} days after each contact:`,
    ),
    kpis([
      estimateCell("Expected extra profit", est, m, "its own forecast"),
      plainCell(shadow ? "Contacts it would make" : "Contacts", count(expected.with_forecast), `to ${count(expected.customers)} customers`),
      plainCell("Expected spend", m(expected.expected_spend), "on discounts and messages"),
    ]),
  ];
  if (present(expected.contacts) && present(expected.with_forecast) && Number(expected.contacts) !== Number(expected.with_forecast)) {
    kids.push(h("p", { class: "meta" },
      `Of ${count(expected.contacts)} contacts, ${count(expected.with_forecast)} carry a forecast of their own; only those are added up.`));
  }
  if (straddlesZero(est)) kids.push(h("p", { class: "meta pf-unsure" }, "Even its own forecast is not sure yet: the range includes zero."));
  kids.push(h("p", { class: "meta" },
    "A forecast is what it thinks will happen, not proof. Only the comparison with the people we never contact shows what really happened."));
  return kids;
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

function docLink() {
  const base = globalThis.AUTONOMY_DOCS_BASE;
  if (typeof base === "string" && base) {
    try {
      const href = new URL("../research/v2/03_DECISION_SCIENCE.md", base.endsWith("/") ? base : `${base}/`).href;
      return h("a", { href, rel: "noopener" }, EXPECT_DOC);
    } catch {
      // A base we cannot resolve: name the file instead.
    }
  }
  return h("code", null, EXPECT_DOC);
}

/** The 3-10% guidance, labelled as a fact about AI marketing in general, never as this shop's number. */
function expectFact(proof, measured, span) {
  const share = proof && complete(proof.incremental_profit_share) ? proof.incremental_profit_share : null;
  return h(
    "aside",
    { class: `card pf-fact ${span}`, "aria-labelledby": "pf-fact-title" },
    h(
      "div",
      { class: "card-head" },
      h("h2", { class: "card-title", id: "pf-fact-title" }, "What to expect"),
      h("span", { class: "pill pill-neutral" }, "General guidance"),
    ),
    h("p", { class: "meta" }, "General guidance about AI marketing, not a result from your business."),
    h(
      "p",
      null,
      "Well-run AI marketing, measured honestly against people who are never contacted, typically adds 3-10% in real extra profit. Claims of much more are rarely measured this way.",
    ),
    share
      ? h("p", null, h("strong", null, "This goal so far: "), `${range(share, pct)} extra profit${straddlesZero(share) ? " (not sure yet: the range includes zero)" : ""}.`)
      : h("p", { class: "muted" }, measured
          ? "Your own result is the one at the top of this page, in money, with its range. It is measured, not taken from this guidance."
          : "Your own result will show at the top of this page, in money, with its range, once there is enough data."),
    h("p", { class: "meta pf-source" }, "Source: published results measured against an untouched group, summarised in ", docLink(), "."),
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
  const r = complete(lift) ? range(lift, estimateFormat(lift, m)) : null;
  const sentence = {
    beats: `Per customer, it made ${r} more profit than the simple rule. The whole range is above zero, so the AI is earning its keep.`,
    unsure: `Per customer, ${r} more profit than the simple rule. The range includes zero, so it has not yet clearly beaten it.`,
    behind: `Per customer, ${r} compared with the simple rule: the simple rule is doing better so far. Worth a look at the plan.`,
    early: `${NOT_ENOUGH} to compare with the simple rule.`,
  }[rv.key];
  return h(
    "section",
    { class: `card pf-rule ${span}`, "aria-labelledby": "pf-rule-title" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title", id: "pf-rule-title" }, "Compared with a simple rule anyone could set up")),
    verdictLine(rv, "pf-rule-verdict", "sm"),
    h("p", { class: "pf-verdict-text" }, sentence),
    complete(lift) &&
      figure(kpis([estimateCell("Extra profit per customer", lift, m)]),
        estimateChart([{ label: "Per customer", est: lift }], m, "Extra profit per customer compared with the simple rule, with its 95% range"), "single"),
    h("p", { class: "meta" },
      `The simple rule treats everyone in the audience the same way, like the usual "10% off to everyone who hasn't bought in 60 days" email. ${count(proof.baseline_n)} customers, picked at random, get the simple rule instead of the agent.`),
  );
}

function groupsCardFor(proof, m) {
  return h(
    "section",
    { class: "card pf-groups span-12", "aria-labelledby": "pf-groups-title" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title", id: "pf-groups-title" }, "Spend and the three groups")),
    kpis([
      plainCell("Spend so far", m(proof.spend), "on discounts and messages"),
      estimateCell("Return on spend", proof.roi, times, `Extra profit for each ${m(1)} spent`),
      plainCell("People we never contact", count(proof.holdout_n), "picked at random, left alone"),
      plainCell("Agent decides", count(proof.agent_n), "customers"),
      plainCell("Simple rule", count(proof.baseline_n), "customers"),
    ], "pf-groups-strip"),
  );
}

function notesCardFor(proof, span) {
  const gloss = notesGloss(proof.notes);
  return h(
    "section",
    { class: `card pf-notes ${span}`, "aria-labelledby": "pf-notes-title" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title", id: "pf-notes-title" }, "Notes from the measurement")),
    gloss && h("p", { class: "meta" }, gloss),
    h("ul", { class: "list pf-note-list" }, ...proof.notes.map((n) => h("li", null, String(n)))),
  );
}

function shareCardFor(proof, goal, ctx, announcer) {
  const summary = proofSummaryText(proof, goal);
  const field = h("textarea", { class: "pf-summary", readonly: true, rows: 5, id: "ap-proof-summary" });
  field.value = summary;
  const copyBtn = h("button", { type: "button", class: "btn btn-secondary btn-sm" }, "Copy summary");
  copyBtn.addEventListener("click", async () => {
    const ok = await copyText(summary, field);
    notify(ctx, ok ? "Summary copied." : "Couldn't copy; the text is selected, so press Ctrl+C (or Cmd+C).", ok ? "good" : "critical", announcer);
  });
  return h(
    "section",
    { class: "card pf-share span-12" },
    h(
      "div",
      { class: "card-head" },
      h("h2", { class: "card-title" }, h("label", { for: "ap-proof-summary" }, "Ready to share")),
      copyBtn,
    ),
    h("p", { class: "meta pf-share-hint" }, "Plain text with every figure and its range, for an email or a report."),
    field,
  );
}

/** The title row: "Proof", the goal, and the date the proof is as of (a stamp, only when the API sent one). */
function head(sub, { asOf, line } = {}) {
  return h(
    "div",
    { class: "page-head pf-head" },
    h("div", null,
      h("h1", { class: "page-title" }, "Proof"),
      present(sub) && h("p", { class: "page-sub" }, sub),
      present(line) && h("p", { class: "meta pf-head-line" }, line)),
    present(asOf) && h("div", { class: "page-actions" }, h("span", { class: "stamp" }, `As of ${day(asOf)}`)),
  );
}

const block = (w, hgt, extra = "") => h("span", { class: `skeleton ${extra}`.trim(), "aria-hidden": "true", style: css({ width: w, height: hgt }) });

/** While the proof loads: the title, and the result block at its real size in grey (nothing shifts after). */
function loadingView() {
  return [
    h("div", { class: "page-head pf-head" }, h("div", null,
      h("h1", { class: "page-title" }, "Proof"),
      h("p", { class: "page-sub" }, block("320px", "20px", "skeleton-inline")),
      h("p", { class: "meta pf-head-line" }, block("240px", "16px", "skeleton-inline")))),
    h(
      "div",
      { class: "grid" },
      h(
        "section",
        { class: "card pf-hero span-12", role: "status", "aria-busy": "true" },
        h("span", { class: "visually-hidden" }, "Loading the proof…"),
        h("div", { class: "card-head" }, h("h2", { class: "card-title" }, "Extra profit compared with people we never contact")),
        h("p", { class: "pf-verdict pf-verdict-lg" }, block("220px", "28px", "skeleton-inline")),
        h("div", { class: "pf-lines" }, block("100%", "16px"), block("70%", "16px")),
        h("div", { class: "pf-figure pf-figure-pair" },
          h("div", { class: "kpis", style: css({ "--kpi-count": "2" }) },
            ...["Extra profit", "Extra revenue"].map((l) => h("div", { class: "kpi" }, h("p", { class: "kpi-label" }, l),
              h("p", { class: "kpi-value" }, block("110px", "28px", "skeleton-inline")), h("p", { class: "kpi-range" }, block("140px", "12px", "skeleton-inline"))))),
          block("100%", "120px")),
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

  // 1. The verdict first, then the money against the people we never contact, then how it is measured.
  const resultCard = h(
    "section",
    { class: "card pf-hero span-12", "aria-labelledby": "pf-result-title" },
    h("div", { class: "card-head" }, h("h2", { class: "card-title", id: "pf-result-title" }, "Extra profit compared with people we never contact")),
  );
  if (proofError) {
    resultCard.append(failedBox("proof", proofError, retry));
  } else {
    const verdict = profitVerdict(proof?.incremental_profit);
    resultCard.append(verdictLine(verdict, "pf-verdict"), h("p", { class: "pf-verdict-text" }, profitSentence(verdict, proof?.incremental_profit, m, goal, Boolean(proof?.notes?.length))));
    if (proof && (proof.incremental_profit || proof.incremental_revenue)) {
      const chart = estimateChart(
        [{ label: "Extra profit", est: proof.incremental_profit }, { label: "Extra revenue", est: proof.incremental_revenue }], m,
        "Extra money compared with people we never contact, with 95% ranges");
      resultCard.append(figure(
        kpis([
          estimateCell("Extra profit", proof.incremental_profit, m, "after the cost of discounts and messages"),
          estimateCell("Extra revenue", proof.incremental_revenue, m),
        ]),
        chart,
      ));
    }
    resultCard.append(methodBlock(proof));
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

  const hasNotes = Boolean(proof && (proof.notes || []).length);
  const notes = hasNotes ? notesCardFor(proof, "span-7") : null;

  const level = goal ? `Autonomy level: ${autonomyName(goal.autonomy)} · ranges are 95% ranges` : "Ranges are 95% ranges";

  // append() turns null into the text "null", so leave out what is not there.
  page.replaceChildren(
    ...[
      head(goal ? goal.text : "The whole programme", { asOf: proof?.as_of, line: level }),
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
          proof ? groupsCardFor(proof, m) : null,
          expectFact(proof, direction(proof?.incremental_profit) !== "early", hasNotes ? "span-5" : "span-12"),
          notes,
          proof ? shareCardFor(proof, goal, ctx, announcer) : null,
        ].filter(Boolean),
      ),
      announcer,
    ].filter(Boolean),
  );
}
