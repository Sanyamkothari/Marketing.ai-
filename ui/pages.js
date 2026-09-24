// The Data, Model and Output pages, rendered from a run's artefacts (plan §7's table, §9.3).
//
// Every figure on these three screens comes out of an artefact this run wrote. Nothing is filled
// in from the prototype's illustrative figures: a value this run did not produce is an em dash in its
// one cell, or a card that says, in plain words, when that value will exist (v1: never a file name).
//
// v1 (docs/ui/FOUNDATION.md, WP4): one header pattern, plain words first (configs/pilot/help.yaml, read
// through dom.js's glossary helpers), ids, codes and lineage behind "Technical details", one primary
// action per screen, and each page asks only for the artefacts its run's mode writes.

import {
  EM_DASH,
  bandPill,
  barTrack,
  columnBar,
  dash,
  dataTable,
  emptyState,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtMetric,
  fmtNum,
  fmtPct,
  fmtSize,
  fmtStamp,
  glossaryCode,
  glossaryMetric,
  glossarySetting,
  glossaryTerm,
  headActions,
  journeyCrumb,
  kvs,
  metricShortName,
  noticeCard,
  pageHead,
  present,
  sortNote,
  table,
  techDetails,
  toggletip,
  typeChip,
} from "./dom.js";
import { indexSchema, readPath } from "./settings.js";

/**
 * What each page reads, by the run's mode: `data` / `model` / `output` for a training run, the
 * `_score` lists for a scoring run. A scoring run writes no `split.json`, no evaluation and no
 * `decile_lift.json`, and a training run no `scoring_summary.json` or `drift.json`, so asking for
 * them only logged 404s (and failed `runs.artefact_download` audit events). A scoring run's Model
 * page reads `drift.json` for one field: the training run its model came from (`baseline_run_id`).
 */
export const PAGE_ARTEFACTS = {
  data: ["profile.json", "prepare.json", "split.json", "validation.json", "run_config.json"],
  model: [
    "run_config.json",
    "best_model.json",
    "evaluation.json",
    "baseline.json",
    "feature_importance.json",
    "confusion_matrix.json",
    "leaderboard.json",
    "fairness.json",
  ],
  output: ["decile_lift.json", "run_config.json"],
  data_score: ["profile.json", "prepare.json", "validation.json", "run_config.json"],
  model_score: ["drift.json"],
  output_score: ["scoring_summary.json", "drift.json", "run_config.json"],
};

/**
 * The Model page's Details beeswarm. It is fetched only when the section is opened: the default view
 * stays the importance chart, and a plot of up to 2,000 rows x 15 features is not worth loading for
 * a reader who never asks for it (DEC-802).
 */
export const BEESWARM_ARTEFACT = "shap_beeswarm.json";

/**
 * What an uplift run's Data page reads instead (DEC-651: `run.problem_type === "uplift"`). An uplift
 * run writes no `prepare.json`, `drift.json`, `decile_lift.json` or ROC-style evaluation. Its Model and
 * Output are the uplift module's own screens (`#/uplift/<uc>/{model,output}/<run>`): a Phase 1 link to
 * them redirects there and reads nothing here.
 */
export const UPLIFT_PAGE_ARTEFACTS = {
  data: ["profile.json", "split.json", "validation.json", "uplift_validation.json", "run_config.json"],
  data_score: ["profile.json", "validation.json", "uplift_drift.json", "run_config.json"],
};

export const isUplift = (run) => !!run && run.problem_type === "uplift";

const isScoring = (run) => !!run && run.mode !== "train";

/** The artefacts one page of this run reads: by its mode, or the uplift list (none on a redirect). */
export function pageArtefacts(kind, run) {
  const key = isScoring(run) ? `${kind}_score` : kind;
  return (isUplift(run) ? UPLIFT_PAGE_ARTEFACTS : PAGE_ARTEFACTS)[key] || [];
}

const TAB_LABEL = { data: "Data", model: "Model", output: "Output", campaign: "Campaign results" };
const CRUMB_SEP = '<span class="sep" aria-hidden="true">›</span>';

// The router's seams (the active top-bar goal) are reached lazily: `modules/router.js`
// touches `window` when it loads, and this file must stay importable by node's unit tests. In the
// browser `app.js` has already loaded the router, so this resolves before the first page is drawn.
let seams = null;
if (typeof window !== "undefined" && typeof document !== "undefined") {
  import("./modules/router.js").then(
    (module) => {
      seams = module;
    },
    () => {},
  );
}

// The plain wording of the few help.yaml entries these pages lead with, used only until the pilot
// module registers the catalogue (`registerGlossary`); the words are help.yaml's own.
const FALLBACK_CODES = {
  THRESHOLD_FALLBACK: {
    title: "The automatic cut-off was replaced by the top 10%",
    meaning:
      "The automatic way of choosing which customers to flag would have flagged almost everyone, so the platform flags the top 10% of scores instead.",
    fix: "Nothing is required. If a different share suits your campaign budget, set the cut-off in the settings.",
  },
  DRIFT_WATCH: {
    title: "The new customers look a little different",
    meaning:
      "Some measures in the file being scored have moved away from what the model learned on. Scores are still usable.",
    fix: "No action now. If it persists for a few months, retrain on recent data.",
  },
  DRIFT_DRIFTED: {
    title: "The new customers look very different",
    meaning:
      "Several measures have moved far from what the model learned on, so scores may be less reliable.",
    fix: "Check that the new file was exported the same way; if it was, retrain the model on recent data.",
  },
};
const FALLBACK_METRICS = {
  roc_auc: "How well it ranks customers with the outcome above those without (1 is perfect)",
  pr_auc: "How precise its top of the list is, across all list lengths (1 is perfect)",
  f1: "Balance between catching cases and avoiding false alarms (1 is perfect)",
  recall: "Share of all cases it catches at the chosen cut-off",
  precision: "Share of flagged customers who really have the outcome",
  rmse: "Typical size of its error, in the outcome's own units (lower is better)",
  mae: "Average size of its error, in the outcome's own units (lower is better)",
};
const FALLBACK_TERMS = {
  lift: "How many times more cases a group contains than a random group of the same size. A lift of 3 means three times as many.",
  baseline: "A simple model used as a yardstick; the platform's model should beat it.",
};
const codeEntry = (code) => glossaryCode(code) || FALLBACK_CODES[code] || null;
const metricName = (id) => {
  const entry = glossaryMetric(id);
  return (entry && (entry.name || entry)) || FALLBACK_METRICS[id] || null;
};
const termText = (term) => glossaryTerm(term) || FALLBACK_TERMS[term] || null;
/** The short label a metric goes by on these pages (dom.js `METRIC_SHORT`); its help.yaml name is its "?". */
const metricShort = (id, label) => metricShortName(id, label) || humanise(id);

/** The key as the user named it: one column, or its columns joined by " + " for a two-column key. */
const keyText = (pk) => (Array.isArray(pk) ? pk.join(" + ") : dash(pk));

/** A choice label from the advanced-settings schema, so enum wording matches the Setup screen. */
function choiceLabel(byPath, path, value) {
  const field = byPath.get(path);
  if (field && Array.isArray(field.choices)) {
    const choice = field.choices.find((c) => c.value === value);
    if (choice) return choice.label;
  }
  return present(value) ? humanise(value) : EM_DASH;
}

/** A vocabulary word the schema does not carry a label for: `clip_percentile` → `Clip percentile`. */
function humanise(value) {
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const capital = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);
const plural = (noun) => (/s$/.test(noun) ? noun : `${noun}s`);
const article = (noun) => (/^[aeiou]/i.test(noun) ? `an ${noun}` : `a ${noun}`);
const entityOf = (uc) => String((uc && uc.entity) || "customer").toLowerCase();

/** A column name as words: `days_since_last_activity` → "Days since last activity", `usage_mb_30d` → "Usage MB 30 days". */
function columnWords(name) {
  const words = String(name)
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .replace(/\b(\d+)d\b/g, "$1 days")
    .replace(/\b(\d+)m\b/g, "$1 months")
    .replace(/\b(\d+)h\b/g, "$1 hours")
    .replace(/\bmb\b/gi, "MB")
    .replace(/\s+/g, " ")
    .trim();
  return capital(lowerFirstWords(words));
}

/** Lower-case every word but the first, keeping acronyms ("MB") as they are. */
function lowerFirstWords(text) {
  return text
    .split(" ")
    .map((w, i) => (i === 0 || /^[A-Z0-9]{2,}$/.test(w) ? w : w.toLowerCase()))
    .join(" ");
}

/** The use case's own one-line description of a column (template, standard schema, suggested features). */
const describedCache = new WeakMap();
function columnDescriptions(uc) {
  if (!uc || typeof uc !== "object") return new Map();
  if (describedCache.has(uc)) return describedCache.get(uc);
  const config = uc.config || {};
  const map = new Map();
  const add = (list) => {
    for (const column of list || []) {
      if (column && column.name && column.description && !map.has(column.name)) {
        map.set(column.name, String(column.description).split(/(?<=\.)\s/)[0]);
      }
    }
  };
  add(config.suggested_features);
  add(config.standard_schema && config.standard_schema.columns);
  add(config.template && config.template.columns);
  describedCache.set(uc, map);
  return map;
}

/** "Days since last activity" plus, when the use case describes it, one muted line under it. */
function featureCell(uc, name, { describe = true } = {}) {
  const words = columnWords(name);
  const description = describe ? columnDescriptions(uc).get(name) : null;
  return `${esc(words)}${description ? `<span class="pg-desc">${esc(description)}</span>` : ""}`;
}

/** What the model predicts, in words: the built dataset's label, else the use case's target definition. */
function outcomeText(uc, run) {
  const label = uc && uc.config && uc.config.label;
  if (run && run.dataset_id && label && label.description) return label.description;
  return (uc && uc.target && uc.target.definition) || null;
}

// Page-level rules the foundation stylesheet has no class for yet (reported to the integrator: they
// belong in ui/index.html's v1 block). Tokens only.
const PAGE_STYLE = `<style data-pages>
.pg-desc{display:block;font-size:12px;color:var(--muted);margin-top:2px;font-weight:400}
.kpi .pg-kn{font-size:12px;color:var(--muted);margin-top:4px}
.pg-lead{margin:0;padding:16px 20px 0;font-size:20px;font-weight:600;line-height:1.35;color:var(--ink)}
.pg-p{margin:0;padding:8px 20px 0;font-size:14px;line-height:1.55;color:var(--ink2)}
.pg-p.muted{color:var(--muted);font-size:13px}
.pg-body{padding:0 0 16px}
.pg-body>.card-body{padding-bottom:0}
.pg-list{margin:8px 0 0;padding:0 20px 0 40px;font-size:14px;line-height:1.55;color:var(--ink2)}
.pg-next{display:flex;justify-content:flex-end}
.pg-sect{margin:16px 0 0;font-size:13px;font-weight:600;color:var(--ink)}
details.pg-more>.bars{padding-left:0;padding-right:0}
.pg-tech .card-body>details.tech,[data-pg-tech] .card-body>details.tech{border-top:0;padding-top:0}
.kpis.pg-one{grid-template-columns:1fr}
.kpis.pg-one .pg-kn{font-size:14px;color:var(--ink2);margin-top:8px}
.stack>.pg-p{padding:0}
details.tech h4{background:none;border:0;padding:12px 0 4px;color:var(--ink2)}
details.tech .kv{padding-left:0;padding-right:0}
details.tech .cm{padding:8px 0}
details.tech .tbl-wrap{margin:0}
details.tech .caption{padding:4px 0 8px}
@media (max-width:700px){.vchart{gap:4px;padding:16px 12px 8px;min-height:200px;overflow-x:visible}.vcol{min-width:0}.pg-next{justify-content:stretch}.pg-next .btn{width:100%}}
</style>`;

/** KPI tiles with an optional muted line under the value: `[label, value, note?]`. */
const tiles = (arr) =>
  `<div class="kpis">${arr
    .filter(Boolean)
    .map(
      ([l, v, note]) =>
        `<div class="kpi"><div class="l">${esc(l)}</div><div class="v">${esc(v)}</div>${
          note ? `<div class="pg-kn">${esc(note)}</div>` : ""
        }</div>`,
    )
    .join("")}</div>`;

// --- the shell: header, tabs, "Next", technical details -----------------------------------------

const upliftHref = (kind, uc, run) =>
  `#/uplift/${encodeURIComponent(uc.id)}/${kind}/${encodeURIComponent(run.run_id)}`;
const campaignHref = (uc, run) => `#/campaign/${encodeURIComponent(uc.id)}/${encodeURIComponent(run.run_id)}`;
const pageHref = (kind, uc, run) => {
  if (kind === "campaign") return campaignHref(uc, run);
  if (isUplift(run) && kind !== "data") return upliftHref(kind, uc, run);
  return `#/uc/${esc(uc.id)}/${kind}/${esc(run.run_id)}`;
};

/** The run's tabs: Data · Model · Output, and Campaign results on a scoring run (no numbers). */
function tabKinds(run) {
  if (isUplift(run)) return isScoring(run) ? ["data", "output", "campaign"] : ["data", "model", "output"];
  return isScoring(run) ? ["data", "model", "output", "campaign"] : ["data", "model", "output"];
}

const DESCRIPTION = {
  data: {
    train: "The data this model learned from, and how it was prepared.",
    score: "The data this run scored, and how it was prepared.",
  },
  model: {
    train: "How well the model predicts, and which details drive its scores.",
    score: "The model this run used to score.",
  },
  output: {
    train: "How well the top of the list is picked out, before any customer is contacted.",
    score: "Who to contact, and what to offer them.",
  },
};

function shell(uc, kind, run, body, { primary = null, secondary = [], tech = [], techHtml = "", stamp = true } = {}) {
  const label = TAB_LABEL[kind];
  const kinds = tabKinds(run);
  const tabs = kinds
    .map(
      (t, i) =>
        `${i ? '<span class="arr" aria-hidden="true">→</span>' : ""}<a class="tab ${t === kind ? "on" : ""}" href="${esc(
          pageHref(t, uc, run),
        )}"${t === kind ? ' aria-current="page"' : ""}>${esc(TAB_LABEL[t])}</a>`,
    )
    .join("");
  const when = fmtDate(run.finished_at || run.created_at);
  const note = stamp && present(run.finished_at || run.created_at)
    ? `${isScoring(run) ? "Scored" : "Trained"} ${when}`
    : "";
  const mode = isScoring(run) ? "score" : "train";
  // The header keeps only this page's own actions: a run action (the uplift module's flow block) is
  // not merged in here - the tab bar already leads to "Campaign results".
  const actions = headActions({ primary, secondary: [].concat(secondary).filter(Boolean) });
  const next = kinds[kinds.indexOf(kind) + 1];
  const nextButton =
    !primary && next && next !== "campaign"
      ? `<div class="pg-next"><a class="btn primary" href="${esc(pageHref(next, uc, run))}">Next: ${esc(
          TAB_LABEL[next],
        )} ›</a></div>`
      : "";
  const list = techDetails(
    [
      ["Run", run.run_id],
      ["Run started", present(run.created_at) ? fmtStamp(run.created_at) : null],
      ["Model version", run.model_version_id],
      ["Dataset", run.dataset_id],
      ["Upload", run.upload_id],
      ["Data fingerprint", run.dataset_fingerprint],
      ["Engine", present(run.engine_version) ? `Marketing AI ${run.engine_version}` : null],
      ...tech,
    ],
    "Technical details",
  ).replace(/^<details class="tech"><summary>[^<]*<\/summary>/, "").replace(/<\/details>$/, "");
  const details = list || techHtml
    ? `<details class="tech" data-tech><summary>Technical details</summary>${list}${techHtml}</details>`
    : "";
  return `<main class="screen t-${esc(uc.marker)}">${PAGE_STYLE}
    ${pageHead(`<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}${CRUMB_SEP}<a href="#/uc/${esc(
      uc.id,
    )}">${esc(uc.name)}</a>${CRUMB_SEP}<span class="cur" aria-current="page">${esc(label)}</span></nav>
      <h1 class="h1">${esc(uc.pages[kind])}</h1><p class="desc">${esc(DESCRIPTION[kind][mode])}</p><div class="chips">${typeChip(
        uc,
      )}</div>${actions}`)}
    <div class="tabs-bar"><nav class="tabs" aria-label="Pipeline">${tabs}</nav>${
      note ? `<span class="note">${esc(note)}</span>` : ""
    }</div>
    <div class="stack">${body}${details ? `<section class="card pg-tech"><div class="card-body">${details}</div></section>` : ""}${nextButton}</div>
  </main>`;
}

/** A card: a title (HTML already escaped) and a body. */
const card = (title, body, attrs = "") =>
  `<section class="card"${attrs ? ` ${attrs}` : ""}>${title ? `<h3>${title}</h3>` : ""}${body}</section>`;

/** A card that says when its value will exist, with at most one way there. */
const pendingCard = (title, text, action = null) =>
  card(esc(title), emptyState({ title: text.title, text: text.text, action }));

// --- Data --------------------------------------------------------------------------------------

function dateRange(split) {
  const parts = (split && split.parts) || [];
  const starts = parts.map((p) => p.start_date).filter(present);
  const ends = parts.map((p) => p.end_date).filter(present);
  if (!starts.length || !ends.length) return null;
  const from = starts.slice().sort()[0];
  const to = ends.slice().sort().slice(-1)[0];
  return `${fmtDate(from)} – ${fmtDate(to)}`;
}

function leakageLine(validation, config) {
  const checks = ((validation && validation.checks) || []).filter((c) => c.code.startsWith("LEAKAGE"));
  if (checks.length) {
    return checks
      .map((c) => `${c.severity === "error" ? "Flagged" : "Warned"} on ${columnWords(c.column || EM_DASH)}`)
      .join(" · ");
  }
  const enabled = readPath(config || {}, "validation.leakage_check");
  if (enabled === false) return "Not run";
  if (!validation) return null;
  return "Passed: no column gives the answer away";
}

/** One column of the lineage block: a heading and its cards, the file or node name first. */
function lineageColumn(title, cards) {
  return `<div class="lin"><div class="lt">${esc(title)}</div>${cards
    .filter(Boolean)
    .map(
      (node) =>
        `<div class="ld"><b>${esc(node.label)}</b>${node.detail ? ` · ${esc(node.detail)}` : ""}</div><div class="li">${esc(
          node.id,
        )}</div>`,
    )
    .join("")}</div>`;
}

/**
 * "Where this data came from" (Plan A M35): one plain sentence naming the files, then sources ->
 * mapping -> recipe -> dataset -> this run behind "Details: data lineage". Every node is the engine's
 * own `GET /datasets/{id}/lineage` node; the last is this run's record. A run that read an uploaded
 * file names that file; its format, size and fingerprint are in the page's technical details.
 */
function sourceCard(run, profile, extra) {
  if (extra.lineageError) {
    return card("Where this data came from", `<div class="card-body">${errorBox(extra.lineageError)}</div>`);
  }
  const lineage = extra.lineage;
  if (lineage) {
    const files = (lineage.sources || []).map((s) => s.label).filter(Boolean);
    const runNode = {
      id: run.run_id,
      label: isScoring(run) ? "Scoring run" : "Training run",
      detail: fmtStamp(run.created_at),
    };
    const block = `<div class="lineage">${[
      lineageColumn("Sources", lineage.sources || []),
      lineageColumn("Mapping", lineage.mappings || []),
      lineageColumn("Recipe", [lineage.spec]),
      lineageColumn("Dataset", [lineage.dataset]),
      lineageColumn("Run", [runNode]),
    ].join("")}</div>`;
    return card(
      "Where this data came from",
      `<p class="pg-p">Built from ${fmtInt(files.length)} ${files.length === 1 ? "table" : "tables"}${
        files.length ? `: ${esc(files.join(", "))}` : ""
      }.</p><p class="pg-p muted">The same recipe can be replayed on next month's tables.</p><div class="card-body"><details class="tech" data-lineage><summary>Details: data lineage</summary>${block}</details></div>`,
    );
  }
  if (profile && present(profile.file_name) && !run.dataset_id) {
    return card("Where this data came from", `<p class="pg-p">Uploaded file <b>${esc(profile.file_name)}</b>.</p><div class="pg-body"></div>`);
  }
  return "";
}

/** The file's own facts, for the technical details: format, size, encoding, fingerprint. */
function fileDetails(profile) {
  if (!profile) return "";
  return `<h4>File details</h4>${kvs([
    ["File", dash(profile.file_name)],
    ["Format", present(profile.file_format) ? String(profile.file_format).toUpperCase() : EM_DASH],
    ["Size", present(profile.file_size_bytes) ? fmtSize(profile.file_size_bytes) : EM_DASH],
    ["Rows", dash(profile.row_count, fmtInt)],
    ["Columns", dash(profile.column_count, String)],
    ["Encoding", dash(profile.encoding)],
    ["Fingerprint", profile.fingerprint && profile.fingerprint.hash ? profile.fingerprint.hash : EM_DASH],
  ])}`;
}

/** Transforms grouped by kind, each a plain step: "Extreme values capped · on 11 columns". */
const TRANSFORM_WORDS = {
  clip_percentile: "Extreme values capped",
  dedupe: "Duplicate rows removed",
  redact: "Personal details masked",
  cast: "Dates read as dates",
  consent_filter: "Customers without marketing consent removed",
};

function transformSteps(prepare) {
  const groups = new Map();
  for (const t of (prepare && prepare.transforms) || []) {
    const params = t.parameters || {};
    if (params.applied === false) continue;
    if (!groups.has(t.kind)) groups.set(t.kind, { kind: t.kind, columns: [], rows: 0, hasRows: false, params });
    const g = groups.get(t.kind);
    g.columns.push(...(t.columns || []));
    if (present(params.rows_removed)) {
      g.rows += Number(params.rows_removed);
      g.hasRows = true;
    }
  }
  return [...groups.values()].map((g) => {
    const title = TRANSFORM_WORDS[g.kind] || humanise(g.kind);
    const parts = [];
    if (g.columns.length === 1) parts.push(`on ${columnWords(g.columns[0])}`);
    else if (g.columns.length > 1) parts.push(`on ${fmtInt(g.columns.length)} columns`);
    if (g.hasRows) parts.push(g.rows ? `${fmtInt(g.rows)} ${g.rows === 1 ? "row" : "rows"} removed` : "none found");
    return [title, parts.join(" · ") || "Applied"];
  });
}

const stepsList = (arr) =>
  `<ol class="steps">${arr
    .map(
      ([a, b], i) =>
        `<li><span class="no">${i + 1}</span><span><div class="s1">${esc(a)}</div><div class="s2">${esc(b)}</div></span></li>`,
    )
    .join("")}</ol>`;

const DROP_WORDS = {
  user_excluded: "Excluded in the settings",
  constant: "Only one value",
  high_null: "Mostly empty",
  id_like: "Looks like an ID",
  pii: "Personal details",
  leakage: "Gives the answer away",
};
const REMOVAL_WORDS = {
  duplicate: "Exact duplicates",
  consent_false: "No marketing consent",
  missing_target: "Outcome not recorded",
  outlier: "Extreme values",
  missing_values: "Missing values",
};

/** The distinct count of the entity column, when the key is entity + snapshot: "2,000 subscribers". */
function entityCount(run, profile) {
  const pk = run.primary_key;
  if (!Array.isArray(pk) || pk.length < 2 || !profile) return null;
  const column = (profile.columns || []).find((c) => c.name === pk[0]);
  return column && present(column.distinct_count) ? column.distinct_count : null;
}

function dataPage(uc, run, art, byPath, extra) {
  const profile = art["profile.json"];
  const prepare = art["prepare.json"];
  const split = art["split.json"];
  const validation = art["validation.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;
  const entity = entityOf(uc);
  const scoring = isScoring(run);

  const features = (prepare && prepare.feature_columns) || [];
  const entities = entityCount(run, profile);
  const range = dateRange(split);
  const glance = tiles([
    ["Rows", dash(profile && profile.row_count, fmtInt), entities !== null ? `One per ${entity} per date` : null],
    entities !== null ? [capital(plural(entity)), fmtInt(entities)] : null,
    ["Details used to predict", prepare ? String(features.length) : EM_DASH],
    ["Missing values", dash(profile && profile.missing_value_rate_pct, (v) => `${fmtNum(v, 1)}%`)],
    range ? ["Date range", range] : null,
  ]);

  // What the model predicts: only on a training run (a scoring run has no outcome column), and the
  // label's source only for an uploaded file - a built dataset's label is the recipe's own rule.
  const outcome = outcomeText(uc, run);
  const labelSource = !run.dataset_id && uc.target && present(uc.target.label_source) ? uc.target.label_source : null;
  const targetCard = scoring
    ? ""
    : card(
        "What the model predicts",
        `${kvs(
          [
            ["Outcome", dash(outcome)],
            [uc.target && uc.target.label ? uc.target.label : "Target column", dash(run.target)],
            labelSource ? ["Label source", labelSource] : null,
          ].filter(Boolean),
        )}`,
      );

  const trainPart = ((split && split.parts) || []).find((p) => p.name === "train");
  const parts = (split && split.parts) || [];
  const shareOf = (name) => {
    const part = parts.find((p) => p.name === name);
    return part ? fmtPct(part.share, 0) : null;
  };
  const splitWords = parts.length
    ? [
        shareOf("train") ? `${shareOf("train")} to learn` : null,
        shareOf("validation") && parts.find((p) => p.name === "validation").share ? `${shareOf("validation")} to choose the model` : null,
        shareOf("test") ? `${shareOf("test")} to test it` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  const leakage = scoring ? null : leakageLine(validation, config);
  const noticed = ((validation && validation.checks) || []).filter((c) => !String(c.code).startsWith("LEAKAGE"));
  const qualityRows = [
    leakage ? ["Leakage check", leakage] : null,
    splitWords ? ["Split for testing", splitWords] : null,
    split && split.group_column ? ["Kept together", `All rows of one ${entity} stay in the same part`] : null,
  ].filter(Boolean);
  const noticedList = noticed.length
    ? `<p class="pg-sect pg-p">Things we noticed</p><ul class="pg-list">${noticed
        .map((c) => {
          const entry = codeEntry(c.code);
          const titled = !!(entry && entry.title);
          const rest = (titled ? [c.message, c.suggestion] : [c.suggestion]).filter(Boolean).join(" ");
          return `<li data-code="${esc(c.code)}">${esc(titled ? entry.title : c.message)}${
            rest ? ` <span class="muted">${esc(rest)}</span>` : ""
          }</li>`;
        })
        .join("")}</ul><div class="pg-body"></div>`
    : "";

  const steps = transformSteps(prepare);
  const pipelineCard = card(
    "How the data was prepared",
    `${steps.length ? stepsList(steps) : `<div class="empty">Nothing needed changing.</div>`}${
      qualityRows.length ? `<h4>Data quality</h4>${kvs(qualityRows)}` : ""
    }${noticedList}`,
  );

  const byName = new Map(((profile && profile.columns) || []).map((c) => [c.name, c]));
  const transformsFor = (name) =>
    [...new Set(((prepare && prepare.transforms) || [])
      .filter((t) => (t.columns || []).includes(name) && !(t.parameters && t.parameters.applied === false))
      .map((t) => TRANSFORM_WORDS[t.kind] || humanise(t.kind)))]
      .join(", ") || EM_DASH;
  const featureRows = features
    .map((name) => ({ name, column: byName.get(name) }))
    .sort((a, b) => (b.column ? b.column.null_rate : -1) - (a.column ? a.column.null_rate : -1) || a.name.localeCompare(b.name))
    .map(({ name, column }) => [
      featureCell(uc, name),
      column ? esc(`${fmtNum(column.null_rate * 100, 1)}%`) : EM_DASH,
      `<code class="mono">${esc(name)}</code>`,
      column ? esc(humanise(column.inferred_type)) : EM_DASH,
      esc(transformsFor(name)),
    ]);
  const featureCard = card(
    `Details used to predict (${fmtInt(features.length)} of ${dash(profile && profile.column_count, String)} columns)${sortNote(
      "most missing first",
    )}`,
    prepare
      ? featureRows.length
        ? dataTable(
            [
              { label: "Detail" },
              { label: "Missing", num: true },
              { label: "Column", more: true },
              { label: "Type", more: true },
              { label: "Prepared by", more: true },
            ],
            featureRows,
          )
        : `<div class="empty">No column was used to predict.</div>`
      : emptyState({
          title: "The list of details appears once the data has been prepared",
          text: "It is written early in every run; if this run stopped before that step, run it again from Setup.",
          action: { label: "Go to Setup", href: `#/uc/${uc.id}`, kind: "secondary" },
        }),
  );

  const dropped = (prepare && prepare.dropped_columns) || [];
  const removals = (prepare && prepare.row_removals) || [];
  // Kept with the rows but never trained on - the as-of date (DEC-092). Not a dropped column, but
  // a column the user uploaded that is not a feature has to say why somewhere.
  const carried = (prepare && prepare.carried_columns) || [];
  // A carried column has one reason in the contract (the as-of date); its detail says so in words.
  const set = (title, rows, why = null) =>
    rows.length
      ? card(
          esc(title),
          dataTable(
            [{ label: "Column" }, { label: "Why" }, { label: "Detail" }],
            rows.map((c) => [esc(columnWords(c.name)), esc(why || DROP_WORDS[c.reason] || humanise(c.reason)), esc(dash(c.detail))]),
          ),
        )
      : "";
  const removalCard = removals.length
    ? card(
        "Rows left out",
        dataTable(
          [{ label: "Why" }, { label: "Rows", num: true }],
          removals.map((r) => [esc(REMOVAL_WORDS[r.reason] || humanise(r.reason)), esc(fmtInt(r.rows))]),
        ),
      )
    : "";

  const techRows = `${fileDetails(profile)}${
    trainPart && present(trainPart.positive_rate)
      ? `<h4>Split</h4>${kvs([
          ["Share with the outcome (learning part)", fmtPct(trainPart.positive_rate)],
          ["Train / validation / test", parts.map((p) => fmtNum(p.share * 100, 0)).join(" / ")],
          ...(split.detail ? [["Split", split.detail]] : []),
        ])}`
      : ""
  }`;

  const body = `${glance}
    ${sourceCard(run, profile, extra)}
    ${targetCard}
    ${pipelineCard}
    ${featureCard}
    ${set("Columns not used", dropped)}${set("Columns kept, not used to predict", carried, "The date each row describes")}${removalCard}`;
  return shell(uc, "data", run, body, { techHtml: techRows });
}

// --- Model -------------------------------------------------------------------------------------

/** The cut-off in words; `THRESHOLD_FALLBACK` by its help.yaml title, never as the code. */
function thresholdText(evaluation) {
  if (!evaluation || !present(evaluation.threshold)) return null;
  const cut = fmtNum(evaluation.threshold, 2);
  const detail = String(evaluation.threshold_detail || "");
  if (detail.includes("THRESHOLD_FALLBACK")) {
    const entry = codeEntry("THRESHOLD_FALLBACK");
    return {
      code: "THRESHOLD_FALLBACK",
      title: entry ? entry.title : "The automatic cut-off was replaced by the top 10%",
      text: `${entry && entry.meaning ? `${entry.meaning} ` : ""}Customers scoring ${cut} or more are flagged.`,
    };
  }
  const mode = evaluation.threshold_mode;
  const how =
    mode === "manual"
      ? "Set in the settings"
      : mode === "fixed"
        ? "A fixed cut-off"
        : "Chosen automatically to balance catching cases against false alarms";
  return { code: null, title: null, text: `${how}: customers scoring ${cut} or more are flagged.` };
}

function verdictCard(uc, run, evaluation, byPath) {
  const entity = entityOf(uc);
  const primary = evaluation.primary_metric;
  const score = evaluation.headline_score;
  const short = metricShort(primary, evaluation.primary_metric_label);
  let lead;
  let sub;
  if (primary === "roc_auc" && present(score)) {
    lead = `It ranks ${article(entity)} who has the outcome above one who does not ${fmtPct(score, 0)} of the time.`;
    sub = "100% would be perfect; 50% is no better than a coin toss.";
  } else {
    lead = `${short}: ${dash(score, fmtMetric)}`;
    sub = metricName(primary);
  }
  const outcome = outcomeText(uc, run);
  const cut = thresholdText(evaluation);
  const cutHtml = cut
    ? cut.title
      ? `<p class="pg-p" data-code="${esc(cut.code)}"><b>${esc(cut.title)}.</b> ${esc(cut.text)}</p>`
      : `<p class="pg-p">${esc(cut.text)}</p>`
    : "";
  return card(
    "How good is this model?",
    `<p class="pg-lead">${esc(lead)}</p>${sub ? `<p class="pg-p muted">${esc(sub)}</p>` : ""}${
      outcome ? `<p class="pg-p">The outcome: ${esc(outcome)}</p>` : ""
    }${cutHtml}<div class="pg-body"></div>`,
    "data-verdict",
  );
}

/** A calm note when the yardstick model beat this one on the primary metric. */
function baselineNotice(evaluation, baseline) {
  if (!evaluation || !baseline) return "";
  const row = (baseline.rows || []).find((r) => r.id === evaluation.primary_metric);
  if (!row || row.model_better !== false || !present(row.baseline_value) || !present(row.model_value)) return "";
  const short = metricShort(row.id, row.label).toLowerCase();
  const close = Math.abs(Number(row.baseline_value) - Number(row.model_value)) < 0.02;
  return noticeCard({
    title: `A simpler yardstick model scored ${close ? "slightly " : ""}higher`,
    text: [
      `On ${short} the yardstick scored ${fmtMetric(row.baseline_value)} against this model's ${fmtMetric(row.model_value)}.`,
      "Consider retraining with more data or a longer history before relying on this model.",
    ],
    attrs: "data-baseline-warning",
  });
}

/** "What drives the score": the top 8, largest first, the rest behind "Show all". */
function driversCard(uc, importance) {
  const items = ((importance && importance.items) || []).slice().sort((a, b) => b.share_pct - a.share_pct);
  if (!items.length) {
    return pendingCard("What drives the score", {
      title: "This appears when training finishes",
      text: "Each detail's share of the score is measured on the test data at the end of training.",
    });
  }
  const maxShare = Math.max(...items.map((i) => i.share_pct));
  const bar = (item) =>
    `<div class="brow"><span class="lab">${esc(columnWords(item.feature))}</span>${barTrack(
      maxShare ? (100 * item.share_pct) / maxShare : 0,
      `${columnWords(item.feature)} ${fmtNum(item.share_pct, 1)} percent`,
    )}<span class="pct">${fmtNum(item.share_pct, 1)}%</span></div>`;
  const top = items.slice(0, 8);
  const rest = items.slice(8);
  return card(
    `What drives the score${sortNote("largest first")}`,
    `<div class="bars">${top.map(bar).join("")}</div>${
      rest.length
        ? `<details class="tbl-more pg-more"><summary>Show all ${fmtInt(items.length)}</summary><div class="bars">${rest
            .map(bar)
            .join("")}</div></details>`
        : ""
    }<p class="caption">Each detail's share of how much the score depends on it, measured on customers the model never saw.</p>`,
  );
}

function evaluationCard(evaluation, baseline) {
  const metrics = (evaluation && evaluation.metrics) || [];
  const name = (id, label) =>
    `${esc(metricShort(id, label))}${metricName(id) ? toggletip(metricName(id), metricShort(id, label)) : ""}`;
  let rows;
  let cols;
  if (baseline) {
    cols = [{ label: "Measure" }, { label: "This model", num: true }, { label: "Yardstick", num: true }, { label: "Code", more: true }];
    rows = (baseline.rows || []).map((r) => [
      name(r.id, r.label),
      esc(fmtMetric(r.model_value)),
      esc(fmtMetric(r.baseline_value)),
      `<code class="mono">${esc(r.label)}</code>`,
    ]);
  } else {
    cols = [{ label: "Measure" }, { label: "This model", num: true }, { label: "Code", more: true }];
    rows = metrics.map((m) => [name(m.id, m.label), esc(fmtMetric(m.value)), `<code class="mono">${esc(m.label)}</code>`]);
  }
  const yardstick = baseline ? termText("baseline") : null;
  return card(
    "Measured on customers it never saw",
    `${dataTable(cols, rows, { cls: "eval" })}<p class="caption">${[
      evaluation && present(evaluation.rows_evaluated) ? `${fmtInt(evaluation.rows_evaluated)} rows in the test set.` : null,
      yardstick ? `Yardstick: ${yardstick}` : null,
    ]
      .filter(Boolean)
      .map(esc)
      .join(" ")}</p>`,
  );
}

function modelPage(uc, run, art, byPath) {
  if (isScoring(run)) return scoringModelPage(uc, run, art);
  const best = art["best_model.json"];
  const evaluation = art["evaluation.json"];
  const baseline = art["baseline.json"];
  const importance = art["feature_importance.json"];
  const matrix = art["confusion_matrix.json"];
  const board = art["leaderboard.json"];
  const fairness = art["fairness.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;

  if (!evaluation && !best) {
    return shell(
      uc,
      "model",
      run,
      pendingCard("How good is this model?", {
        title: "The model's results appear when training finishes",
        text: "This run has not finished training. Open the use case to see its progress.",
      }, { label: "Go to Setup", href: `#/uc/${uc.id}`, kind: "secondary" }),
    );
  }

  const metrics = (evaluation && evaluation.metrics) || [];
  const recall = metrics.find((m) => m.id === "recall");
  const primary = evaluation && evaluation.primary_metric;
  const second = recall && primary !== "recall" ? recall : metrics.find((m) => m.id !== primary);
  const modelTiles = tiles([
    ["Model", dash((best && best.display_name) || run.best_model)],
    evaluation
      ? [metricShort(primary, evaluation.primary_metric_label), dash(evaluation.headline_score, fmtMetric)]
      : null,
    second
      ? second.id === "recall"
        ? ["Cases caught", fmtPct(second.value, 0), "At the cut-off, on the test set"]
        : [metricShort(second.id, second.label), fmtMetric(second.value)]
      : null,
    ["Trained", dash(run.finished_at, fmtDate)],
  ]);

  const setupRows = [
    ["Problem type", dash(uc.problem_type_label)],
    ["Target", dash(run.target)],
    ["Algorithm", dash((best && best.display_name) || run.best_model)],
    ["Hyperparameters", dash(best && best.hyperparameters_summary)],
    ["Validation", config ? `${readPath(config, "model_search.folds")}-fold cross-validation` : EM_DASH],
    [
      "Class imbalance",
      config ? choiceLabel(byPath, "model_search.imbalance", readPath(config, "model_search.imbalance")) : EM_DASH,
    ],
    [
      "Search strategy",
      config ? choiceLabel(byPath, "model_search.strategy", readPath(config, "model_search.strategy")) : EM_DASH,
    ],
    ["Calibration", dash(evaluation && evaluation.calibration && evaluation.calibration.method, humanise)],
    ["Decision threshold", dash(evaluation && evaluation.threshold_detail)],
    ["Training rows", dash(best && best.training_rows, fmtInt)],
    ["Last trained", dash(run.finished_at, fmtStamp)],
  ];

  // Collapsed by default below the default view; `bindPage` loads the plot the first time it opens.
  const shapOn = config ? readPath(config, "evaluation.shap") : undefined;
  const detailsCard = `<section class="card"><details class="bs-details" data-beeswarm="${esc(
    run.run_id,
  )}" data-shap="${esc(String(shapOn))}"><summary><span class="mt">Details</span><span class="ms">SHAP beeswarm: how the top 15 details push each ${esc(entityOf(uc))}'s score up or down</span></summary><div class="bs-body"><p class="caption">Loading…</p></div></details></section>`;

  const matrixBlock = matrix
    ? `<h4>Confusion matrix</h4><div class="cm">
          <span></span><span class="hd">Flagged</span><span class="hd">Not flagged</span>
          <span class="rl">Had the outcome</span><div class="cell good"><b>${fmtInt(
            matrix.true_positive,
          )}</b><small>Caught</small></div><div class="cell"><b>${fmtInt(
            matrix.false_negative,
          )}</b><small>Missed</small></div>
          <span class="rl">Did not</span><div class="cell"><b>${fmtInt(
            matrix.false_positive,
          )}</b><small>False alarm</small></div><div class="cell good"><b>${fmtInt(
            matrix.true_negative,
          )}</b><small>Correctly left out</small></div>
        </div><p class="caption">Test set, cut-off ${fmtNum(matrix.threshold, 2)}</p>`
    : "";

  const boardBlock = board
    ? `<h4>Models tried (${esc(fmtInt(board.models_trained))}, by ${esc(board.metric_label)})</h4>${table(
        ["rank", "model", "validation", "test", "fit (s)"],
        (board.entries || []).map((e) => [
          String(e.rank),
          e.model_name || e.family_label,
          fmtMetric(e.validation_score),
          dash(e.test_score, fmtMetric),
          fmtNum(e.fit_time_seconds, 2),
        ]),
      )}<p class="caption">Time limit ${esc(board.time_limit_seconds)}s · presets ${esc(board.presets)}</p>`
    : "";

  const hyper = best && best.hyperparameters ? Object.entries(best.hyperparameters) : [];
  const hyperBlock = hyper.length
    ? `<h4>Hyperparameters</h4>${kvs(hyper.map(([k, v]) => [k, typeof v === "number" ? fmtNum(v, 6) : String(v)]))}`
    : "";
  const importanceNote = importance
    ? `<h4>Importance method</h4>${kvs([["Method", dash(importance.caption || importance.method)]])}`
    : "";

  const technical = `<h4>Training setup</h4>${kvs(setupRows)}${matrixBlock}${boardBlock}${hyperBlock}${importanceNote}`;

  const fairnessCard =
    fairness && fairness.evaluated
      ? card(
          `Does it work equally well for each ${esc(columnWords(fairness.column).toLowerCase())}?`,
          `${dataTable(
            [
              { label: "Group" },
              { label: "Rows", num: true },
              { label: "With the outcome", num: true },
              { label: "Cases caught", num: true },
              { label: "Flags that are right", num: true },
            ],
            (fairness.groups || []).map((g) => [
              esc(g.value),
              esc(fmtInt(g.rows)),
              esc(fmtPct(g.positive_rate)),
              esc(dash(g.recall, (v) => fmtPct(v, 0))),
              esc(dash(g.precision, (v) => fmtPct(v, 0))),
            ]),
          )}<p class="caption">${esc(fairness.note)}</p>`,
        )
      : "";

  const body = `${evaluation ? verdictCard(uc, run, evaluation, byPath) : ""}
    ${baselineNotice(evaluation, baseline)}
    ${modelTiles}
    <div class="row rev">
      ${driversCard(uc, importance)}
      ${evaluation ? evaluationCard(evaluation, baseline) : ""}
    </div>
    ${detailsCard}
    ${fairnessCard}`;
  return shell(uc, "model", run, body, {
    tech: [["Model search", board && board.best_model_name]],
    techHtml: technical,
    stamp: false,
  });
}

/** A scoring run trains nothing: say which model it used and where that model's results are. */
function scoringModelPage(uc, run, art) {
  const drift = art["drift.json"];
  const trainingRun = drift && drift.baseline_run_id;
  const body = noticeCard({
    title: `This run used the ${run.best_model || "approved"} model`,
    text: [
      "A scoring run only applies a trained model, so there is nothing new to measure here.",
      "How well the model predicts was measured when it was trained.",
    ],
    action: trainingRun
      ? { label: "Open the training run's Model page", href: `#/uc/${uc.id}/model/${trainingRun}`, kind: "secondary" }
      : { label: `Back to ${uc.name}`, href: `#/uc/${uc.id}`, kind: "secondary" },
    attrs: "data-scoring-model",
  });
  return shell(uc, "model", run, body, { tech: [["Training run", trainingRun]] });
}

// --- Model: the Details beeswarm ---------------------------------------------------------------
// Every coordinate comes from `shap_beeswarm.json`: a dot's x is its contribution, its offset and
// its colour were laid out at explain time. This code only scales those numbers to the SVG's size.

const BS = { width: 960, label: 190, legend: 90, row: 28, top: 10, radius: 2.4, steps: 5 };

const bsLabel = (name) => (name.length > 28 ? `${name.slice(0, 27)}…` : name);

/** The colour class for one dot: five steps of one hue, low to high, grey when there is no value. */
const bsClass = (colour) =>
  colour === null || colour === undefined ? "na" : `c${Math.min(BS.steps - 1, Math.floor(colour * BS.steps))}`;

export function beeswarmHtml(bs) {
  const features = (bs && bs.features) || [];
  if (!features.length) {
    return `<div class="empty">${esc((bs && bs.caption) || "No beeswarm was drawn for this run.")}</div>`;
  }
  const x0 = BS.label + 12;
  const x1 = BS.width - BS.legend - 16;
  const span = bs.x_max - bs.x_min || 1;
  const px = (v) => x0 + ((v - bs.x_min) / span) * (x1 - x0);
  const bottom = BS.top + features.length * BS.row;
  const height = bottom + 52;
  const r = BS.radius;

  const paths = { na: [], c0: [], c1: [], c2: [], c3: [], c4: [] };
  const rows = features.map((item, i) => {
    const centre = BS.top + (i + 0.5) * BS.row;
    item.contributions.forEach((value, j) => {
      const x = px(value).toFixed(1);
      const y = (centre + item.offsets[j] * BS.row * 0.45).toFixed(1);
      paths[bsClass(item.colours[j])].push(`M${x} ${y}m${-r} 0a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`);
    });
    const tip = `${columnWords(item.feature)}: mean absolute contribution ${fmtNum(item.mean_abs_contribution, 6)} across ${fmtInt(
      item.contributions.length,
    )} rows${item.numeric ? "" : " (not numeric, drawn grey)"}`;
    return {
      grid: `<line class="bs-grid" x1="${x0}" x2="${x1}" y1="${centre}" y2="${centre}"/>`,
      label: `<text class="bs-lab" x="${BS.label}" y="${centre}" dy="0.35em" text-anchor="end">${esc(
        bsLabel(columnWords(item.feature)),
      )}</text>`,
      hit: `<rect class="bs-hit" x="0" y="${centre - BS.row / 2}" width="${x1}" height="${BS.row}"><title>${esc(
        tip,
      )}</title></rect>`,
    };
  });

  const ticks = (bs.ticks || [])
    .map(
      (t) =>
        `<line class="bs-tick" x1="${px(t)}" x2="${px(t)}" y1="${bottom}" y2="${bottom + 4}"/><text class="bs-num" x="${px(
          t,
        )}" y="${bottom + 18}" text-anchor="middle">${esc(fmtNum(t, 6))}</text>`,
    )
    .join("");
  const zero = bs.x_min <= 0 && bs.x_max >= 0 ? `<line class="bs-zero" x1="${px(0)}" x2="${px(0)}" y1="${BS.top}" y2="${bottom}"/>` : "";
  const dots = ["na", "c0", "c1", "c2", "c3", "c4"]
    .filter((k) => paths[k].length)
    .map((k) => `<path class="bs-${k}" d="${paths[k].join("")}"/>`)
    .join("");

  const lx = x1 + 34;
  const cell = 16;
  const ly = BS.top + 18;
  const legend = `<g class="bs-legend"><text class="bs-num" x="${lx + 5}" y="${ly - 6}" text-anchor="middle">High</text>${[4, 3, 2, 1, 0]
    .map((k, i) => `<rect class="bs-c${k}" x="${lx}" y="${ly + i * cell}" width="10" height="${cell}"/>`)
    .join("")}<text class="bs-num" x="${lx + 5}" y="${ly + 5 * cell + 14}" text-anchor="middle">Low</text><text class="bs-num" transform="translate(${
    lx + 26
  } ${ly + (5 * cell) / 2}) rotate(-90)" text-anchor="middle">Feature value</text></g>`;

  const aria = `Beeswarm of ${bs.method || "measured"} contributions for ${fmtInt(bs.rows_sampled)} rows across ${
    features.length
  } features`;
  return `<div class="bswarm"><svg viewBox="0 0 ${BS.width} ${height}" role="img" aria-label="${esc(aria)}">
      ${rows.map((row) => row.grid).join("")}${zero}${dots}${rows.map((row) => row.label).join("")}
      <line class="bs-axis" x1="${x0}" x2="${x1}" y1="${bottom}" y2="${bottom}"/>${ticks}
      <text class="bs-num" x="${(x0 + x1) / 2}" y="${bottom + 40}" text-anchor="middle">${esc(bs.x_label)}</text>
      ${legend}${rows.map((row) => row.hit).join("")}
    </svg></div><p class="caption">${esc(bs.caption)}</p>`;
}

/** What the Details section says when the run has no beeswarm, worded by why it has none. */
function noBeeswarm(shap) {
  return shap === "false"
    ? "Per-customer reasons are turned off for this use case, so this run has no beeswarm."
    : "This run has no beeswarm. Runs trained before it was added have none; train again to see it.";
}

/**
 * Wire a painted page: on the Model page, load the beeswarm the first time Details is opened.
 * `api.js` is imported here rather than at the top: it reads `window` when it loads, and this file
 * must stay importable by node's unit tests.
 */
export function bindPage(kind, root) {
  if (kind !== "model") return;
  root.querySelectorAll("details[data-beeswarm]").forEach((details) => {
    let loaded = false;
    details.addEventListener("toggle", async () => {
      if (!details.open || loaded) return;
      loaded = true;
      const body = details.querySelector(".bs-body");
      try {
        const { getArtefact } = await import("./api.js");
        const bs = await getArtefact(details.dataset.beeswarm, BEESWARM_ARTEFACT);
        body.innerHTML = bs ? beeswarmHtml(bs) : `<div class="empty">${esc(noBeeswarm(details.dataset.shap))}</div>`;
      } catch (error) {
        loaded = false;
        body.innerHTML = errorBox(error);
      }
    });
  });
}

// --- Output ------------------------------------------------------------------------------------

const DOWNLOAD_ICON =
  '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 2v8m0 0 3.5-3.5M8 10 4.5 6.5M3 13h10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/** "Settings used for this run", closed, worded from help.yaml; rows that cannot apply are left out. */
function settingsCard(uc, run, config, byPath, summary, drift) {
  if (!config) return "";
  const scoring = isScoring(run);
  const bands = readPath(config, "actions.bands") || [];
  const control = readPath(config, "actions.control_group_fraction");
  const suppression = (key) => readPath(config, `actions.suppression.${key}`);
  const rules = [];
  if (suppression("suppress_opted_out")) {
    rules.push(
      suppression("opt_out_column")
        ? "Customers who opted out of marketing"
        : "Opted-out customers (not applied: the data has no opt-out column)",
    );
  }
  if (suppression("suppress_recently_contacted")) {
    const days = suppression("recently_contacted_days");
    rules.push(
      suppression("recently_contacted_column")
        ? `Customers contacted in the last ${days} days`
        : `Recently contacted customers (not applied: the data has no last-contact column)`,
    );
  }
  const retention = readPath(config, "governance.retention_days");
  const psi = readPath(config, "monitoring.drift_psi_threshold");
  const drop = readPath(config, "monitoring.performance_alert_drop_pct");
  const retraining = readPath(config, "monitoring.retraining");
  const retrainingMeaning = (glossarySetting("monitoring.retraining") || {}).meaning || "";
  const retrainingInactive = !retrainingMeaning || /later release/i.test(retrainingMeaning);
  const rows = [
    [
      "Customer groups",
      bands.length
        ? bands.map((b) => `${b.name} from ${fmtNum(b.min_score, 2)}: ${b.action}`).join(" · ")
        : EM_DASH,
    ],
    present(control) ? ["Held back to measure results", `${fmtPct(control, 0)} of eligible customers, at random`] : null,
    ["Never contacted", rules.length ? rules.join(" · ") : "Nobody is excluded by a rule"],
    scoring && summary && summary.rows_with_fallback_reasons !== undefined
      ? [
          "Reasons for each score",
          summary.rows_with_fallback_reasons
            ? `${fmtInt(summary.rows_with_fallback_reasons)} of ${fmtInt(summary.rows_scored)} have general reasons only`
            : "Worked out for every customer",
        ]
      : null,
    present(psi) ? ["Warn when customers change", `When they move far from the training data (limit ${fmtNum(psi, 2)})`] : null,
    scoring && drift ? ["Change measured", `${humanise(drift.status || "")}`.trim() || EM_DASH] : null,
    present(retraining)
      ? [
          "Retraining",
          retrainingInactive
            ? `Not active yet (set to ${choiceLabel(byPath, "monitoring.retraining", retraining)})`
            : choiceLabel(byPath, "monitoring.retraining", retraining),
        ]
      : null,
    present(drop) ? ["Performance alert", `When results fall by more than ${fmtNum(drop, 1)}%`] : null,
    present(retention) ? ["Data kept for", `${fmtInt(retention)} days`] : null,
  ].filter(Boolean);
  const tech = kvs(
    [
      ["Score field", dash(readPath(config, "actions.score_field"))],
      ["Drift alert", present(psi) ? `PSI above ${psi}` : EM_DASH],
      drift ? ["Drift measured", `${humanise(drift.status)} · max PSI ${fmtNum(drift.max_psi, 3)}`] : null,
    ].filter(Boolean),
  );
  return card(
    "",
    `<div class="card-body"><details class="tech" data-settings><summary>Settings used for this run</summary>${kvs(
      rows,
    )}<h4>Technical</h4>${tech}</details></div>`,
    "data-pg-tech",
  );
}

/** The score as a column heading: `churn_prob` → "Churn likelihood". */
function scoreHeading(uc, field) {
  if (uc && uc.problem_type && uc.problem_type !== "binary_classification") return "Predicted value";
  const stem = String(field || "")
    .replace(/_?(prob|probability|score)(_.*)?$/i, "")
    .replace(/_/g, " ")
    .trim();
  return stem ? `${capital(stem)} likelihood` : "Likelihood";
}

const fmtValue = (value) => {
  const n = Number(value);
  return present(value) && String(value).trim() !== "" && Number.isFinite(n) ? fmtNum(n, 2) : String(value);
};

/** A row's main reason in words: "Days since last activity is 240 (raises it)". */
function reasonText(reason) {
  if (!reason) return EM_DASH;
  if (!reason.feature) return reason.text || EM_DASH;
  const direction = reason.direction === "up" ? " (raises it)" : reason.direction === "down" ? " (lowers it)" : "";
  const value = present(reason.value) ? ` is ${fmtValue(reason.value)}` : "";
  return `${columnWords(reason.feature)}${value}${direction}`;
}

const ACTION_WORDS = { "Control (hold out)": "Held back (control group)" };
const actionText = (row) =>
  row.suppressed_reason
    ? `Not contacted (${humanise(row.suppressed_reason).toLowerCase()})`
    : ACTION_WORDS[row.action] || row.action || EM_DASH;

/** The key's first column is the customer; a two-column key's date goes in "Show more columns". */
const customerOf = (key) => String(key).split("|")[0];

function sampleCard(uc, run, summary) {
  const sample = (summary && summary.sample_rows) || [];
  const entity = entityOf(uc);
  if (!sample.length) return "";
  const binary = !uc.problem_type || uc.problem_type === "binary_classification";
  const rows = sample.map((row) => [
    esc(customerOf(row.primary_key)),
    esc(binary ? fmtPct(row.score, 0) : fmtNum(row.score, 2)),
    bandPill(row.band),
    esc(reasonText((row.reasons || [])[0])),
    esc(actionText(row)),
    `<code class="mono">${esc(row.primary_key)}</code>`,
    esc(fmtNum(row.score, 4)),
  ]);
  return card(
    `A sample of ${fmtInt(sample.length)} ${esc(plural(entity))}${sortNote("in file order")}`,
    `${dataTable(
      [
        { label: capital(entity) },
        { label: `${scoreHeading(uc, summary.score_field)}${binary ? " %" : ""}`, num: true },
        { label: "Band" },
        { label: "Main reason" },
        { label: "Action" },
        { label: `Record key (${keyText(run.primary_key)})`, more: true },
        { label: `Score (${summary.score_field})`, more: true },
      ],
      rows,
    )}<p class="caption">Every ${esc(entity)} is in the contact list download, with up to three reasons each.</p>`,
  );
}

/** Band names a KPI formula counts (`count_where_band_in(["High","Medium"])`), or null. */
function kpiBands(kpi) {
  const match = /count_where_band_in\(\s*\[(.*?)\]\s*\)/.exec((kpi && kpi.formula) || "");
  return match ? [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]) : null;
}

function scoringOutputPage(uc, run, art, byPath, scoresHref) {
  const summary = art["scoring_summary.json"];
  const drift = art["drift.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;
  const entity = entityOf(uc);

  if (!summary) {
    return shell(
      uc,
      "output",
      run,
      card(
        "Who to contact",
        emptyState({
          title: "No contact list yet",
          text: "The contact list is written when a scoring run finishes. Score new customers to get one.",
          action: { label: "Go to Setup", href: `#/uc/${uc.id}` },
        }),
      ),
    );
  }

  const bands = summary.bands || [];
  const counted = kpiBands(summary.kpi);
  const kpiNote = counted
    ? counted
        .map((name) => {
          const band = bands.find((b) => b.name === name);
          return band ? `${name} ${fmtInt(band.rows)}` : null;
        })
        .filter(Boolean)
        .join(" + ")
    : null;
  const held = summary.control_group_rows;
  const outputTiles = tiles([
    [
      (summary.kpi && summary.kpi.label) || (uc.output && uc.output.kpi && uc.output.kpi.label) || "Customers to contact",
      // A count reads exactly ("1,675"), not the engine's rounded display ("2K").
      summary.kpi && Number.isInteger(summary.kpi.value) ? fmtInt(summary.kpi.value) : dash(summary.kpi && summary.kpi.display),
      kpiNote,
    ],
    ["Customers scored", dash(summary.rows_scored, fmtInt)],
    [
      "Held back to measure results",
      present(held)
        ? `${fmtInt(held)}${summary.rows_scored ? ` (${fmtPct(held / summary.rows_scored, 0)})` : ""}`
        : EM_DASH,
      "Chosen at random, not contacted",
    ],
  ]);

  const status = summary.drift_status || (drift && drift.status);
  const driftEntry = status && status !== "stable" ? codeEntry(`DRIFT_${String(status).toUpperCase()}`) : null;
  const driftNotice = driftEntry
    ? noticeCard({
        title: driftEntry.title,
        text: [driftEntry.meaning, driftEntry.fix].filter(Boolean),
        attrs: `data-code="DRIFT_${esc(String(status).toUpperCase())}"`,
      })
    : "";

  const actions = (summary.actions || []).slice().sort((a, b) => b.rows - a.rows);
  const suppressed = summary.suppressed || [];
  const bandsCard = card(
    `Bands &amp; actions${sortNote("highest scores first")}`,
    `${dataTable(
      [{ label: "Band" }, { label: "Suggested action" }, { label: capital(plural(entity)), num: true }, { label: "Share", num: true }],
      bands.map((b) => [bandPill(b.name), esc(b.action), esc(fmtInt(b.rows)), esc(`${fmtNum(b.share_pct, 1)}%`)]),
    )}${
      actions.length
        ? `<h4>What happens on the list</h4>${dataTable(
            [{ label: "Action" }, { label: capital(plural(entity)), num: true }, { label: "Share", num: true }],
            actions.map((a) => [esc(ACTION_WORDS[a.action] || a.action), esc(fmtInt(a.rows)), esc(`${fmtNum(a.share_pct, 1)}%`)]),
          )}<p class="caption">${
            present(held) ? `${esc(fmtInt(held))} ${esc(plural(entity))} are held back at random, whatever their band, so the campaign's effect can be measured. ` : ""
          }${suppressed.length ? "" : "Nobody was left out by a contact rule."}</p>`
        : ""
    }${
      suppressed.length
        ? `<h4>Left out by a contact rule</h4>${dataTable(
            [{ label: "Why" }, { label: capital(plural(entity)), num: true }],
            suppressed.map((s) => [esc(humanise(s.reason)), esc(fmtInt(s.rows))]),
          )}`
        : ""
    }`,
  );

  const body = `${outputTiles}
    ${driftNotice}
    ${bandsCard}
    ${sampleCard(uc, run, summary)}
    ${settingsCard(uc, run, config, byPath, summary, drift)}`;
  return shell(uc, "output", run, body, {
    primary: `<a class="btn primary" href="${esc(scoresHref)}" data-download="scores">${DOWNLOAD_ICON}Download contact list (CSV)</a>`,
    tech: [["Training run", drift && drift.baseline_run_id]],
  });
}

function trainingOutputPage(uc, run, art, byPath) {
  const lift = art["decile_lift.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;
  const entity = entityOf(uc);
  const values = (lift && lift.values) || [];
  const bins = (lift && lift.bins) || [];
  const unit = (lift && lift.unit) || "";
  const times = unit === "x" ? "×" : unit;

  let liftTiles = "";
  if (values.length) {
    const top = values[0];
    const sentence =
      unit === "x"
        ? `The 10% of ${plural(entity)} with the highest scores had the outcome ${fmtNum(top, 1)} times as often as a random 10%.`
        : `The 10% of ${plural(entity)} with the highest scores averaged ${fmtNum(top, 1)}${unit}.`;
    liftTiles = tiles([[unit === "x" ? "Lift in the top 10%" : "Top 10% of scores", `${fmtNum(top, 1)}${times}`, sentence]]).replace(
      'class="kpis"',
      'class="kpis pg-one" data-lift',
    );
  }
  const term = termText("lift");

  const maxValue = values.length ? Math.max(...values) : 0;
  const chart = values.length
    ? `<div class="vchart">${values
        .map((v, i) => {
          const bin = bins[i] || {};
          const top = i < 3;
          return `<div class="vcol ${top ? "top" : ""}"><span class="bv">${top ? `${fmtNum(v, 1)}${esc(times)}` : ""}</span>${columnBar(
            maxValue ? (160 * v) / maxValue : 0,
            top,
            `${bin.label || `D${i + 1}`} ${fmtNum(v, 1)}${times}`,
          )}<span class="bl">${esc(bin.label || `D${i + 1}`)}</span></div>`;
        })
        .join("")}</div><p class="caption">${esc(
        `D1 is the 10% of ${plural(entity)} with the highest scores, D10 the lowest. Measured on the test set.`,
      )}${term ? ` ${esc(term)}` : ""}</p>`
    : emptyState({
        title: "The lift chart appears when training finishes",
        text: "It is measured on the test set at the end of training.",
      });

  const body = `${liftTiles}
    <p class="pg-p muted">${esc(`How many ${plural(entity)} to contact appears after you score new customers with this model.`)}</p>
    ${card(esc(unit === "x" || !lift ? "Lift by score decile" : "Actual rate by score decile"), chart)}
    ${settingsCard(uc, run, config, byPath, null, null)}`;
  return shell(uc, "output", run, body, {
    primary: { label: "Score new customers with this model", href: `#/uc/${uc.id}`, kind: "primary" },
  });
}

function outputPage(uc, run, art, byPath, scoresHref) {
  if (isScoring(run) && seams && seams.setActiveNav) {
    try {
      seams.setActiveNav("campaigns");
    } catch {
      /* the top bar is cosmetic here */
    }
  }
  return isScoring(run)
    ? scoringOutputPage(uc, run, art, byPath, scoresHref)
    : trainingOutputPage(uc, run, art, byPath);
}

// --- Uplift runs (M53) ----------------------------------------------------------------------------
//
// An uplift run's Data page is drawn here (treatment and control instead of the feature pipeline);
// its Model and Output are the uplift module's own screens, so `#/uc/<uc>/{model,output}/<run>` of an
// uplift run redirects to `#/uplift/<uc>/{model,output}/<run>` (docs/UI_AUDIT.md C14).

const NOT_CAUSAL =
  "Not causal: the treatment was not randomly assigned, so these numbers describe who was contacted, not what contacting them changed.";

const fmtShare = (v) => dash(v, (x) => fmtPct(x, 1));

/** Treated or control: in customers and rows for a two-column key, in rows otherwise. */
function armLine(report, arm) {
  if (!report || !present(report[`${arm}_rows`])) return EM_DASH;
  const rows = fmtInt(report[`${arm}_rows`]);
  const entities = report[`${arm}_entities`];
  return present(entities) ? `${fmtInt(entities)} customers (${rows} rows)` : `${rows} rows`;
}

/** The arm count a tile shows: customers for a two-column key, rows otherwise. */
function armCount(report, arm) {
  if (!report) return EM_DASH;
  return present(report[`${arm}_entities`])
    ? fmtInt(report[`${arm}_entities`])
    : dash(report[`${arm}_rows`], fmtInt);
}

function treatedShare(report) {
  if (!report || !present(report.treated_rows) || !present(report.control_rows)) return EM_DASH;
  const total = report.treated_rows + report.control_rows;
  return total ? fmtPct(report.treated_rows / total) : EM_DASH;
}

/** The randomness check in words: its AUC against the limit, and whether a failure was acknowledged. */
function randomnessLine(report, config) {
  if (!report) return EM_DASH;
  const limit = config ? readPath(config, "uplift.randomness_auc_max") : null;
  const finding = (report.checks || []).find((c) => c.code === "TREATMENT_NOT_RANDOM");
  if (!present(report.randomness_auc)) return finding ? "Failed" : "Not measured";
  const auc = `AUC ${fmtNum(report.randomness_auc, 2)}${present(limit) ? `, limit ${fmtNum(limit, 2)}` : ""}`;
  if (!finding) return `Passed (${auc})`;
  return finding.acknowledged ? `Targeted, acknowledged (${auc})` : `Targeted (${auc})`;
}

function driftRows(drift) {
  const t = drift.treatment || {};
  const features = drift.features
    ? `${humanise(drift.features.status)} · max PSI ${fmtNum(drift.features.max_psi, 3)}`
    : dash(drift.features_reason);
  const share =
    t.status === "not_applicable"
      ? dash(t.reason)
      : `${fmtShare(t.current_treated_share)} now, ${fmtShare(t.training_treated_share)} in training · ${humanise(
          t.status,
        )} (tolerance ${fmtNum(t.tolerance, 2)})`;
  return [
    ["Feature drift", features],
    ["Treated share", share],
  ];
}

/** `uplift_drift.json`, on a scoring run's Data page; nothing on a training run. */
function driftCard(drift, scoring) {
  if (!scoring && !drift) return "";
  const drifted = drift && drift.features ? drift.features.drifted_features || [] : [];
  const body = drift
    ? `${kvs(driftRows(drift))}${drifted.length ? `<p class="caption">Drifted: ${esc(drifted.join(", "))}</p>` : ""}`
    : `<div class="empty">The comparison with the training data appears when this scoring run finishes.</div>`;
  return card("Drift against the training data", body);
}

function upliftDataPage(uc, run, art, byPath, extra) {
  const profile = art["profile.json"];
  const split = art["split.json"];
  const uv = art["uplift_validation.json"];
  const drift = art["uplift_drift.json"];
  const config = art["run_config.json"] && art["run_config.json"].config;
  const scoring = isScoring(run);

  const upliftTiles = tiles([
    ["Rows", dash(profile && profile.row_count, fmtInt)],
    ["Treated", armCount(uv, "treated")],
    ["Control", armCount(uv, "control")],
    ["Randomness", uv && present(uv.randomness_auc) ? `AUC ${fmtNum(uv.randomness_auc, 2)}` : EM_DASH],
  ]);

  const fileRows = profile
    ? [
        ["Rows", fmtInt(profile.row_count)],
        ["Columns", String(profile.column_count)],
        ["Key", keyText(run.primary_key)],
        [uc.target.label || "Outcome column", dash(run.target)],
      ]
    : [["Rows", EM_DASH]];

  const parts = ((split && split.parts) || []).filter((p) => p.rows);
  const experimentRows = [
    ["Treatment column", dash(uv && uv.treatment_column)],
    ["Treated", armLine(uv, "treated")],
    ["Control", armLine(uv, "control")],
    ["Treated share", treatedShare(uv)],
    ["Randomness check", randomnessLine(uv, config)],
    ["Causal", uv ? (uv.causal ? "Yes" : "No") : EM_DASH],
    ["Rows set aside (outcome not final)", dash(uv && uv.rows_immature, fmtInt)],
    [
      "Train / test",
      parts.length
        ? `${parts.map((p) => `${p.name} ${fmtInt(p.rows)}`).join(" · ")}${
            split.group_column ? ` · grouped by ${split.group_column}` : ""
          }`
        : EM_DASH,
    ],
  ];

  const checks = (uv && uv.checks) || [];
  const checksBody = uv
    ? checks.length
      ? dataTable(
          [{ label: "Finding" }, { label: "Severity" }, { label: "What it means" }, { label: "Code", more: true }],
          checks.map((c) => {
            const entry = codeEntry(c.code);
            return [
              esc(entry && entry.title ? entry.title : humanise(String(c.code).toLowerCase())),
              esc(`${humanise(c.severity)}${c.acknowledged ? " (acknowledged)" : ""}`),
              esc(c.message),
              `<code class="mono" data-code="${esc(c.code)}">${esc(c.code)}</code>`,
            ];
          }),
        )
      : `<div class="empty">Every uplift check passed.</div>`
    : `<div class="empty">${
        scoring
          ? "A scoring run is not checked as an experiment; its treated share is compared with training's in the drift card."
          : "The experiment checks appear when this training run has read its data."
      }</div>`;

  const body = `${upliftTiles}
    ${sourceCard(run, profile, extra)}
    <div class="row">
      ${card("Input dataset", kvs(fileRows))}
      ${card(
        "Treatment &amp; control",
        `${kvs(experimentRows)}${uv && uv.causal === false ? `<p class="caption">${esc(NOT_CAUSAL)}</p>` : ""}`,
      )}
    </div>
    ${card("Uplift checks", checksBody)}
    ${driftCard(drift, scoring)}`;
  return shell(uc, "data", run, body, { techHtml: fileDetails(profile) });
}

/**
 * `#/uc/<uc>/{model,output}/<uplift run>`: the uplift module's own screen is the one page for it. The
 * notice is what shows for the moment before the hash is replaced (and where there is no browser).
 */
function upliftRedirect(uc, kind, run) {
  const href = upliftHref(kind, uc, run);
  if (typeof window !== "undefined" && window.location) {
    const from = window.location.hash;
    setTimeout(() => {
      if (window.location.hash === from) window.location.replace(href);
    }, 0);
  }
  return `<main class="screen t-${esc(uc.marker)}" data-redirect="${esc(href)}">${pageHead(
    `<nav class="crumbs" aria-label="Breadcrumb">${journeyCrumb(uc)}${CRUMB_SEP}<a href="#/uc/${esc(uc.id)}">${esc(
      uc.name,
    )}</a>${CRUMB_SEP}<span class="cur" aria-current="page">${esc(TAB_LABEL[kind])}</span></nav><h1 class="h1">${esc(
      uc.pages[kind],
    )}</h1>`,
  )}<div class="stack">${noticeCard({
    title: "This is an uplift model",
    text: "Its results are shown with the other uplift models.",
    action: { label: `Open its ${TAB_LABEL[kind]} page`, href, kind: "primary" },
  })}</div></main>`;
}

/** A problem type's label: the use case's own, a Setup choice's, or the id in words. */
export function problemTypeLabel(uc, value) {
  if (!present(value)) return EM_DASH;
  if (uc && value === uc.problem_type && uc.problem_type_label) return uc.problem_type_label;
  const choices = (uc && uc.setup && uc.setup.problem_type_choices) || [];
  const choice = choices.find((c) => c.value === value);
  return choice && choice.label ? choice.label : humanise(value);
}

// --- entry point --------------------------------------------------------------------------------

/**
 * `extra` carries what is not a run artefact: the Data page's `lineage` (or `lineageError`).
 */
export function renderPage(kind, uc, run, art, scoresHref, extra = {}) {
  const byPath = indexSchema(uc.advanced_settings || { stages: [] });
  if (isUplift(run)) {
    return kind === "data" ? upliftDataPage(uc, run, art, byPath, extra) : upliftRedirect(uc, kind, run);
  }
  if (kind === "data") return dataPage(uc, run, art, byPath, extra);
  if (kind === "model") return modelPage(uc, run, art, byPath);
  return outputPage(uc, run, art, byPath, scoresHref);
}
