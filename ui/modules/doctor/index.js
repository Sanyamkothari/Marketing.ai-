// Data Doctor, the place (Plan L M151, DEC-1513): `#/doctor`, "Bring your data".
//
// The person drops whatever files they have, before choosing anything. Each file is uploaded the
// ordinary way (`POST /uploads`, so intake reads it and every limit, retention rule and erasure is an
// upload's), then `POST /doctor/diagnosis` says what each file is and what the files can power. A use
// case that is ready, or ready after a check, has a "Start" button: `POST /doctor/handoff` makes the
// file an upload of that use case and its Guided setup opens on it (`handoff.js`). Home links here
// through its "For you" line (`registerForYou`), so no shared screen is edited.
//
// Two more answers come with the diagnosis. How the files fit (M155): how each log's IDs meet the
// customer list, with the fix in plain words, and which files are the same report for different months.
// And, for a use case no file has the outcome of, the outcome as a sentence the person can agree to
// (M157): the choices are radio buttons, each with what it would count, and nothing is recorded until
// the person presses "Use this outcome" (`POST /doctor/outcome`, the sentence sent word for word).
//
// Classes are prefixed `dd-`; the stylesheet is injected once, tokens only.

import { API_BASE, ApiError, getIndustries, postUpload } from "../../api.js";
import { crumbs, errorBox, esc, fmtInt, pageHead } from "../../dom.js";
import { registerForYou } from "../../overview.js";
import { TABLE_FILE_TYPES, intakeNotesHtml } from "../intake/notes.js";
import { registerModule } from "../router.js";
import { handOff } from "./handoff.js";

const CSS = `
.dd .dd-drop{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.dd .dd-files{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.dd .dd-file h4{margin:0 0 4px;font-size:14px;overflow-wrap:anywhere}
.dd .dd-file dl{margin:8px 0 0;display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 12px;font-size:13px}
.dd .dd-file dt{color:var(--muted)}
.dd .dd-file dd{margin:0;color:var(--ink);overflow-wrap:anywhere}
.dd .dd-uc{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;align-items:start;padding:12px 0;border-top:1px solid var(--line)}
.dd .dd-uc:first-of-type{border-top:0}
.dd .dd-uc h4{margin:0;font-size:14px}
.dd .dd-uc p{margin:2px 0 0;font-size:13px;color:var(--ink2);line-height:1.5}
.dd .dd-uc ul{margin:4px 0 0;padding-left:18px;font-size:13px;color:var(--ink2)}
.dd .dd-stage{font-size:12px;color:var(--muted)}
.dd .dd-fit{padding:10px 0;border-top:1px solid var(--line);font-size:13px;color:var(--ink2);line-height:1.5}
.dd .dd-fit:first-of-type{border-top:0}
.dd .dd-fit ul{margin:4px 0 0;padding-left:18px}
.dd .dd-outcome{margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--soft)}
.dd .dd-outcome legend{font-weight:600;font-size:13px;color:var(--ink)}
.dd .dd-outcome fieldset{border:0;margin:0;padding:0}
.dd .dd-choice{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 8px;margin:8px 0;font-size:13px}
.dd .dd-choice small{grid-column:2;color:var(--muted)}
.dd .dd-outcome .fhint{margin-top:6px}
`;

let injected = false;
function injectStyles() {
  if (injected || typeof document === "undefined") return;
  injected = true;
  const style = document.createElement("style");
  style.id = "doctor-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}

async function call(path, body) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", `Could not reach the API at ${API_BASE}.`, null);
  }
  const parsed = await response.json().catch(() => null);
  if (response.ok) return parsed;
  const detail = parsed && parsed.detail;
  throw new ApiError(
    response.status,
    (detail && detail.code) || `HTTP_${response.status}`,
    (detail && detail.message) || `The API answered ${response.status}.`,
    parsed,
  );
}

export const postDiagnosis = (uploadIds) => call("/doctor/diagnosis", { upload_ids: uploadIds });
export const postHandoff = (uploadId, useCase) => call("/doctor/handoff", { upload_id: uploadId, use_case: useCase });
export const postOutcome = (body) => call("/doctor/outcome", body);

/** The page's state: survives a repaint, not a reload. */
const state = {
  phase: "empty", // empty | reading | diagnosing | done
  reading: [], // file names being uploaded
  uploads: [], // [{ upload_id, name, intake }]
  errors: [], // [{ name, error }]
  diagnosis: null,
  diagnosisError: null,
  starting: "", // the use case a hand-off is being made for
  startError: null,
  picked: {}, // use case id -> the outcome window (days) the person picked
  agreeing: "", // the use case an outcome is being agreed for
  agreed: {}, // use case id -> the agreement `POST /doctor/outcome` answered
  agreeError: null,
};

/** The use case each file is uploaded under before one is chosen: Home's first trainable card. */
async function firstUseCase() {
  const payload = await getIndustries();
  const industry =
    (payload.industries || []).find((item) => item.id === payload.default_industry) || (payload.industries || [])[0];
  for (const stage of (industry && industry.stages) || []) {
    for (const card of stage.use_cases || []) {
      if (card.status === "available" && card.ai_type !== "generative") return card.id;
    }
  }
  throw new ApiError(0, "DOCTOR_NO_USE_CASES", "There is no use case to read the files for.", null);
}

const STATUS = {
  ready: { title: "Ready", pill: "ok" },
  check: { title: "Ready after a quick check", pill: "warn" },
  too_small: { title: "Not enough data yet", pill: "warn" },
  missing: { title: "Missing something", pill: "" },
};

function fileHtml(file, uploaded) {
  const rows = [
    ["What it looks like", file.kind_label],
    ["Size", `${fmtInt(file.rows)} rows, ${fmtInt(file.columns)} columns`],
    [
      "One row per",
      file.id_column
        ? file.rows_per_id && file.rows_per_id > 1.05
          ? `${file.id_column} appears on about ${file.rows_per_id} rows each`
          : `${file.id_column}`
        : "No ID column found",
    ],
    ["Dates", file.date_columns.length ? file.date_columns.join(", ") : "None found"],
  ];
  if (file.personal_columns) rows.push(["Personal data", `${file.personal_columns} column(s), hidden from every AI`]);
  if (file.fixable_columns) rows.push(["Fixable", `${file.fixable_columns} column(s) Guided setup can tidy`]);
  return `<section class="card dd-file" data-dd-file="${esc(file.upload_id)}"><h4>${esc(file.file_name)}</h4>${
    file.kind_reason ? `<div class="fhint">${esc(file.kind_reason)}</div>` : ""
  }<dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>${intakeNotesHtml(
    uploaded ? uploaded.intake : null,
  )}</section>`;
}

function pct(share) {
  if (share === null || share === undefined) return "";
  const value = share * 100;
  if (value > 99 && value < 100) return "over 99%";
  if (value > 0 && value < 1) return "under 1%";
  return `${Math.round(value)}%`;
}

/** How the files fit together (M155): each log against the customer list, and monthly copies of one report. */
export function fitHtml(d) {
  const links = d.links || [];
  const stacks = d.stacks || [];
  if (!links.length && !stacks.length) return "";
  const linkRows = links.map((link) => {
    const good = link.coverage >= 0.8;
    const fixed = link.fix && link.fixed_coverage !== null && link.fixed_coverage !== undefined;
    return `<div class="dd-fit" data-dd-link="${esc(link.upload_id)}"><strong>${esc(link.file_name)}</strong> → ${esc(
      link.list_file_name,
    )} <span class="pill ${good ? "ok" : "warn"}">${esc(pct(link.coverage))} match</span>${
      fixed ? ` <span class="pill ok">${esc(pct(link.fixed_coverage))} after the fix</span>` : ""
    }<div>${esc(link.summary)}</div>${link.sampled ? `<div class="fhint">Measured on the rows read, not the whole file.</div>` : ""}</div>`;
  });
  const stackRows = stacks.map((stack) => {
    const drift = (stack.drift || [])
      .map((item) => {
        const parts = [];
        if (item.renamed && item.renamed.length)
          parts.push(`renamed ${item.renamed.map((r) => `${r.before} → ${r.after}`).join(", ")}`);
        if (item.added && item.added.length) parts.push(`new: ${item.added.join(", ")}`);
        if (item.missing && item.missing.length) parts.push(`missing: ${item.missing.join(", ")}`);
        return parts.length ? `<li>${esc(item.file_name)}: ${esc(parts.join("; "))}</li>` : "";
      })
      .join("");
    return `<div class="dd-fit" data-dd-stack><strong>${esc(stack.file_names.join(", "))}</strong> <span class="pill">same report</span><div>${esc(
      stack.summary,
    )}</div>${drift ? `<ul>${drift}</ul>` : ""}</div>`;
  });
  return `<section class="card" data-dd-fit><h3>How your files fit together</h3>${[...linkRows, ...stackRows].join("")}</section>`;
}

/** A use case's outcome built from events (M157): the sentence with its choices, agreed explicitly. */
export function outcomeHtml(item) {
  const offer = item.outcome_offer;
  if (!offer || !offer.choices || !offer.choices.length) return "";
  const agreement = state.agreed[item.use_case_id];
  if (agreement) {
    return `<div class="dd-outcome" data-dd-agreed="${esc(item.use_case_id)}"><strong>Outcome agreed:</strong> ${esc(
      agreement.sentence,
    )}<div class="fhint">${esc(agreement.choice.summary)}</div><ul>${(agreement.next_steps || [])
      .map((step) => `<li>${esc(step)}</li>`)
      .join("")}</ul></div>`;
  }
  const fallback = offer.choices.find((c) => c.default) || offer.choices[0];
  const picked = state.picked[item.use_case_id] || fallback.days;
  const busy = state.agreeing === item.use_case_id;
  const name = `dd-outcome-${item.use_case_id}`;
  const choices = offer.choices
    .map(
      (choice) =>
        `<label class="dd-choice"><input type="radio" name="${esc(name)}" value="${esc(choice.days)}" data-dd-pick="${esc(
          item.use_case_id,
        )}"${choice.days === picked ? " checked" : ""}${choice.customers ? "" : " disabled"}><span>${esc(
          choice.sentence,
        )}</span><small>${esc(choice.summary)}</small></label>`,
    )
    .join("");
  const usable = offer.choices.some((c) => c.days === picked && c.customers);
  return `<div class="dd-outcome" data-dd-outcome="${esc(item.use_case_id)}"><fieldset><legend>${esc(
    offer.question,
  )}</legend><div class="fhint">From ${esc(offer.event_file_name)} (${esc(offer.event_id_column)}, ${esc(
    offer.date_column,
  )}) and the customers in ${esc(offer.entity_file_name)}.</div>${choices}</fieldset><div class="fhint">${esc(
    offer.censoring,
  )}</div>${(offer.notes || []).map((note) => `<div class="fhint">${esc(note)}</div>`).join("")}<button type="button" class="btn secondary sm" data-dd-agree="${esc(
    item.use_case_id,
  )}"${busy || !usable || state.agreeing ? " disabled" : ""}>${busy ? "Saving…" : "Use this outcome"}</button>${
    state.agreeError && state.agreeError.useCase === item.use_case_id ? errorBox(state.agreeError.error) : ""
  }</div>`;
}

function useCaseHtml(item) {
  const status = STATUS[item.status] || STATUS.missing;
  const startable = (item.status === "ready" || item.status === "check") && item.upload_id;
  const busy = state.starting === item.use_case_id;
  const button = startable
    ? `<button type="button" class="btn ${item.status === "ready" ? "primary" : "secondary"} sm" data-dd-start="${esc(
        item.use_case_id,
      )}" data-dd-upload="${esc(item.upload_id)}"${state.starting ? " disabled" : ""}>${busy ? "Opening…" : `Start ${esc(item.name)}`}</button>`
    : "";
  return `<div class="dd-uc" data-dd-usecase="${esc(item.use_case_id)}" data-dd-status="${esc(item.status)}"><div><h4>${esc(
    item.name,
  )} <span class="pill ${status.pill}">${esc(status.title)}</span></h4><div class="dd-stage">${esc(item.stage)}</div><p>${esc(
    item.summary,
  )}</p>${item.needs && item.needs.length ? `<ul>${item.needs.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}${outcomeHtml(
    item,
  )}</div>${button}</div>`;
}

export function doctorHtml() {
  injectStyles();
  const head = pageHead(
    `${crumbs([{ label: "Home", href: "#/" }, { label: "Data Doctor" }])}<h1 class="h1">Bring your data</h1><p class="desc">Drop the files you have, as they come out of your systems: CSV, Excel or Parquet, one or several. Data Doctor says what each one is, what they can power, and what is missing. Nothing is trained and your files are never changed.</p>`,
  );
  const busy = state.phase === "reading" || state.phase === "diagnosing";
  const drop = `<section class="card"><div class="dd-drop"><label class="btn primary"><input type="file" class="sr" multiple accept="${TABLE_FILE_TYPES}" data-dd-files${
    busy ? " disabled" : ""
  }><span>${state.uploads.length ? "Add more files" : "Choose files"}</span></label>${
    state.uploads.length ? `<button type="button" class="btn quiet sm" data-dd-clear${busy ? " disabled" : ""}>Start over</button>` : ""
  }</div>${state.reading.map((name) => `<div class="loading" role="status">Reading ${esc(name)}…</div>`).join("")}${
    state.phase === "diagnosing" ? `<div class="loading" role="status">Data Doctor is looking at your files…</div>` : ""
  }${state.errors
    .map(({ name, error }) => errorBox(error, { title: `${name} could not be read.` }))
    .join("")}${state.diagnosisError ? errorBox(state.diagnosisError) : ""}</section>`;
  const d = state.diagnosis;
  if (!d) return `<main class="screen dd">${head}${drop}</main>`;
  const byId = Object.fromEntries(state.uploads.map((u) => [u.upload_id, u]));
  const files = `<section class="card"><h3>Your files</h3><div class="dd-files">${d.files
    .map((file) => fileHtml(file, byId[file.upload_id]))
    .join("")}</div></section>`;
  const cases = `<section class="card" data-dd-usecases><h3>What your data can power</h3>${d.use_cases
    .map(useCaseHtml)
    .join("")}${state.startError ? errorBox(state.startError) : ""}</section>`;
  return `<main class="screen dd">${head}${drop}${cases}${fitHtml(d)}${files}</main>`;
}

let mounted = null;

function paint() {
  if (!mounted) return;
  mounted.innerHTML = doctorHtml();
  bind(mounted);
}

async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  state.errors = [];
  state.diagnosisError = null;
  state.startError = null;
  state.phase = "reading";
  let useCase;
  try {
    useCase = await firstUseCase();
  } catch (error) {
    state.diagnosisError = error;
    state.phase = state.uploads.length ? "done" : "empty";
    paint();
    return;
  }
  for (const file of files) {
    state.reading = [...state.reading, file.name];
    paint();
    try {
      const answer = await postUpload(file, useCase, "train");
      state.uploads = [...state.uploads, { upload_id: answer.upload_id, name: file.name, intake: answer.intake }];
    } catch (error) {
      state.errors = [...state.errors, { name: file.name, error }];
    }
    state.reading = state.reading.filter((name) => name !== file.name);
  }
  await diagnose();
}

async function diagnose() {
  if (!state.uploads.length) {
    state.phase = "empty";
    paint();
    return;
  }
  state.phase = "diagnosing";
  paint();
  try {
    state.diagnosis = await postDiagnosis(state.uploads.map((u) => u.upload_id));
  } catch (error) {
    state.diagnosisError = error;
  }
  state.phase = "done";
  paint();
}

async function start(useCaseId, uploadId) {
  state.starting = useCaseId;
  state.startError = null;
  paint();
  try {
    const upload = await postHandoff(uploadId, useCaseId);
    const file = state.uploads.find((u) => u.upload_id === uploadId);
    handOff(useCaseId, "train", upload, file ? file.name : upload.profile.file_name);
    state.starting = "";
    window.location.hash = `#/uc/${encodeURIComponent(useCaseId)}`;
  } catch (error) {
    state.starting = "";
    state.startError = error;
    paint();
  }
}

async function agree(useCaseId) {
  const item = ((state.diagnosis && state.diagnosis.use_cases) || []).find((u) => u.use_case_id === useCaseId);
  const offer = item && item.outcome_offer;
  if (!offer) return;
  const fallback = offer.choices.find((c) => c.default) || offer.choices[0];
  const days = state.picked[useCaseId] || fallback.days;
  const choice = offer.choices.find((c) => c.days === days);
  state.agreeing = useCaseId;
  state.agreeError = null;
  paint();
  try {
    state.agreed = {
      ...state.agreed,
      [useCaseId]: await postOutcome({
        use_case: useCaseId,
        entity_upload_id: offer.entity_upload_id,
        event_upload_id: offer.event_upload_id,
        date_column: offer.date_column,
        days,
        sentence: choice.sentence,
        agreed: true,
      }),
    };
  } catch (error) {
    state.agreeError = { useCase: useCaseId, error };
  }
  state.agreeing = "";
  paint();
}

/** Show a diagnosis already made (a test, or a page that remembers the files). */
export function showDiagnosis(diagnosis, uploads = []) {
  Object.assign(state, { phase: "done", diagnosis, uploads, picked: {}, agreed: {}, agreeError: null });
  paint();
}

function bind(root) {
  const input = root.querySelector("[data-dd-files]");
  if (input) input.addEventListener("change", (event) => addFiles(event.target.files));
  const clear = root.querySelector("[data-dd-clear]");
  if (clear) {
    clear.addEventListener("click", () => {
      Object.assign(state, {
        phase: "empty",
        reading: [],
        uploads: [],
        errors: [],
        diagnosis: null,
        diagnosisError: null,
        picked: {},
        agreed: {},
        agreeError: null,
      });
      paint();
    });
  }
  root.querySelectorAll("[data-dd-start]").forEach((button) =>
    button.addEventListener("click", () => start(button.dataset.ddStart, button.dataset.ddUpload)),
  );
  root.querySelectorAll("[data-dd-pick]").forEach((input) =>
    input.addEventListener("change", () => {
      state.picked = { ...state.picked, [input.dataset.ddPick]: Number(input.value) };
      paint();
    }),
  );
  root.querySelectorAll("[data-dd-agree]").forEach((button) =>
    button.addEventListener("click", () => agree(button.dataset.ddAgree)),
  );
}

registerModule({
  name: "doctor",
  routes: ["doctor"],
  render: async (app) => {
    mounted = app;
    paint();
  },
});

registerForYou(() => ({ text: "Not sure where to start? Bring your data to Data Doctor", href: "#/doctor" }));
