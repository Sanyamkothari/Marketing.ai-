// Data Doctor, the place (Plan L M151, DEC-1513): `#/doctor`, "Bring your data".
//
// The person drops whatever files they have, before choosing anything. Each file is uploaded the
// ordinary way (`POST /uploads`, so intake reads it and every limit, retention rule and erasure is an
// upload's), then `POST /doctor/diagnosis` says what each file is and what the files can power. A use
// case that is ready, or ready after a check, has a "Start" button: `POST /doctor/handoff` makes the
// file an upload of that use case and its Guided setup opens on it (`handoff.js`). Home links here
// through its "For you" line (`registerForYou`), so no shared screen is edited.
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
.dd .dd-next{margin-top:8px;font-size:13px;color:var(--ink2)}
.dd .dd-next h5{margin:8px 0 2px;font-size:12px;font-weight:600;color:var(--ink2)}
.dd .dd-next .dd-opt{margin:4px 0 0;line-height:1.5}
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

// Plan L M158 (DEC-1583): when a use case is not ready, what is short, what to do now and what to collect.
function nextStepsHtml(next) {
  if (!next) return "";
  const list = (items) => (items.length ? `<ul>${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "");
  const shortText = (next.shortfalls || []).map((s) => s.sentence);
  const options = (next.options || []).map((o) => {
    const button =
      o.kind === "other_use_case" && o.use_case_id && o.upload_id
        ? ` <button type="button" class="btn secondary sm" data-dd-start="${esc(o.use_case_id)}" data-dd-upload="${esc(
            o.upload_id,
          )}"${state.starting ? " disabled" : ""}>${esc(o.title)}</button>`
        : "";
    return `<p class="dd-opt" data-dd-option="${esc(o.kind)}"><strong>${esc(o.title)}.</strong> ${esc(o.detail)}${button}</p>`;
  });
  const request = next.data_request_use_case
    ? `<p class="dd-opt"><a href="${API_BASE}/pilot/data-request?use_case=${encodeURIComponent(
        next.data_request_use_case,
      )}" download data-dd-request>Download the data request</a></p>`
    : "";
  const collect = next.collect || [];
  if (!shortText.length && !options.length && !collect.length) return "";
  return `<div class="dd-next" data-dd-next>${shortText.length ? `<h5>What is short</h5>${list(shortText)}` : ""}${
    options.length ? `<h5>What you can do now</h5>${options.join("")}` : ""
  }${collect.length ? `<h5>What to collect, and from whom</h5>${list(collect)}${request}` : ""}</div>`;
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
  )}</p>${item.needs && item.needs.length ? `<ul>${item.needs.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}${nextStepsHtml(
    item.next_steps,
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
  return `<main class="screen dd">${head}${drop}${cases}${files}</main>`;
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

function bind(root) {
  const input = root.querySelector("[data-dd-files]");
  if (input) input.addEventListener("change", (event) => addFiles(event.target.files));
  const clear = root.querySelector("[data-dd-clear]");
  if (clear) {
    clear.addEventListener("click", () => {
      Object.assign(state, { phase: "empty", reading: [], uploads: [], errors: [], diagnosis: null, diagnosisError: null });
      paint();
    });
  }
  root.querySelectorAll("[data-dd-start]").forEach((button) =>
    button.addEventListener("click", () => start(button.dataset.ddStart, button.dataset.ddUpload)),
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
