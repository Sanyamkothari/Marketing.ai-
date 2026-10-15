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
  const sentence = contactListLead({ scoring, treat, policy });
  const taken = takenOffLines(treat);
  return taken ? (sentence ? `${sentence} ${taken}` : taken) : sentence;
}

/**
 * What the contact-list check took off the list, one plain sentence each ("12 excluded because they are under 18."),
 * from the treat list summary's `contact_exclusion_lines`, which our code worded from its own counts. "" when the
 * check read nothing.
 */
export function takenOffLines(treat) {
  const lines = treat && Array.isArray(treat.contact_exclusion_lines) ? treat.contact_exclusion_lines : [];
  return lines
    .filter((line) => typeof line === "string" && line.trim())
    .map((line) => (/[.!?]$/.test(line) ? line : `${line}.`))
    .join(" ");
}

function contactListLead({ scoring = null, treat = null, policy = null } = {}) {
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
  // An uplift run's recommended contacts are what the policy chose after it had left out the comparison group and
  // the suppressed customers, and cut to the budget: those cannot explain a gap below that number. Only the two
  // things that happen after the policy chose (no channel to reach a customer on, the compliance gate) can.
  const chosen = !!(policy && count(policy.contacts_recommended));
  const budget =
    chosen && policy.stop_reason === "budget" && count(policy.budget_contacts)
      ? ` The contact budget of ${people(policy.budget_contacts)} limits the list.`
      : "";
  if (worth === null || treated === null || worth <= treated) return `${lead}${budget}`;

  const why = [];
  if (!chosen) {
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
  }
  if (treat && count(treat.uncontactable_rows) && treat.uncontactable_rows > 0) {
    why.push(`${people(treat.uncontactable_rows)} cannot be reached on any channel they have agreed to`);
  }
  if (treat && count(treat.compliance_withheld_rows) && treat.compliance_withheld_rows > 0) {
    why.push(`${people(treat.compliance_withheld_rows)} ${treat.compliance_withheld_rows === 1 ? "was" : "were"} taken off by the compliance check before sending`);
  }
  return `${why.length ? `${lead} Why the difference: ${why.join("; ")}.` : lead}${budget}`;
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
export function contactDownloadLink(download, { icon = true, attrs = "", runId = "" } = {}) {
  return `<a class="btn primary" href="${esc(download.href)}" download data-download="contact-list" data-download-kind="${esc(
    download.kind,
  )}"${runId ? ` data-run-id="${esc(runId)}"` : ""}${attrs ? ` ${attrs}` : ""}>${icon ? DOWNLOAD_ICON : ""}${CONTACT_LIST_LABEL}</a>`;
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

/**
 * The download and the sentence for one page, from what is known so far. `facts` is what `loadContactList` has
 * answered (null before it has), `treatHref` is where the treat list is. Only an answer that says the list cannot
 * be built (`facts.refused`) switches to the scored list: no answer yet, or a request that failed for another
 * reason, keeps the treat list and says nothing about the run that the server did not say.
 */
export function contactListParts({ facts = null, treatHref = "", scoresHref = "#", scoring = null, policy = null } = {}) {
  const f = facts || {};
  const download = contactListDownload({
    treatHref: f.treatHref || treatHref,
    scoresHref: f.scoresHref || scoresHref,
    treatError: f.refused ? f.error || new Error("The contact list is not available.") : null,
  });
  return { download, sentence: contactListSentence({ scoring, treat: f.summary || null, policy }) };
}

/** The sentence's place on a page, kept even when empty so the answer can be drawn into it when it arrives. */
export const contactSlotHtml = (runId, sentence, download) =>
  `<div data-contact-slot data-run-id="${esc(runId)}">${contactSentenceHtml(sentence, download)}</div>`;

/** The scored list's place inside Technical details, kept for the same reason. */
export const allScoresSlotHtml = (runId, download, scoresHref) =>
  `<div data-all-scores-slot data-run-id="${esc(runId)}">${allScoresHtml(download, scoresHref)}</div>`;

/**
 * A page was drawn before the server answered about the treat list; draw the answer into the page's own download,
 * sentence and scored-list slots (matched by run, so a page the person has since left is not touched).
 */
export function repaintContactList(root, { runId, facts, scoring = null, policy = null, scoresHref = "#" } = {}) {
  if (!root || !runId) return;
  const mine = (el) => el.getAttribute("data-run-id") === runId;
  const all = (selector) => Array.from((root.querySelectorAll && root.querySelectorAll(selector)) || []);
  const { download, sentence } = contactListParts({ facts, scoresHref, scoring, policy });
  for (const a of all('a[data-download="contact-list"]')) {
    if (!mine(a)) continue;
    a.setAttribute("href", download.href);
    a.setAttribute("data-download-kind", download.kind);
  }
  for (const el of all("[data-contact-slot]")) if (mine(el)) el.innerHTML = contactSentenceHtml(sentence, download);
  for (const el of all("[data-all-scores-slot]")) if (mine(el)) el.innerHTML = allScoresHtml(download, scoresHref);
}
