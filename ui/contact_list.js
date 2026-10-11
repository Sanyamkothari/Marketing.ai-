// One contact list (Plan M M171, DEC-1601): the single file a marketer downloads, and the one sentence that
// says why fewer customers are contacted than are worth contacting.
//
// The file is the run's treat list (`treat_list.csv`, Plan J M98): every column a sending tool needs (who,
// priority, offer, channel, reason, comparison-group flag). A run that has none (a use case that does not
// contact customers, or a run whose treat list cannot be built) falls back to the scored list (`scores.csv`)
// under the same label, with a one-line note saying what the file is. The scored list stays one click away,
// inside Technical details, as "All scores (for analysts)".
//
// Pure: everything drawn comes from the arguments, so node can test it. Nothing is typed in here: every
// figure in the sentence is a field of the run's own `scoring_summary.json`, `treat_list_summary.json` or
// uplift `policy_recommendation.json`, and a part whose figure is missing is left out.

import { esc, fmtInt } from "./dom.js";

export const CONTACT_LIST_LABEL = "Download contact list";
export const ALL_SCORES_LABEL = "All scores (for analysts)";

const DOWNLOAD_ICON =
  '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 2v8m0 0 3.5-3.5M8 10 4.5 6.5M3 13h10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const FALLBACK_NOTE =
  "This run has no contact list with offers and channels, so this file is the list of every customer scored, each with a priority and an action.";
const SCORES_NOTE =
  "Every customer scored, with the likelihood, priority, action and reasons for each. Most people want the contact list instead.";

const count = (n) => Number.isInteger(n) && n >= 0;
const people = (n) => `${fmtInt(n)} ${n === 1 ? "customer" : "customers"}`;

/**
 * How many customers are worth contacting, from the run's own summary, or `null` when it does not say.
 * A propensity run's headline count is the customers in the bands it names (`count_where_band_in`); an
 * uplift run's is the policy's recommended contacts. A KPI that is a sum, or counts something else, is not a
 * number of customers and is never used here.
 */
export function worthContacting(scoring, policy = null) {
  if (policy && count(policy.contacts_recommended)) return policy.contacts_recommended;
  const kpi = scoring && scoring.kpi;
  if (kpi && /^\s*count_where_band_in\(/.test(kpi.formula || "") && count(kpi.value)) return kpi.value;
  return null;
}

const SUPPRESSION_WORDS = {
  opted_out: "opted out of marketing",
  consent_false: "have not given consent",
  recently_contacted: "were contacted recently",
};

/**
 * "1,645 customers are worth contacting; 831 will be contacted. Why the difference: ...", built only from the
 * run's artefacts. `scoring` is `scoring_summary.json`, `treat` is `treat_list_summary.json` and `policy` is an
 * uplift run's `policy_recommendation.json`; each may be null. Returns "" when there is nothing to say.
 *
 * The reasons are figures for the whole list (the comparison group is drawn from every customer, a contact rule
 * applies to every customer), so they are worded as such and never added up to the difference: the summaries do
 * not say how many of the customers worth contacting each one removed.
 */
export function contactListSentence({ scoring = null, treat = null, policy = null } = {}) {
  const worth = worthContacting(scoring, policy);
  const treated = treat && count(treat.treat_rows) ? treat.treat_rows : null;
  if (worth === null && treated === null) return "";
  let lead;
  if (worth !== null && treated !== null) {
    lead =
      worth === treated
        ? `${people(worth)} ${worth === 1 ? "is" : "are"} worth contacting, and everyone will be contacted.`
        : `${people(worth)} ${worth === 1 ? "is" : "are"} worth contacting; ${fmtInt(treated)} will be contacted.`;
  } else if (treated !== null) {
    lead = `${people(treated)} will be contacted.`;
  } else {
    lead = `${people(worth)} ${worth === 1 ? "is" : "are"} worth contacting.`;
  }
  if (worth === null || treated === null || worth <= treated) return lead;

  const why = [];
  const comparison = treat && count(treat.holdout_rows) ? treat.holdout_rows : scoring && count(scoring.control_group_rows) ? scoring.control_group_rows : null;
  if (comparison) {
    why.push(
      `${people(comparison)} in the whole list ${comparison === 1 ? "is" : "are"} held back at random as the comparison group (not contacted, so the effect of contacting can be measured)`,
    );
  }
  for (const rule of (scoring && scoring.suppressed) || []) {
    const words = SUPPRESSION_WORDS[rule.reason];
    if (words && count(rule.rows) && rule.rows > 0) why.push(`${people(rule.rows)} ${words}`);
  }
  if (treat && count(treat.uncontactable_rows) && treat.uncontactable_rows > 0) {
    why.push(`${people(treat.uncontactable_rows)} cannot be reached on any channel they have agreed to`);
  }
  if (treat && count(treat.compliance_withheld_rows) && treat.compliance_withheld_rows > 0) {
    why.push(`${people(treat.compliance_withheld_rows)} ${treat.compliance_withheld_rows === 1 ? "was" : "were"} taken off by the compliance check before sending`);
  }
  if (policy && policy.stop_reason === "budget" && count(policy.budget_contacts)) {
    why.push(`the contact budget of ${people(policy.budget_contacts)} was reached`);
  }
  return why.length ? `${lead} Why the difference: ${why.join("; ")}.` : lead;
}

/**
 * What the one download is. `treat` is the run's `treat_list_summary.json` (null when there is none), `treatError`
 * the answer that said the list cannot be built, `treatHref` and `scoresHref` the two files. Without a treat summary
 * the scored list is offered under the same label, with the note.
 */
export function contactListDownload({ treatHref = "", scoresHref = "#", treatError = null } = {}) {
  // The treat list unless the run said it cannot have one; with no answer yet it is assumed, as the route builds it.
  if (treatHref && !treatError) return { href: treatHref, kind: "contact", note: "" };
  return { href: scoresHref, kind: "scores", note: FALLBACK_NOTE };
}

/** The page's one primary download button. */
export function contactDownloadLink(download, { icon = true, attrs = "" } = {}) {
  return `<a class="btn primary" href="${esc(download.href)}" download data-download="contact-list" data-download-kind="${esc(
    download.kind,
  )}"${attrs ? ` ${attrs}` : ""}>${icon ? DOWNLOAD_ICON : ""}${CONTACT_LIST_LABEL}</a>`;
}

/** The sentence and, when the file is the scored list, its note, as a block that can sit under a page's tiles. */
export function contactSentenceHtml(sentence, download) {
  const lines = [];
  if (sentence) lines.push(`<p class="contact-sentence" data-contact-sentence>${esc(sentence)}</p>`);
  if (download && download.note) lines.push(`<p class="caption" data-contact-note>${esc(download.note)}</p>`);
  return lines.length ? `<div class="contact-block">${lines.join("")}</div>` : "";
}

/** The scored list, for analysts, for a Technical details section; nothing when it is already the main file. */
export function allScoresHtml(download, scoresHref) {
  if (!download || download.kind !== "contact") return "";
  return `<div class="all-scores" data-all-scores><p class="caption">${esc(
    SCORES_NOTE,
  )}</p><a class="btn quiet sm" href="${esc(scoresHref)}" download data-download="scores">${ALL_SCORES_LABEL}</a></div>`;
}
