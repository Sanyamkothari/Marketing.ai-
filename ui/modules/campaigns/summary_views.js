// The plain results summary a person sees first on the campaign's Review page (pure: it draws from its
// arguments, so the jsdom tests call it with recorded answers). One short card: how many customers to contact and
// who was left out and why, the first customers best first, how well the model did on customers it had not
// seen, and what is not proven yet. The numbers and tables of the old Review page sit one click away behind
// "Show technical details" (`technicalBoxHtml`). Words follow the glossary (`ui/labels.js`).

import { button } from "../../components.js";
import { esc } from "../../dom.js";
import { term } from "../../labels.js";
import { contactSentence, doctorHref, exclusionLines, likelihoodText, notProvenText, unseenSentence } from "./summary_rules.js";

export const SHOW_TECHNICAL_LABEL = "Show technical details";
export const HIDE_TECHNICAL_LABEL = "Hide technical details";

const p = (text, attrs = "", cls = "") => (text ? `<p class="cw-p${cls ? ` ${cls}` : ""}"${attrs ? ` ${attrs}` : ""}>${esc(text)}</p>` : "");
const h3 = (text) => `<h3 class="cw-h3 cw-sum-h">${esc(text)}</h3>`;

/**
 * The first customers as a small table, best first. A column that no row fills (offer, channel, reason) is left out.
 * `top` is the server's answer `{ customers, unavailable }`, `{ error }` when it could not be read, or `{ denied }`
 * for a person whose role does not allow the names, or `{ elsewhere: true }` for a list no campaign run made.
 */
function customersPart(top) {
  const title = h3("The best customers first");
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

/**
 * The summary card. `list` is the contact list's summary, `version` the model's registry record (or null),
 * `top` the first customers (see `customersPart`), `caseId` the Data Doctor case the file came from (or null),
 * `resultsOn` the day results can be added (or ""). A part whose figure is missing says so in words.
 */
export function resultsSummaryHtml({ list = null, version = null, top = null, caseId = null, resultsOn = "" } = {}) {
  const contact = contactSentence(list);
  const excluded = exclusionLines(list);
  const unseen = unseenSentence(version);
  const left = excluded.length
    ? `${h3("Left out of the list")}<ul class="cw-list" data-cw-sum-excluded>${excluded.map((l) => `<li>${esc(l.text)}</li>`).join("")}</ul>`
    : "";
  const doctor = caseId
    ? `<div class="cw-actions">${button({ label: "What the Doctor did", href: doctorHref(caseId), kind: "tertiary", attrs: "data-cw-sum-doctor" })}</div>`
    : "";
  return `<section class="card cw-card cw-sum" data-cw-results><div class="cw-card-body"><div class="cw-card-head"><h2 class="cw-h">Your results at a glance</h2></div>${
    contact
      ? `<p class="cw-sum-lead" data-cw-sum-contact>${esc(contact)}</p>`
      : p("How many customers will be contacted could not be read for this list.", "data-cw-sum-contact", "cw-note")
  }${left}${customersPart(top)}${h3("How well it did")}${
    unseen
      ? p(unseen, "data-cw-sum-model")
      : p("How well the model did on customers it had not seen could not be read, so check the model's page before you rely on this list.", "data-cw-sum-model", "cw-note")
  }${h3("What is not proven yet")}${p(notProvenText(resultsOn), "data-cw-sum-unproven")}${doctor}</div></section>`;
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
