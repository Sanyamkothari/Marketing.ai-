// "Model reports": today's Reports hub (`#/pilot`, ui/modules/pilot/screen.js) in one closed section (Plan M, M178).
// They are technical: the results report of each approved model, each scored campaign's value in rupees, and the
// data readiness reports. Until M180's switch decides who sees them, the section is closed and marked
// `data-technical`, as is the tab that leads to it. The Value Proof Packs, which were the hub's third list, are the
// first tab; every link here is the one the hub offered (the report pages themselves are unchanged).

import { dataTable, emptyState, esc, fmtDate, fmtInt, present, sortNote } from "../../dom.js";
import { getDemo, getIndustries, listDatasets, listModels, listScoringRuns, readinessUrl, resultsUrl, roiUrl } from "./api.js";

const cell = (text) => (present(text) ? esc(text) : "—");

const reportActions = (openHref, openLabel, pdfHref) =>
  `<span class="rp-acts"><a class="btn secondary sm" href="${esc(openHref)}" data-report-open>${esc(openLabel)}</a><a class="btn quiet sm" href="${esc(
    pdfHref,
  )}" download data-report-pdf>PDF</a></span>`;

async function settle(promise) {
  try {
    return { value: await promise, error: null };
  } catch (error) {
    return { value: null, error };
  }
}

/** Use case names by id, from `GET /industries`. */
function namesOf(industries) {
  const names = new Map();
  for (const industry of (industries && industries.industries) || []) {
    for (const stage of industry.stages || []) for (const u of stage.use_cases || []) names.set(u.id, u.name);
  }
  return (id) => names.get(id) || id;
}

function campaignTitle(demo, runId) {
  const m = demo && demo.manifest;
  const campaign = m && Array.isArray(m.campaigns) ? m.campaigns.find((c) => c.score_run_id === runId) : null;
  return campaign ? campaign.title : null;
}

const unavailable = `<p class="rp-note">This list could not be loaded.</p>`;

function section(name, title, body) {
  return `<section class="rp-models-part" data-models-part="${name}"><h2>${title}</h2>${body}</section>`;
}

function resultsPart(models, name) {
  if (models.error) return unavailable;
  const champions = ((models.value && models.value.versions) || [])
    .filter((v) => v.is_champion)
    .sort((a, b) => String(b.version.approved_at || b.version.created_at).localeCompare(String(a.version.approved_at || a.version.created_at)));
  if (!champions.length) {
    return emptyState({ title: "No approved model yet", text: "A model's results report appears here once a trained model is approved." });
  }
  return dataTable(
    [{ label: "Goal" }, { label: "Model" }, { label: "In use since" }, { label: "Method", more: true }, { label: "Report", num: true }],
    champions.map((c) => {
      const id = c.version.use_case_id;
      return [
        `<span class="rp-name">${esc(name(id))}</span>`,
        `Version ${esc(c.version.version)}`,
        esc(fmtDate(c.version.approved_at || c.version.created_at)),
        cell(c.version.model_display_name),
        reportActions(`#/pilot/view/results/${encodeURIComponent(id)}`, "Open", resultsUrl(id, "pdf")),
      ];
    }),
  );
}

function valuePart(runs, name, demo) {
  if (runs.error) return unavailable;
  const scored = ((runs.value && runs.value.runs) || [])
    .filter((r) => r.state === "done")
    .sort((a, b) => String(b.finished_at || b.created_at).localeCompare(String(a.finished_at || a.created_at)));
  if (!scored.length) {
    return emptyState({
      title: "No campaign has been scored yet",
      text: "Once a campaign's results are in, its value in rupees appears here.",
    });
  }
  return dataTable(
    [{ label: "Campaign" }, { label: "Scored on" }, { label: "Report", num: true }],
    scored.map((r) => [
      `<span class="rp-name">${esc(campaignTitle(demo, r.run_id) || r.use_case_name || name(r.use_case_id))}</span>`,
      esc(fmtDate(r.finished_at || r.created_at)),
      reportActions(`#/pilot/value/${encodeURIComponent(r.run_id)}`, "Value in rupees", roiUrl(r.run_id, "pdf")),
    ]),
  );
}

function readinessPart(datasets, name, demo) {
  if (datasets.error) return unavailable;
  const rows = [];
  const m = demo && demo.manifest;
  if (m && m.broken_dataset_id) rows.push({ id: m.broken_dataset_id, uc: m.use_case_id, practice: true, n: null, when: null });
  for (const d of (datasets.value && datasets.value.datasets) || []) {
    rows.push({ id: d.dataset_id, uc: d.use_case, practice: false, n: d.n_rows, when: d.built_at });
  }
  rows.sort((a, b) => String(b.when || "").localeCompare(String(a.when || "")));
  if (!rows.length) {
    return emptyState({
      title: "No dataset has been built yet",
      text: "Once your tables are built into a dataset, its readiness report appears here.",
    });
  }
  return dataTable(
    [{ label: "Dataset" }, { label: "Rows", num: true }, { label: "Built" }, { label: "Report", num: true }],
    rows.map((r) => [
      `<span class="rp-name">${esc(name(r.uc))}</span>${r.practice ? `<span class="chip neutral">Practice data with problems</span>` : ""}`,
      present(r.n) ? esc(fmtInt(r.n)) : "—",
      present(r.when) ? esc(fmtDate(r.when)) : "—",
      reportActions(`#/pilot/view/readiness/${encodeURIComponent(r.id)}`, "Open", readinessUrl(r.id, "pdf")),
    ]),
  );
}

/** The closed "Model reports" section, drawn from the answers of `loadModelReports`. */
export function modelReportsHtml({ models, runs, datasets, industries, demo }) {
  const name = namesOf(industries && industries.value);
  const d = demo && demo.value;
  return `<details class="tech rp-models" data-technical data-rp-models><summary>Model reports</summary>
    ${section("results", `Model results${sortNote("newest first")}`, resultsPart(models, name))}
    ${section("value", `Campaign value${sortNote("newest first")}`, valuePart(runs, name, d))}
    ${section("readiness", `Data readiness${sortNote("newest first")}`, readinessPart(datasets, name, d))}
  </details>`;
}

/** Ask for everything the section lists; one failed list does not stop the others. */
export async function loadModelReports() {
  const [models, runs, datasets, industries, demo] = await Promise.all([
    settle(listModels()),
    settle(listScoringRuns()),
    settle(listDatasets()),
    settle(getIndustries()),
    settle(getDemo()),
  ]);
  return { models, runs, datasets, industries, demo };
}
