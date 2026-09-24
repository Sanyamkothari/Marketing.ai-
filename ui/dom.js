// The prototype's rendering helpers, plus the one rule that runs through every screen: a value
// nobody measured is an em dash, never a sample number (plan §13.3). Since v1 (docs/ui/FOUNDATION.md)
// this file is also the one home of the shared components: the page header (logo only on the right),
// the breadcrumb, header actions, the plain-language error, empty, not-found and loading states,
// "Technical details", tables with "Show more columns", and the one number locale.

export const EM_DASH = "—";

export const LOGO = `<svg class="logo" viewBox="0 0 2576 690" role="img" aria-label="Minfy"><rect class="b" x="0" y="0" width="493" height="493"/><rect class="y" x="246" y="0" width="247" height="246"/><g fill="none" class="s" stroke-width="87"><path d="M719.5 493V338A121.5 121.5 0 0 1 962.5 338V493"/><path d="M961 493V338A121 121 0 0 1 1203 338V493"/><path d="M1548.5 493V338A120.5 120.5 0 0 1 1789.5 338V493"/><path d="M1962.5 493V182A121 121 0 0 1 2204.5 182"/><path d="M2291 354A120.75 120.75 0 0 0 2532.5 354"/><path d="M2532.5 182V520A120.5 120.5 0 0 1 2319.7 597.5"/></g><circle class="b" cx="1376" cy="61" r="61"/><rect class="b" x="1332" y="182" width="87" height="311"/><rect class="b" x="2006" y="268" width="156" height="86"/><rect class="y" x="2248" y="182" width="86" height="86"/></svg>`;

export const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** True only for a value the API actually sent. `0` and `false` are values; `null` is not. */
export const present = (v) => v !== null && v !== undefined && v !== "";

/** A value, or the em dash. `fmt` only ever runs on a value that is present. */
export const dash = (v, fmt = (x) => String(x)) => (present(v) ? fmt(v) : EM_DASH);

/** A number with at most `places` decimals and no trailing zeroes: 0.9000 → "0.9", 70 → "70". */
export function fmtNum(value, places = 2) {
  const fixed = Number(value).toFixed(places);
  if (!fixed.includes(".")) return fixed;
  return fixed.replace(/0+$/, "").replace(/\.$/, "");
}

/** The prototype's compact row count: a million rows reads "1.0M", four thousand reads "4K".
 * Never where a user compares numbers - use `fmtInt` there. */
export const fmtN = (n) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);

/**
 * One number locale for every count and rate on every screen, so digit grouping matches the
 * sentences the engine writes ("24,000"). Dates keep the Indian day-month order. Every formatter
 * below reads these two constants; no other file names a locale (docs/ui/FOUNDATION.md).
 */
export const NUMBER_LOCALE = "en-US";
export const DATE_LOCALE = "en-IN";

export const fmtInt = (n) => Number(n).toLocaleString(NUMBER_LOCALE);

/** A count of people: always whole ("281", never "280.9"). */
export const fmtPeople = (n) => Number(n).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: 0 });

/** Rupees, whole, built by `Intl` - so no currency sign is typed into any source file. */
export const fmtMoney = (n) =>
  new Intl.NumberFormat(NUMBER_LOCALE, { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(
    Number(n),
  );

export const fmtPct = (fraction, places = 1) => `${fmtNum(Number(fraction) * 100, places)}%`;

/** A model metric (ROC-AUC, AUUC, ...): at most three decimals. */
export const fmtMetric = (v) => fmtNum(v, 3);

export const fmtSize = (b) =>
  b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

/** The prototype's timestamp format, applied to an ISO instant from the API. */
export function fmtStamp(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at
    .toLocaleString(DATE_LOCALE, {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
    .replace(/(\d{4}),?\s*/, "$1, ");
}

/** A date alone, for the date-range line on the Data page. */
export function fmtDate(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at.toLocaleDateString(DATE_LOCALE, { day: "2-digit", month: "short", year: "numeric" });
}

export const typeChip = (entry) =>
  `<span class="chip type t-${esc(entry.marker)}">${esc(entry.stars)} ${esc(
    entry.label || entry.type_label || "",
  )}</span>`;

export const stageChip = (s) => `<span class="chip stage"><span>Stage</span>${esc(s)}</span>`;

export const kvs = (pairs) =>
  pairs
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join("");

export function bandPill(v) {
  const cls = /High|Critical/.test(v) ? "bad" : /Medium/.test(v) ? "warn" : "ok";
  return `<span class="pill ${cls}">${esc(v)}</span>`;
}

export const table = (cols, rows, cls = "", band = -1) =>
  `<div class="tbl-wrap"><table class="${cls}"><thead><tr>${cols
    .map((c) => `<th>${esc(c)}</th>`)
    .join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${r.map((v, j) => `<td>${j === band ? bandPill(v) : esc(v)}</td>`).join("")}</tr>`)
    .join("")}</tbody></table></div>`;

/**
 * A table in the one v1 pattern: `cols` are `{ label, num, more }` - `more` columns sit behind "Show
 * more columns" (at most 5 without it), `num` right-aligns with tabular figures - and `rows` are arrays
 * of cell HTML the caller has already escaped. Below 700px each row becomes a stacked card, every cell
 * labelled from its column. State the default sort in the card title (`sortNote`).
 */
export function dataTable(cols, rows, { cls = "", moreLabel = "Show more columns" } = {}) {
  const classes = (c) => [c.num ? "num" : "", c.more ? "more" : ""].filter(Boolean).join(" ");
  const attr = (c) => (classes(c) ? ` class="${classes(c)}"` : "");
  const hidden = cols.filter((c) => c.more).length;
  const toggle = hidden
    ? `<details class="tbl-more"><summary>${esc(moreLabel)} (${hidden})</summary></details>`
    : "";
  return `${toggle}<div class="tbl-wrap"><table class="tbl tbl-stack${cls ? ` ${esc(cls)}` : ""}"><thead><tr>${cols
    .map((c) => `<th${attr(c)} scope="col">${esc(c.label)}</th>`)
    .join("")}</tr></thead><tbody>${rows
    .map(
      (r) =>
        `<tr>${r
          .map((v, j) => {
            const col = cols[j] || {};
            return `<td${attr(col)} data-label="${esc(col.label || "")}">${v}</td>`;
          })
          .join("")}</tr>`,
    )
    .join("")}</tbody></table></div>`;
}

/** " (newest first)" after a card title: every table states its default sort. */
export const sortNote = (text) => ` <span class="sort-note">(${esc(text)})</span>`;

export const kpis = (arr) =>
  `<div class="kpis">${arr
    .map(([l, v]) => `<div class="kpi"><div class="l">${esc(l)}</div><div class="v">${esc(v)}</div></div>`)
    .join("")}</div>`;

export const steps = (arr) =>
  `<ol class="steps">${arr
    .map(
      ([a, b], i) =>
        `<li><span class="no">${i + 1}</span><span><div class="s1">${esc(a)}</div><div class="s2">${esc(
          b,
        )}</div></span></li>`,
    )
    .join("")}</ol>`;

// --- the breadcrumb ---------------------------------------------------------------------------------

/**
 * The journey a use-case screen belongs to: `uc.journey`, the `{ href, label }` of the industry
 * journey that lists it, which the router resolves from `GET /industries` (`journeyFor` in
 * `overview.js`). The label is that industry's own `journey_label` - its file calls it the
 * "overview subtitle and breadcrumb root" - so a Banking use case reads "Client Lifecycle" and goes
 * back to Banking, not to the Telecom journey the bare `#/` opens. With no journey (no industry file
 * is configured) the breadcrumb is Home alone.
 */
const journeyOf = (uc) => (uc && uc.journey) || null;

const HOME = { label: "Home", href: "#/" };
const CRUMB_SEP = `<span class="sep" aria-hidden="true">›</span>`;

const crumbLink = ({ label, href }) =>
  href ? `<a href="${esc(href)}">${esc(label)}</a>` : `<span class="cur" aria-current="page">${esc(label)}</span>`;

/**
 * Every screen's breadcrumb: `Home › ... › current`. `items` are `{ label, href }`; the last one, with
 * no `href`, is the screen itself. The root is always Home, added here and never by the caller.
 */
export function crumbs(items = []) {
  const trail = [HOME, ...items.filter((item) => item && item.label && item !== HOME)];
  return `<nav class="crumbs" aria-label="Breadcrumb">${trail.map(crumbLink).join(CRUMB_SEP)}</nav>`;
}

/** The breadcrumb above a use case's title: Home › journey › the use case. */
export function backLink(uc) {
  return crumbs([journeyOf(uc), uc && uc.name ? { label: uc.name } : null]);
}

/** The first links of a use case's breadcrumb, `Home › journey`, for a caller that adds the rest. */
export function journeyCrumb(uc) {
  return [HOME, journeyOf(uc)].filter(Boolean).map(crumbLink).join(CRUMB_SEP);
}

/** The `{ href, label }` a "Back to ..." button on a use-case screen returns to. */
export const journeyBack = (uc) => journeyOf(uc) || HOME;

// --- the page header ----------------------------------------------------------------------------------

// The header tool: `modules/router.js`'s `registerHeaderTool` fills it - the client picker (Plan A
// M35) - and this file imports nothing back: `modules/production/boot.js` reaches this file while
// `router.js` is still evaluating, so a path from here to the router would be a cycle (DEC-790).
// Since v1 the top bar (`chrome.js`) draws it, in its context slot; `pageHead` no longer does.
// `{ name, html() }`, or `null`.
let headerTool = null;

/** Called by `registerHeaderTool` only; a phase module registers through the router. */
export function setHeaderTool(tool) {
  headerTool = tool;
}

/** The header tool's markup, or `""` when none is registered. */
export function headerToolHtml() {
  return headerTool ? headerTool.html() : "";
}

/**
 * Every screen's header: breadcrumb, H1, one-sentence description, at most one chip and the header
 * actions on the left; the logo alone on the right (the owner's request). The client picker lives in
 * the top bar (`chrome.js`), not here.
 */
export function pageHead(inner) {
  return `<div class="head"><div class="titles">${inner}</div>${LOGO}</div><div class="rule"></div>`;
}

/** One header button or link: an HTML string as it is, or `{ label, href | id, kind, attrs }`. */
function actionHtml(action, kind) {
  if (!action) return "";
  if (typeof action === "string") return action;
  const related = kind === "related";
  const cls = related ? "related" : `btn ${action.kind || kind}`;
  const attrs = `${action.id ? ` id="${esc(action.id)}"` : ""}${action.attrs ? ` ${action.attrs}` : ""}`;
  const label = `${esc(action.label)}${related ? " ›" : ""}`;
  return action.href
    ? `<a class="${cls}" href="${esc(action.href)}"${attrs}>${label}</a>`
    : `<button type="button" class="${cls}"${attrs}>${label}</button>`;
}

/**
 * The header's actions in the one order: at most one primary, then secondaries, then one quiet
 * related link. Each is an HTML string or `{ label, href | id, attrs }`. Empty with nothing to offer.
 */
export function headActions({ primary = null, secondary = [], related = null } = {}) {
  const parts = [
    actionHtml(primary, "primary"),
    ...[].concat(secondary || []).map((a) => actionHtml(a, "secondary")),
    actionHtml(related, "related"),
  ].filter(Boolean);
  return parts.length ? `<div class="head-actions">${parts.join("")}</div>` : "";
}

// --- the plain-language catalogue (configs/pilot/help.yaml, served as `GET /pilot/help`) -------------
// Filled through `registerGlossary` in `modules/router.js` (the pilot module reads the catalogue once),
// and kept here, like the header tool, because this file may not import the router (DEC-790).

const none = () => null;
let glossary = { code: none, term: none, metric: none, setting: none };

const lookup = (source) => {
  if (typeof source === "function") return source;
  if (!source || typeof source !== "object") return none;
  return (key) => (Object.prototype.hasOwnProperty.call(source, key) ? source[key] : null);
};

/**
 * `{ code, term, metric, setting }`, each a lookup function or a plain map - or the catalogue as
 * `GET /pilot/help` answers it, `{ codes, terms, metrics, settings }`. `null` clears it.
 */
export function setGlossary(source) {
  const g = source || {};
  glossary = {
    code: lookup(g.code || g.codes),
    term: lookup(g.term || g.terms),
    metric: lookup(g.metric || g.metrics),
    setting: lookup(g.setting || g.settings),
  };
}

const safely = (fn, key) => {
  if (key === null || key === undefined) return null;
  try {
    return fn(key) || null;
  } catch {
    return null;
  }
};

/** `{ title, meaning, fix }` for a check or error code, or `null` when the catalogue has none. */
export const glossaryCode = (code) => safely(glossary.code, code);
/** The one-line explanation of a term ("champion", "uplift"), or `null`. */
export const glossaryTerm = (term) => safely(glossary.term, term);
/** A metric's plain entry, or `null`. */
export const glossaryMetric = (name) => safely(glossary.metric, name);
/** A setting's plain entry (`{ meaning }`), or `null`. */
export const glossarySetting = (path) => safely(glossary.setting, path);

// --- states: error, not found, notice, empty, loading ------------------------------------------------

export const GENERIC_ERROR = "Something went wrong while loading this page.";

/**
 * The plain sentence an error leads with: the catalogue's title for its code; else a sentence for the
 * kind of failure. A refusal the server already words for people (a 4xx such as "This is the last
 * active Admin.") leads with that sentence; a server fault or an unknown failure never shows raw text.
 */
export function plainError(error) {
  const err = error || {};
  const entry = err.code ? glossaryCode(err.code) : null;
  if (entry && entry.title) return entry.title;
  const status = Number(err.status);
  if (err.code === "NETWORK_ERROR") return "We could not reach Marketing AI. Check your connection and try again.";
  if (status === 404) return "We could not find what this page asks for. It may have been deleted, or the link is incomplete.";
  if (status >= 400 && status < 500 && err.message) return err.message;
  return GENERIC_ERROR;
}

/**
 * An error, calmly: the plain sentence first (`title` overrides it), the catalogue's fix when it has
 * one, an optional "Try again" (`retry: true`; `app.js` repaints the current screen on `[data-retry]`),
 * and the code with the server's message inside a closed "Details". Everything stays in the `.apierr`
 * element's DOM, so a test reading its text still finds the code and the server's words.
 */
export function errorBox(error, { title = null, retry = false } = {}) {
  const err = error || {};
  const code = err.code || "ERROR";
  const message = err.message || String(error);
  const lead = title || plainError(err);
  const entry = err.code ? glossaryCode(err.code) : null;
  const fix = entry && entry.fix ? `<p class="apierr-fix">${esc(entry.fix)}</p>` : "";
  const again = retry
    ? `<div class="btn-row"><button type="button" class="btn secondary sm" data-retry>Try again</button></div>`
    : "";
  const said = message && message !== lead ? ` ${esc(message)}` : "";
  return `<div class="apierr" role="alert" data-code="${esc(code)}"><b>${esc(
    lead,
  )}</b>${fix}${again}<details class="tech"><summary>Details</summary><p class="mono"><code>${esc(
    code,
  )}</code>${said}</p></details></div>`;
}

/**
 * Ids, hashes, codes and versions behind one closed disclosure, each value in monospace with a copy
 * button (`[data-copy]`, handled once by `chrome.js`). `pairs` are `[label, value]`; a value that is
 * not present is left out, and with nothing left the disclosure is not drawn.
 */
export function techDetails(pairs, summary = "Technical details") {
  const rows = (pairs || [])
    .filter(([, value]) => present(value))
    .map(
      ([label, value]) =>
        `<div><dt>${esc(label)}</dt><dd><code>${esc(value)}</code><button type="button" class="btn quiet sm" data-copy="${esc(
          value,
        )}" aria-label="Copy ${esc(label)}">Copy</button></dd></div>`,
    )
    .join("");
  return rows
    ? `<details class="tech"><summary>${esc(summary)}</summary><dl class="tech-list">${rows}</dl></details>`
    : "";
}

/**
 * A calm notice (DEC-954): a title, one or two sentences and at most one button (`action` as in
 * `headActions`). For a feature this environment cannot offer, or a planned use case.
 */
export function noticeCard({ title, text = [], action = null, attrs = "" } = {}) {
  const body = []
    .concat(text)
    .filter(Boolean)
    .map((t) => `<p>${esc(t)}</p>`)
    .join("");
  const act = action ? `<div class="notice-actions">${actionHtml(action, action.kind || "secondary")}</div>` : "";
  return `<section class="card notice-card" role="status"${attrs ? ` ${attrs}` : ""}><h3>${esc(
    title,
  )}</h3>${body}${act}</section>`;
}

/**
 * Something the URL names that is not there: one calm card and one way back. `what` is the plain noun
 * ("run", "schedule"); `back` is `{ href, label }`, drawn as "Back to <label>". The error, when given,
 * is kept under "Details".
 */
export function notFound(what, back = HOME, error = null) {
  const target = back || HOME;
  const tech = error
    ? techDetails([
        ["Code", error.code || "ERROR"],
        ["Message", error.message || String(error)],
      ], "Details")
    : "";
  return `<section class="card notice-card" role="status" data-not-found><h3>This ${esc(
    what,
  )} could not be found</h3><p>It may have been deleted, or the link is incomplete.</p><div class="notice-actions"><a class="btn secondary" href="${esc(
    target.href,
  )}">Back to ${esc(target.label)}</a></div>${tech ? `<div class="card-body">${tech}</div>` : ""}</section>`;
}

/**
 * An empty list or card: what will appear here, and the one action that fills it (`{ label, href |
 * id, attrs }`, a primary button unless `action.kind` says otherwise).
 */
export function emptyState({ title, text = "", action = null } = {}) {
  const act = action ? actionHtml(action, action.kind || "primary") : "";
  return `<div class="empty-state"><p class="es-t">${esc(title)}</p>${text ? `<p>${esc(text)}</p>` : ""}${act}</div>`;
}

/**
 * Grey blocks in the shape of the page, painted at once while its data loads: never a blank screen and
 * never the previous one. `kind` is "page" (four tiles and two cards), "list" or "form"; `title` shows
 * the real H1 when it is already known.
 */
export function skeleton(kind = "page", { title = null } = {}) {
  const bar = (cls) => `<span class="skel ${cls}"></span>`;
  const h1 = title ? `<h1 class="h1">${esc(title)}</h1>` : bar("skel-h1");
  let body;
  if (kind === "list") body = `<div class="skel-list">${bar("skel-row").repeat(5)}</div>`;
  else if (kind === "form") body = `<div class="skel-cards">${bar("skel-card").repeat(2)}</div>`;
  else body = `<div class="skel-kpis">${bar("skel-kpi").repeat(4)}</div><div class="skel-cards">${bar("skel-card").repeat(2)}</div>`;
  return `<main class="screen skel-screen" aria-busy="true" data-skeleton>${pageHead(
    `${bar("skel-crumb")}${h1}${bar("skel-desc")}<span class="sr">Loading…</span>`,
  )}<div class="stack">${body}</div></main>`;
}

/** A "?" that opens one short explanation; `label` names what it explains. `chrome.js` wires it. */
export function toggletip(text, label = "") {
  return `<span class="toggletip"><button type="button" class="tt-btn" aria-expanded="false" aria-label="What does this mean?${
    label ? ` ${esc(label)}` : ""
  }" data-toggletip>?</button><span class="tt-pop" role="status" hidden>${esc(text)}</span></span>`;
}

/** Say one short thing ("Saved", "3 results") to screen-reader users through the page's `#status`. */
export function announceStatus(text) {
  const region = typeof document !== "undefined" ? document.getElementById("status") : null;
  if (!region) return;
  region.textContent = "";
  setTimeout(() => {
    region.textContent = String(text || "");
  }, 30);
}

/** The event `modules/router.js` dispatches when a registration or a module's state changes a screen. */
export const MODULES_EVENT = "marketing-ai:modules-changed";

// --- charts: inline SVG (plan §9.3), drawn to the prototype's own geometry -------------------
// No `viewBox`: percentage widths resolve against the element itself, so the 4px corner radius
// stays 4 real pixels exactly as the prototype's `border-radius` did, at any column width.

export function barTrack(sharePct, label) {
  const width = Math.max(0, Math.min(100, Number(sharePct)));
  return `<svg class="track" height="8" role="img" aria-label="${esc(
    label,
  )}"><rect x="0" y="0" width="100%" height="8" rx="4" fill="var(--track)"/><rect x="0" y="0" width="${width.toFixed(
    2,
  )}%" height="8" rx="4" fill="var(--c)"/></svg>`;
}

export function columnBar(heightPx, top, label) {
  const height = Math.max(3, Math.round(heightPx));
  const r = Math.min(4, height / 2);
  const fill = top ? "var(--c)" : "var(--t)";
  return `<svg class="bb" height="${height}" role="img" aria-label="${esc(
    label,
  )}"><rect x="0" y="0" width="100%" height="${height}" rx="${r}" fill="${fill}"/><rect x="0" y="${r}" width="100%" height="${
    height - r
  }" fill="${fill}"/></svg>`;
}
