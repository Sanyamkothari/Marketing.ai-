// The Pilot screen (Plan E): one place for everything a pilot hands to the client.
//
//   #/pilot                          the kit, the readiness reports, the results reports, the campaigns
//   #/pilot/view/readiness/<dataset>  a data readiness report, shown in place, with its PDF
//   #/pilot/view/results/<use case>   the results report of that use case's champion
//   #/pilot/value/<run>               a campaign's value: the measured effect, the inputs form, the PDF
//
// Every list is built from what the API returns - datasets, champions, scoring runs - and every
// report is the server's own page, shown in a sandboxed frame (no script runs in it) after being
// fetched through the wrapped `fetch`, so a sign-in is carried. PDFs are plain links; the production
// module's download handler adds the sign-in to them when one is held.

import { errorBox, esc, fmtStamp, pageHead } from "../../dom.js";
import {
  dataRequestUrl,
  demoRawUrl,
  feedbackExportUrl,
  getDataRequest,
  getReportHtml,
  getRoi,
  listDatasets,
  listModels,
  listScoringRuns,
  putRoi,
  readinessUrl,
  resultsUrl,
  roiUrl,
  templateUrl,
} from "./api.js";

const crumbs = (current) =>
  `<div class="crumbs"><a href="#/">Overview</a><span class="sep">›</span>${
    current ? `<a href="#/pilot">Pilot</a><span class="sep">›</span><span class="cur">${esc(current)}</span>` : `<span class="cur">Pilot</span>`
  }</div>`;

function frame(html) {
  const doc = esc(html);
  return `<iframe class="pe-frame" sandbox title="Report" srcdoc="${doc}"></iframe>`;
}

async function settle(promise) {
  try {
    return { value: await promise, error: null };
  } catch (error) {
    return { value: null, error };
  }
}

function kitCard(request, demo) {
  const tables = request.value ? request.value.tables : [];
  const templates = tables.length
    ? `<ul class="pe-list">${tables
        .map(
          (t) =>
            `<li><span>${esc(t.title)} <span class="pe-note">(${esc(t.need)})</span></span><a href="${esc(
              templateUrl(t.role),
            )}">Template</a></li>`,
        )
        .join("")}</ul>`
    : request.error
      ? errorBox(request.error)
      : "";
  const raw =
    demo && demo.seeded
      ? `<p>Try the pre-flight check on the demo's own raw tables:</p><div class="pe-links">
          <a href="${esc(demoRawUrl("clean"))}">Demo raw tables (clean)</a>
          <a href="${esc(demoRawUrl("broken"))}">Demo raw tables (broken extract)</a></div>`
      : "";
  return `<section class="pe-card"><h2>1. Data request kit</h2>
    <p>What to ask the client for, in their words: tables, columns, history, formats, pseudonymising the customer ID, and what never to send.</p>
    <div class="pe-links"><a href="${esc(dataRequestUrl())}">Download the data request</a></div>
    ${templates}
    <p style="margin-top:12px">Before sending, the client runs the pre-flight check on their own laptop; nothing is uploaded:</p>
    <div class="pe-code">python -m scripts.preflight &lt;folder&gt; --use-case &lt;use case&gt;</div>
    ${raw}</section>`;
}

function readinessCard(datasets, demo) {
  const rows = [];
  const m = demo && demo.manifest;
  if (m && m.broken_dataset_id) {
    rows.push({ id: m.broken_dataset_id, label: "Demo Telecom (broken extract)", when: null });
  }
  for (const d of datasets.value ? datasets.value.datasets : []) {
    rows.push({ id: d.dataset_id, label: `${d.use_case} · ${d.n_rows.toLocaleString("en-IN")} rows`, when: d.built_at });
  }
  const list = rows.length
    ? `<ul class="pe-list">${rows
        .map(
          (r) => `<li><span>${esc(r.label)}<br><span class="pe-note">${esc(r.id)}${r.when ? ` · ${esc(fmtStamp(r.when))}` : ""}</span></span>
          <span class="pe-links"><a href="#/pilot/view/readiness/${encodeURIComponent(r.id)}">View</a><a href="${esc(
            readinessUrl(r.id, "pdf"),
          )}">PDF</a></span></li>`,
        )
        .join("")}</ul>`
    : datasets.error
      ? errorBox(datasets.error)
      : `<p class="pe-note">No dataset has been built yet. Build one from raw tables on a use case's Setup screen.</p>`;
  return `<section class="pe-card"><h2>2. Data readiness</h2>
    <p>After the upload: what arrived, how it links up, whether there is enough history, and exactly what to fix. Ends in Ready, Ready with warnings, or Not ready.</p>${list}</section>`;
}

function resultsCard(models) {
  const champions = models.value ? models.value.versions.filter((v) => v.is_champion) : [];
  const list = champions.length
    ? `<ul class="pe-list">${champions
        .map(
          (c) => `<li><span>${esc(c.version.use_case_id)}<br><span class="pe-note">version ${esc(c.version.version)} · ${esc(
            c.version.model_display_name || "",
          )}</span></span><span class="pe-links"><a href="#/pilot/view/results/${encodeURIComponent(
            c.version.use_case_id,
          )}">View</a><a href="${esc(resultsUrl(c.version.use_case_id, "pdf"))}">PDF</a></span></li>`,
        )
        .join("")}</ul>`
    : models.error
      ? errorBox(models.error)
      : `<p class="pe-note">No use case has an approved model yet.</p>`;
  return `<section class="pe-card"><h2>3. Results report</h2>
    <p>One page for the client's marketing head: what the model does, how good it is in business terms, why customers are flagged, what to do, and its limits.</p>${list}</section>`;
}

function valueCard(runs) {
  const scored = runs.value ? runs.value.runs.filter((r) => r.state === "done") : [];
  const list = scored.length
    ? `<ul class="pe-list">${scored
        .slice(0, 12)
        .map(
          (r) => `<li><span>${esc(r.use_case_name)}<br><span class="pe-note">${esc(r.run_id)} · ${esc(
            fmtStamp(r.finished_at || r.created_at),
          )}</span></span><span class="pe-links"><a href="#/pilot/value/${encodeURIComponent(r.run_id)}">Value view</a></span></li>`,
        )
        .join("")}</ul>`
    : runs.error
      ? errorBox(runs.error)
      : `<p class="pe-note">No campaign has been scored yet.</p>`;
  return `<section class="pe-card"><h2>4. Campaign value</h2>
    <p>Once a campaign's outcome period has passed: the extra customers it caused, compared with the control group, and their value in rupees as a range.</p>${list}</section>`;
}

async function renderIndex(app, demo) {
  const [request, datasets, models, runs] = await Promise.all([
    settle(getDataRequest()),
    settle(listDatasets()),
    settle(listModels()),
    settle(listScoringRuns()),
  ]);
  const banner =
    demo && demo.demo_mode
      ? demo.seeded
        ? `<p class="pe-note">Demo mode: everything below belongs to Demo Telecom, a synthetic client.</p>`
        : `<div class="apierr" role="status"><b>DEMO_NOT_SEEDED</b>Demo mode is on, but no demo has been seeded. Run <code>${esc(
            demo.how_to_seed,
          )}</code> once.</div>`
      : "";
  app.innerHTML = `<div class="screen">${pageHead(
    `${crumbs(null)}<h1 class="h1">Pilot</h1><p class="sub">Everything the pilot hands to the client, from the data request to the value of a campaign.</p>`,
  )}${banner}
    <div class="pe-cards">${kitCard(request, demo)}${readinessCard(datasets, demo)}${resultsCard(models)}${valueCard(runs)}</div>
    <p class="pe-note" style="margin-top:18px">Feedback from the pilot team: <a href="${esc(
      feedbackExportUrl(),
    )}">export all feedback</a> (Admin).</p></div>`;
  document.title = "Pilot · Marketing AI";
}

async function renderReport(app, kind, id) {
  const htmlUrl = kind === "readiness" ? readinessUrl(id, "html") : resultsUrl(id, "html");
  const pdfUrl = kind === "readiness" ? readinessUrl(id, "pdf") : resultsUrl(id, "pdf");
  const title = kind === "readiness" ? "Data readiness report" : "Results report";
  const { value, error } = await settle(getReportHtml(htmlUrl));
  const body = error
    ? errorBox(error)
    : value
      ? frame(value)
      : `<p class="pe-note">There is nothing to report on yet.</p>`;
  app.innerHTML = `<div class="screen">${pageHead(
    `${crumbs(title)}<h1 class="h1">${esc(title)}</h1><p class="sub"><a href="${esc(pdfUrl)}">Download as PDF</a></p>`,
  )}${body}</div>`;
  document.title = `${title} · Marketing AI`;
}

const INPUTS = [
  ["value_per_outcome", "What one extra customer is worth (₹)", "number"],
  ["offer_cost", "Offer cost when taken (₹)", "number"],
  ["contact_cost", "Cost per contact (₹)", "number"],
  ["outcome_is_good", "The outcome is one we want more of (untick when it is a customer leaving)", "checkbox"],
  ["value_basis", "What that value is (for example 12 months of revenue)", "text"],
  ["note", "Note", "text"],
];

async function renderValue(app, runId, rerender) {
  const [view, page] = await Promise.all([settle(getRoi(runId)), settle(getReportHtml(roiUrl(runId, "html")))]);
  const inputs = (view.value && view.value.inputs) || {};
  const field = ([name, label, type]) => {
    if (type === "checkbox") {
      const saved = inputs[name];
      const on = saved === undefined || saved === null ? !(view.value && view.value.outcome_is_good === false) : Boolean(saved);
      return `<label><span><input name="${name}" type="checkbox" ${on ? "checked" : ""}> ${esc(label)}</span></label>`;
    }
    const value = inputs[name] !== undefined && inputs[name] !== null ? esc(inputs[name]) : "";
    const extra = type === "number" ? 'min="0" step="any"' : type === "text" ? 'maxlength="300"' : "";
    return `<label>${esc(label)}<input class="pe-in" name="${name}" type="${type}" ${extra} value="${value}"></label>`;
  };
  const form = `<form class="pe-form" data-pe-roi>${INPUTS.map(field).join("")}<div class="pe-row"><span class="pe-note" data-pe-roi-status></span><button type="submit" class="pe-btn primary">Save values</button></div></form>`;
  const body = page.error ? errorBox(page.error) : page.value ? frame(page.value) : `<p class="pe-note">No such campaign run.</p>`;
  app.innerHTML = `<div class="screen">${pageHead(
    `${crumbs("Campaign value")}<h1 class="h1">Campaign value</h1><p class="sub"><a href="${esc(
      roiUrl(runId, "pdf"),
    )}">Download as PDF</a></p>`,
  )}<p class="pe-note">Your values are stored with this campaign and printed on the report. The measured number of extra customers does not depend on them; the rupee amounts do.</p>${form}${body}</div>`;
  const element = app.querySelector("[data-pe-roi]");
  element.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(element);
    const payload = {};
    for (const [name, , type] of INPUTS) {
      const raw = String(data.get(name) || "").trim();
      if (type === "checkbox") payload[name] = data.get(name) !== null;
      else if (type === "number") {
        if (raw !== "") payload[name] = Number(raw); // a blank box is left out: the server says what is missing
      }
      else payload[name] = raw;
    }
    const status = element.querySelector("[data-pe-roi-status]");
    status.textContent = "Saving…";
    try {
      await putRoi(runId, payload);
      rerender();
    } catch (error) {
      status.innerHTML = errorBox(error);
    }
  });
  document.title = "Campaign value · Marketing AI";
}

export async function renderPilot(app, parts, demo) {
  const [, second, third, fourth] = parts.map((p) => decodeURIComponent(p));
  try {
    if (second === "view" && (third === "readiness" || third === "results") && fourth) {
      await renderReport(app, third, fourth);
    } else if (second === "value" && third) {
      await renderValue(app, third, () => renderPilot(app, parts, demo));
    } else {
      await renderIndex(app, demo);
    }
  } catch (error) {
    app.innerHTML = `<div class="screen">${pageHead(`${crumbs(null)}<h1 class="h1">Pilot</h1>`)}${errorBox(error)}</div>`;
  }
}
