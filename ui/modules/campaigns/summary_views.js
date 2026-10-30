// The plain results a person sees on the campaign's Review page (pure: they draw from their arguments, so the jsdom
// tests call them with recorded answers). A short strip above the tabs says how many customers to contact and how good
// the model is, in one honest sentence (`resultsSummaryHtml`). The Output tab holds the rest: who is contacted and who is
// not, with reasons and the first customers best first (`contactPartHtml`), and the model's quality tenth by tenth
// (`decileTableHtml`). Words follow the glossary (`ui/labels.js`).

import { esc } from "../../dom.js";
import { goalOutcome, term } from "../../labels.js";
import {
  comparisonRows,
  contactSentence,
  exclusionLines,
  liftRows,
  liftSentence,
  upliftSentence,
  likelihoodText,
  notPickedRows,
  notProvenText,
  unseenSentence,
} from "./summary_rules.js";
import { count } from "../../format.js";

const p = (text, attrs = "", cls = "") => (text ? `<p class="cw-p${cls ? ` ${cls}` : ""}"${attrs ? ` ${attrs}` : ""}>${esc(text)}</p>` : "");
const people = (n) => `${count(n)} ${n === 1 ? "customer" : "customers"}`;

/**
 * The first customers as a small table, best first. A column that no row fills (offer, channel, reason) is left out.
 * `top` is the server's answer `{ customers, unavailable }`, `{ error }` when it could not be read, or `{ denied }`
 * for a person whose role does not allow the names, or `{ elsewhere: true }` for a list no campaign run made.
 */
function customersPart(top) {
  const title = p("The best customers first", "", "cw-sum-sub");
  if (!top) return "";
  if (top.elsewhere) {
    return `${title}${p("This list was not made by a campaign run, so its first customers are not shown here. They are in the contact list file.", "data-cw-sum-names", "cw-note")}`;
  }
  if (top.denied) return `${title}${p("Showing the customers by name needs the Analyst role.", "data-cw-sum-names", "cw-note")}`;
  if (top.error) return `${title}${p("The first customers could not be loaded just now. Reload the page to try again.", "data-cw-sum-names", "cw-note")}`;
  const rows = top.customers || [];
  if (!rows.length) {
    const why = top.unavailable ? ` ${top.unavailable}` : "";
    return `${title}${p(`No customers are shown yet.${why}`, "data-cw-sum-names", "cw-note")}`;
  }
  const columns = [
    ["Customer", (r) => r.customer, ""],
    ["Likelihood", (r) => likelihoodText(r.score), "num"],
    [term("priority"), (r) => r.priority, ""],
    ["Offer", (r) => r.offer, ""],
    ["Channel", (r) => r.channel, ""],
    ["Why", (r) => r.reason, ""],
  ].filter(([label, get]) => label === "Customer" || label === "Likelihood" || rows.some((r) => get(r)));
  const head = columns.map(([label, , cls]) => `<th${cls ? ` class="${cls}"` : ""}>${esc(label)}</th>`).join("");
  const body = rows
    .map((r) => `<tr>${columns.map(([, get, cls]) => `<td${cls ? ` class="${cls}"` : ""}>${esc(get(r) || "")}</td>`).join("")}</tr>`)
    .join("");
  return `${title}<div class="cw-sum-scroll"><table class="tbl cw-tbl" data-cw-sum-customers><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/**
 * The groups behind the model's quality, tenth by tenth, as a table (the Output tab): how many customers, how many went
 * on to the result, and how that compares with the average. Empty when the lift file is not a yes/no ranking.
 */
export function decileTableHtml(lift, goal) {
  const rows = liftRows(lift);
  if (!rows.length) return "";
  const outcome = esc(goalOutcome(goal));
  return `<p class="cw-p">Customers the model had not seen, sorted by how likely it thought they were, in ${rows.length} equal groups:</p><div class="cw-sum-scroll"><table class="tbl cw-tbl" data-cw-sum-groups><thead><tr><th>Group</th><th class="num">Customers</th><th class="num">Went on ${outcome}</th><th class="num">Versus average</th></tr></thead><tbody>${rows
    .map((r) => `<tr><td>${esc(r.group)}</td><td class="num">${esc(r.customers)}</td><td class="num">${esc(r.rate)}</td><td class="num">${esc(r.times)}</td></tr>`)
    .join("")}</tbody></table></div>`;
}

/**
 * Who is contacted and who is not, and why (the Output tab): the customers left out of the list with their reasons, the
 * rest the model ranks lower, and the first customers best first. `list` is the contact list's summary, `campaign` the
 * campaign record (its comparison group), `top` the server's first customers (see `customersPart`).
 */
export function contactPartHtml({ list = null, campaign = null, top = null } = {}) {
  const comparison = comparisonRows(list, campaign);
  const rest = notPickedRows(list, comparison);
  const lines = exclusionLines(list, comparison).map((l) => l.text);
  if (rest) lines.push(`${people(rest)}: not on the list this time (the model ranks them lower, or the plan's limits left them out)`);
  const left = lines.length ? `${p("Left out of the list", "", "cw-sum-sub")}<ul class="cw-list" data-cw-sum-excluded>${lines.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
  return `${p(
    "The list is one file, best first. Send only the customers the file marks to contact, not simply the top of it. Every customer left out is marked, with their reason.",
    "data-cw-sum-list",
  )}${left}${customersPart(top)}`;
}

/**
 * The strip above the tabs: how many to contact, how good the model is in one honest sentence, and what is not proven
 * yet. `list` is the contact list's summary, `version` the model's registry record (or null), `story` the model's
 * `{ lift, importance, evaluation }` files (or null), `campaign` the campaign record (its comparison group), `resultsOn`
 * the day results can be added (or ""). A figure that is missing says so in words.
 */
export function resultsSummaryHtml({ list = null, version = null, story = null, campaign = null, resultsOn = "" } = {}) {
  const contact = contactSentence(list);
  const goal = campaign && campaign.use_case_id;
  const headline = liftSentence(story && story.lift, goal) || upliftSentence(story && story.evaluation, goal) || unseenSentence(version, goal);
  return `<section class="card cw-card cw-sum" data-cw-results><div class="cw-card-body">${
    contact
      ? `<p class="cw-sum-lead" data-cw-sum-contact>${esc(contact)}</p>`
      : p("How many customers will be contacted could not be read for this list.", "data-cw-sum-contact", "cw-note")
  }${
    headline
      ? p(headline, "data-cw-sum-model")
      : p("How well the model did on customers it had not seen could not be read, so check the Model tab before you rely on this list.", "data-cw-sum-model", "cw-note")
  }${p(notProvenText(resultsOn, comparisonRows(list, campaign) > 0), "data-cw-sum-unproven", "cw-note")}</div></section>`;
}
