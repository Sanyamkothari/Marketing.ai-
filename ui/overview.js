// Home: the use-case catalogue, rendered from `GET /industries` (plan §9.1; v1 docs/UI_AUDIT.md 5.1).
//
// One journey at a time. The API lists every industry file and names the one to open on
// (`default_industry`, telecom); the "Industry" chooser in the header switches journey through the URL
// (`#/industry/<id>`), so a chosen industry survives a reload and the back button.
//
// The header is breadcrumb "Home", the journey's own label as the H1 and one sentence of what to do.
// The AI type of each use case is its card's coloured edge, explained once by "What do the colours
// mean?" (the legend and the per-stage type chips said the same thing three times). A planned card
// is muted and tagged "Coming soon"; in demo mode the demo's use cases are tagged "Sample data ready",
// and an AI-writing use case that has no AI service here "Needs AI service" (the notice it opens to).
//
// What only the browser can know - the demo, whether any model exists yet, the "For you" line a phase
// module adds (`registerForYou`) - is read after the first paint and asks for a repaint when it
// arrives (`MODULES_EVENT`, which `app.js` redraws the route on without a skeleton). This file is
// also imported under plain node (`tests/integration/test_ui_journey.py` runs `journeyFor`), so
// nothing here touches `window` or `document` at import time, and the browser-only modules
// (`api.js`, `availability.js`) are imported on demand.

import { MODULES_EVENT, esc, pageHead } from "./dom.js";

/** The industry `wanted` names, else the API's default, else the first listed. */
export function chooseIndustry(payload, wanted) {
  const industries = payload.industries || [];
  return (
    industries.find((i) => i.id === wanted) ||
    industries.find((i) => i.id === payload.default_industry) ||
    industries[0] ||
    null
  );
}

/** The route of an industry's overview; the default one keeps the bare `#/` it always had. */
export const industryHref = (payload, id) =>
  id === payload.default_industry ? "#/" : `#/industry/${encodeURIComponent(id)}`;

/**
 * Where a use case's screens lead back to, as `{ href, label }`: the journey of the industry file
 * that lists it, among its available and planned cards alike, so a planned card's 404 still goes
 * back to the journey it was opened from. Industries are searched in the order the API lists them,
 * default first, so a use case two files share belongs to the default journey. One that no file
 * lists goes back to the default journey; null only when there is no industry at all.
 */
export function journeyFor(payload, useCaseId) {
  const industries = (payload && payload.industries) || [];
  const lists = (industry) =>
    (industry.stages || []).some((stage) => (stage.use_cases || []).some((u) => u.id === useCaseId));
  const owner = industries.find(lists) || chooseIndustry(payload || {}, null);
  return owner ? { href: industryHref(payload, owner.id), label: owner.journey_label } : null;
}

const cardsOf = (industry) => (industry.stages || []).flatMap((stage) => stage.use_cases || []);

/** Whether a card can be opened today (`status` is "available"; a card without one is). */
export const isAvailable = (u) => !u.status || u.status === "available";

/** Industries with something to open first, in the API's order; the rest after, marked "coming soon". */
export function industryOptions(payload) {
  const industries = payload.industries || [];
  const live = (i) => cardsOf(i).some(isAvailable);
  return [...industries.filter(live), ...industries.filter((i) => !live(i))].map((i) => ({
    id: i.id,
    href: industryHref(payload, i.id),
    label: live(i) ? i.name : `${i.name} (coming soon)`,
  }));
}

function industryChooser(payload, current) {
  const options = industryOptions(payload);
  if (options.length < 2) return "";
  return `<div class="ov-industry"><label for="industry-select">Industry</label><div class="control sel"><select id="industry-select">${options
    .map((o) => `<option value="${esc(o.href)}"${o.id === current.id ? " selected" : ""}>${esc(o.label)}</option>`)
    .join("")}</select></div></div>`;
}

/** Wires the chooser `overviewHtml` drew; changing it is a navigation, not a re-render. */
export function bindOverview(root) {
  const select = root.querySelector("#industry-select");
  if (!select) return;
  select.addEventListener("change", () => {
    window.location.hash = select.value;
  });
}

// --- "For you": one next step per role, from whichever module knows the role ---------------------

const forYou = [];

/**
 * Add to Home's "For you" line. `provider()` returns `{ text, href }`, a list of them, or nothing; it
 * is asked on every paint, so it answers from state it already holds, and calls for a repaint
 * (`announceModulesChanged`) when that state changes.
 */
export function registerForYou(provider) {
  if (typeof provider !== "function") throw new Error("registerForYou needs a function");
  forYou.push(provider);
}

function forYouHtml() {
  const items = forYou
    .flatMap((provider) => {
      try {
        return [].concat(provider() || []);
      } catch {
        return [];
      }
    })
    .filter((item) => item && item.text && item.href);
  if (!items.length) return "";
  return `<p class="foryou"><span class="fy-l">For you</span>${items
    .map((item) => `<a href="${esc(item.href)}">${esc(item.text)}</a>`)
    .join("")}</p>`;
}

// --- what only the browser can tell: the demo, the AI service, whether any model exists ----------

const extras = { at: 0, busy: false, demo: null, needsAi: new Set(), noRuns: false };
const STALE_MS = 30000;

async function readExtras(industry) {
  const [{ API_BASE, getUseCase }, { demoStatus, needsAiNotice }] = await Promise.all([
    import("./api.js"),
    import("./availability.js"),
  ]);
  const demo = await demoStatus();
  const needsAi = new Set();
  if (demo && demo.demo_mode) {
    const writing = cardsOf(industry).filter((u) => isAvailable(u) && u.ai_type === "generative");
    await Promise.all(
      writing.map(async (u) => {
        try {
          if (await needsAiNotice(await getUseCase(u.id))) needsAi.add(u.id);
        } catch {
          // a use case that cannot be read is tagged nothing; opening it explains itself
        }
      }),
    );
  }
  let noRuns = false;
  try {
    const response = await fetch(`${API_BASE}/runs?limit=1`);
    if (response.ok) noRuns = ((await response.json()).runs || []).length === 0;
  } catch {
    noRuns = false; // unknown is not "empty": say nothing
  }
  return { demo, needsAi, noRuns };
}

const signature = (e) => JSON.stringify([e.demo && e.demo.seeded, [...e.needsAi].sort(), e.noRuns]);

/** Read the extras once a while (never on every repaint), and repaint only when they changed. */
function refreshExtras(industry) {
  if (typeof window === "undefined" || extras.busy || Date.now() - extras.at < STALE_MS) return;
  extras.busy = true;
  const before = signature(extras);
  readExtras(industry)
    .then((next) => Object.assign(extras, next))
    .catch(() => {})
    .finally(() => {
      extras.busy = false;
      extras.at = Date.now();
      if (signature(extras) !== before) window.dispatchEvent(new Event(MODULES_EVENT));
    });
}

/** The demo's use cases: the ones it seeded a model (and a scoring run) for. */
function sampleIds() {
  const demo = extras.demo;
  if (!demo || !demo.demo_mode || !demo.seeded || !demo.manifest) return new Set();
  return new Set([demo.manifest.use_case_id, demo.manifest.uplift_use_case_id].filter(Boolean));
}

// --- the page -----------------------------------------------------------------------------------

/** Colour names for the three AI-type markers, as the coloured card edges show them. */
const COLOUR = { P: "Blue", G: "Purple", H: "Green" };

function coloursTip(industry) {
  const rows = (industry.legend || [])
    .map(
      (entry) =>
        `<span class="ov-key t-${esc(entry.marker)}"><span class="ov-sw" aria-hidden="true"></span>${esc(
          COLOUR[entry.marker] || entry.marker,
        )}: ${esc(entry.label)}</span>`,
    )
    .join("");
  if (!rows) return "";
  return `<span class="toggletip ov-colours"><button type="button" class="btn quiet sm" data-toggletip aria-expanded="false">What do the colours mean?</button><span class="tt-pop" role="status" hidden><span class="ov-keys">The coloured edge of each card shows the kind of AI it uses.${rows}</span></span></span>`;
}

function cardHtml(u, sample) {
  const planned = !isAvailable(u);
  const tags = [
    planned ? `<span class="chip neutral">Coming soon</span>` : "",
    !planned && sample.has(u.id) ? `<span class="pill ok">Sample data ready</span>` : "",
    !planned && extras.needsAi.has(u.id) ? `<span class="chip neutral">Needs AI service</span>` : "",
  ].join("");
  return `<a class="uc${planned ? " planned" : ""}" href="#/uc/${esc(u.id)}"><span class="uc-b"><span>${esc(
    u.name,
  )}</span>${tags ? `<span class="uc-tags">${tags}</span>` : ""}</span><span class="chev" aria-hidden="true">›</span></a>`;
}

export function overviewHtml(payload, wanted = null) {
  injectStyles();
  const industry = chooseIndustry(payload, wanted);
  const home = `<nav class="crumbs" aria-label="Breadcrumb"><span class="cur" aria-current="page">Home</span></nav>`;
  if (!industry) {
    return `<main class="screen ov">${pageHead(
      `${home}<h1 class="h1">Home</h1>`,
    )}<section class="card notice-card" role="status"><h3>No use cases yet</h3><p>No industry template is configured on this installation. An administrator adds one under configs/industries.</p></section></main>`;
  }
  refreshExtras(industry);
  const sample = sampleIds();
  const stages = industry.stages || [];
  const columns = stages
    .map(
      (stage) =>
        `<div class="stage-col t-${esc(stage.marker)}">
      <div class="stage-pill">${esc(stage.name)}</div>
      <div class="uc-list">${(stage.use_cases || []).map((u) => cardHtml(u, sample)).join("")}</div>
    </div>`,
    )
    .join("");
  const empty =
    extras.noRuns && !sample.size
      ? `<p class="ov-line">No models yet. Choose a use case below and upload your data to train the first one.</p>`
      : "";
  return `<main class="screen ov">
    ${pageHead(
      `${home}<h1 class="h1">${esc(industry.journey_label)}</h1><p class="desc">Pick what you want to predict or improve. Each card opens its setup.</p>${industryChooser(
        payload,
        industry,
      )}`,
    )}
    ${forYouHtml()}${empty}
    <div class="ov-tools">${coloursTip(industry)}</div>
    <div class="timeline-wrap"><div class="timeline">${columns}</div></div>
  </main>`;
}

// Home's own layout rules, beside the prototype's `.timeline` / `.stage-*` / `.uc` in `index.html`:
// tokens only, so both themes apply. The stage columns size themselves (no inline style) and stack
// below 700px with no arrows.
const CSS = `
.ov .ov-industry{display:flex;align-items:center;gap:8px;margin-top:4px}
.ov .ov-industry label{font-size:12px;font-weight:500;color:var(--muted)}
.ov .ov-industry .control{width:240px;height:36px}
.ov .foryou{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin:0 0 16px;font-size:13px}
.ov .foryou .fy-l{font-size:12px;font-weight:600;color:var(--muted)}
.ov .foryou a{color:var(--brand-blue);font-weight:500}
.ov .foryou a:hover{text-decoration:underline}
.ov .ov-line{margin:0 0 16px;font-size:13px;color:var(--muted)}
.ov .ov-tools{display:flex;justify-content:flex-end}
.ov .ov-keys{display:flex;flex-direction:column;gap:8px}
.ov .ov-key{display:flex;align-items:center;gap:8px;color:var(--ink)}
.ov .ov-sw{width:4px;height:16px;border-radius:2px;background:var(--c)}
.ov .timeline-wrap{margin-top:16px}
.ov .timeline{grid-template-columns:none;grid-auto-flow:column;grid-auto-columns:minmax(200px,1fr);min-width:0}
.ov .uc-b{display:flex;flex-direction:column;align-items:flex-start;gap:8px;min-width:0}
.ov .uc-tags{display:flex;flex-wrap:wrap;gap:4px}
.ov .uc.planned{background:var(--soft);border-color:var(--line);color:var(--muted)}
.ov .uc.planned::before{background:var(--line2)}
@media (max-width:700px){
  .ov .timeline{grid-auto-flow:row;grid-template-columns:minmax(0,1fr);row-gap:24px}
  .ov .stage-col:not(:last-child) .stage-pill::after{content:none}
  .ov .ov-tools{justify-content:flex-start}
  .ov .ov-industry .control{width:auto;flex:1}
}
`;

function injectStyles() {
  if (typeof document === "undefined" || !document.head || document.getElementById("ov-styles")) return;
  const style = document.createElement("style");
  style.id = "ov-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}
