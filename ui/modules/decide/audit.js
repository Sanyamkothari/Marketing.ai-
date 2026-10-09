// Audit any campaign, and the programme readout (Plan J M103, DEC-1313): the form that sends a past
// campaign's two files, the page cards that say what its numbers can claim and who was contacted.
//
// Everything here that is a number or a sentence about a result comes from the server's answer
// (`POST /campaigns/audit`, `POST /campaigns/programme`, `GET /campaigns/{id}`): the label ("Causal",
// "Random by your statement, not verified", "Descriptive only"), its explanation, the rates, the
// complier-adjusted effect and every reason a value is missing. This file only lays them out, and the form
// asks the person for the things nobody else knows: which column is which, and how the groups were chosen
// (never assumed: neither choice is ticked until they make it).
//
// The builders are pure (no document, no fetch) so they can be run by node alone; `index.js` registers the
// page and the events, and `views.js` draws the cards on a campaign's page.

import { EM_DASH, RESULTS_CRUMB, crumbs, esc, fmtDate, fmtInt, fmtPct, pageHead, present } from "../../dom.js";

export const AUDIT_ROUTE = "audit";
export const auditHref = () => "#/audit";

const NOT_SET = "";

/** The label's tone: only a verified assignment reads as a proven effect. */
const tone = (audit) => (audit && audit.causal ? "ok" : "neutral");

// --- cards on a campaign's page ---------------------------------------------------------------------------

/** What the numbers of an audited campaign can claim: the server's label, why, and what it checked. */
export function auditCardHtml(audit) {
  if (!audit) return "";
  const check = audit.randomness || {};
  const checked =
    check.status === "not_run"
      ? `<p class="dc-text" data-audit-check="not_run">${esc(check.reason || "")}</p>`
      : check.status
        ? `<p class="dc-text" data-audit-check="${esc(check.status)}">We tried to tell the contacted customers from the held-back ones using ${esc(
            fmtInt(check.columns_used || 0),
          )} details of each customer, on ${esc(fmtInt(check.rows_used || 0))} customers. Guessing score: ${esc(
            present(check.auc) ? Number(check.auc).toFixed(2) : EM_DASH,
          )} (0.50 is chance, our limit is ${esc(Number(check.threshold).toFixed(2))}).</p>`
        : "";
  const notes = (audit.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  const offers = (audit.offers || []).length
    ? `<p class="dc-text" data-audit-offers>Offers compared with the same control (${esc(audit.control_level)}): ${(audit.offers || [])
        .map(esc)
        .join(", ")}.</p>`
    : "";
  return `<section class="card dc-card" data-audit-card data-causal="${audit.causal ? "true" : "false"}">
    <h3>What these numbers can claim</h3>
    <p><span class="chip ${tone(audit)}" data-audit-label>${esc(audit.label)}</span></p>
    <p class="dc-text" data-audit-explanation>${esc(audit.explanation)}</p>
    ${checked}${offers}
    ${notes ? `<ul class="dc-text" data-audit-notes>${notes}</ul>` : ""}
  </section>`;
}

/** The programme's period, its holdout and what the comparison is (and is not). */
export function programmeCardHtml(programme) {
  if (!programme) return "";
  const share = present(programme.realised_share) ? fmtPct(programme.realised_share) : EM_DASH;
  const notes = (programme.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  return `<section class="card dc-card" data-programme-card>
    <h3>The whole programme</h3>
    <p><span class="chip ok" data-programme-label>${esc(programme.label)}</span></p>
    <p class="dc-text" data-programme-explanation>${esc(programme.explanation)}</p>
    <div class="dc-kvs">
      <div class="dc-kv"><span class="dc-k">Period</span><span class="dc-v">${esc(fmtDate(programme.period_start))} to ${esc(fmtDate(programme.period_end))}</span></div>
      <div class="dc-kv"><span class="dc-k">Customers</span><span class="dc-v">${esc(fmtInt(programme.customers))}</span></div>
      <div class="dc-kv"><span class="dc-k">Held back by the universal control group</span><span class="dc-v">${esc(fmtInt(programme.holdout_members))} (${esc(share)})</span></div>
      <div class="dc-kv"><span class="dc-k">Everyone else</span><span class="dc-v">${esc(fmtInt(programme.other_customers))}</span></div>
    </div>
    ${notes ? `<ul class="dc-text" data-programme-notes>${notes}</ul>` : ""}
  </section>`;
}

const rate = (value) => (present(value) ? fmtPct(value) : EM_DASH);

function complierHtml(readout) {
  const c = readout.complier;
  if (!c) {
    return `<p class="dc-text" data-complier-reason>${esc(readout.complier_reason || "")}</p>`;
  }
  const e = c.effect;
  const pct = c.unit === "rate";
  const show = (v) => (pct ? `${(Number(v) * 100).toFixed(1)} points` : Number(v).toFixed(2));
  return `<div class="dc-note" data-complier>
    <p><b>Secondary: the effect on the customers who were contacted</b></p>
    <p class="dc-headline" data-complier-value>${esc(show(e.value))} <span class="dc-text">(95% range ${esc(show(e.ci_low))} to ${esc(show(e.ci_high))})</span></p>
    <p class="dc-text" data-complier-label>${esc(c.label)}</p>
  </div>`;
}

/** Who was actually contacted: the contact rate, the contamination and the secondary effect, or why not. */
export function contactsCardHtml(readout) {
  if (!readout) return "";
  const row = (label, value, attr) =>
    `<div class="dc-kv"><span class="dc-k">${esc(label)}</span><span class="dc-v"${attr ? ` ${attr}` : ""}>${value}</span></div>`;
  const of = (k, n) => `${esc(fmtInt(k))} of ${esc(fmtInt(n))}`;
  const notes = (readout.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  return `<section class="card dc-card" data-contacts-card>
    <h3>Who was actually contacted</h3>
    <div class="dc-kvs">
      ${row("Contacted, of the customers meant to be", readout.contact_rate === null ? esc(readout.contact_rate_reason || EM_DASH) : `${esc(rate(readout.contact_rate))} (${of(readout.treated_contacted, readout.treated_listed)})`, "data-contact-rate")}
      ${row("Contacted anyway, of the customers held back", readout.contamination === null ? esc(readout.contamination_reason || EM_DASH) : `${esc(rate(readout.contamination))} (${of(readout.holdout_contacted, readout.holdout_listed)})`, "data-contamination")}
    </div>
    ${notes ? `<ul class="dc-text" data-contact-notes>${notes}</ul>` : ""}
    ${complierHtml(readout)}
  </section>`;
}

// --- the form --------------------------------------------------------------------------------------------

const options = (columns, { blank = null, selected = NOT_SET } = {}) =>
  [
    blank === null ? "" : `<option value=""${selected === NOT_SET ? " selected" : ""}>${esc(blank)}</option>`,
    ...(columns || []).map((c) => `<option value="${esc(c)}"${c === selected ? " selected" : ""}>${esc(c)}</option>`),
  ].join("");

const select = (name, label, columns, opts = {}) =>
  `<label>${esc(label)}<select name="${esc(name)}"${opts.required ? " required" : ""}>${options(columns, opts)}</select></label>`;

const field = (name, label, attrs = "") => `<label>${esc(label)}<input name="${esc(name)}" ${attrs}></label>`;

function filePicker(kind, label, hint, file) {
  const got = file ? `<span class="dc-text" data-file-name="${esc(kind)}">${esc(file.fileName)} · ${esc(fmtInt(file.rows))} rows</span>` : "";
  return `<label>${esc(label)}<input type="file" accept=".csv,.parquet" data-audit-file="${esc(kind)}"></label>${got}<p class="dc-text">${esc(hint)}</p>`;
}

const problem = (error) =>
  error ? `<p class="dc-error" role="alert" data-audit-error>${esc(error.message || String(error))}</p>` : "";

function auditFormHtml(state) {
  const a = state.files.assignment;
  const o = state.files.outcomes;
  const c = state.files.contact;
  const ready = Boolean(a && o);
  const assignmentFields = a
    ? `${select("assignment_key", "Customer id column", a.columns, { required: true })}
       ${select("arm_column", "Column saying contacted or held back", a.columns, { required: true })}
       ${field("control_value", "Value meaning held back (if it is not 0, no or control)", 'placeholder="for example none"')}
       ${field("treated_values", "Values meaning contacted, if there are several offers (separate with commas)", 'placeholder="for example gold, silver"')}
       ${select("sent_date_column", "Column with the date each customer was contacted", a.columns, { blank: "None: one date for everyone" })}
       ${select("intended_column", "Column saying the customer was meant to be contacted", a.columns, { blank: "None: compare everyone" })}`
    : "";
  const outcomeFields = o
    ? `${select("outcomes_key", "Customer id column", o.columns, { required: true })}
       ${select("outcome_column", "Outcome column", o.columns, { required: true })}
       <label>The outcome is<select name="outcome_kind"><option value="binary" selected>Yes or no</option><option value="continuous">An amount, such as revenue</option></select></label>
       ${field("positive_label", "Value meaning yes (if it is not 1, yes or true)", 'placeholder="for example converted"')}
       ${select("outcome_date_column", "Column with the date each customer was contacted", o.columns, { blank: "None" })}
       ${field("treatment_start", "When the campaign went out", 'type="date"')}
       ${field("outcome_window_days", "Days the outcome is counted over", 'type="number" min="0" step="1"')}
       <label class="dc-check"><input type="checkbox" name="outcome_is_good" checked> More of the outcome is better (untick for churn or defaults)</label>`
    : "";
  const contactFields = c
    ? `${select("contact_key", "Customer id column", c.columns, { required: true })}
       ${select("contacted_column", "Column saying whether the customer was contacted", c.columns, { required: true })}
       <label>Customers the file does not list<select name="unlisted_customers"><option value="unknown" selected>Unknown: leave them out</option><option value="not_contacted">Not contacted (the file lists only the customers it sent to)</option></select></label>`
    : "";
  const basis = `<fieldset class="dc-basis" data-audit-basis><legend>How were the groups chosen?</legend>
      <label class="dc-check"><input type="radio" name="assignment_basis" value="random" required> At random</label>
      <label class="dc-check"><input type="radio" name="assignment_basis" value="not_random"> Not at random: by a score, a rule or a person's choice</label>
      <p class="dc-text">Never assumed. If you say at random and the file has customer details, we test it; the answer decides whether the numbers can be called causal.</p>
    </fieldset>`;
  return `<section class="card dc-card" data-audit-form-card>
    <h3>Audit a campaign another tool ran</h3>
    <p class="dc-text">Upload who was in which group and what happened. We measure it the same way as our own campaigns and say what the numbers can claim.</p>
    <form data-audit-form>
      <div class="dc-form">
        ${filePicker("assignment", "Who was in which group", "One row per customer: their id, whether they were contacted or held back, and any details about them.", a)}
        ${assignmentFields}
        ${filePicker("outcomes", "What happened", "One row per customer: their id and the outcome.", o)}
        ${outcomeFields}
        ${filePicker("contact", "Who was actually contacted (optional)", "One row per customer: their id and whether they were reached.", c)}
        ${contactFields}
        ${field("name", "A name for this campaign (optional)", 'maxlength="120"')}
      </div>
      ${basis}
      ${problem(state.error)}
      <div class="dc-actions"><button type="submit" class="btn secondary" data-audit-submit${ready && !state.busy ? "" : " disabled"}>${state.busy ? "Measuring…" : "Audit this campaign"}</button></div>
    </form>
  </section>`;
}

function programmeFormHtml(state) {
  const o = state.files.programme;
  const fields = o
    ? `${select("programme_key", "Customer id column", o.columns, { required: true })}
       ${select("programme_outcome", "Outcome column", o.columns, { required: true })}
       <label>The outcome is<select name="programme_kind"><option value="binary" selected>Yes or no</option><option value="continuous">An amount, such as revenue</option></select></label>
       ${field("programme_positive", "Value meaning yes (if it is not 1, yes or true)")}
       ${field("programme_name", "A name (optional)", 'maxlength="120"')}`
    : "";
  return `<section class="card dc-card" data-programme-form-card>
    <h3>The whole programme</h3>
    <p class="dc-text">Everything done over a period, against the customers the universal control group kept out of every campaign. Upload the outcome of every customer, not only those on a list.</p>
    <form data-programme-form>
      <div class="dc-form">
        ${field("period_start", "First day of the period", 'type="date" required')}
        ${field("period_end", "Last day of the period", 'type="date" required')}
        ${filePicker("programme", "What happened to every customer", "One row per customer: their id and the outcome over the period.", o)}
        ${fields}
      </div>
      ${problem(state.programmeError)}
      <div class="dc-actions"><button type="submit" class="btn secondary" data-programme-submit${o && !state.programmeBusy ? "" : " disabled"}>${state.programmeBusy ? "Measuring…" : "Read the programme"}</button></div>
    </form>
  </section>`;
}

/** The page at `#/audit`. `can(method, path)` says what this person may do; `state` is the controller's. */
export function auditPageHtml({ can = () => true, state }) {
  const head = pageHead(
    `${crumbs([RESULTS_CRUMB, { label: "Audit a campaign" }])}<h1 class="h1">Audit a campaign</h1><p class="desc">Measure a campaign another tool ran, or the whole programme, against a group that was kept out of it.</p>`,
  );
  if (!can("POST", "/campaigns/audit")) {
    return `<main class="screen dc" data-module="decide" data-audit-page>${head}<section class="card dc-card" data-audit-refused><p class="dc-text">Auditing a campaign needs the Analyst role. Ask an administrator.</p></section></main>`;
  }
  return `<main class="screen dc" data-module="decide" data-audit-page>${head}<div class="dc-stack">${auditFormHtml(state)}${programmeFormHtml(state)}</div></main>`;
}

// --- the requests ---------------------------------------------------------------------------------------

const text = (v) => (v === undefined || v === null || String(v).trim() === "" ? null : String(v).trim());
const list = (v) => (text(v) === null ? null : String(v).split(",").map((s) => s.trim()).filter(Boolean));
const number = (v) => (text(v) === null ? null : Number(v));
const day = (v) => (text(v) === null ? null : `${text(v)}T00:00:00Z`);
const keysIfDifferent = (primary, other) => (text(other) !== null && other !== primary ? [other] : null);

/**
 * The body of `POST /campaigns/audit` from the form's values and the uploaded files. The customer id is
 * named as the assignment file names it; the other files say theirs only when it differs.
 */
export function auditBody(values, files) {
  const primary = text(values.assignment_key);
  const body = {
    name: text(values.name),
    primary_key: primary,
    assignment: {
      upload_id: files.assignment.uploadId,
      key_columns: null,
      arm_column: text(values.arm_column),
      control_value: text(values.control_value),
      treated_values: list(values.treated_values),
      sent_date_column: text(values.sent_date_column),
      intended_column: text(values.intended_column),
    },
    outcomes: {
      upload_id: files.outcomes.uploadId,
      key_columns: keysIfDifferent(primary, values.outcomes_key),
      outcome_column: text(values.outcome_column),
      positive_label: text(values.positive_label),
      outcome_kind: text(values.outcome_kind) || "binary",
      treatment_date_column: text(values.outcome_date_column),
    },
    assignment_basis: text(values.assignment_basis),
    treatment_start: day(values.treatment_start),
    outcome_window_days: number(values.outcome_window_days),
    outcome_is_good: values.outcome_is_good === "on" || values.outcome_is_good === true,
  };
  if (files.contact) {
    body.contact = {
      upload_id: files.contact.uploadId,
      key_columns: keysIfDifferent(primary, values.contact_key),
      contacted_column: text(values.contacted_column),
      unlisted_customers: text(values.unlisted_customers) || "unknown",
    };
  }
  return body;
}

/** The body of `POST /campaigns/programme`. */
export function programmeBody(values, files) {
  const primary = text(values.programme_key);
  return {
    name: text(values.programme_name),
    primary_key: primary,
    period: { start: text(values.period_start), end: text(values.period_end) },
    outcome: {
      upload_id: files.programme.uploadId,
      outcome_column: text(values.programme_outcome),
      positive_label: text(values.programme_positive),
      outcome_kind: text(values.programme_kind) || "binary",
    },
  };
}

/** What a file upload's answer gives the form: the id to send, and the columns to choose from. */
export function fileOf(uploadResponse) {
  const profile = uploadResponse.profile || {};
  return {
    uploadId: uploadResponse.upload_id,
    fileName: profile.file_name || "",
    rows: profile.row_count || 0,
    columns: (profile.columns || []).map((c) => c.name),
  };
}
