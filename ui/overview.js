// The Overview screen, rendered from `GET /industries` (plan §9.1).
//
// One journey at a time. The API lists every industry file and names the one to open on
// (`default_industry`, telecom); the selector switches journey through the URL (`#/industry/<id>`),
// so a chosen industry survives a reload and the back button, and nothing is kept in memory.

import { esc, pageHead, typeChip } from "./dom.js";

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

function industrySelect(payload, current) {
  const industries = payload.industries || [];
  if (industries.length < 2) return "";
  return `<div class="control sel" style="width:220px"><select id="industry-select" aria-label="Industry">${industries
    .map(
      (i) =>
        `<option value="${esc(industryHref(payload, i.id))}"${i.id === current.id ? " selected" : ""}>${esc(
          i.name,
        )}</option>`,
    )
    .join("")}</select></div>`;
}

/** The caption, with the selector before it when there is more than one journey to choose from. */
function industryBar(payload, current) {
  const caption = `<span class="cap">Lifecycle stage → AI use cases</span>`;
  const select = industrySelect(payload, current);
  return select
    ? `<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">${select}${caption}</div>`
    : caption;
}

/** Wires the selector `overviewHtml` drew; changing it is a navigation, not a re-render. */
export function bindOverview(root) {
  const select = root.querySelector("#industry-select");
  if (!select) return;
  select.addEventListener("change", () => {
    window.location.hash = select.value;
  });
}

export function overviewHtml(payload, wanted = null) {
  const industry = chooseIndustry(payload, wanted);
  if (!industry) {
    return `<main class="screen">${pageHead(
      `<h1 class="h1">Marketing AI</h1>`,
    )}<p class="hint">No industry template is configured.</p></main>`;
  }
  const stages = industry.stages || [];
  const columns = stages
    .map(
      (stage) =>
        `<div class="stage-col t-${esc(stage.marker)}">
      <div class="stage-pill"><span class="n">${String(stage.order).padStart(2, "0")}</span>${esc(
        stage.name,
      )}</div>
      ${typeChip({ marker: stage.marker, stars: stage.stars, label: stage.type_label })}
      <div class="uc-list">${(stage.use_cases || [])
        .map(
          (u) =>
            `<a class="uc" href="#/uc/${esc(u.id)}"><span>${esc(
              u.name,
            )}</span><span class="chev" aria-hidden="true">›</span></a>`,
        )
        .join("")}</div>
    </div>`,
    )
    .join("");
  const grid = `grid-template-columns:repeat(${stages.length},minmax(200px,1fr));min-width:${
    stages.length * 238
  }px`;
  return `<main class="screen">
    ${pageHead(`<h1 class="h1">Marketing AI</h1><p class="sub">${esc(industry.journey_label)}</p>`)}
    <div class="bar">${industryBar(payload, industry)}
      <div class="legend"><span class="lbl">Legend</span>${(industry.legend || [])
        .map((entry) => typeChip(entry))
        .join("")}</div></div>
    <div class="timeline-wrap"><div class="timeline" style="${grid}">${columns}</div></div>
    <p class="hint">Select a use case to see its Data → Model → Output pipeline.</p>
  </main>`;
}
