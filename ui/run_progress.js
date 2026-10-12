// Plan M M189 (DEC-1619): one reading of a run's progress for every screen that shows one - the goal's
// Running screen (ui/usecase.js), the New campaign wizard's learning step and the uplift workbench.
//
// `status.json` carries two numbers. `progress_pct` is done stages over total, every stage weighted the same,
// so a training run sat at 50% while it trained (almost all of its time). `estimated_pct` weights each stage
// by how long it is expected to take and credits the running one for the time it has run, with `eta_seconds`
// beside it. This prefers the estimate and falls back to `progress_pct` for a status without one. While the
// running stage takes longer than expected the server sends no time left: the bar then moves on its own
// ("Finishing up…") instead of showing a number that has stopped.

// The two rules this helper's markup needs, injected once from here rather than added to ui/index.html (whose
// shared styles another milestone restyles): the time-left line, and the bar that moves on its own while a
// run is "Finishing up…". Their own class names, so they do not lean on `.run-meter-fill`'s geometry; no new
// colour (the moving fill borrows the bar's own fill colour through `.run-meter-fill`, the line `--muted`);
// with reduced motion the busy fill stands still across the whole bar.
const STYLE_ID = "run-progress-styles";
const CSS = `
.run-eta{margin:6px 0 0;font-size:12px;color:var(--muted)}
.run-meter .run-busy-fill{width:100%;opacity:.45}
@media (prefers-reduced-motion:no-preference){
  .run-meter .run-busy-fill{width:30%;opacity:1;animation:run-busy 1.4s ease-in-out infinite}
  @keyframes run-busy{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}
}
`;

/** Add the helper's style sheet to the page once; a no-op where there is no document (node, server). */
export function injectRunProgressStyles(doc = typeof document === "undefined" ? null : document) {
  if (!doc || !doc.head || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

const num = (value) => (typeof value === "number" && !Number.isNaN(value) ? value : null);
const clamp = (value) => Math.max(0, Math.min(100, Math.round(value)));

/**
 * "Less than a minute left", "About a minute left", "About 3 minutes left", "About 25 minutes left",
 * "About 1 hour 20 minutes left": minutes under ten to the minute, under an hour to five, beyond to ten.
 * Empty for no number.
 */
export function etaText(seconds) {
  const s = num(seconds);
  if (s === null || s < 0) return "";
  if (s < 60) return "Less than a minute left";
  const minutes = s / 60;
  if (minutes < 1.5) return "About a minute left";
  if (minutes < 10) return `About ${Math.round(minutes)} minutes left`;
  if (minutes < 57.5) return `About ${Math.max(10, Math.round(minutes / 5) * 5)} minutes left`;
  const rounded = Math.round(minutes / 10) * 10;
  const hours = Math.floor(rounded / 60);
  const rest = rounded % 60;
  const h = `${hours} ${hours === 1 ? "hour" : "hours"}`;
  return rest ? `About ${h} ${rest} minutes left` : `About ${h} left`;
}

/**
 * The reading of a `RunStatus` (or `null`): `pct` (0-100, or null when there is none), whether it is the
 * time-weighted estimate, whether the run is `finishing` (running past what was expected), and the
 * `eta` sentence ("" when there is none to give).
 */
export function runProgress(status) {
  if (!status) return { pct: null, estimated: false, finishing: false, eta: "" };
  const estimate = num(status.estimated_pct);
  const fallback = num(status.progress_pct);
  const running = status.state === "running";
  const finishing = estimate !== null && running && num(status.eta_seconds) === null;
  const pct = estimate !== null ? clamp(estimate) : fallback !== null ? clamp(fallback) : null;
  const eta = estimate !== null && running && !finishing ? etaText(status.eta_seconds) : "";
  return { pct, estimated: estimate !== null, finishing, eta };
}

/** The "N%" pill beside a running screen's title; nothing while finishing up or without a number. */
export function progressBadgeHtml(progress) {
  if (!progress || progress.finishing || progress.pct === null) return "";
  return `<span class="run-pct">${progress.pct}%</span>`;
}

/**
 * The bar. Determinate with the reading; while finishing up, a bar that moves on its own (still when the
 * person asked for reduced motion) with no number on it. `extraClass` adds a screen's own class.
 */
export function progressMeterHtml(progress, extraClass = "") {
  if (!progress || (progress.pct === null && !progress.finishing)) return "";
  injectRunProgressStyles();
  const cls = extraClass ? ` ${extraClass}` : "";
  if (progress.finishing) {
    return `<div class="run-meter run-meter-busy${cls}" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuetext="Finishing up" data-run-busy><div class="run-meter-fill run-busy-fill"></div></div>`;
  }
  return `<div class="run-meter${cls}" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.pct}"><div class="run-meter-fill" style="width:${progress.pct}%"></div></div>`;
}

/** The line under the bar: "About 3 minutes left", or "Finishing up…"; nothing without either. */
export function progressNoteHtml(progress) {
  if (!progress) return "";
  const text = progress.finishing ? "Finishing up…" : progress.eta;
  if (text) injectRunProgressStyles();
  return text ? `<p class="run-eta" data-run-eta>${text}</p>` : "";
}
