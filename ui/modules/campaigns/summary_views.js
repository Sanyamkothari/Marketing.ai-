// The plain results summary a person sees first on the campaign's Review page (pure: it draws from its
// arguments, so the jsdom tests call it with recorded answers). One card in the order a marketer asks: who to contact
// (how many, who was left out and why, the first customers best first), why these customers (what the model relied
// on), how good the model is (one honest sentence and what it does not prove), and what was done to the data (a link
// to the approved plan). Each part keeps its detail behind "See more"; the numbers and tables of the old Review page
// sit behind "Show technical details" (`technicalBoxHtml`). Words follow the glossary (`ui/labels.js`).

import { button } from "../../components.js";
import { esc } from "../../dom.js";
import { term, userText } from "../../labels.js";
import {
  comparisonRows,
  contactSentence,
  doctorHref,
  exclusionLines,
  liftRows,
  liftSentence,
  likelihoodText,
  notPickedRows,
  notProvenText,
  reasonItems,
  unseenSentence,
} from "./summary_rules.js";
import { count } from "../../format.js";

export const SHOW_TECHNICAL_LABEL = "Show technical details";
export const HIDE_TECHNICAL_LABEL = "Hide technical details";

const p = (text, attrs = "", cls = "") => (text ? `<p class="cw-p${cls ? ` ${cls}` : ""}"${attrs ? ` ${attrs}` : ""}>${esc(text)}</p>` : "");
const people = (n) => `${count(n)} ${n === 1 ? "customer" : "customers"}`;
const h3 = (text) => `<h3 class="cw-h3 cw-sum-h">${esc(text)}</h3>`;

/**
 * The first customers as a small table, best first. A column that no row fills (offer, channel, reason) is left out.
 * `top` is the server's answer `{ customers, unavailable }`, `{ error }` when it could not be read, or `{ denied }`
 * for a person whose role does not allow the names, or `{ elsewhere: true }` for a list no campaign run made.
 */
function customersPart(top) {
  const title = p("The best customers first", "", "cw-sum-sub");
  if (!top) return "";
  if (top.elsewhere) {
    return `${title}${p('This list\'s first customers are on the full results page: open "Show technical details", then "Open the full results page".', "data-cw-sum-names", "cw-note")}`;
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

const MORE_LABEL = "See more";
const SHOWN_REASONS = 3;

/** A "See more" fold (a plain `<details>`): `inner` is its markup, empty gives nothing. */
const more = (inner, attrs = "") =>
  inner ? `<details class="cw-more" data-cw-sum-more${attrs ? ` ${attrs}` : ""}><summary>${esc(MORE_LABEL)}</summary><div class="cw-more-body">${inner}</div></details>` : "";

/** Why these customers: what the model relied on most, three of them in view, the rest one click away. */
function whyPart(story) {
  const items = reasonItems(story && story.importance);
  const title = h3("Why these customers");
  if (!items.length) {
    return `${title}${p("What the model relied on most could not be read for this list. The model's page has the details.", "data-cw-sum-why", "cw-note")}`;
  }
  const li = (r) => `<li>${userText(r.name)} <span class="cw-note">(${esc(r.share)} of what it relied on)</span></li>`;
  const rest = items.slice(SHOWN_REASONS);
  return `${title}${p("The model picked these customers mostly because of:", "data-cw-sum-why")}<ul class="cw-list" data-cw-sum-reasons>${items
    .slice(0, SHOWN_REASONS)
    .map(li)
    .join("")}</ul>${more(`<ul class="cw-list">${rest.map(li).join("")}</ul>`)}${p(
    "Each customer on the list also has their own three reasons, written next to them in the file.",
    "",
    "cw-note",
  )}`;
}

/** How good the model is: one honest sentence, what it does not prove, and the groups behind it one click away. */
function qualityPart({ version, story, list, campaign, resultsOn }) {
  const lift = liftSentence(story && story.lift);
  const unseen = unseenSentence(version);
  const headline = lift || unseen;
  const rows = liftRows(story && story.lift);
  const table = rows.length
    ? `<p class="cw-p">Customers the model had not seen, sorted by how likely it thought they were, in equal groups:</p><div class="cw-sum-scroll"><table class="tbl cw-tbl" data-cw-sum-groups><thead><tr><th>Group</th><th class="num">Customers</th><th class="num">Had the result</th><th class="num">Compared with average</th></tr></thead><tbody>${rows
        .map((r) => `<tr><td>${esc(r.group)}</td><td class="num">${esc(r.customers)}</td><td class="num">${esc(r.rate)}</td><td class="num">${esc(r.times)}</td></tr>`)
        .join("")}</tbody></table></div>`
    : "";
  const fold = more(`${table}${lift && unseen ? p(unseen) : ""}`);
  return `${h3("How good the model is")}${
    headline
      ? p(headline, "data-cw-sum-model")
      : p("How well the model did on customers it had not seen could not be read, so check the model's page before you rely on this list.", "data-cw-sum-model", "cw-note")
  }${p(notProvenText(resultsOn, comparisonRows(list, campaign) > 0), "data-cw-sum-unproven")}${fold}`;
}

/** What was done to the data: the approved plan is one link away. Left out when the file did not come through the Data Doctor. */
function dataPart(caseId) {
  if (!caseId) return "";
  return `${h3("What was done to your data")}${p(
    "Your file was checked and prepared by the Data Doctor before the model learned from it. Every change you approved is written down.",
    "data-cw-sum-data",
  )}<div class="cw-actions">${button({ label: "See the approved plan", href: doctorHref(caseId), kind: "tertiary", attrs: "data-cw-sum-doctor" })}</div>`;
}

/**
 * The summary card, in the order a marketer asks: who to contact, why these customers, how good the model is, what was
 * done to the data. `list` is the contact list's summary, `version` the model's registry record (or null), `top` the
 * first customers (see `customersPart`), `story` the model's `{ lift, importance }` files (or null), `campaign` the
 * campaign record (its comparison group), `caseId` the Data Doctor case the file came from (or null), `resultsOn` the
 * day results can be added (or ""). A part whose figure is missing says so in words.
 */
export function resultsSummaryHtml({ list = null, version = null, top = null, story = null, campaign = null, caseId = null, resultsOn = "" } = {}) {
  const contact = contactSentence(list);
  const comparison = comparisonRows(list, campaign);
  const excluded = exclusionLines(list, comparison);
  const rest = notPickedRows(list, comparison);
  const lines = excluded.map((l) => l.text);
  if (rest) lines.push(`${people(rest)}: not picked this time, because the model ranks them lower than the customers on the list`);
  const left = lines.length ? `${p("Left out of the list", "", "cw-sum-sub")}<ul class="cw-list" data-cw-sum-excluded>${lines.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
  return `<section class="card cw-card cw-sum" data-cw-results><div class="cw-card-body"><div class="cw-card-head"><h2 class="cw-h">Your results at a glance</h2></div>${h3("Who to contact")}${
    contact
      ? `<p class="cw-sum-lead" data-cw-sum-contact>${esc(contact)}</p>${p(
          "The list is one file, best first: the customers you can contact come first, then the ones left out, each with its reason.",
          "data-cw-sum-list",
        )}`
      : p("How many customers will be contacted could not be read for this list.", "data-cw-sum-contact", "cw-note")
  }${left}${customersPart(top)}${whyPart(story)}${qualityPart({ version, story, list, campaign, resultsOn })}${dataPart(caseId)}</div></section>`;
}

/**
 * Everything the Review page showed before the summary (the numbers, the tables, the campaign's ids), folded behind
 * one button, with the way to the full pages. `inner` is that markup; `links` are `[label, href]` pairs.
 */
export function technicalBoxHtml(inner, links = []) {
  const open = links
    .filter(([, href]) => href)
    .map(([label, href]) => button({ label, href, kind: "tertiary", size: "sm" }))
    .join("");
  return `<details class="cw-techbox" data-cw-technical><summary class="btn secondary" data-cw-technical-toggle><span class="cw-techbox-show">${esc(
    SHOW_TECHNICAL_LABEL,
  )}</span><span class="cw-techbox-hide">${esc(HIDE_TECHNICAL_LABEL)}</span></summary><div class="cw-body cw-techbox-body">${inner}${
    open ? `<div class="cw-actions" data-cw-technical-links>${open}</div>` : ""
  }</div></details>`;
}
