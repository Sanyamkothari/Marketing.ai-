// The plain results summary's rules (pure: no DOM, no fetch). Every figure is read from an answer the server gave
// (the list's summary, the model's registry record, the first customers); a figure that is missing is left out
// or said to be missing, never filled in.

import { fmtNum, fmtPct } from "../../dom.js";
import { count } from "../../format.js";

const whole = (n) => Number.isInteger(n) && n >= 0;
const people = (n) => `${count(n)} ${n === 1 ? "customer" : "customers"}`;

/** Where "What the Doctor did" for a case opens: the Data Doctor screen on that case. */
export const doctorHref = (caseId) => `#/data/check/case/${encodeURIComponent(caseId)}`;

/**
 * How well the model did on customers it had not seen while it learned, in one sentence, from the model's
 * registry record (`test_score` is measured on customers held out of learning). The ranking measure is said the
 * way the Model page says it; any other measure is given as a number without its (technical) name. `null` when the record is
 * missing, so the page can say that it could not be read.
 */
export function unseenSentence(version) {
  if (!version || typeof version.test_score !== "number") return null;
  if (version.metric === "roc_auc") {
    return `On customers it had not seen while learning, the model put a customer who has the outcome above one who does not ${fmtPct(
      version.test_score,
      0,
    )} of the time. 50% would be no better than a coin toss; 100% would be perfect.`;
  }
  // Any other measure (an effect model's, a regression's) is an analyst's word: the number is given, its name is on the model's page.
  return `On customers it had not seen while learning, the model's main measure was ${fmtNum(version.test_score, 3)}. The model's page explains what that measure means.`;
}

/**
 * What is not proven yet, always said: a ranking is not a result. `resultsOn` is the day to add results, if known.
 * `comparison` is whether customers are kept back as a comparison group: without one the list only says who is likely
 * to respond among the customers contacted, never how many extra sales contacting them brings.
 */
export function notProvenText(resultsOn = "", comparison = false) {
  const when = resultsOn ? ` Add the results after ${resultsOn}.` : "";
  const group = comparison
    ? " Some customers are kept back and not contacted, so the extra customers this campaign brings can be measured once its results are in."
    : " No customers are kept back to compare with, so this shows who is likely to respond among the customers you contact, not how many extra sales contacting them brings.";
  return `Not yet proven: nothing shows yet that contacting these customers works. That can only be measured after the campaign is sent and its results come in, so until then this is a ranking, not a result.${group}${when}`;
}

/** The number of customers kept back as a comparison group, from the list's summary and the campaign's own counts. */
export function comparisonRows(list, campaign) {
  const own = list && whole(list.holdout_rows) ? list.holdout_rows : 0;
  const counted = campaign && campaign.counts && whole(campaign.counts.holdout) ? campaign.counts.holdout : 0;
  return Math.max(own, counted);
}

/** A column name as words for a person: `visits_last_7d` reads "Visits last 7d". */
export function columnWords(name) {
  const words = String(name || "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return words.slice(0, 1).toUpperCase() + words.slice(1);
}

/**
 * How much better than average the model's first customers did, in one sentence, from the training run's
 * `decile_lift.json` (customers it had not seen while learning, sorted best first in equal groups). `null` for any
 * answer that is not a ranking of a yes/no outcome with a base rate, so the page falls back to the model's own sentence.
 */
export function liftSentence(lift) {
  const bins = lift && lift.mode === "classification" && Array.isArray(lift.bins) ? lift.bins : [];
  const first = bins[0];
  const total = bins.reduce((sum, b) => sum + (whole(b && b.rows) ? b.rows : 0), 0);
  const ok = (n) => typeof n === "number" && Number.isFinite(n);
  if (!first || !total || !whole(first.rows) || !ok(first.lift) || !ok(first.actual_rate_pct) || !ok(lift.base_rate_pct) || lift.base_rate_pct <= 0) {
    return null;
  }
  const top = Math.max(1, Math.round((100 * first.rows) / total));
  const seen = `Measured on ${count(total)} customers the model had not seen while learning.`;
  if (first.lift < 1.2) {
    return `The ${top}% of customers the model puts first are not clearly more likely than the average customer to have the result you want, so do not rely on this list yet. ${seen}`;
  }
  const rates = `${fmtPct(first.actual_rate_pct / 100, 0)} against ${fmtPct(lift.base_rate_pct / 100, 0)} for an average customer`;
  return `The ${top}% of customers the model puts first are ${fmtNum(first.lift, 1)} times as likely as the average customer to have the result you want (${rates}). ${seen}`;
}

/** The groups of `decile_lift.json` as table rows, best first: `{ group, customers, rate, times }`; empty when it is not one. */
export function liftRows(lift) {
  const bins = lift && lift.mode === "classification" && Array.isArray(lift.bins) ? lift.bins : [];
  return bins
    .filter((b) => b && whole(b.rows) && typeof b.actual_rate_pct === "number" && typeof b.lift === "number")
    .map((b, i) => ({
      group: i === 0 ? "1 (best first)" : String(i + 1),
      customers: count(b.rows),
      rate: fmtPct(b.actual_rate_pct / 100, 0),
      times: `${fmtNum(b.lift, 1)} times`,
    }));
}

/**
 * What the model relied on most, from the training run's `feature_importance.json`: `[{ name, share }]` best first,
 * `share` as "48%". Columns that count for nothing are left out.
 */
export function reasonItems(importance, limit = 8) {
  const items = importance && Array.isArray(importance.items) ? importance.items : [];
  return items
    .filter((i) => i && i.feature && typeof i.share_pct === "number" && i.share_pct >= 0.5)
    .slice(0, limit)
    .map((i) => ({ name: columnWords(i.feature), share: fmtPct(i.share_pct / 100, 0) }));
}

/** The customers who are neither on the list nor left out (the model ranks them lower), `null` when it does not add up. */
export function notPickedRows(list, comparison = 0) {
  if (!list || !whole(list.total_rows) || !whole(list.treat_rows)) return null;
  const known = (whole(list.holdout_rows) ? 0 : comparison) + ["holdout_rows", "suppressed_rows", "uncontactable_rows", "compliance_withheld_rows"].reduce(
    (sum, k) => sum + (whole(list[k]) ? list[k] : 0),
    0,
  );
  const rest = list.total_rows - list.treat_rows - known;
  return rest > 0 ? rest : null;
}

/**
 * The customers left out of the list and why, one line each, from the list's summary. Only counts above nothing are
 * listed. A summary that gives its own `exclusions` (a list of `{ label, rows }` or an object of label to rows) has
 * them drawn too, so a reason the server adds shows up without a change here.
 */
export function exclusionLines(list, comparison = 0) {
  if (!list) return [];
  const lines = [];
  const add = (n, text) => {
    if (whole(n) && n > 0) lines.push({ rows: n, text });
  };
  add(whole(list.holdout_rows) ? list.holdout_rows : comparison, "kept back on purpose as the comparison group, so the effect of contacting can be measured");
  add(list.suppressed_rows, "left out by your contact rules (for example opted out, no consent, or contacted recently)");
  add(list.uncontactable_rows, "cannot be reached on any channel they have agreed to");
  add(list.compliance_withheld_rows, "taken off by the compliance check before sending");
  const extra = list.exclusions;
  const entries = Array.isArray(extra)
    ? extra.map((e) => [e && (e.label || e.reason), e && (e.rows ?? e.count)])
    : extra && typeof extra === "object"
      ? Object.entries(extra)
      : [];
  for (const [label, n] of entries) if (label) add(n, String(label));
  return lines.map((l) => ({ ...l, text: `${people(l.rows)}: ${l.text}` }));
}

/** The big sentence: how many to contact, out of how many. `null` when the list does not say. */
export function contactSentence(list) {
  if (!list || !whole(list.treat_rows)) return null;
  const total = whole(list.total_rows) && list.total_rows >= list.treat_rows ? ` of the ${count(list.total_rows)} checked` : "";
  return `${people(list.treat_rows)} to contact${total}`;
}

/** A customer's likelihood as shown in the table: a share, "91%", when the number is one; else as given. */
export function likelihoodText(score) {
  if (typeof score !== "number" || !Number.isFinite(score)) return "";
  return score >= 0 && score <= 1 ? fmtPct(score, 0) : fmtNum(score, 3);
}
