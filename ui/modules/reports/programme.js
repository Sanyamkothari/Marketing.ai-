// "Programme summary": the value proven to date and the rules it is counted by (Plan M, M178). It is the block
// Results draws from `GET /campaigns/summary` (`summaryCardHtml`, ui/modules/simple/pages.js), drawn here too from the
// same source; Results keeps it until the campaigns layout replaces it. The numbers and the structure are unchanged, and
// the "needs your attention" cards stay on Results.
//
// The server writes some of its sentences in the engine's own words ("control group", "Causal: ..."). A marketer
// reads the glossary's words instead (Plan M §4.1), so the sentences the server sends are put into them on the way
// in: the rule, each campaign's claim and each reason. Names and figures are never touched.

import { emptyState, kpiRow } from "../../components.js";
import { EM_DASH, count, money, rupees } from "../../format.js";
import { esc } from "../../dom.js";
import { campaignNameIndex, summaryCardHtml } from "../simple/pages.js";
import { claimOf } from "./value_proof.js";

/** The glossary's word for the engine's: whole words, the first letter's case kept. */
const PLAIN = [
  [/\bcontrol groups?\b/gi, "comparison group"],
  [/\bhold-?outs?\b/gi, "comparison group"],
  [/\bcausal\b/gi, "proven"],
];

export function plainWords(sentence) {
  let out = String(sentence ?? "");
  for (const [pattern, word] of PLAIN) {
    out = out.replace(pattern, (found) => (found[0] === found[0].toUpperCase() ? word[0].toUpperCase() + word.slice(1) : word));
  }
  return out;
}

const SHORT = {
  proven: "the customers held back were chosen at random",
  estimated: "random by your statement, not verified",
  "not-proven": "the groups were not chosen at random",
};

/** "Causal: the customers held back were chosen at random" -> "Proven: the customers held back were chosen at random". */
const claimWords = (label) => {
  const claim = claimOf(label);
  return `${claim.word}: ${SHORT[claim.key]}`;
};

/** A copy of `proven` with the server's sentences in plain words. */
export function plainProven(proven) {
  const lines = (list) => (list || []).map((line) => ({ ...line, claim_label: line.claim_label ? claimWords(line.claim_label) : line.claim_label }));
  return {
    ...proven,
    rule: proven.rule ? plainWords(proven.rule) : proven.rule,
    totals: (proven.totals || []).map((total) => ({ ...total, campaigns: lines(total.campaigns) })),
    apart: lines(proven.apart).map((item) => ({ ...item, reason: plainWords(item.reason) })),
    excluded: (proven.excluded || []).map((item) => ({ ...item, reason: plainWords(item.reason) })),
    unpriced: (proven.unpriced || []).map((item) => ({ ...item, reason: plainWords(item.reason) })),
  };
}

/** A total's figure in the one number format the card below uses (rupees in lakh and crore, an amount as a plain number). */
function totalText(figure) {
  if (!figure || figure.value === null || figure.value === undefined) return EM_DASH;
  switch (figure.format) {
    case "inr":
    case "inr_unit":
      return rupees(figure.value);
    case "amount":
      return money(figure.value, null);
    case "count":
      return count(figure.value);
    default:
      return figure.text != null && figure.text !== "" ? String(figure.text) : EM_DASH;
  }
}

/** The tile's words for one total: rupees first, then each other unit by the column it was measured in. */
function totalLabel(total) {
  if (total.unit === "rupees") return "Net value proven";
  const column = total.unit_column && total.unit_column.text ? String(total.unit_column.text) : "";
  return column ? `Extra ${column} proven` : "Proven to date";
}

/**
 * Up to four tiles above the card: each total the server counts (`proven.totals`), as the one figure a person reads
 * first. The figures and the campaign counts are the server's own; the rule that they are a floor and not an estimate
 * is the server's too (it is the first sentence of the card), said here in the caption. Nothing is computed.
 */
export function programmeKpis(proven) {
  const totals = ((proven && proven.totals) || []).slice().sort((a, b) => (b.unit === "rupees") - (a.unit === "rupees")).slice(0, 4);
  return totals.length ? kpiRow(totals.map(totalTile), { attrs: "data-programme-kpis" }) : "";
}

const totalTile = (total) => {
  const n = (total.campaigns || []).length;
  return {
    label: totalLabel(total),
    value: totalText(total.total),
    caption: `A floor, not an estimate. From ${n} ${n === 1 ? "campaign" : "campaigns"}.`,
    attrs: `data-total-tile="${esc(total.unit)}"`,
  };
};

/**
 * `summary` is `GET /campaigns/summary`'s answer (a read that failed never gets here: `index.js` shows its error).
 * `campaigns` is `GET /campaigns`'s (Plan M M172): each campaign by its short name, as Results names it; null when it
 * could not be read, and each campaign then shows its recorded label, as on Results.
 */
export function programmeHtml(summary, campaigns = null) {
  const proven = summary && typeof summary === "object" && summary.proven ? summary.proven : null;
  const block = proven ? summaryCardHtml({ cards: [], proven: plainProven(proven) }, campaignNameIndex(campaigns)) : "";
  if (block) return `${programmeKpis(proven)}${block}`;
  return `<section class="card" data-programme-empty>${emptyState({
    icon: "report",
    title: "Nothing is proven yet",
    text: "The value proven to date appears here once a campaign's results are measured against customers who were held back at random.",
    action: { label: "See the Value Proof Packs", href: "#/reports/value-proof", kind: "secondary" },
  })}</section>`;
}
