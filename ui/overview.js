// The Overview screen, rendered from `GET /industries` (plan §9.1).

import { esc, pageHead, typeChip } from "./dom.js";

export function overviewHtml(payload) {
  const industry = (payload.industries || [])[0];
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
    <div class="bar"><span class="cap">Lifecycle stage → AI use cases</span>
      <div class="legend"><span class="lbl">Legend</span>${(industry.legend || [])
        .map((entry) => typeChip(entry))
        .join("")}</div></div>
    <div class="timeline-wrap"><div class="timeline" style="${grid}">${columns}</div></div>
    <p class="hint">Select a use case to see its Data → Model → Output pipeline.</p>
  </main>`;
}
