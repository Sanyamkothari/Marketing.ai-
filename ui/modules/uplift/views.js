// Every uplift screen as a pure function: state and artefacts in, an HTML string out.
//
// Nothing in this file calls the API, touches `window` or keeps state, so each screen can be
// rendered from a fixture in node (tests/unit/uplift/uplift_ui.test.mjs) exactly as it renders in
// the browser. The controllers in `controller.js` own the fetching; this file owns what a response
// looks like on screen.
//
// Three rules run through every function here:
//  * a number is shown only when an artefact carries it - anything else is `EM_DASH` or a sentence
//    saying what is missing, never a sample figure (plan §13.3);
//  * any artefact with `causal: false` puts the not-causal banner at the top of the screen, because
//    a number from a non-random assignment describes who was contacted, not what contact changed;
//  * every results screen leads with a plain verdict and one primary action; metric names (AUUC,
//    Qini, ATE, p-value), ids and codes sit behind "Technical details" (docs/UI_AUDIT.md §3).
//
// A few sentences are pinned word for word by the design prototype
// (tests/prototype/consistency.test.mjs, "the uplift entry point and its words"). They change
// together with `marketing-ai-prototype.html` (work package WP9), never here alone.

import {
  EM_DASH,
  crumbs,
  dash,
  dataTable,
  emptyState,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtNum,
  fmtSize,
  fmtStamp,
  glossaryCode,
  glossaryMetric,
  glossaryTerm,
  headActions,
  kvs,
  noticeCard,
  pageHead,
  present,
  sortNote,
  techDetails,
  toggletip,
} from "../../dom.js";
import { decileChart, qiniChart, segmentChart } from "./charts.js";
import {
  fmtCi,
  fmtCount,
  fmtDay,
  fmtInterval,
  fmtLikely,
  fmtP,
  fmtPts,
  fmtRate,
  fmtVal,
  signed,
} from "./format.js";

/** The one-line explanation plan B §8 gives the problem type, shown wherever it is chosen. */
export const UPLIFT_EXPLANATION = "predicts who changes behaviour because of your action";

/** `engine.uplift.contracts.NOT_CAUSAL_NOTE`, the label every output of a non-random run carries. */
export const NOT_CAUSAL_NOTE =
  "Not causal: the treatment was not randomly assigned, so these numbers describe who was " +
  "contacted, not what contacting them changed.";

/** The acknowledgement token of the one uplift check a user may accept (plan B §4). */
export const NOT_RANDOM = "TREATMENT_NOT_RANDOM";

/** What each screen reads from `GET /runs/{id}/uplift/{name}`. */
export const MODEL_ARTEFACTS = [
  "uplift_validation.json",
  "uplift_evaluation.json",
  "qini_curve.json",
  "segments.json",
  "policy_recommendation.json",
  "ope_report.json",
];
export const OUTPUT_ARTEFACTS = ["uplift_validation.json", "segments.json", "policy_recommendation.json"];

/** The share of customers a "What if…" estimate starts from, and the one the verdicts quote. */
export const DEFAULT_TOP_SHARE_PCT = 10;

const LEARNER_LABEL = { s_learner: "S-learner", t_learner: "T-learner", x_learner: "X-learner" };
const BASE_MODEL_LABEL = { lightgbm: "LightGBM", autogluon_fast: "AutoGluon (fast preset)" };

const STOP_REASON = {
  all_persuadables: "Every eligible persuadable fits within the budget.",
  budget: "The contact budget is reached.",
  value_below_cost: "The next customer's expected value is below the cost of contacting them.",
  no_persuadables: "No customer is predicted to be persuadable.",
};

/** What each of the four segments means, in words (the formulas are under Details). */
const SEGMENT_MEANING = {
  persuadable: "Contact changes what they do: the people to contact.",
  sure_thing: "Would respond anyway, so contact is wasted.",
  lost_cause: "Would not respond either way.",
  sleeping_dog: "Contact makes them less likely to respond: leave them alone.",
};

const STATE_LABEL = {
  pending: "Queued",
  running: "Running…",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

const humanise = (value) => {
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

const labelOf = (map, value) => (present(value) ? map[value] || humanise(value) : EM_DASH);
const plural = (n, one, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;
const tipOf = (text, label) => (text ? toggletip(text, label) : "");

// --- routes ------------------------------------------------------------------------------------

export const routes = {
  index: () => "#/uplift",
  setup: (ucId) => `#/uplift/${encodeURIComponent(ucId)}`,
  score: (ucId) => `#/uplift/${encodeURIComponent(ucId)}/score`,
  run: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/run/${encodeURIComponent(runId)}`,
  model: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/model/${encodeURIComponent(runId)}`,
  output: (ucId, runId) => `#/uplift/${encodeURIComponent(ucId)}/output/${encodeURIComponent(runId)}`,
  campaign: (ucId, runId) => `#/campaign/${encodeURIComponent(ucId)}/${encodeURIComponent(runId)}`,
  data: (ucId, runId) => `#/uc/${encodeURIComponent(ucId)}/data/${encodeURIComponent(runId)}`,
  useCase: (ucId) => `#/uc/${encodeURIComponent(ucId)}`,
  phase1Output: (ucId, runId) => `#/uc/${encodeURIComponent(ucId)}/output/${encodeURIComponent(runId)}`,
  value: (runId) => `#/pilot/value/${encodeURIComponent(runId)}`,
  campaigns: () => "#/monitoring/runs",
};

// --- shared pieces -----------------------------------------------------------------------------

/**
 * The not-causal banner, when any of the given artefacts says `causal: false`. `null` artefacts
 * are skipped: a report the run did not produce says nothing either way.
 */
export function notCausalBanner(...artefacts) {
  const flagged = artefacts.some((a) => a && a.causal === false);
  if (!flagged) return "";
  return `<div class="unotcausal" role="note"><b>Not causal</b><span>${esc(
    NOT_CAUSAL_NOTE.replace(/^Not causal: /, ""),
  )} TREATMENT_NOT_RANDOM was acknowledged for this run.</span></div>`;
}

const upliftChip = () => `<div class="chips"><span class="chip type">Uplift</span></div>`;

/** A screen: the `data-module` marker is how `index.js` knows the page on screen is its own. */
const screenOf = (uc, head, body) =>
  `<main class="screen t-${esc((uc && uc.marker) || "P")}" data-module="uplift">${pageHead(head)}${body}</main>`;

const INDEX_CRUMB = { label: "Uplift modelling", href: routes.index() };

/** Home › Uplift modelling › <use case> › <current>. */
const upliftCrumbs = (uc, current) =>
  crumbs([INDEX_CRUMB, { label: uc.name, href: routes.setup(uc.id) }, { label: current }]);

/**
 * A result tile: a label, a value, and an optional second line (a range, a comparison). The label
 * may carry a "?" with the plain meaning of its term.
 */
function tiles(items) {
  return `<div class="kpis ukpis n${items.length}">${items
    .map(
      (t) =>
        `<div class="kpi"><div class="l">${esc(t.label)}</div><div class="v">${esc(t.value)}</div>${
          t.sub ? `<div class="s">${esc(t.sub)}</div>` : ""
        }${t.tip ? `<div class="t">${tipOf(t.tip, t.label)}</div>` : ""}</div>`,
    )
    .join("")}</div>`;
}

/** The verdict first: one sentence in 20/600, one line of context, and at most one action. */
function verdictCard(tone, title, text = "", extra = "") {
  const mark = { ok: "✓", warn: "!", bad: "✕", info: "i" }[tone] || "i";
  return `<section class="card uverdict ${esc(tone)}" role="status"><div class="uv-row"><span class="uv-mark" aria-hidden="true">${mark}</span><div><p class="uv-t">${esc(
    title,
  )}</p>${text ? `<p class="uv-x">${esc(text)}</p>` : ""}${extra}</div></div></section>`;
}

/** A closed disclosure holding plain rows (numbers, not ids): "Technical metrics", "Statistical details". */
const detailsKv = (summary, rows, extra = "") =>
  `<details class="tech udetails"><summary>${esc(summary)}</summary>${
    rows.length ? `<div class="ukv">${kvs(rows)}</div>` : ""
  }${extra}</details>`;

// --- index: every use case, one link each ------------------------------------------------------

/** Why a use case in the catalogue has no uplift screen, or `""` when it has one. */
export function upliftUnavailable(u) {
  if (!u) return "Not available";
  if (u.status === "planned") return "Coming soon";
  if (u.ai_type === "generative") return "Writes text, so uplift does not apply";
  if (u.trainable_in_phase_1 === false) return "Not available yet";
  return "";
}

/** A row's status line: "Uplift model trained 24 Sep 2026" / "No uplift model yet" / checking. */
export function indexStatusHtml(status) {
  if (status === undefined) return `<span class="s" data-ustatus>Checking…</span>`;
  if (!status) return `<span class="s" data-ustatus>No uplift model yet</span>`;
  return `<span class="s ok" data-ustatus>${esc(`Uplift model trained ${fmtDate(status.created_at)}`)}${
    status.approved ? " (approved)" : ""
  }</span>`;
}

/**
 * `#/uplift`: the module's own entry page, drawn from `GET /industries`. `statuses` maps a use-case
 * id to its newest uplift model (`{created_at, approved}`), `null` for none, or leaves it out while
 * it is still being asked for.
 */
export function upliftIndexHtml(payload, statuses = {}) {
  const industry = ((payload && payload.industries) || [])[0];
  const cards = industry ? (industry.stages || []).flatMap((stage) => (stage.use_cases || []).map((u) => [stage, u])) : [];
  const open = cards.filter(([, u]) => !upliftUnavailable(u));
  const closed = cards.filter(([, u]) => upliftUnavailable(u));
  const rows = open
    .map(
      ([stage, u]) =>
        `<a href="${esc(routes.setup(u.id))}" data-uc="${esc(u.id)}"><span class="n"><b>${esc(u.name)}</b><span class="st">${esc(
          stage.name,
        )}</span></span>${indexStatusHtml(statuses[u.id])}<span class="go" aria-hidden="true">›</span></a>`,
    )
    .concat(
      closed.map(
        ([stage, u]) =>
          `<div class="off" aria-disabled="true"><span class="n"><b>${esc(u.name)}</b><span class="st">${esc(
            stage.name,
          )}</span></span><span class="s">${esc(upliftUnavailable(u))}</span></div>`,
      ),
    );
  const head = `${crumbs([{ label: "Uplift modelling" }])}<h1 class="h1">Measure what a campaign changes (uplift)</h1><p class="desc">Uplift ${esc(
    UPLIFT_EXPLANATION,
  )}: it compares customers who were treated with a randomly held-out control group, so you contact the persuadable and leave alone the ones who would convert anyway, would never convert, or react badly.</p>`;
  const body = rows.length
    ? `<p class="hint uhint">Pick a use case to train an uplift model on a campaign with a random control group.</p><nav class="uindex" aria-label="Use cases">${rows.join(
        "",
      )}</nav>`
    : emptyState({
        title: "No use case is set up yet",
        text: "Uplift models are trained per use case. Use cases appear here once an industry template is configured.",
        action: { label: "Go to Home", href: "#/" },
      });
  return `<main class="screen" data-module="uplift">${pageHead(head)}${body}</main>`;
}

// --- Setup -------------------------------------------------------------------------------------

const profileOf = (s) => (s.upload ? s.upload.profile : null);

/** Model versions an uplift run registered: the only metric they are ranked on is AUUC. */
export const upliftVersions = (models) => (models || []).filter((m) => m.version && m.version.metric === "auuc");

/** How a trained uplift model reads in a list: "Uplift model trained 24 Sep 2026 (approved)". */
export const modelOptionLabel = (v) =>
  `Uplift model trained ${fmtDate(v.version.created_at)}${v.is_champion ? " (approved)" : ""} · ${
    v.version.model_display_name
  }`;

/** Why the Run button is disabled, or `""` when it is not. */
export function setupBlocker(s) {
  if (!s.upload) return "Upload a dataset to continue";
  if (!s.pk) return "Choose the customer ID column";
  if (s.mode === "train") {
    if (!s.target) return "Choose the result column";
    if (!s.treatment) return "Choose the column that says who was contacted";
    if (s.treatment === s.target) return "The contacted column and the result column must be different columns";
  } else if (!s.modelVersionId) {
    return "Train an uplift model first";
  }
  return "";
}

const option = (value, label, current) =>
  `<option value="${esc(value)}"${String(value) === String(current) ? " selected" : ""}>${esc(label)}</option>`;

function treatmentOptions(s, names) {
  const candidates = (s.candidates && s.candidates.candidates) || [];
  const listed = new Set(candidates.map((c) => c.column));
  const rest = names.filter((n) => !listed.has(n) && n !== s.pk && n !== s.target);
  const first = candidates
    .map((c) =>
      option(
        c.column,
        `${c.column} · ${present(c.treated_share) ? `${fmtNum(c.treated_share * 100, 1)}% treated` : EM_DASH}${
          c.hinted ? " · named like a treatment" : ""
        }`,
        s.treatment,
      ),
    )
    .join("");
  return `<option value="">Select a column…</option>${
    first ? `<optgroup label="0/1 columns">${first}</optgroup>` : ""
  }${rest.length ? `<optgroup label="Other columns">${rest.map((n) => option(n, n, s.treatment)).join("")}</optgroup>` : ""}`;
}

/** The acknowledgement token of a check: the one it names, else (for an uplift check) its code. */
const upliftTokenOf = (c) => (c.details && c.details.acknowledge) || c.code;
const phase1TokenOf = (c) => (c.details && c.details.acknowledge) || null;

/** Every check of both 409 reports, one row per code; each code's repeats are grouped. */
function checkGroups(s) {
  const uplift = (s.upliftValidation && s.upliftValidation.checks) || [];
  const phase1 = (s.validation && s.validation.checks) || [];
  const groups = new Map();
  for (const [check, token] of [
    ...uplift.map((c) => [c, upliftTokenOf(c)]),
    ...phase1.map((c) => [c, phase1TokenOf(c)]),
  ]) {
    const key = check.code || check.message;
    if (!groups.has(key)) groups.set(key, { code: check.code, checks: [], token: null });
    const g = groups.get(key);
    g.checks.push(check);
    if (check.acknowledgeable && token) g.token = token;
  }
  return [...groups.values()].map((g) => {
    const error = g.checks.some((c) => c.severity === "error");
    const warning = !error && g.checks.some((c) => c.severity === "warning");
    const accepted = g.checks.every((c) => c.severity !== "error" || c.acknowledged) || (g.token && s.acknowledged.includes(g.token));
    return { ...g, error, warning, blocking: error && !accepted, accepted: error && accepted };
  });
}

/** The randomness measure: merged into the refusal when it fails, its own green row when it passes. */
function randomnessLine(s, failed) {
  const v = s.upliftValidation;
  if (!v || !present(v.randomness_auc)) return "";
  const check = ((v.checks || []).find((c) => c.code === NOT_RANDOM) || {}).details || {};
  const limit = present(check.threshold) ? `; above ${fmtNum(check.threshold, 2)} means it was not random` : "";
  return `<div class="vrand"><span class="pill ${failed ? "bad" : "ok"}" data-code="RANDOMNESS">${esc(
    failed ? "Not random" : "Looks random",
  )}</span><span>${esc(`Randomness measured ${fmtNum(v.randomness_auc, 3)} (0.5 = random${limit}).`)}</span></div>`;
}

function checkItem(s, g) {
  const first = g.checks[0];
  const entry = g.code ? glossaryCode(g.code) : null;
  const title = (entry && entry.title) || first.message;
  const messages = [...new Set(g.checks.map((c) => c.message).filter(Boolean))].filter((m) => m !== title);
  const fix = (entry && entry.fix) || first.suggestion;
  const pill = g.blocking
    ? `<span class="pill bad" data-code="${esc(g.code || "")}">Must fix</span>`
    : g.accepted
      ? `<span class="pill warn" data-code="${esc(g.code || "")}">Accepted</span>`
      : g.warning
        ? `<span class="pill warn" data-code="${esc(g.code || "")}">Warning</span>`
        : `<span class="pill ok" data-code="${esc(g.code || "")}">OK</span>`;
  const repeat = g.checks.length > 1 ? ` (${g.checks.length} times)` : "";
  const random = g.code === NOT_RANDOM ? randomnessLine(s, true) : "";
  return `<div class="vitem" data-code="${esc(g.code || "")}">${pill}<div>
      <div class="vmsg">${esc(title)}${esc(repeat)}</div>
      ${messages.map((m) => `<div class="vsug">${esc(m)}</div>`).join("")}
      ${fix ? `<div class="vsug"><b>What to do:</b> ${esc(fix)}</div>` : ""}${random}</div></div>`;
}

/**
 * Both 409 reports as one list: a headline that counts what blocks training, the blocking checks
 * with their glossary titles (the code stays in `data-code`), the randomness measure, and the
 * warnings folded. The acknowledge control sits next to the Train button (`acknowledgeHtml`).
 */
export function validationHtml(s) {
  const groups = checkGroups(s);
  if (!groups.length) return "";
  const blocking = groups.filter((g) => g.blocking);
  const warnings = groups.filter((g) => g.warning);
  const first = groups.filter((g) => !g.warning);
  const head = `${
    blocking.length
      ? `${plural(blocking.length, "thing")} to fix before training`
      : "Nothing left to fix before training"
  }${warnings.length ? ` (${plural(warnings.length, "warning")} can be ignored)` : ""}.`;
  const hasNotRandom = groups.some((g) => g.code === NOT_RANDOM);
  const passing = hasNotRandom ? "" : randomnessLine(s, false);
  const folded = warnings.length
    ? `<details class="uwarns"><summary>${esc(`Show ${plural(warnings.length, "warning")}`)}</summary>${warnings
        .map((g) => checkItem(s, g))
        .join("")}</details>`
    : "";
  return `<div class="vlist" role="alert"><div class="vhead">${esc(head)}</div>${first
    .map((g) => checkItem(s, g))
    .join("")}${passing ? `<div class="vitem">${passing}</div>` : ""}${folded}</div>`;
}

/** The acknowledge checkboxes, beside the Train button: one per acknowledgeable blocking code. */
function acknowledgeHtml(s) {
  return checkGroups(s)
    .filter((g) => g.token && (g.blocking || s.acknowledged.includes(g.token)))
    .map(
      (g) =>
        `<label class="uack"><input type="checkbox" data-uack="${esc(g.token)}"${
          s.acknowledged.includes(g.token) ? " checked" : ""
        }> I confirm this is expected — run anyway${
          g.token === NOT_RANDOM ? " (every result will be labelled not causal)" : ""
        }</label>`,
    )
    .join("");
}

function upliftRunsCard(uc, runs, { collapsed = false } = {}) {
  const rows = (runs || []).map((run) => {
    const train = run.mode === "train";
    const headline =
      run.state === "done"
        ? train
          ? `Trained · ${dash(run.best_model)}`
          : `Scored ${dash(run.row_count, fmtInt)} customers`
        : `${train ? "Training" : "Scoring"} · ${STATE_LABEL[run.state] || run.state}`;
    const inUse = train && run.champion && run.state === "done";
    return `<a class="runrow" href="${esc(routes.run(uc.id, run.run_id))}">
      <div><div class="r1">${esc(headline)}${inUse ? '<span class="champ">In use</span>' : ""}</div>
      <div class="r2">${esc(run.file_name)} · ${esc(fmtStamp(run.created_at))}</div></div>
      <div class="r3"><span>${esc(train ? "Trained" : "Scored")}</span></div></a>`;
  });
  const list = `<div class="runs-list">${
    rows.length ? rows.join("") : `<div class="empty">No uplift runs yet.</div>`
  }</div>`;
  if (collapsed) {
    return `<section class="card"><details class="adv urunsfold"><summary>${esc(
      `Previous uplift runs (${rows.length})`,
    )}</summary>${list}</details></section>`;
  }
  return `<section class="card"><h3>Previous uplift runs${sortNote("newest first")}</h3>${list}</section>`;
}

function setupBody(uc, s) {
  const train = s.mode === "train";
  const profile = profileOf(s);
  const names = profile ? profile.columns.map((c) => c.name) : [];
  const why = setupBlocker(s);

  const step1 = `<div class="fstep ${s.upload ? "done" : ""}"><div class="stepno">1</div><div>
    <div class="flabel">Dataset</div><div class="fhint">${esc(
      train
        ? "One row per customer of a past campaign: the features, whether they were treated (0/1, assigned at random) and the outcome."
        : "The customers to score. The trained uplift model ranks them by how much the action changes their outcome.",
    )}</div>
    <div class="orline"><label class="control file ${
      s.upload ? "has" : ""
    }"><input type="file" id="u-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
      profile ? profile.file_name : "Upload CSV or Parquet",
    )}</span><span class="ico" aria-hidden="true">⤒</span></label>${
      profile
        ? `<span>${esc(fmtInt(profile.row_count))} rows · ${esc(String(profile.column_count))} columns · ${esc(
            `file size ${fmtSize(profile.file_size_bytes)}`,
          )}</span>`
        : ""
    }</div>
    ${s.uploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${s.uploadError ? errorBox(s.uploadError) : ""}</div></div>`;

  const columnSelect = (id, label, current, choices) =>
    `<div class="field"><label class="sub" for="${id}">${esc(label)}</label><div class="control sel"><select id="${id}">${option(
      "",
      "Select a column…",
      current,
    )}${choices.map((n) => option(n, n, current)).join("")}</select></div></div>`;

  const candidates = (s.candidates && s.candidates.candidates) || [];
  const treatmentField = train
    ? `<div class="field wide"><label class="sub" for="u-treatment">Column that says who was contacted (1) or held back (0)</label><div class="control sel"><select id="u-treatment">${treatmentOptions(
        s,
        names,
      )}</select></div></div>`
    : "";
  const candidateNote = train
    ? s.candidatesLoading
      ? `<div class="loading">Looking for 0/1 treatment columns…</div>`
      : s.candidatesError
        ? errorBox(s.candidatesError)
        : s.upload && !candidates.length && s.candidates
          ? `<p class="seg-help">No 0/1 column in this file looks like a treatment. Uplift needs one: 1 for customers who got the action, 0 for the randomly held-out control group.</p>`
          : ""
    : "";
  const ptype =
    train && s.treatment
      ? `<div class="ptype"><span>Problem type</span><span class="pill">Uplift</span><span>${esc(
          UPLIFT_EXPLANATION,
        )}</span>${
          s.acknowledged.includes(NOT_RANDOM) ? `<span class="uwarn">${esc(NOT_CAUSAL_NOTE)}</span>` : ""
        }</div>`
      : "";
  // Step 2 is one line until a file exists: three empty dropdowns explain nothing.
  const step2 = s.upload
    ? `<div class="fstep ${!why || (s.pk && !train) ? "done" : ""}"><div class="stepno">2</div><div>
    <div class="flabel">Columns</div><div class="fhint">${esc(
      train
        ? "Which column identifies a customer, which one says whether they were treated, and which one is the outcome."
        : "Which column identifies a customer.",
    )}</div>
    <div class="frow">${columnSelect("u-pk", "Customer ID column", s.pk, names)}${
      train
        ? `${treatmentField}${columnSelect(
            "u-target",
            "Result column",
            s.target,
            names.filter((n) => n !== s.pk && n !== s.treatment),
          )}`
        : ""
    }</div>${candidateNote}${ptype}</div></div>`
    : `<div class="fstep locked"><div class="stepno">2</div><div><div class="flabel">Columns</div><div class="fhint">Available once a file is uploaded.</div></div></div>`;

  const versions = upliftVersions(s.models);
  const chosen = versions.find((v) => v.version.model_id === s.modelVersionId);
  const modelSelect = `<div class="field wide"><div class="control sel"><select id="u-model" aria-label="Trained uplift model">${
    versions.length
      ? versions.map((v) => option(v.version.model_id, modelOptionLabel(v), s.modelVersionId)).join("")
      : `<option value="">No trained uplift model yet</option>`
  }</select></div></div>`;
  // One model: say which, and fold the choice away. Several: the dropdown, open.
  const modelChoice =
    versions.length === 1 && chosen
      ? `<p class="umodel">${esc(modelOptionLabel(chosen))}</p><details class="adv"><summary>Change model</summary><div class="frow">${modelSelect}</div></details>`
      : `<div class="frow">${modelSelect}</div>`;
  const step3 = train
    ? ""
    : `<div class="fstep ${versions.length ? "done" : "locked"}"><div class="stepno">3</div><div>
        <div class="flabel">Trained uplift model</div><div class="fhint">The saved uplift model that will rank the uploaded customers.</div>
        ${modelChoice}</div></div>`;

  const acks = acknowledgeHtml(s);
  return `<div class="setup-grid">
    <section class="card"><div class="form-body">
      <div class="seg" role="group" aria-label="Mode"><button type="button" data-umode="train" class="${
        train ? "on" : ""
      }" aria-pressed="${train}">Train uplift model</button><button type="button" data-umode="score" class="${
        train ? "" : "on"
      }" aria-pressed="${!train}">Score new data</button></div>
      <p class="seg-help">${esc(
        train
          ? "It needs a past campaign where the action was given at random."
          : "Scoring writes the treat list: persuadables within budget are Treat, sleeping dogs never are, and a control group is held out.",
      )}</p>
      <form id="u-setup" novalidate class="uform-setup">
        ${step1}${step2}${step3}
        ${validationHtml(s)}
        ${s.submitError ? errorBox(s.submitError) : ""}
        ${acks ? `<div class="uacks">${acks}</div>` : ""}
        <div class="actions"><button type="submit" class="btn primary run" id="u-run"${
          why || s.submitting ? " disabled" : ""
        }>${esc(s.submitting ? "Starting…" : train ? "Train uplift model" : "Score customers")}</button><span class="reason">${esc(
          why,
        )}</span></div>
      </form></div></section>
    ${upliftRunsCard(uc, s.runs)}
  </div>
  <p class="next">${esc(
    train
      ? "After the run: Model (how well it finds the persuadable) → Output (segments, treat list) → Campaign results."
      : "After scoring: download the contact list, run the campaign, then upload its outcomes to see the campaign results.",
  )}</p>`;
}

// --- Running (a thin copy of the shared Running component, ui/usecase.js; WP2 owns the original) --

/** `status.json`'s stages collapsed onto their `group_label` rows, as the Phase 1 Running screen does. */
export function groupStages(stages) {
  const groups = [];
  const index = new Map();
  for (const stage of stages || []) {
    if (!index.has(stage.group_label)) {
      index.set(stage.group_label, groups.length);
      groups.push({ label: stage.group_label, stages: [] });
    }
    groups[index.get(stage.group_label)].stages.push(stage);
  }
  return groups.map((group) => {
    const states = group.stages.map((st) => st.state);
    const state = states.includes("failed")
      ? "failed"
      : states.includes("cancelled")
        ? "cancelled"
        : states.includes("running")
          ? "running"
          : states.every((st) => st === "done" || st === "skipped")
            ? "done"
            : "pending";
    const failed = group.stages.find((st) => st.state === "failed");
    const details = group.stages.filter((st) => st.detail).map((st) => st.detail);
    const detail = failed && failed.error ? failed.error.message : details.length ? details[details.length - 1] : "";
    return { label: group.label, state, detail, details };
  });
}

/** The pipeline's step names, in the words a user knows. Unknown ones are shown as they are. */
const GROUP_PLAIN = {
  "Validating data": "Checking the campaign data",
  "Preparing features": "Preparing the data",
  "Training candidate models": "Learning who responds to contact",
  "Evaluating on hold-out set": "Testing on customers the model has not seen",
  "Generating explanations & saving": "Saving the model",
  "Validating columns": "Checking the file",
  "Loading champion model": "Loading the uplift model",
  "Scoring rows": "Scoring customers",
  "Generating reasons & actions": "Writing the contact list",
};

/** "7K" -> "about 7,000": the stage lines round their counts, so they are read back as "about". */
const expandK = (text) =>
  String(text).replace(/\b(\d+(?:\.\d+)?)([KM])\b/g, (_, n, unit) =>
    `about ${fmtInt(Math.round(Number(n) * (unit === "K" ? 1e3 : 1e6)))}`,
  );

/**
 * A stage's detail line in plain words, and whether it is a warning. The engine writes these lines
 * for engineers ("train 7K · test 3K · stratified on treatment and outcome"); the known shapes are
 * reworded and anything else is shown as written, with its counts spelled out.
 */
export function plainDetail(detail) {
  const text = String(detail || "");
  if (!text) return { text: "", warn: false };
  let m = text.match(/^(\d+) warnings?\b/);
  if (m) return { text: `${plural(Number(m[1]), "warning")} to review`, warn: true };
  m = text.match(/^train (\S+) · test (\S+)/);
  if (m) return { text: `Learning group ${expandK(m[1])} · testing group ${expandK(m[2])} customers`, warn: false };
  if (/AUUC/.test(text)) {
    const beats = /measurable uplift/.test(text) && !/no measurable uplift/i.test(text);
    return { text: beats ? "The model beats picking customers at random" : "The model does not beat picking at random", warn: !beats };
  }
  if (/TreeSHAP/.test(text)) return { text: "Reasons worked out for each customer", warn: false };
  if (/^kept as candidate/.test(text)) return { text: "Saved; an Approver decides whether it is used", warn: false };
  m = text.match(/rows segmented · (\S+) to treat · (\S+) suppressed · (\S+) held out as control/);
  if (m) {
    return {
      text: `${expandK(m[1])} to contact · ${expandK(m[2])} opted out · ${expandK(m[3])} held back to measure`,
      warn: false,
    };
  }
  if (/written to scores\.csv/.test(text)) return { text: "Contact list written", warn: false };
  m = text.match(/^(\S+) rows scored/);
  if (m) return { text: `${expandK(m[1])} customers scored`, warn: false };
  if (/prepared with the feature spec/.test(text)) return { text: "Using the uplift model you chose", warn: false };
  return { text: expandK(text.split(" · ").slice(0, 2).join(" · ")), warn: /warning/i.test(text) };
}

function runningBody(uc, s) {
  const run = s.detail && s.detail.run;
  const train = !run || run.mode === "train";
  const status = s.detail && s.detail.status;
  const groups = status ? groupStages(status.stages) : [];
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = groups.length
    ? groups
        .map((g, i) => {
          const plain = g.state === "failed" ? { text: g.detail, warn: false } : plainDetail(g.detail);
          return `<li class="${cls[g.state]}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
            GROUP_PLAIN[g.label] || g.label,
          )}</div><div class="pd${plain.warn ? " w" : ""}"${g.details.length ? ` title="${esc(g.details.join(" · "))}"` : ""}>${esc(
            plain.text,
          )}</div></div></li>`;
        })
        .join("")
    : `<li><span class="dot">1</span><div><div class="pt">Waiting for the run to start</div><div class="pd"></div></div></li>`;
  const cancel = s.confirmCancel
    ? `<div class="btn-row ucancel"><span>Cancel this run? It stops now and cannot be resumed.</span><button type="button" class="btn danger sm confirm" id="u-cancel">Yes, cancel run</button><button type="button" class="btn quiet sm" id="u-cancel-keep">Keep running</button></div>`
    : `<div class="btn-row ucancel"><span class="spacer"></span><button type="button" class="btn danger sm" id="u-cancel">Cancel run</button></div>`;
  return `<div class="setup-grid"><section class="card"><h3>${esc(
    train ? "Training your uplift model…" : "Scoring customers…",
  )}</h3><p class="urun-intro">${esc(
    "Running… This usually takes a few minutes. You can leave this page; the run keeps going.",
  )}</p><ol class="progress">${rows}</ol>${cancel}${s.submitError ? errorBox(s.submitError) : ""}</section>${upliftRunsCard(
    uc,
    s.runs,
    { collapsed: true },
  )}</div>`;
}

// --- Results -----------------------------------------------------------------------------------

/** The top-N% gain of an evaluation, or `null`: the row nearest to `pct` percent. */
function topGain(evaluation, pct = DEFAULT_TOP_SHARE_PCT) {
  const rows = (evaluation && evaluation.uplift_at) || [];
  const found = rows.find((u) => Math.abs(u.fraction * 100 - pct) < 0.5);
  return found && found.uplift && present(found.uplift.value) ? { pct, cv: found.uplift } : null;
}

/** "Contacting the top 10% the model picks raises the response rate by about 20.6 points." */
function gainSentence(evaluation) {
  const top = topGain(evaluation);
  if (!top) return "";
  const v = top.cv.value * 100;
  const change = v >= 0 ? "raises" : "lowers";
  return `Contacting the top ${top.pct}% the model picks ${change} the response rate by about ${fmtNum(
    Math.abs(v),
    1,
  )} points.`;
}

function resultsBody(uc, s) {
  const run = s.detail && s.detail.run;
  if (!run) return `<div class="loading">Loading the run…</div>`;
  const train = run.mode === "train";
  const done = run.state === "done";
  const gain = train ? gainSentence(s.evaluation) : "";
  const headline = done
    ? `<span class="ok">✓ ${train ? "Uplift model trained" : "Scoring complete"}</span>`
    : run.state === "cancelled"
      ? `<span class="muted">Run cancelled</span>`
      : `<span class="bad">✕ Run failed</span>`;
  const detailLine = done
    ? train
      ? `<span>${esc(gain || `${dash(run.best_model)} · ${dash(run.headline_metric_label)} ${fmtVal(run.headline_score)}`)}</span>`
      : `<span><b>${esc(dash(run.row_count, fmtInt))}</b> customers scored</span>`
    : run.error
      ? `<span>${esc(run.error.message)}</span>`
      : "";
  const primary = !done
    ? { label: "Try again", href: routes.setup(uc.id) }
    : train
      ? { label: "Score customers with this model", href: routes.score(uc.id) }
      : { label: "Download contact list (CSV)", href: s.scoresHref || "#", attrs: "download" };
  const actions = headActions({
    primary,
    related: done ? { label: "Change settings and train again", href: routes.setup(uc.id) } : null,
  });
  const blocks = train
    ? [
        ["Data", routes.data(uc.id, run.run_id), run.file_name, `${dash(run.row_count, fmtInt)} customers · result ${dash(run.target)}`],
        [
          "Model",
          routes.model(uc.id, run.run_id),
          dash(run.best_model),
          gain ? "How well it finds the customers contact changes" : `${dash(run.headline_metric_label)} ${fmtVal(run.headline_score)}`,
        ],
        ["Contact list", routes.output(uc.id, run.run_id), "Who to contact", "Checked on the test customers; score new data for a list"],
      ]
    : [
        ["Data", routes.data(uc.id, run.run_id), run.file_name, `${dash(run.row_count, fmtInt)} customers`],
        ["Contact list", routes.output(uc.id, run.run_id), "Who to contact", "Segments, recommended contacts, download"],
        // Not "Done" when the scoring run is: its results exist only once outcomes are uploaded, which
        // run.json does not record, so the block says when it applies instead.
        [
          "Campaign results",
          routes.campaign(uc.id, run.run_id),
          "Contacted vs not contacted",
          "Upload outcomes once the campaign has run",
          "After the campaign",
        ],
      ];
  const flow = blocks
    .map(
      ([label, href, value, meta, next], i) =>
        `${i ? '<div class="arrow" aria-hidden="true">→</div>' : ""}<a class="block${
          done ? "" : " pending"
        }" href="${esc(href)}"><div><div class="lab"><span>${esc(label)}</span>${
          !done
            ? '<span class="bstate waiting">Not completed</span>'
            : next
              ? `<span class="bstate waiting">${esc(next)}</span>`
              : '<span class="bstate">✓ Done</span>'
        }</div><div class="val">${esc(value)}</div><div class="meta">${esc(
          meta,
        )}</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`,
    )
    .join("");
  return `<div class="results"><div class="summary">${headline}${detailLine}<span class="muted">${esc(
    run.file_name,
  )} · ${esc(fmtStamp(run.created_at))}</span></div>${actions}
    <span class="cap">Uplift pipeline</span><div class="flow">${flow}</div>
    <div class="runs-below">${upliftRunsCard(uc, s.runs)}</div>
    ${techDetails([
      ["Run", run.run_id],
      ["Model version", run.model_version_id],
      ["Model", run.best_model],
    ])}</div>`;
}

/** `#/uplift/<use case>[/run/<run>]`: Setup, Running or Results, like the Phase 1 use-case screen. */
export function upliftScreenHtml(uc, s) {
  const body = s.view === "running" ? runningBody(uc, s) : s.view === "results" ? resultsBody(uc, s) : setupBody(uc, s);
  const current = s.view === "setup" || !s.view ? { label: uc.name } : { label: uc.name, href: routes.setup(uc.id) };
  const trail = crumbs([INDEX_CRUMB, current, s.view === "running" || s.view === "results" ? { label: "Run" } : null]);
  return screenOf(
    uc,
    `${trail}<h1 class="h1">${esc(
      uc.name,
    )} · Uplift</h1><p class="desc">Uplift ${esc(UPLIFT_EXPLANATION)}.</p>${upliftChip()}`,
    body,
  );
}

// --- Results pages: shell ----------------------------------------------------------------------

const isUpliftRun = (run) => !run.problem_type || run.problem_type === "uplift";

/**
 * One stepper for every run page: Data · Model · Contact list · Campaign results. A step that does
 * not apply to this run is shown but not a link (a scoring run has no model of its own; a training
 * run has no campaign). A run that is not an uplift one (a Phase 1 churn campaign) links its Data and
 * Output to the Phase 1 pages.
 */
function stepper(uc, run, kind) {
  const uplift = isUpliftRun(run);
  const train = run.mode === "train";
  const steps = [
    ["data", "Data", routes.data(uc.id, run.run_id), ""],
    ["model", "Model", routes.model(uc.id, run.run_id), train && uplift ? "" : "A scoring run uses a model trained earlier"],
    [
      "output",
      uplift ? "Contact list" : "Output",
      uplift ? routes.output(uc.id, run.run_id) : routes.phase1Output(uc.id, run.run_id),
      "",
    ],
    ["campaign", "Campaign results", routes.campaign(uc.id, run.run_id), train ? "Measured on a scoring run" : ""],
  ];
  // A step that does not apply says why in words everyone gets: the reason is printed under the
  // tabs and tied to the step by `aria-describedby`, and the step is focusable (`aria-disabled`,
  // not a link) so a keyboard reaches it and a screen reader reads the reason. The `title` stays
  // for a mouse hover, but it is never the only place the reason is.
  const offSteps = steps.filter(([key, , , off]) => off && key !== kind);
  const whyId = (key) => `ustep-why-${key}`;
  const why = offSteps.length
    ? `<p class="note ustep-why">${offSteps
        .map(([key, label, , off]) => `<span id="${whyId(key)}">${esc(`${label}: ${off}.`)}</span>`)
        .join(" ")}</p>`
    : "";
  return `<nav class="tabs" aria-label="Run pages">${steps
    .map(([key, label, href, off]) =>
      off && key !== kind
        ? `<span class="tab off" aria-disabled="true" tabindex="0" aria-describedby="${whyId(key)}" title="${esc(off)}">${esc(
            label,
          )}</span>`
        : `<a class="tab ${key === kind ? "on" : ""}" href="${esc(href)}"${key === kind ? ' aria-current="page"' : ""}>${esc(
            label,
          )}</a>`,
    )
    .join("")}</nav>${why}`;
}

const runWhen = (run) => `${run.mode === "train" ? "Trained" : "Scored"} ${fmtStamp(run.created_at)}`;

function pageShell(uc, run, kind, { title, desc = "", actions = "", trail = null, chip = true }, body) {
  const label = { model: "Model", output: "Contact list", campaign: "Campaign results" }[kind];
  return screenOf(
    uc,
    `${trail || upliftCrumbs(uc, label)}<h1 class="h1">${esc(title)}</h1>${desc ? `<p class="desc">${esc(desc)}</p>` : ""}${
      chip ? upliftChip() : ""
    }${actions}`,
    `<div class="tabs-bar">${stepper(uc, run, kind)}<span class="note">${esc(runWhen(run))}</span></div><div class="stack">${body}</div>`,
  );
}

const runTech = (run, extra = []) =>
  techDetails([
    [run.mode === "train" ? "Training run" : "Scoring run", run.run_id],
    ["Model version", run.model_version_id],
    ...extra,
  ]);

// --- Model -------------------------------------------------------------------------------------

function opeResult(report) {
  if (!report) return "";
  const dr = (report.estimates || []).find((e) => e.method === "dr") || (report.estimates || [])[0];
  const methodLabel = { ips: "Inverse propensity weighting (IPS)", snips: "Self-normalised IPS", dr: "Doubly robust (DR)" };
  const share = present(report.policy_treat_share) ? fmtRate(report.policy_treat_share) : EM_DASH;
  const lead =
    dr && dr.value && present(dr.value.value)
      ? `If you contact only the customers this rule picks (${share} of them), about ${fmtRate(dr.value.value)} would respond${
          present(dr.value.ci_low) && present(dr.value.ci_high)
            ? ` (likely ${fmtRate(dr.value.ci_low)} to ${fmtRate(dr.value.ci_high)})`
            : ""
        }.`
      : "No estimate could be made for this rule.";
  const compare = [
    report.treat_all_value && present(report.treat_all_value.value) ? `contacting everyone: ${fmtRate(report.treat_all_value.value)}` : "",
    report.treat_none_value && present(report.treat_none_value.value) ? `contacting no one: ${fmtRate(report.treat_none_value.value)}` : "",
  ].filter(Boolean);
  return `${notCausalBanner(report)}<div class="uope-result"><p class="uv-x"><b>${esc(lead)}</b>${
    compare.length ? ` ${esc(`For comparison, ${compare.join("; ")}.`)}` : ""
  }</p><p class="caption">${esc(`Rule: ${dash(report.policy_description)}`)}</p>${detailsKv(
    "How this was estimated",
    [
      ["Policy", dash(report.policy_description)],
      ["Customers the rule contacts", share],
      ["Response rate as it happened", fmtRate(report.logged_value)],
      ["Contact everyone (DR)", fmtCi(report.treat_all_value, (v) => fmtRate(v))],
      ["Contact no one (DR)", fmtCi(report.treat_none_value, (v) => fmtRate(v))],
      ["Rows", dash(report.rows, fmtInt)],
      ...(report.estimates || []).map((e) => [methodLabel[e.method] || e.method, fmtCi(e.value, (v) => fmtRate(v))]),
      ["Estimated", fmtStamp(report.computed_at)],
    ],
  )}</div>`;
}

function opeCard(o) {
  const report = o && o.report;
  const share = o && present(o.topSharePct) && o.topSharePct !== "" ? o.topSharePct : String(DEFAULT_TOP_SHARE_PCT);
  const open = report || (o && (o.submitting || o.error));
  return `<section class="card"><details class="adv ufold uwhatif"${open ? " open" : ""}><summary>What if we contacted only the top customers?</summary>
    <form id="u-ope" class="uform" novalidate><p class="seg-help">Estimate the response rate if you contact only the customers the model ranks highest. Estimated on test customers the model never saw.</p><div class="frow"><div class="field xs"><label class="sub" for="u-ope-share">Top share (%)</label><div class="control"><input type="number" id="u-ope-share" min="1" max="100" step="1" value="${esc(
      share,
    )}" aria-describedby="u-ope-help"></div></div></div>
    <div class="actions"><button type="submit" class="btn secondary"${o && o.submitting ? " disabled" : ""}>${
      o && o.submitting ? "Estimating…" : "Estimate"
    }</button><span class="reason" id="u-ope-help">A whole number from 1 to 100.</span></div>
    ${o && o.error ? errorBox(o.error) : ""}</form>${opeResult(report)}</details></section>`;
}

/** `#/uplift/<use case>/model/<run>`: the finding first, the Qini curve and deciles, metrics behind Details. */
export function modelPageHtml(uc, run, art, ope) {
  const validation = art["uplift_validation.json"];
  const evaluation = art["uplift_evaluation.json"];
  const curve = art["qini_curve.json"];
  const e = evaluation || {};
  const top10 = topGain(evaluation, 10);
  const top30 = topGain(evaluation, 30);
  const ate = e.average_treatment_effect;
  const gainTile = (label, top) => ({
    label,
    value: top ? fmtPts(top.cv.value) : EM_DASH,
    sub: top ? fmtLikely(top.cv.ci_low, top.cv.ci_high, (v) => fmtPts(v).replace(" pts", "")) : "",
  });
  const tileRow = tiles([
    gainTile("Top 10% gain", top10),
    gainTile("Top 30% gain", top30),
    {
      label: "Everyone contacted",
      value: fmtPts(ate && ate.value),
      sub: ate ? fmtLikely(ate.ci_low, ate.ci_high, (v) => fmtPts(v).replace(" pts", "")) : "",
    },
    {
      label: "Model beats random targeting",
      value: evaluation ? (evaluation.measurable_uplift ? "Yes" : "No") : EM_DASH,
    },
  ]);
  let verdict;
  if (!evaluation) {
    verdict = emptyState({
      title: "The model's test results are not available",
      text: "This run did not save its test results, so there is nothing to show yet. Train the model again to see them.",
      action: { label: "Go to uplift Setup", href: routes.setup(uc.id), kind: "secondary" },
    });
    verdict = `<section class="card">${verdict}</section>`;
  } else {
    const top = top10 || topGain(evaluation, ((e.uplift_at || [])[0] || {}).fraction * 100);
    const title = top
      ? `Contacting the top ${fmtNum(top.pct, 0)}% the model picks: ${fmtPts(top.cv.value).replace(" pts", " points")} ${
          top.cv.value >= 0 ? "more" : "fewer"
        } responses than not contacting them${
          present(top.cv.ci_low) && present(top.cv.ci_high)
            ? ` (likely ${fmtPts(top.cv.ci_low).replace(" pts", "")} to ${fmtPts(top.cv.ci_high).replace(" pts", "")})`
            : ""
        }.`
      : evaluation.measurable_uplift
        ? "The model finds the customers contact changes."
        : "The model does not yet find the customers contact changes.";
    verdict = verdictCard(
      evaluation.measurable_uplift ? "ok" : "warn",
      title,
      evaluation.measurable_uplift
        ? "Measurable uplift: picking customers this way beats picking them at random."
        : "No measurable uplift: it does not yet beat picking customers at random, so do not rely on its list.",
    );
  }
  const auucName = (glossaryMetric("auuc") || {}).name;
  const metrics = detailsKv(
    "Technical metrics",
    [
      ["AUUC", fmtCi(e.auuc)],
      ["Qini coefficient", fmtCi(e.qini_coefficient)],
      ["Treating everyone (ATE)", fmtCi(ate, (v) => fmtPts(v))],
      ["Randomness check (AUC)", validation && present(validation.randomness_auc) ? fmtNum(validation.randomness_auc, 3) : EM_DASH],
    ],
    `${auucName ? `<p class="caption">${esc(`AUUC: ${auucName}.`)}</p>` : ""}${
      evaluation && evaluation.summary ? `<p class="caption">${esc(evaluation.summary)}</p>` : ""
    }`,
  );
  const setupRows = [
    ["Learner", labelOf(LEARNER_LABEL, e.learner)],
    ["Base model", labelOf(BASE_MODEL_LABEL, e.base_model)],
    ["Hold-out rows", dash(e.rows_evaluated, fmtInt)],
    ["Contacted", `${dash(e.treated_rows, fmtInt)} · ${fmtRate(e.treated_rate)} responded`],
    ["Not contacted", `${dash(e.control_rows, fmtInt)} · ${fmtRate(e.control_rate)} responded`],
    ["Bootstrap resamples", dash(e.bootstrap_samples, fmtInt)],
    ["Evaluated", fmtStamp(e.evaluated_at)],
  ];
  const upliftAt = (e.uplift_at || []).length
    ? dataTable(
        [{ label: "Customers contacted" }, { label: "Measured gain", num: true }],
        e.uplift_at.map((u) => {
          const range = fmtLikely(u.uplift && u.uplift.ci_low, u.uplift && u.uplift.ci_high, (v) =>
            fmtPts(v).replace(" pts", ""),
          );
          return [
            esc(`Top ${fmtNum(u.fraction * 100, 0)}%`),
            `${esc(fmtPts(u.uplift && u.uplift.value))}${range ? `<small class="urange">${esc(range)}</small>` : ""}`,
          ];
        }),
      )
    : `<div class="empty">No gain was measured for this run.</div>`;
  const deciles = e.deciles || [];
  const decileTable = deciles.length
    ? `<details class="adv utable"><summary>Show table</summary>${dataTable(
        [
          { label: "Customers" },
          { label: "Measured gain", num: true },
          { label: "Predicted gain", num: true },
          { label: "Customers in group", num: true },
          { label: "Contacted", num: true, more: true },
          { label: "Not contacted", num: true, more: true },
          { label: "Response rate, contacted", num: true, more: true },
          { label: "Response rate, not contacted", num: true, more: true },
        ],
        deciles.map((d) => [
          esc(d.decile === 1 ? "Top 10%" : `${(d.decile - 1) * 10}–${d.decile * 10}%`),
          esc(fmtPts(d.observed_uplift)),
          esc(fmtPts(d.predicted_uplift)),
          esc(dash(d.rows, fmtInt)),
          esc(dash(d.treated_rows, fmtInt)),
          esc(dash(d.control_rows, fmtInt)),
          esc(fmtRate(d.treated_rate)),
          esc(fmtRate(d.control_rate)),
        ]),
      )}</details>`
    : "";
  const qini = curve ? qiniChart(curve) : `<div class="empty">The gain chart is not available for this run.</div>`;
  const decile = deciles.length ? decileChart(deciles) : `<div class="empty">The gain for each tenth of customers is not available for this run.</div>`;
  const body = `${notCausalBanner(validation, evaluation, curve, art["segments.json"], art["policy_recommendation.json"])}
    ${verdict}${tileRow}
    <div class="row">
      <section class="card"><h3>Gain from targeting by the model vs at random</h3>${qini}<p class="caption">${esc(
        `The further the solid line sits above the dashed one, the more the model's picks beat picking at random${
          curve ? ` (${fmtInt(curve.rows_evaluated)} test customers)` : ""
        }.`,
      )}</p></section>
      <section class="card"><h3>Gain in the top customers</h3>${upliftAt}<p class="caption">Response rate of contacted minus not-contacted customers, among those the model ranks highest.</p></section>
    </div>
    <section class="card"><h3>Gain in each tenth of customers</h3>${decile}${decileTable}</section>
    ${opeCard(ope)}
    <section class="card card-body udetails-card">${metrics}${detailsKv("Training setup", setupRows)}${runTech(run)}</section>`;
  return pageShell(
    uc,
    run,
    "model",
    {
      title: "How well the model finds the persuadable",
      desc: "Tested on customers the model never saw during learning.",
      actions: headActions({ primary: { label: "Score customers with this model", href: routes.score(uc.id) } }),
    },
    body,
  );
}

// --- Output ------------------------------------------------------------------------------------

/**
 * "Contact 2,241 customers. Of 2,804 persuadable customers, 579 persuadables held back at random to
 * measure the campaign or opted out are not on the list. Those held back are among the 720 of all
 * customers held back to measure the campaign." Each held-back count names whom it counts, so the
 * persuadables here, the tile's all-customer count (`heldBack`, `scoring_summary.json`'s
 * `control_group_rows`) and the campaign page's measured control group read as parts of one group.
 */
export function contactLine(policy, segments, train, heldBack = null) {
  if (!policy || !present(policy.contacts_recommended)) return "";
  const persuadable = ((segments && segments.segments) || []).find((s) => s.segment === "persuadable");
  const p = persuadable && present(persuadable.rows) ? persuadable.rows : null;
  const eligible = policy.eligible_persuadables;
  const r = policy.contacts_recommended;
  const parts = [
    train
      ? `On the test customers the model would contact ${plural(r, "customer")}.`
      : `Contact ${plural(r, "customer")}.`,
  ];
  if (present(p) && present(eligible) && p > eligible) {
    const off = p - eligible;
    parts.push(
      `Of ${plural(p, "persuadable customer")}, ${plural(off, "persuadable")} held back at random to measure the campaign or opted out ${
        off === 1 ? "is" : "are"
      } not on the list.`,
    );
    if (present(heldBack) && heldBack > 0) {
      parts.push(`Those held back are among the ${fmtInt(heldBack)} of all customers held back to measure the campaign.`);
    }
  }
  if (present(eligible) && eligible > r) {
    parts.push(`${STOP_REASON[policy.stop_reason] || humanise(policy.stop_reason)} ${fmtInt(eligible - r)} more could be contacted.`);
  }
  return parts.join(" ");
}

/** `#/uplift/<use case>/output/<run>`: who to contact, the four segments, the contact list download. */
export function outputPageHtml(uc, run, art, extra = {}) {
  const validation = art["uplift_validation.json"];
  const segments = art["segments.json"];
  const policy = art["policy_recommendation.json"];
  const summary = extra.summary || null;
  const p = policy || {};
  const train = run.mode === "train";
  const expected = p.expected_incremental_conversions;
  const held =
    summary && present(summary.control_group_rows)
      ? {
          value: fmtInt(summary.control_group_rows),
          sub: present(summary.rows_scored) && summary.rows_scored
            ? `${fmtRate(summary.control_group_rows / summary.rows_scored)} of all ${fmtInt(summary.rows_scored)} customers, chosen at random`
            : "chosen at random",
        }
      : { value: EM_DASH, sub: train ? "Set when new customers are scored" : "" };
  const tileRow = tiles([
    { label: "Customers to contact", value: dash(p.contacts_recommended, fmtInt) },
    {
      label: "Extra customers expected to respond",
      value: expected && present(expected.value) ? `about ${fmtCount(expected.value)}` : EM_DASH,
      sub: expected ? fmtLikely(expected.ci_low, expected.ci_high) : "",
      tip: glossaryTerm("incremental"),
    },
    {
      label: "All customers held back to measure the campaign",
      value: held.value,
      sub: held.sub,
      tip: glossaryTerm("control group"),
    },
  ]);
  const line = contactLine(policy, segments, train, summary ? summary.control_group_rows : null);
  const scope = policy
    ? policy.computed_on === "test"
      ? "Measured on the hold-out split of the training run."
      : "Computed over every customer this run scored."
    : "";
  const lead = policy
    ? `<p class="ulead">${esc(line)} <span class="muted">${esc(scope)}</span></p>`
    : emptyState({
        title: "No contact list for this run",
        text: "This run did not work out who to contact. Score new customers with an uplift model to get one.",
        action: { label: "Score new data", href: routes.score(uc.id), kind: "secondary" },
      });
  const t = (segments && segments.thresholds) || {};
  const segmentRows = segments
    ? `<div class="usegmean">${(segments.segments || [])
        .map((sg) => `<p><b>${esc(sg.label)}:</b> ${esc(SEGMENT_MEANING[sg.segment] || sg.action)}</p>`)
        .join("")}</div>${detailsKv("How the segments are cut", [
        ["Persuadable", `predicted uplift ≥ ${fmtPts(t.persuadable_min_uplift)}`],
        ["Sleeping dog", `predicted uplift ≤ ${fmtPts(t.sleeping_dog_max_uplift)}`],
        [
          "Sure thing vs lost cause",
          `P(outcome if not treated) ${present(t.sure_thing_min_probability) ? `≥ ${fmtRate(t.sure_thing_min_probability)}` : EM_DASH}${
            t.sure_thing_from_base_rate ? " (the training base rate)" : ""
          }`,
        ],
        ["Customers segmented", dash(segments.rows, fmtInt)],
      ])}`
    : "";
  const costs = policy && [p.cost_per_contact, p.value_per_conversion].some(present);
  const policyRows = policy
    ? `${kvs([
        [
          "Recommended to contact",
          `${fmtInt(policy.contacts_recommended)} of ${fmtInt(policy.eligible_persuadables)} persuadables`,
        ],
        ["Contact budget", present(policy.budget_contacts) ? fmtInt(policy.budget_contacts) : "No limit set"],
        ...(present(policy.budget_contacts) || policy.stop_reason !== "all_persuadables"
          ? [["Why not more", STOP_REASON[policy.stop_reason] || humanise(policy.stop_reason)]]
          : []),
        ...(costs
          ? [
              ["Cost per contact", fmtCount(policy.cost_per_contact)],
              ["Value per conversion", fmtCount(policy.value_per_conversion)],
              ["Expected cost", fmtCount(policy.expected_cost)],
              ["Expected value", fmtCount(policy.expected_value)],
              ["Expected net value", fmtCount(policy.expected_net_value)],
            ]
          : []),
      ])}${
        costs
          ? ""
          : `<p class="caption">The value in money appears once a cost per contact and a value per response are set in the uplift settings.</p>`
      }${detailsKv("More about this recommendation", [
        ["Expected incremental conversions", fmtCi(expected, (v) => fmtCount(v))],
        ["Model's own prediction", fmtCount(policy.predicted_incremental_conversions)],
        ...(costs ? [] : [["Cost per contact", EM_DASH]]),
      ])}`
    : `<div class="empty">No recommendation was made for this run.</div>`;

  let listCard;
  if (!train) {
    listCard = `<p class="caption ufile">${esc(
      "The contact list file has every scored customer with their group and action. Treat rows are the list to contact; control-group rows are held out at random so the campaign can be measured; sleeping dogs are never Treat.",
    )}</p>`;
  } else {
    const scoreRuns = (extra.scoreRuns || []).filter((r) => r.state === "done");
    listCard = `<section class="card"><h3>Contact lists from this model${sortNote("newest first")}</h3>${
      scoreRuns.length
        ? `<div class="runs-list">${scoreRuns
            .map(
              (r) =>
                `<a class="runrow" href="${esc(routes.output(uc.id, r.run_id))}"><div><div class="r1">Scored ${esc(
                  dash(r.row_count, fmtInt),
                )} customers</div><div class="r2">${esc(r.file_name)} · ${esc(
                  fmtStamp(r.created_at),
                )}</div></div><div class="r3"><b>Contact list ›</b></div></a>`,
            )
            .join("")}</div>`
        : emptyState({
            title: "No contact list yet",
            text: "A contact list comes from scoring new customers with this model.",
            action: { label: "Score new data", href: routes.score(uc.id), kind: "secondary" },
          })
    }</section>`;
  }

  const body = `${notCausalBanner(validation, segments, policy)}
    ${lead}${policy ? tileRow : ""}
    <div class="row">
      <section class="card"><h3>Four groups of customers</h3>${segmentChart(segments)}${segmentRows}</section>
      <section class="card"><h3>Targeting recommendation</h3>${policyRows}</section>
    </div>
    ${listCard}
    <section class="card card-body udetails-card">${runTech(run)}</section>`;
  const actions = train
    ? headActions({ primary: { label: "Score customers with this model", href: routes.score(uc.id) } })
    : headActions({
        primary: { label: "Download contact list (CSV)", href: extra.scoresHref || "#", attrs: "download" },
        secondary: [{ label: "Measure campaign results", href: routes.campaign(uc.id, run.run_id) }],
      });
  return pageShell(
    uc,
    run,
    "output",
    {
      title: "Who to contact",
      desc: train
        ? "What the model would recommend, checked on customers it never saw during learning."
        : "The customers worth contacting, and a random group held back to measure the campaign.",
      actions,
    },
    body,
  );
}

// --- Campaign results --------------------------------------------------------------------------

/**
 * The verdict of a measured campaign, in the value view's words and sign (`GET /pilot/roi/{run}`):
 * `benefit` is already "customers gained" - the measured difference, turned round when the outcome is
 * one to prevent (a customer leaving) - so a churn campaign reads "customers kept", never a negative
 * count of "conversions". Without the value view the sentence only states the difference in rates.
 */
export function campaignVerdict(report, roi) {
  const good = !roi || roi.outcome_is_good !== false;
  const b = roi && roi.benefit;
  if (b && present(b.value)) {
    const n = fmtCount(Math.abs(b.value));
    const hasRange = present(b.low) && present(b.high);
    const range = hasRange ? `${fmtCount(b.low)} to ${fmtCount(b.high)}` : "";
    const likely = hasRange ? ` (likely ${range})` : "";
    if (hasRange && b.low > 0) {
      return {
        tone: "ok",
        title: good
          ? `The campaign worked: about ${n} extra customers responded because of it${likely}.`
          : `The campaign worked: it kept about ${n} customers${likely}.`,
      };
    }
    if (hasRange && b.high < 0) {
      return {
        tone: "bad",
        title: good
          ? `The campaign did harm: about ${n} fewer customers responded than without it${likely}.`
          : `The campaign did harm: about ${n} more customers were lost than without it${likely}.`,
      };
    }
    if (!hasRange) {
      return {
        tone: "warn",
        title: good
          ? `Most likely ${fmtCount(b.value)} extra customers responded because of the campaign, but there is no range to judge it by.`
          : `Most likely ${fmtCount(b.value)} customers kept by the campaign, but there is no range to judge it by.`,
      };
    }
    return {
      tone: "warn",
      title: good
        ? `We cannot yet tell whether the campaign brought extra customers: most likely ${fmtCount(b.value)} extra, but the range (${range}) includes zero.`
        : `We cannot yet tell whether the campaign kept customers: most likely ${fmtCount(b.value)} kept, but the range (${range}) includes zero.`,
    };
  }
  const lift = report && report.absolute_lift;
  if (!lift || !present(lift.value)) return { tone: "info", title: "The campaign's effect could not be measured." };
  const range =
    present(lift.ci_low) && present(lift.ci_high)
      ? ` (likely ${fmtPts(lift.ci_low).replace(" pts", "")} to ${fmtPts(lift.ci_high).replace(" pts", "")} points)`
      : "";
  return {
    tone: "info",
    title: `The outcome rate of contacted customers differs from the control group by ${fmtPts(lift.value).replace(
      " pts",
      " points",
    )}${range}.`,
  };
}

/**
 * Of N customers: contacted, held back, not part of the test, with the zero rows left out. The
 * held-back count is this campaign's measured control group - a part of the scoring run's whole
 * control group the Contact list page counts - so it says so rather than "the control group".
 */
export function reconciliationLine(report) {
  const parts = [
    [report.treated_rows, "contacted"],
    [report.control_rows, "held back and measured as this campaign's control group"],
    [report.rows_suppressed_or_untreated, "not part of the test (opted out or not selected)"],
    [report.rows_immature, "still inside the outcome period"],
    [report.rows_without_outcome, "with no row in the outcomes file"],
  ].filter(([n]) => present(n) && n > 0);
  const total = parts.reduce((sum, [n]) => sum + n, 0);
  if (!total) return "";
  return `Of ${fmtInt(total)} customers on this campaign's list: ${parts
    .map(([n, what]) => `${fmtInt(n)} ${what}`)
    .join(", ")}.`;
}

function reportSection(report, roi, run) {
  if (!report) {
    return `<section class="card">${emptyState({
      title: "No campaign results yet",
      text: "No outcomes have been uploaded for this run yet. Upload them below once the campaign has run.",
    })}</section>`;
  }
  const banner = notCausalBanner(report);
  if (report.status === "immature") {
    return `${banner}<section class="card"><div class="uwait"><b>Results available on ${esc(
      fmtDay(report.results_available_on),
    )}</b><span>${esc(
      `${fmtInt(report.rows_immature)} customers are still inside their ${
        present(report.outcome_window_days) ? `${report.outcome_window_days}-day ` : ""
      }outcome window (as of ${fmtStamp(report.as_of)}). Nothing is estimated before it has elapsed: an early read would count conversions that have not happened yet.`,
    )}</span></div></section>`;
  }
  const good = !roi || roi.outcome_is_good !== false;
  const verdict = campaignVerdict(report, roi);
  const context = report.causal === false
    ? "The groups were not chosen at random, so this describes the difference but cannot show what the campaign caused."
    : "Contacted customers compared with a random group that was not contacted.";
  const partial = present(report.results_available_on)
    ? ` ${fmtInt(report.rows_immature)} customers were still inside their outcome window and are left out; every customer's window has elapsed on ${fmtDay(
        report.results_available_on,
      )}.`
    : "";
  const b = roi && roi.benefit;
  const lift = report.absolute_lift;
  const first = b
    ? {
        label: roi.benefit_label || (good ? "Extra customers because of the campaign" : "Customers kept by the campaign"),
        value: fmtCount(b.value),
        sub: fmtLikely(b.low, b.high),
        tip: glossaryTerm("incremental"),
      }
    : {
        label: "Difference in outcome rate, contacted minus not contacted",
        value: lift ? fmtPts(lift.value).replace(" pts", " points") : EM_DASH,
        sub: lift ? fmtLikely(lift.ci_low, lift.ci_high, (v) => fmtPts(v).replace(" pts", "")) : "",
      };
  const diff = lift && present(lift.value) ? Math.abs(lift.value * 100) : null;
  const direction = lift && present(lift.value) ? (lift.value >= 0 ? "higher" : "lower") : "";
  const second = {
    label: good ? "Response rate: contacted vs not contacted" : "Share lost: contacted vs not contacted",
    value: `${fmtRate(report.treated_rate)} vs ${fmtRate(report.control_rate)}`,
    sub: present(diff) ? `${fmtNum(diff, 1)} points ${direction} when contacted` : "",
    tip: glossaryTerm("control group"),
  };
  const withOutcome = "With the outcome";
  const arms = dataTable(
    [
      { label: "Group" },
      { label: "Customers", num: true },
      { label: withOutcome, num: true },
      { label: "Rate", num: true },
    ],
    [
      ["Contacted", fmtInt(report.treated_rows), fmtInt(report.treated_conversions), fmtRate(report.treated_rate)],
      [
        "Not contacted (control group)",
        fmtInt(report.control_rows),
        fmtInt(report.control_conversions),
        fmtRate(report.control_rate),
      ],
    ].map((row) => row.map((cell) => esc(cell))),
  );
  const stats = detailsKv("Statistical details", [
    ["Absolute lift", fmtCi(lift, (v) => fmtPts(v))],
    ["Relative lift", present(report.relative_lift) ? `${signed(report.relative_lift * 100, 1)}%` : EM_DASH],
    ["p-value", fmtP(report.p_value)],
    ...(b ? [[`${roi.benefit_label || "Customers gained"} (range)`, `${fmtCount(b.value)} (${fmtLikely(b.low, b.high) || EM_DASH})`]] : []),
  ], `<p class="caption">${esc(
    (glossaryTerm("confidence interval") ||
      "The range the true value very probably lies in. A range that does not include zero means the effect is real, not luck."),
  )}</p>`);
  const measuredRows = [
    ["Outcome column", dash(report.outcome_column)],
    ["Outcome window", present(report.outcome_window_days) ? `${report.outcome_window_days} days` : EM_DASH],
    ["Judged as of", fmtStamp(report.as_of)],
    ...[
      [report.rows_immature, "Not yet known (outcome period still running)"],
      [report.rows_without_outcome, "No row in the outcomes file"],
      [report.rows_suppressed_or_untreated, "Not part of the test (opted out or not selected)"],
    ]
      .filter(([n]) => present(n) && n > 0)
      .map(([n, label]) => [label, fmtInt(n)]),
    ["Computed", fmtStamp(report.computed_at)],
  ];
  const reconcile = reconciliationLine(report);
  const measured = detailsKv(
    "How it was measured",
    measuredRows,
    techDetails([
      ["Scoring run", run.run_id],
      ["Campaign", report.campaign_id],
    ]),
  );
  return `${banner}${verdictCard(verdict.tone, verdict.title, `${context}${partial}`)}${tiles([first, second])}
    <section class="card"><h3>What was measured</h3>${arms}<p class="caption ucap">${esc(
      reconcile ||
        (report.causal === false
          ? "The groups were not chosen at random, so the difference is descriptive only."
          : "The control group was chosen at random and not contacted, so the difference between the two rates is what the campaign caused."),
    )}</p><div class="card-body">${stats}${measured}</div></section>`;
}

function outcomesForm(c, { again }) {
  const profile = c.upload ? c.upload.profile : null;
  const names = profile ? profile.columns.map((col) => col.name) : [];
  const f = c.form || {};
  const select = (id, label, current, emptyLabel, help = "") =>
    `<div class="field"><label class="sub" for="${id}">${esc(label)}</label><div class="control sel"><select id="${id}">${option(
      "",
      emptyLabel,
      current,
    )}${names.map((n) => option(n, n, current)).join("")}</select></div>${help ? `<span class="fhelp">${esc(help)}</span>` : ""}</div>`;
  const ready = profile && f.outcome_column;
  const form = `<form id="u-camp" class="uform" novalidate>
    <p class="seg-help">One row per customer of this run: its customer ID and whether they had the outcome. Contacted customers are compared with the control group; customers who were not part of the test are left out.</p>
    <div class="orline"><label class="control file ${profile ? "has" : ""}"><input type="file" id="u-camp-file" class="sr" accept=".csv,.parquet"><span class="fname">${esc(
      profile ? profile.file_name : "Upload outcomes CSV or Parquet",
    )}</span><span class="ico" aria-hidden="true">⤒</span></label>${
      profile ? `<span>${esc(fmtInt(profile.row_count))} rows</span>` : ""
    }</div>
    ${c.uploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${c.uploadError ? errorBox(c.uploadError) : ""}
    ${
      profile
        ? `<div class="frow">${select("u-camp-outcome", "Outcome column", f.outcome_column, "Select a column…")}${select(
            "u-camp-date",
            "Date each customer was contacted (optional)",
            f.treatment_date_column,
            "None: use the scoring time",
          )}</div>
    <div class="frow"><div class="field"><label class="sub" for="u-camp-window">Outcome window (days)</label><div class="control"><input type="number" id="u-camp-window" min="1" max="3650" step="1" value="${esc(
      present(f.outcome_window_days) ? f.outcome_window_days : "",
    )}"></div></div></div>
    <details class="adv"${f.positive_label || f.as_of ? " open" : ""}><summary>Advanced</summary><div class="frow"><div class="field"><label class="sub" for="u-camp-label">Value that means "had the outcome" (optional)</label><div class="control"><input type="text" id="u-camp-label" value="${esc(
      f.positive_label || "",
    )}" placeholder="1, true or yes"></div></div><div class="field"><label class="sub" for="u-camp-asof">Judge as of (optional)</label><div class="control"><input type="date" id="u-camp-asof" value="${esc(
      f.as_of || "",
    )}"></div></div></div></details>`
        : ""
    }
    ${c.submitError ? errorBox(c.submitError) : ""}
    <div class="actions"><button type="submit" class="btn ${again ? "secondary" : "primary run"}" id="u-camp-run"${
      !ready || c.submitting ? " disabled" : ""
    }>${c.submitting ? "Measuring…" : "Measure the campaign"}</button><span class="reason">${esc(
      !profile ? "Upload the outcomes file to continue" : !f.outcome_column ? "Choose the outcome column" : "",
    )}</span></div></form>`;
  if (!again) return `<section class="card"><h3>Upload campaign outcomes</h3>${form}</section>`;
  const open = c.upload || c.uploading || c.uploadError || c.submitting || c.submitError;
  return `<section class="card"><details class="adv ufold uagain"${open ? " open" : ""}><summary>Measure again with a new outcomes file</summary>${form}</details></section>`;
}

/** The campaign's name: the demo manifest's title when it names this run, else the use case's. */
export const campaignTitle = (uc, run, c) => (c && c.title) || `${uc.name} campaign`;

/** `#/campaign/<use case>/<run>`: the verdict of a scoring run's campaign, or when it will exist. */
export function campaignPageHtml(uc, run, c) {
  const title = campaignTitle(uc, run, c);
  const trail = crumbs([{ label: "Campaigns", href: routes.campaigns() }, { label: title }]);
  const chip = isUpliftRun(run);
  if (run.mode !== "score") {
    const body = noticeCard({
      title: "Campaign results need a scoring run",
      text: "Campaign results are measured on a scoring run: its control group was held out at random when the treat list was written. This is a training run.",
      action: { label: "Score new data", href: isUpliftRun(run) ? routes.score(uc.id) : routes.useCase(uc.id) },
    });
    return pageShell(uc, run, "campaign", { title, trail, chip }, body);
  }
  const report = c.report;
  const measured = report && report.status !== "immature";
  const actions = measured
    ? headActions({
        primary: { label: "See the value in rupees", href: routes.value(run.run_id) },
        related: { label: "Who was contacted", href: isUpliftRun(run) ? routes.output(uc.id, run.run_id) : routes.phase1Output(uc.id, run.run_id) },
      })
    : "";
  const body = `${c.loadError ? errorBox(c.loadError, { retry: true }) : ""}${reportSection(report, c.roi, run)}${outcomesForm(c, {
    again: !!report,
  })}`;
  return pageShell(
    uc,
    run,
    "campaign",
    {
      title,
      desc: `Did contacting customers change what they did? ${uc.name}.`,
      actions,
      trail,
      chip,
    },
    body,
  );
}
