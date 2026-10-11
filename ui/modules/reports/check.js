// "Check a past campaign": today's audit form (`#/audit`, ui/modules/decide/audit.js, Plan J M103) regrouped into three
// short steps (Plan M, M178):
//
//   1  Who was in which group      the assignment file, and how the groups were chosen
//   2  What happened               the outcomes file
//   3  Who was actually contacted  the contact file (optional)
//
// It is the SAME request: the fields keep their names, and the body is built by `auditBody` / `programmeBody` from
// decide/audit.js, so the server's answer for a given pair of files is the answer the old form got. Only the
// grouping and some of the wording changed. The form is also the home of the whole-programme readout, which lived on
// the same old page. Pure builders (no document, no fetch); `index.js` binds the events.
//
// The attributes differ from the old page's (`data-rp-*`, not `data-audit-*`), on purpose: decide/index.js listens on
// the whole document for those, and one form must not be handled by both.

import { esc, fmtInt } from "../../dom.js";

const NOT_SET = "";

const options = (columns, { blank = null, selected = NOT_SET } = {}) =>
  [
    blank === null ? "" : `<option value=""${selected === NOT_SET ? " selected" : ""}>${esc(blank)}</option>`,
    ...(columns || []).map((c) => `<option value="${esc(c)}"${c === selected ? " selected" : ""}>${esc(c)}</option>`),
  ].join("");

const select = (name, label, columns, opts = {}) =>
  `<label>${esc(label)}<select name="${esc(name)}"${opts.required ? " required" : ""}>${options(columns, opts)}</select></label>`;

const field = (name, label, attrs = "") => `<label>${esc(label)}<input name="${esc(name)}" ${attrs}></label>`;

function filePicker(kind, label, hint, file) {
  const got = file ? `<span class="rp-note" data-file-name="${esc(kind)}">${esc(file.fileName)} · ${esc(fmtInt(file.rows))} rows</span>` : "";
  return `<div class="rp-file"><label>${esc(label)}<input type="file" accept=".csv,.xlsx,.xlsm,.parquet" data-rp-file="${esc(kind)}"></label>${got}<p class="rp-note">${esc(hint)}</p></div>`;
}

const problem = (error) => (error ? `<p class="rp-error" role="alert" data-rp-error>${esc(error.message || String(error))}</p>` : "");

function step(number, title, sentence, inner) {
  return `<li class="rp-step" data-step="${number}"><h3 class="rp-step-h"><span class="rp-step-n" aria-hidden="true">${number}</span>${esc(title)}</h3><p class="rp-note">${esc(
    sentence,
  )}</p><div class="rp-form">${inner}</div></li>`;
}

function stepOne(state) {
  const a = state.files.assignment;
  const columns = a
    ? `${select("assignment_key", "Customer id column", a.columns, { required: true })}
       ${select("arm_column", "Column saying contacted or held back", a.columns, { required: true })}
       ${field("control_value", "Value meaning held back (if it is not 0, no or control)", 'placeholder="for example none"')}
       ${field("treated_values", "Values meaning contacted, if there are several offers (separate with commas)", 'placeholder="for example gold, silver"')}
       ${select("sent_date_column", "Column with the date each customer was contacted", a.columns, { blank: "None: one date for everyone" })}
       ${select("intended_column", "Column saying the customer was meant to be contacted", a.columns, { blank: "None: compare everyone" })}`
    : "";
  const basis = `<fieldset class="rp-basis" data-rp-basis><legend>How were the groups chosen?</legend>
      <label class="rp-check"><input type="radio" name="assignment_basis" value="random" required> At random</label>
      <label class="rp-check"><input type="radio" name="assignment_basis" value="not_random"> Not at random: by a ranking, a rule or a person's choice</label>
      <p class="rp-note">Never assumed. If you say at random and the file has customer details, we test it; the answer decides whether the result can be called proven.</p>
    </fieldset>`;
  return step(
    1,
    "Who was in which group",
    "Upload one row per customer: their id, whether they were contacted or held back, and any details about them.",
    `${field("name", "A name for this campaign (optional)", 'maxlength="120"')}
     ${filePicker("assignment", "File with the two groups", "Contacted and held back, one row per customer.", a)}
     ${columns}
     <div class="rp-wide">${basis}</div>`,
  );
}

function stepTwo(state) {
  const o = state.files.outcomes;
  const columns = o
    ? `${select("outcomes_key", "Customer id column", o.columns, { required: true })}
       ${select("outcome_column", "Outcome column", o.columns, { required: true })}
       <label>The outcome is<select name="outcome_kind"><option value="binary" selected>Yes or no</option><option value="continuous">An amount, such as revenue</option></select></label>
       ${field("positive_label", "Value meaning yes (if it is not 1, yes or true)", 'placeholder="for example converted"')}
       ${select("outcome_date_column", "Column with the date each customer was contacted", o.columns, { blank: "None" })}
       ${field("treatment_start", "When the campaign went out", 'type="date"')}
       ${field("outcome_window_days", "Days the outcome is counted over", 'type="number" min="0" step="1"')}
       <label class="rp-check"><input type="checkbox" name="outcome_is_good" checked> More of the outcome is better (untick for churn or defaults)</label>`
    : "";
  return step(
    2,
    "What happened",
    "Upload one row per customer: their id and the outcome.",
    `${filePicker("outcomes", "File with the outcomes", "For example whether they bought, or what they spent.", o)}${columns}`,
  );
}

function stepThree(state) {
  const c = state.files.contact;
  const columns = c
    ? `${select("contact_key", "Customer id column", c.columns, { required: true })}
       ${select("contacted_column", "Column saying whether the customer was contacted", c.columns, { required: true })}
       <label>Customers the file does not list<select name="unlisted_customers"><option value="unknown" selected>Unknown: leave them out</option><option value="not_contacted">Not contacted (the file lists only the customers it sent to)</option></select></label>`
    : "";
  return step(
    3,
    "Who was actually contacted (optional)",
    "Skip this if you do not have it. With it we also show how many of the customers meant to be contacted were reached.",
    `${filePicker("contact", "File with who was reached", "One row per customer: their id and whether they were reached.", c)}${columns}`,
  );
}

function programmeHtml(state) {
  const o = state.files.programme;
  const fields = o
    ? `${select("programme_key", "Customer id column", o.columns, { required: true })}
       ${select("programme_outcome", "Outcome column", o.columns, { required: true })}
       <label>The outcome is<select name="programme_kind"><option value="binary" selected>Yes or no</option><option value="continuous">An amount, such as revenue</option></select></label>
       ${field("programme_positive", "Value meaning yes (if it is not 1, yes or true)")}
       ${field("programme_name", "A name (optional)", 'maxlength="120"')}`
    : "";
  return `<details class="adv rp-programme" data-rp-programme-card><summary>Check the whole programme instead</summary>
    <p class="rp-note">Everything done over a period, against the customers who were kept out of every campaign. Upload the outcome of every customer, not only those on a list.</p>
    <form data-rp-programme-form>
      <div class="rp-form">
        ${field("period_start", "First day of the period", 'type="date" required')}
        ${field("period_end", "Last day of the period", 'type="date" required')}
        ${filePicker("programme", "What happened to every customer", "One row per customer: their id and the outcome over the period.", o)}
        ${fields}
      </div>
      ${problem(state.programmeError)}
      <div class="rp-actions"><button type="submit" class="btn secondary" data-rp-programme-submit${o && !state.programmeBusy ? "" : " disabled"}>${
        state.programmeBusy ? "Measuring…" : "Read the programme"
      }</button></div>
    </form></details>`;
}

/** The "Check a past campaign" tab. `can(method, path)` says what this person may do; `state` is the controller's. */
export function checkHtml({ can = () => true, state }) {
  if (!can("POST", "/campaigns/audit")) {
    return `<section class="card" data-rp-refused><p class="rp-note">Checking a past campaign needs the Analyst role. Ask an administrator.</p></section>`;
  }
  const ready = Boolean(state.files.assignment && state.files.outcomes);
  return `<section class="card rp-card" data-rp-check-card>
    <h3>Check a campaign another tool ran</h3>
    <p class="rp-note">Tell us who was in which group and what happened. We measure it the same way as our own campaigns and say what the numbers can claim.</p>
    <form data-rp-check-form>
      <ol class="rp-steps">${stepOne(state)}${stepTwo(state)}${stepThree(state)}</ol>
      ${problem(state.error)}
      <div class="rp-actions"><button type="submit" class="btn primary" data-rp-submit${ready && !state.busy ? "" : " disabled"}>${
        state.busy ? "Measuring…" : "Check this campaign"
      }</button></div>
    </form>
    ${programmeHtml(state)}
  </section>`;
}
