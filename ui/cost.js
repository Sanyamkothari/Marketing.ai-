// Cost before each run, and what runs have cost (Plan J M108, DEC-1318).
//
// Pure builders: what they draw comes from their arguments, which are the API's own answers
// (`GET /use-cases/{id}/cost-estimate`, `GET /cost/spend`, `GET /cost/fx-rate`). Nothing here works a
// figure out: a dollar amount, a rupee amount, a reason and a confirmation sentence are all the
// server's, and a figure the server did not send is not drawn - the reason it gave is.

import { esc } from "./dom.js";

/** `ApiError.code` of the 409 `POST /runs` answers for a run above the cost cap. */
export const COST_NEEDS_CONFIRMATION = "RUN_COST_NEEDS_CONFIRMATION";

const present = (value) => value !== null && value !== undefined;

/** US dollars, two decimals, built by `Intl` so no currency sign is typed into any source file. */
export function usd(amount) {
  const n = Number(amount);
  if (n > 0 && n < 0.01) return `under ${usd(0.01)}`;
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);
}

/** Rupees, two decimals, in the Indian digit grouping. */
export function inr(amount) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(Number(amount));
}

/** "INR 1,234.00 at 84.5 rupees to the dollar (Finance sheet, 1 October 2026)". */
function inrSentence(figure) {
  if (!figure) return "";
  return `${inr(figure.amount)} at ${figure.inr_per_usd} rupees to the dollar (${esc(figure.source)}, ${esc(figure.as_of)})`;
}

function lineRow(line) {
  const money = present(line.usd) ? usd(line.usd) : "not known";
  const note = present(line.usd) ? line.detail : line.reason;
  const counted = line.in_total ? "" : ` <span class="muted">(not counted here)</span>`;
  return `<li><span>${esc(line.label)}</span>: <b>${esc(money)}</b>${counted}${
    note ? `<br><span class="muted">${esc(note)}</span>` : ""
  }</li>`;
}

/**
 * The line beside the Run button. Nothing for a deployment that runs on its own machine (nothing is
 * billed, so there is no number and no reason worth a line on every Setup page); a number with its
 * basis when the cloud bills the run; the server's reason when it cannot say.
 */
export function costLineHtml(estimate) {
  if (!estimate || estimate.backend === "local") return "";
  const cap = present(estimate.cap_usd)
    ? ` The limit set for one run is ${usd(estimate.cap_usd)}${estimate.needs_confirmation ? ", so you will be asked to confirm" : ""}.`
    : "";
  const lead = present(estimate.estimated_usd)
    ? `This run could cost up to <b>${esc(usd(estimate.estimated_usd))}</b>${
        estimate.inr ? ` (${inrSentence(estimate.inr)})` : ""
      }.`
    : `${esc(estimate.reason || "The cost of this run cannot be worked out.")}`;
  const rows = (estimate.lines || []).map(lineRow).join("");
  const how = rows
    ? `<details class="cost-how"><summary>How this is worked out</summary><p class="muted">${esc(estimate.basis)}</p><ul>${rows}</ul>${
        estimate.inr_reason ? `<p class="muted">${esc(estimate.inr_reason)}</p>` : ""
      }</details>`
    : "";
  return `<div class="cost-line" data-cost-estimate role="note"><p>${lead}${esc(cap)}</p>${how}</div>`;
}

/** The warning a capped run answers with, in the server's words, and the two ways out of it. */
export function costConfirmHtml(message) {
  if (!message) return "";
  return `<div class="cost-confirm" data-cost-confirm role="alert"><p>${esc(message)}</p><div class="btn-row"><button type="button" class="btn primary" id="f-cost-confirm">Start it anyway</button><button type="button" class="btn quiet" id="f-cost-decline">Do not start</button></div></div>`;
}

function monthRow(row) {
  const amount = present(row.estimated_usd) ? usd(row.estimated_usd) : "no figure";
  const rupees = row.inr ? ` (${inr(row.inr.amount)})` : "";
  const runs = row.runs
    ? `${row.runs} run${row.runs === 1 ? "" : "s"}${row.unpriced_runs ? `, ${row.unpriced_runs} with no figure` : ""}`
    : "no runs";
  return `<tr><td>${esc(row.month)}</td><td>${esc(runs)}</td><td>${esc(amount)}${esc(rupees)}</td></tr>`;
}

function fxHtml(fx, canEdit) {
  const saved = fx
    ? `<p data-fx-saved>Rupee amounts use <b>${esc(fx.inr_per_usd)}</b> rupees to the dollar, from ${esc(fx.source)} (${esc(fx.as_of)}).</p>`
    : `<p data-fx-none class="muted">No exchange rate is set, so amounts are shown in US dollars only.</p>`;
  if (!canEdit) return saved;
  return `${saved}<form data-fx-form class="cost-fx"><label>Rupees to the dollar <input name="inr_per_usd" type="number" step="any" min="0" required></label>
    <label>Where you read it <input name="source" type="text" maxlength="200" required></label>
    <label>The day you read it <input name="as_of" type="date" required></label>
    <button type="submit" class="btn primary sm">Save the rate</button>${
      fx ? `<button type="button" class="btn quiet sm" data-fx-clear>Remove it</button>` : ""
    }</form>`;
}

/** The Cost page: what finished runs cost each month, and the exchange rate rupee amounts use. */
export function costPageHtml({ spend, fx, canEdit = false } = {}) {
  const rows = ((spend && spend.months) || []).map(monthRow).join("");
  return `<main class="screen cost" data-module="cost"><h1 class="h1">Cost</h1>
    <section class="card"><h3>What runs have cost</h3>
      <p class="muted">${esc((spend && spend.basis) || "")}</p>
      <table class="tbl"><thead><tr><th>Month</th><th>Runs</th><th>Estimated cost</th></tr></thead><tbody>${rows}</tbody></table></section>
    <section class="card"><h3>Exchange rate</h3>${fxHtml(fx, canEdit)}</section></main>`;
}
