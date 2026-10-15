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

/** What is not proven yet, always said: a ranking is not a result. `resultsOn` is the day to add results, if known. */
export function notProvenText(resultsOn = "") {
  const when = resultsOn ? ` Add the results after ${resultsOn}.` : "";
  return `Not yet proven: nothing shows yet that contacting these customers works. That can only be measured after the campaign is sent and its results come in, so until then this is a ranking, not a result.${when}`;
}

/**
 * The customers left out of the list and why, one line each, from the list's summary. Only counts above nothing are
 * listed. A summary that gives its own `exclusions` (a list of `{ label, rows }` or an object of label to rows) has
 * them drawn too, so a reason the server adds shows up without a change here.
 */
export function exclusionLines(list) {
  if (!list) return [];
  const lines = [];
  const add = (n, text) => {
    if (whole(n) && n > 0) lines.push({ rows: n, text });
  };
  add(list.holdout_rows, "kept back on purpose as the comparison group, so the effect of contacting can be measured");
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
