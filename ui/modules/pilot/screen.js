// The pilot's screens (Plan E; v1 navigation): what the pilot hands to the client.
//
//   #/pilot                            Reports: every model results report and every campaign's value
//   #/pilot/kit                        Build data: the data request kit, a dataset from raw tables, and
//                                      the data readiness reports
//   #/pilot/view/readiness/<dataset>   a data readiness report, shown in place, with its PDF
//   #/pilot/view/results/<use case>    the results report of that use case's approved model
//   #/pilot/value/<run>                a campaign's value in rupees: the verdict, the report, the estimates
//
// Every list is built from what the API returns - datasets, approved models, scoring runs - and each
// card fills as its own request settles, after a skeleton painted at once. Every report is the
// server's own page, fetched through the wrapped `fetch` (so a sign-in is carried) and shown in a
// sandboxed frame: `allow-same-origin` so the frame can be sized to its content, and no
// `allow-scripts`, so nothing in it runs. PDFs are plain links; the production module's download
// handler adds the sign-in to them when one is held.

import { getIndustries } from "../../api.js";
import {
  announceStatus,
  crumbs,
  dataTable,
  emptyState,
  errorBox,
  esc,
  fmtDate,
  fmtInt,
  fmtMoney,
  fmtPeople,
  headActions,
  noticeCard,
  pageHead,
  present,
  sortNote,
  techDetails,
} from "../../dom.js";
import {
  dataRequestUrl,
  demoRawUrl,
  feedbackExportUrl,
  getDataRequest,
  getReportDoc,
  getReportHtml,
  getRoi,
  listClients,
  listDatasets,
  listModels,
  listScoringRuns,
  mayCall,
  putRoi,
  readinessUrl,
  resultsUrl,
  roiUrl,
  templateUrl,
} from "./api.js";

// --- painting: one screen at a time, never a late answer over a newer one --------------------------

let paintSeq = 0;
let mounted = null;

async function settle(promise) {
  try {
    return { value: await promise, error: null };
  } catch (error) {
    return { value: null, error };
  }
}

/**
 * Put a screen's shell in place. A new route shows it at once (its cards hold skeleton rows); a
 * repaint of the route already on screen (the client changed, a module registered) builds it off
 * screen and swaps it in when `commit()` is called, so nothing flashes. `live()` is false once a newer
 * paint has started.
 */
function mount(app, html) {
  const seq = ++paintSeq;
  const hash = window.location.hash;
  const holder = document.createElement("div");
  holder.innerHTML = html;
  const main = holder.firstElementChild;
  const same = mounted && mounted.hash === hash && app.firstElementChild === mounted.main;
  const live = () => seq === paintSeq;
  if (!same) {
    app.replaceChildren(main);
    mounted = { hash, main };
  }
  return {
    main,
    live,
    commit() {
      if (same && live()) {
        app.replaceChildren(main);
        mounted = { hash, main };
      }
    },
  };
}

const loadingRows = `<div class="skel-screen pe-body" aria-busy="true"><div class="skel-list"><span class="skel skel-row"></span><span class="skel skel-row"></span><span class="skel skel-row"></span></div><span class="sr">Loading…</span></div>`;

const card = (name, title, extra = "") =>
  `<section class="card" data-pe-card="${name}"><h3>${title}</h3>${extra}<div data-pe-slot>${loadingRows}</div></section>`;

function fill(main, name, html) {
  const slot = main.querySelector(`[data-pe-card="${name}"] [data-pe-slot]`);
  if (slot) slot.innerHTML = html;
}

const unavailable = `<div class="pe-body"><p>This list could not be loaded.</p></div>`;

/** One calm error for the whole screen, however many of its requests failed. */
function showError(main, error) {
  const slot = main.querySelector("[data-pe-error]");
  if (slot && !slot.firstElementChild) slot.innerHTML = errorBox(error, { retry: true });
}

function head(items, title, sub, extra = "") {
  return pageHead(
    `${crumbs(items)}<h1 class="h1">${esc(title)}</h1>${sub ? `<p class="sub">${esc(sub)}</p>` : ""}${extra}`,
  );
}

// --- names instead of ids ------------------------------------------------------------------------------

/** Use cases by id (`name`, `industry`, `ai_type`, `status`) and ids by name, from `GET /industries`. */
function useCaseIndex(industries) {
  const byId = new Map();
  const byName = new Map();
  const payload = industries.value;
  if (!payload) return { byId, byName, list: [] };
  const list = [];
  for (const industry of payload.industries) {
    for (const stage of industry.stages) {
      for (const u of stage.use_cases) {
        const entry = {
          id: u.id,
          name: u.name,
          industry: industry.name,
          industryId: industry.id,
          ai_type: u.ai_type,
          status: u.status,
        };
        if (!byId.has(u.id)) list.push(entry);
        byId.set(u.id, entry);
        byName.set(u.name, u.id);
      }
    }
  }
  const first = payload.default_industry;
  list.sort((a, b) => (a.industryId === first ? 0 : 1) - (b.industryId === first ? 0 : 1));
  return { byId, byName, list };
}

const ucName = (index, id) => (index.byId.get(id) || {}).name || id;

/** The client a row belongs to, in the chooser's own words. */
function clientName(clients, demo, id) {
  if (!present(id)) return null;
  const m = demo && demo.manifest;
  if (m && id === m.broken_client_id) return `${m.client_name}: practice data with problems`;
  const found = clients.value ? clients.value.clients.find((cl) => cl.client_id === id) : null;
  return found ? found.name : m && id === m.client_id ? m.client_name : id;
}

const cell = (text) => (present(text) ? esc(text) : "—");

function reportActions(openHref, openLabel, pdfHref) {
  return `<span class="pe-acts"><a class="btn secondary sm" href="${esc(openHref)}">${esc(
    openLabel,
  )}</a><a class="btn quiet sm" href="${esc(pdfHref)}" download>PDF</a></span>`;
}

/** Demo mode with nothing seeded: one calm notice; the command stays under Details for IT. */
function demoNotice(demo) {
  if (!demo || !demo.demo_mode || demo.seeded) return "";
  return `<div class="stack">${noticeCard({
    title: "Sample data is not loaded yet",
    text: "Demo mode is on, but the sample client has not been set up. Your IT team can load it with one command.",
  })}${techDetails([["Command", demo.how_to_seed]])}</div>`;
}

// --- Reports (#/pilot) -------------------------------------------------------------------------------

async function renderReports(app, demo) {
  const screen = mount(
    app,
    `<main class="screen">${head(
      [{ label: "Reports" }],
      "Reports",
      "What the models found and what each campaign was worth, ready to share as a page or a PDF.",
      `<div data-pe-head-actions></div>`,
    )}${demoNotice(demo)}<div data-pe-error></div>
    <div class="stack" data-pe-reports>
      ${card("results", `Model results${sortNote("newest first")}`)}
      ${card("value", `Campaign value${sortNote("newest first")}`)}
    </div><p class="pe-foot" data-pe-export></p></main>`,
  );
  document.title = "Reports · Marketing AI";
  const { main, live } = screen;
  const industriesP = settle(getIndustries());
  const clientsP = settle(listClients());
  let latest = null;
  const consider = (when, href) => {
    if (present(when) && (!latest || String(when) > latest.when)) latest = { when: String(when), href };
  };

  const results = (async () => {
    const [models, industries] = await Promise.all([settle(listModels()), industriesP]);
    if (!live()) return;
    const index = useCaseIndex(industries);
    if (models.error) {
      showError(main, models.error);
      fill(main, "results", unavailable);
      return;
    }
    const champions = models.value.versions
      .filter((v) => v.is_champion)
      .sort((a, b) =>
        String(b.version.approved_at || b.version.created_at).localeCompare(String(a.version.approved_at || a.version.created_at)),
      );
    if (!champions.length) {
      fill(
        main,
        "results",
        emptyState({
          title: "No approved model yet",
          text: "A model's results report appears here once a trained model is approved.",
          action: { label: "Train a model", href: "#/", kind: "secondary" },
        }),
      );
      return;
    }
    const rows = champions.map((c) => {
      const id = c.version.use_case_id;
      const href = `#/pilot/view/results/${encodeURIComponent(id)}`;
      consider(c.version.approved_at || c.version.created_at, href);
      return [
        `<span class="pe-name">${esc(ucName(index, id))}</span>`,
        `Version ${esc(c.version.version)}`,
        esc(fmtDate(c.version.approved_at || c.version.created_at)),
        cell(c.version.model_display_name),
        reportActions(href, "Open", resultsUrl(id, "pdf")),
      ];
    });
    fill(
      main,
      "results",
      dataTable(
        [{ label: "Use case" }, { label: "Model" }, { label: "In use since" }, { label: "Method", more: true }, { label: "Report", num: true }],
        rows,
      ),
    );
  })();

  const value = (async () => {
    const [runs, industries, clients] = await Promise.all([settle(listScoringRuns()), industriesP, clientsP]);
    if (!live()) return;
    const index = useCaseIndex(industries);
    if (runs.error) {
      showError(main, runs.error);
      fill(main, "value", unavailable);
      return;
    }
    const scored = runs.value.runs
      .filter((r) => r.state === "done")
      .sort((a, b) => String(b.finished_at || b.created_at).localeCompare(String(a.finished_at || a.created_at)));
    if (!scored.length) {
      fill(
        main,
        "value",
        emptyState({
          title: "No campaign has been scored yet",
          text: "Score a list of customers with an approved model; once the campaign's results are in, its value in rupees appears here.",
          action: { label: "Score customers", href: "#/", kind: "secondary" },
        }),
      );
      return;
    }
    const rows = scored.map((r) => {
      const href = `#/pilot/value/${encodeURIComponent(r.run_id)}`;
      consider(r.finished_at || r.created_at, href);
      return [
        `<span class="pe-name">${esc(campaignTitle(demo, r.run_id) || r.use_case_name || ucName(index, r.use_case_id))}</span>`,
        cell(clientName(clients, demo, r.client_id)),
        esc(fmtDate(r.finished_at || r.created_at)),
        reportActions(href, "Value in rupees", roiUrl(r.run_id, "pdf")),
      ];
    });
    fill(
      main,
      "value",
      dataTable([{ label: "Campaign" }, { label: "Client" }, { label: "Scored on" }, { label: "Report", num: true }], rows),
    );
  })();

  await Promise.all([results, value]);
  if (!live()) return;
  const actions = main.querySelector("[data-pe-head-actions]");
  if (actions && latest) actions.outerHTML = headActions({ primary: { label: "Open the latest report", href: latest.href } });
  screen.commit();
  // Exporting everyone's feedback is an Admin action: offered only to whom the API allows it.
  if (await mayCall("GET", "/pilot/feedback/export")) {
    const foot = main.querySelector("[data-pe-export]");
    if (foot && live()) {
      foot.innerHTML = `Feedback from the pilot team: <a class="btn quiet sm" href="${esc(
        feedbackExportUrl(),
      )}" data-pe-feedback-export>Export all feedback</a>`;
    }
  }
}

function campaignTitle(demo, runId) {
  const m = demo && demo.manifest;
  const campaign = m && Array.isArray(m.campaigns) ? m.campaigns.find((c) => c.score_run_id === runId) : null;
  return campaign ? campaign.title : null;
}

// --- Build data (#/pilot/kit) ------------------------------------------------------------------------

const PREFLIGHT = "python -m scripts.preflight <folder> --use-case <use case>";

function kitBody(request, demo) {
  const tables = request.value ? request.value.tables : [];
  const list = tables.length
    ? `<ul class="pe-tables">${tables
        .map((t) => `<li><span class="chip neutral">${esc(t.title)} · ${esc(t.need)}</span></li>`)
        .join("")}</ul>`
    : "";
  const templates = tables.length
    ? `<p>A template for each table, with the columns in order:</p><div class="pe-links">${tables
        .map((t) => `<a class="btn quiet sm" href="${esc(templateUrl(t.role))}" download>${esc(t.title)} template</a>`)
        .join("")}</div>`
    : "";
  const raw =
    demo && demo.seeded
      ? `<p>Sample raw tables to practise the check on:</p><div class="pe-links">
          <a class="btn quiet sm" href="${esc(demoRawUrl("clean"))}" download>Clean sample tables</a>
          <a class="btn quiet sm" href="${esc(demoRawUrl("broken"))}" download>Sample tables with a planted problem</a></div>`
      : "";
  return `<div class="pe-body">
    <p>What to ask your client for, in plain words: the tables and columns we need, how much history, how to hide customer identities, and what never to send.</p>
    ${list}
    <details class="adv"><summary>For your IT team</summary><div>
      ${templates}
      <p>Before sending anything, they can run the pre-flight check on their own computer. Nothing is uploaded:</p>
      <div class="pe-code"><code>${esc(PREFLIGHT)}</code><button type="button" class="btn quiet sm" data-copy="${esc(
        PREFLIGHT,
      )}" aria-label="Copy the pre-flight command">Copy</button></div>
      ${raw}
    </div></details></div>`;
}

function buildBody(index) {
  const predictive = index.list.filter((u) => u.ai_type === "predictive" && u.status === "available");
  if (!predictive.length) {
    return emptyState({
      title: "No use case can build a dataset yet",
      text: "Use cases that learn from your client's data appear here.",
      action: { label: "Go to Home", href: "#/", kind: "secondary" },
    });
  }
  return dataTable(
    [{ label: "Use case" }, { label: "Industry" }, { label: "Set up", num: true }],
    predictive.map((u) => [
      `<span class="pe-name">${esc(u.name)}</span>`,
      esc(u.industry),
      `<span class="pe-acts"><a class="btn secondary sm" href="#/uc/${encodeURIComponent(u.id)}">Set up</a></span>`,
    ]),
  );
}

function readinessBody(datasets, index, clients, demo) {
  const rows = [];
  const m = demo && demo.manifest;
  if (m && m.broken_dataset_id) {
    rows.push({ id: m.broken_dataset_id, uc: m.use_case_id, client: m.broken_client_id, train: true, n: null, when: null });
  }
  for (const d of datasets.value.datasets) {
    rows.push({ id: d.dataset_id, uc: d.use_case, client: d.client_id, train: present(d.target), n: d.n_rows, when: d.built_at });
  }
  rows.sort((a, b) => String(b.when || "").localeCompare(String(a.when || "")));
  if (!rows.length) {
    return emptyState({
      title: "No dataset has been built yet",
      text: "Once your client's tables are built into a dataset, its readiness report appears here: what arrived, how it links up, and exactly what to fix.",
      action: { label: "Download the data request", href: dataRequestUrl(), kind: "secondary" },
    });
  }
  return dataTable(
    [{ label: "Dataset" }, { label: "Client" }, { label: "Rows", num: true }, { label: "Built" }, { label: "Report", num: true }],
    rows.map((r) => [
      `<span class="pe-name">${esc(ucName(index, r.uc))}</span><span class="chip neutral">${r.train ? "For training" : "For scoring"}</span>`,
      cell(clientName(clients, demo, r.client)),
      present(r.n) ? esc(fmtInt(r.n)) : "—",
      present(r.when) ? esc(fmtDate(r.when)) : "—",
      reportActions(`#/pilot/view/readiness/${encodeURIComponent(r.id)}`, "Open", readinessUrl(r.id, "pdf")),
    ]),
  );
}

async function renderKit(app, demo) {
  const screen = mount(
    app,
    `<main class="screen">${head(
      [{ label: "Build data" }],
      "Build data",
      "Ask your client for the right tables, turn them into a dataset, and check it is ready before a model learns from it.",
      headActions({ primary: { label: "Download the data request", href: dataRequestUrl(), attrs: "download" } }),
    )}${demoNotice(demo)}<div data-pe-error></div>
    <div class="stack">
      ${card("kit", "Data request kit")}
      ${card("build", "Build a dataset from raw tables", `<div class="pe-body"><p>Choose the use case the data is for. Its Setup screen maps your client's tables, builds the dataset and checks it.</p></div>`)}
      ${card("readiness", `Data readiness${sortNote("newest first")}`)}
    </div></main>`,
  );
  document.title = "Build data · Marketing AI";
  const { main, live } = screen;
  const industriesP = settle(getIndustries());
  const clientsP = settle(listClients());

  const kit = settle(getDataRequest()).then((request) => {
    if (!live()) return;
    if (request.error) showError(main, request.error);
    fill(main, "kit", request.error ? unavailable : kitBody(request, demo));
  });
  const build = industriesP.then((industries) => {
    if (!live()) return;
    if (industries.error) showError(main, industries.error);
    fill(main, "build", industries.error ? unavailable : buildBody(useCaseIndex(industries)));
  });
  const readiness = Promise.all([settle(listDatasets()), industriesP, clientsP]).then(([datasets, industries, clients]) => {
    if (!live()) return;
    if (datasets.error) {
      showError(main, datasets.error);
      fill(main, "readiness", unavailable);
      return;
    }
    fill(main, "readiness", readinessBody(datasets, useCaseIndex(industries), clients, demo));
  });
  await Promise.all([kit, build, readiness]);
  screen.commit();
}

// --- the report viewers ------------------------------------------------------------------------------

const VERDICT_PILL = {
  ready: ["ok", "Ready"],
  warnings: ["warn", "Ready with warnings"],
  not_ready: ["bad", "Not ready"],
};

function fact(doc, name) {
  const found = doc && Array.isArray(doc.facts) ? doc.facts.find((f) => f[0] === name) : null;
  return found ? found[1] : null;
}

/** The report, in a frame sized to its content, on a paper surround (white in dark mode too). */
function frame(html, title) {
  return `<div class="pe-paper"><iframe class="pe-frame" sandbox="allow-same-origin" title="${esc(
    title,
  )}" srcdoc="${esc(html)}"></iframe></div>`;
}

function sizeFrame(iframe) {
  let doc = null;
  try {
    doc = iframe.contentDocument;
  } catch {
    doc = null; // not same-origin after all: the frame keeps its minimum height
  }
  if (!doc || !doc.documentElement) return;
  const height = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
  if (height > 0) iframe.style.height = `${height}px`;
}

function wireFrames(main) {
  for (const iframe of main.querySelectorAll("iframe.pe-frame")) {
    const size = () => sizeFrame(iframe);
    iframe.addEventListener("load", () => {
      size();
      setTimeout(size, 300); // after the report's fonts settle
    });
    size();
  }
}

let resizeTimer = null;
if (typeof window !== "undefined") {
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => document.querySelectorAll("iframe.pe-frame").forEach(sizeFrame), 150);
  });
}

async function renderReport(app, kind, id) {
  const readiness = kind === "readiness";
  const noun = readiness ? "Data readiness" : "Model results";
  const parent = readiness ? { label: "Build data", href: "#/pilot/kit" } : { label: "Reports", href: "#/pilot" };
  const screen = mount(
    app,
    `<main class="screen">${head([parent, { label: noun }], noun, null)}<div data-pe-body>${loadingRows}</div></main>`,
  );
  document.title = `${noun} · Marketing AI`;
  const { main, live } = screen;
  const [doc, page, industries, models] = await Promise.all([
    settle(getReportDoc(readiness ? readinessUrl(id, "json") : resultsUrl(id, "json"))),
    settle(getReportHtml(readiness ? readinessUrl(id, "html") : resultsUrl(id, "html"))),
    settle(getIndustries()),
    readiness ? Promise.resolve({ value: null, error: null }) : settle(listModels()),
  ]);
  if (!live()) return;
  const index = useCaseIndex(industries);
  const error = doc.error || page.error;
  let body;
  let headHtml;
  if ((error && error.status === 404) || (!error && !page.value)) {
    const missing = error || {};
    body = `${noticeCard(
      readiness
        ? {
            title: "This data check no longer exists",
            text: "The dataset it describes may have been deleted, or the link is incomplete.",
            action: { label: "Back to Build data", href: "#/pilot/kit" },
          }
        : missing.code === "RESULTS_NOT_FOUND"
          ? {
              title: "This use case has no approved model yet",
              text: "Its results report appears here once a trained model is approved.",
              action: { label: "Back to Reports", href: "#/pilot" },
            }
          : {
              title: "This report could not be found",
              text: "The use case may have been removed, or the link is incomplete.",
              action: { label: "Back to Reports", href: "#/pilot" },
            },
    )}${techDetails(
      [
        ["Code", missing.code],
        ["Message", missing.message],
      ],
      "Details",
    )}`;
    headHtml = head([parent, { label: noun }], noun, null);
  } else if (error) {
    body = errorBox(error, { retry: true });
    headHtml = head([parent, { label: noun }], noun, null);
  } else {
    const report = doc.value;
    const name = fact(report, "Use case") || ucName(index, id);
    const title = `${noun} · ${name}`;
    let pill = "";
    let sub;
    const actions = { primary: { label: "Download PDF", href: readiness ? readinessUrl(id, "pdf") : resultsUrl(id, "pdf"), attrs: "download" } };
    if (readiness) {
      const verdict = (report.blocks || []).find((b) => b.kind === "verdict");
      const state = verdict ? VERDICT_PILL[verdict.state] : null;
      if (state) pill = ` <span class="pill ${state[0]}">${esc(state[1])}</span>`;
      const built = fact(report, "Built");
      sub = [report.client_name, built ? `built ${built}` : null].filter(Boolean).join(" · ");
      const ucId = index.byName.get(name);
      if (verdict && verdict.state === "not_ready" && ucId) {
        actions.secondary = [{ label: "Open the mapping screen", href: `#/uc/${encodeURIComponent(ucId)}` }];
      }
    } else {
      const champion = models.value ? models.value.versions.find((v) => v.is_champion && v.version.use_case_id === id) : null;
      sub = champion
        ? `${champion.version.approved_at ? "Approved model" : "Model in use"}, version ${champion.version.version}, trained ${fmtDate(
            champion.version.created_at,
          )}`
        : report.client_name;
    }
    headHtml = pageHead(
      `${crumbs([parent, { label: title }])}<div class="pe-headline"><h1 class="h1">${esc(title)}</h1>${pill}</div>${
        sub ? `<p class="sub">${esc(sub)}</p>` : ""
      }${headActions(actions)}`,
    );
    body = frame(page.value, title);
    document.title = `${title} · Marketing AI`;
  }
  main.innerHTML = `${headHtml}${body}`;
  wireFrames(main);
  screen.commit();
}

// --- a campaign's value in rupees (#/pilot/value/<run>) ----------------------------------------------

// [name, label, type, group, helper]. The three money inputs carry the rupee sign in their label:
// RoiInputs.currency is fixed to INR, so the sign names the unit a figure is typed in, never a figure.
const INPUTS = [
  ["value_per_outcome", "Value of one extra customer (₹)", "number", "value", "For example, what a kept customer brings in over 12 months."],
  ["value_basis", "What that value is based on", "text", "value", "For example: 12 months of revenue."],
  ["offer_cost", "Cost of the offer, each time a customer takes it (₹)", "number", "costs", ""],
  ["contact_cost", "Cost of each contact (₹)", "number", "costs", "An SMS, a call or an e-mail."],
  ["outcome_is_good", "What the campaign aims for", "direction", "outcome", ""],
  ["note", "Note", "text", "about", "Printed on the report."],
];

const GROUPS = [
  ["value", "Value"],
  ["costs", "Costs"],
  ["outcome", "The outcome"],
  ["about", "About these figures"],
];

const TEXT_LIMITS = { value_basis: 300, note: 500 };

const openEstimates = new Set();
let dirtyRun = null;

function valueField([name, label, type, , helper], inputs, view) {
  const help = helper ? `<p class="pe-help" id="pe-h-${name}">${esc(helper)}</p>` : "";
  const err = `<p class="pe-err" id="pe-e-${name}" data-pe-err="${name}"></p>`;
  const described = `${helper ? `pe-h-${name} ` : ""}pe-e-${name}`;
  if (type === "direction") {
    const saved = inputs[name];
    const good = saved === undefined || saved === null ? !(view.value && view.value.outcome_is_good === false) : Boolean(saved);
    return `<div class="pe-field wide" role="radiogroup" aria-labelledby="pe-l-${name}"><span class="pe-lbl" id="pe-l-${name}">${esc(
      label,
    )}</span><div class="pe-radios">
      <label><input type="radio" name="${name}" value="good"${good ? " checked" : ""}> We want more of this outcome (for example, customers coming back)</label>
      <label><input type="radio" name="${name}" value="bad"${good ? "" : " checked"}> We want less of this outcome (for example, customers leaving)</label>
    </div>${err}</div>`;
  }
  const value = present(inputs[name]) ? esc(inputs[name]) : "";
  const extra = type === "number" ? `min="0" step="any" inputmode="decimal"` : `maxlength="${TEXT_LIMITS[name] || 300}"`;
  const wide = type === "text" ? " wide" : "";
  return `<div class="pe-field${wide}"><label for="pe-f-${name}">${esc(label)}</label><div class="control"><input id="pe-f-${name}" name="${name}" type="${
    type === "number" ? "number" : "text"
  }" ${extra} value="${value}" aria-describedby="${described}"></div>${help}${err}</div>`;
}

/** `view` is the settled `GET /pilot/roi/{run}`: the saved inputs, and the direction in use. */
function estimatesForm(view) {
  const inputs = (view.value && view.value.inputs) || {};
  const groups = GROUPS.map(([group, legend]) => {
    const fields = INPUTS.filter((f) => f[3] === group);
    return `<fieldset><legend>${esc(legend)}</legend>${fields.map((f) => valueField(f, inputs, view)).join("")}</fieldset>`;
  }).join("");
  return `<form class="pe-form" data-pe-roi novalidate>
    <p class="pe-help">Stored with this campaign and printed on its report. The number of extra customers is measured and does not change with them; the rupee amounts do.</p>
    ${groups}
    <div class="pe-row"><span class="pe-status" data-pe-roi-status role="status"></span><button type="submit" class="btn secondary">Save and recalculate</button></div></form>`;
}

/** One labelled tile: the most likely figure, and its range on a quieter line under it. */
function tile(label, money, fmt) {
  const spread =
    money && present(money.low) && present(money.high)
      ? `<p class="pe-range">between ${esc(fmt(money.low))} and ${esc(fmt(money.high))}</p>`
      : "";
  return `<div class="kpi"><div class="l">${esc(label)}</div><div class="v">${
    money ? `about ${esc(fmt(money.value))}` : "—"
  }</div>${spread}</div>`;
}

/**
 * One verdict line: the engine's own sentence (`summary`, the same words the PDF prints), or - only
 * when it has none - one read off the range here. Never both: they said the same thing twice.
 */
function verdictCard(view) {
  const b = view.benefit;
  const sentence = present(view.summary)
    ? view.summary
    : !b || !present(b.low) || !present(b.high)
      ? "The campaign's effect was measured."
      : b.low > 0
        ? "The campaign worked."
        : b.high < 0
          ? "The campaign made things worse."
          : "We cannot yet tell whether the campaign made a difference.";
  const tiles = tile(view.benefit_label, b, fmtPeople) + (view.net_value ? tile("Net value, after costs", view.net_value, fmtMoney) : "");
  return `<section class="card pe-verdict-card" data-verdict><div class="pe-body"><p class="pe-verdict">${esc(
    sentence,
  )}</p><div class="kpis pe-tiles">${tiles}</div></div></section>`;
}

/** Client-side checks first (a blank or negative amount), each next to its own field. */
function checkForm(form) {
  const problems = {};
  for (const [name, , type] of INPUTS) {
    if (type !== "number") continue;
    const raw = String(new FormData(form).get(name) || "").trim();
    if (raw === "") {
      if (name === "value_per_outcome") problems[name] = "Enter what one extra customer is worth.";
    } else if (!Number.isFinite(Number(raw)) || Number(raw) < 0) {
      problems[name] = "Enter an amount of zero or more.";
    }
  }
  return problems;
}

function showFieldErrors(form, problems) {
  for (const slot of form.querySelectorAll("[data-pe-err]")) {
    const name = slot.dataset.peErr;
    const text = problems[name] || "";
    slot.textContent = text;
    const field = slot.closest(".pe-field");
    if (field) field.classList.toggle("bad", Boolean(text));
    const input = form.querySelector(`[name="${name}"]`);
    if (input) {
      if (text) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }
  const first = Object.keys(problems)[0];
  if (first) {
    const input = form.querySelector(`[name="${first}"]`);
    if (input) input.focus();
  }
}

/** A 422 from the API names the field it refuses: put its words next to that field. */
function serverFieldErrors(error) {
  const detail = error && error.status === 422 && error.body ? error.body.detail : null;
  if (!Array.isArray(detail)) return null;
  const out = {};
  for (const item of detail) {
    const loc = Array.isArray(item.loc) ? item.loc : [];
    const name = loc[loc.length - 1];
    if (INPUTS.some(([n]) => n === name)) out[name] = item.msg || "This value is not accepted.";
  }
  return Object.keys(out).length ? out : null;
}

async function renderValue(app, runId, rerender, demo) {
  // A repaint of this screen (another module registered, the client changed) must not throw away
  // figures someone is typing.
  if (dirtyRun === runId && mounted && mounted.hash === window.location.hash && app.firstElementChild === mounted.main) return;
  dirtyRun = null;
  const crumbsFor = (name, href) => [
    { label: "Campaigns", href: "#/monitoring/runs" },
    name ? { label: name, href } : null,
    { label: "Value in rupees" },
  ];
  const screen = mount(
    app,
    `<main class="screen">${head(crumbsFor(null), "Value in rupees", null)}<div data-pe-body>${loadingRows}</div></main>`,
  );
  document.title = "Value in rupees · Marketing AI";
  const { main, live } = screen;
  const [view, page, runs] = await Promise.all([
    settle(getRoi(runId)),
    settle(getReportHtml(roiUrl(runId, "html"))),
    settle(listScoringRuns()),
  ]);
  if (!live()) return;

  if (view.error || !view.value) {
    main.innerHTML = `${head(crumbsFor(null), "Value in rupees", null)}${
      view.error
        ? errorBox(view.error, { retry: true })
        : noticeCard({
            title: "We could not find this campaign",
            text: "It may have been deleted, or the link is incomplete.",
            action: { label: "See all campaigns", href: "#/monitoring/runs" },
          })
    }`;
    screen.commit();
    return;
  }

  const v = view.value;
  const uc = v.use_case_id;
  const run = runs.value ? runs.value.runs.find((r) => r.run_id === runId) : null;
  const name = campaignTitle(demo, runId) || (run && run.use_case_name) || uc;
  const campaignHref = `#/campaign/${encodeURIComponent(uc)}/${encodeURIComponent(runId)}`;
  const measured = v.status === "measured";
  const when = run ? run.finished_at || run.created_at : null;
  const sub = [name, present(when) ? `scored ${fmtDate(when)}` : null].filter(Boolean).join(" · ");
  const actions = headActions({
    primary: measured ? { label: "Download PDF", href: roiUrl(runId, "pdf"), attrs: "download" } : null,
    secondary: [{ label: "See campaign results", href: campaignHref }],
  });
  const answer = measured
    ? `${verdictCard(v)}${page.value ? frame(page.value, `Value in rupees · ${name}`) : page.error ? errorBox(page.error, { retry: true }) : ""}`
    : `<section class="card">${emptyState({
        title: "Rupee value appears once the campaign's results are measured",
        text: present(v.results_available_on)
          ? `The campaign's results can be measured from ${fmtDate(v.results_available_on)}. Upload the outcomes then.`
          : "Upload the campaign's outcomes to measure it; the value follows from the result.",
        action: { label: "Upload outcomes", href: campaignHref },
      })}</section>`;
  const estimates = `<section class="card"><div class="pe-body"><details class="adv" data-pe-estimates${
    openEstimates.has(runId) ? " open" : ""
  }><summary>Your estimates</summary>${estimatesForm(view)}</details></div></section>`;

  main.innerHTML = `${pageHead(
    `${crumbs(crumbsFor(name, campaignHref))}<h1 class="h1">Value in rupees</h1>${sub ? `<p class="sub">${esc(sub)}</p>` : ""}${actions}`,
  )}<div class="stack">${answer}${estimates}</div>`;
  document.title = `Value in rupees · ${name} · Marketing AI`;
  wireFrames(main);
  screen.commit();

  const details = main.querySelector("[data-pe-estimates]");
  details.addEventListener("toggle", () => {
    if (details.open) openEstimates.add(runId);
    else openEstimates.delete(runId);
  });
  const element = main.querySelector("[data-pe-roi]");
  element.addEventListener("input", () => {
    dirtyRun = runId;
  });
  element.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = element.querySelector("[data-pe-roi-status]");
    const problems = checkForm(element);
    showFieldErrors(element, problems);
    if (Object.keys(problems).length) {
      status.textContent = "Check the highlighted figures.";
      return;
    }
    const data = new FormData(element);
    const payload = {};
    for (const [name, , type] of INPUTS) {
      const raw = String(data.get(name) || "").trim();
      if (type === "direction") payload[name] = data.get(name) === "good";
      else if (type === "number") {
        if (raw !== "") payload[name] = Number(raw); // a blank box is left out: the server says what is missing
      } else payload[name] = raw;
    }
    status.textContent = "Saving…";
    try {
      await putRoi(runId, payload);
      dirtyRun = null;
      openEstimates.add(runId);
      announceStatus("Saved. The value was recalculated.");
      rerender();
    } catch (error) {
      const fields = serverFieldErrors(error);
      if (fields) {
        showFieldErrors(element, fields);
        status.textContent = "Check the highlighted figures.";
      } else {
        status.innerHTML = errorBox(error);
      }
    }
  });
}

// --- the route ---------------------------------------------------------------------------------------

export async function renderPilot(app, parts, demo) {
  const [, second, third, fourth] = parts.map((p) => decodeURIComponent(p));
  try {
    if (second === "view" && (third === "readiness" || third === "results") && fourth) {
      await renderReport(app, third, fourth);
    } else if (second === "value" && third) {
      await renderValue(app, third, () => renderPilot(app, parts, demo), demo);
    } else if (second === "kit") {
      await renderKit(app, demo);
    } else {
      await renderReports(app, demo);
    }
  } catch (error) {
    mounted = null;
    app.innerHTML = `<main class="screen">${head([{ label: "Reports" }], "Reports", null)}${errorBox(error, {
      retry: true,
    })}</main>`;
  }
}
