/* The components of the look (Plan M M185, DEC-1615): one implementation each, reused by every page.

   Pure functions that return HTML strings, styled by ui/theme.css; docs/ux/VISUAL_DESIGN.md says when to use each.
   Every piece of text passed in is escaped. Three parameters take trusted HTML instead, because another helper made
   it: `crumbs` (dom.js `crumbs()`), and an action given as a string (an already-drawn button or link, as
   dom.js `headActions` accepts). Nothing here reads the document or calls the API, except `wireTabs`, which adds the
   arrow-key behaviour of `tabs()` to a document once.

     statusOf(kind, value, {word})    the one status mapping: {key, tone, icon, word}
     icon(name, {size, label})        a 24px-grid line icon (1.75px stroke), hidden from screen readers unless labelled
     pageHeader({...})                the page's title, one-line subtitle, optional breadcrumbs and its ONE primary action
     button({...})                    primary | secondary | tertiary | danger; 40px or "sm" 32px; icon, disabled, busy
     statusChip(kind, value, {word})  a status badge: icon + word in the status colours
     tagChip(label)                   a quiet grey outline tag ("Win-back")
     statTile({...}) / kpiRow(tiles)  label, value (+unit), caption, optional value range; a row of 2 to 4
     valueRange({...})                a number's likely range as a small interval bar, with the words beside it
     emptyState({...})                a 40px line icon, one sentence, one action
     tabs({...}) / wireTabs(doc)      underline tabs with role=tablist/tab, aria-selected and the arrow keys
     stepper({...})                   numbered circles joined by a line; "Step 2 of 5 · Customers" on a phone
     attentionItem({...})             a coloured icon, a title, one line, the action on the right (below on a phone)

   This file imports two leaves, `dom.js` and `labels.js`, and nothing imports it back but pages, so the module graph
   stays acyclic (DEC-790). */

import { esc } from "./dom.js";
import { term } from "./labels.js";

// --- icons ------------------------------------------------------------------------------------------------------

/** Line icons on a 24px grid. Paths only: `icon()` adds the stroke, so every icon is drawn the same way. */
export const ICONS = Object.freeze({
  check: '<path d="M20 6 9 17l-5-5"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  dot: '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  draft: '<circle cx="12" cy="12" r="8" stroke-dasharray="3 3"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  critical: '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  approx: '<path d="M5 9c2-2 4-2 7 0s5 2 7 0"/><path d="M5 15c2-2 4-2 7 0s5 2 7 0"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  upload: '<path d="M12 20V9"/><path d="m7 14 5-5 5 5"/><path d="M5 4h14"/>',
  arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
  campaign: '<path d="M3 10v4h3l7 4V6L6 10H3z"/><path d="M16 9a4 4 0 0 1 0 6"/>',
  data: '<ellipse cx="12" cy="5.5" rx="7" ry="2.5"/><path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13"/><path d="M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
  report: '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
});

/** One icon as inline SVG. Decorative (aria-hidden) unless `label` names it. An unknown name draws nothing. */
export function icon(name, { size = 16, label = "" } = {}) {
  const body = ICONS[name];
  if (!body) return "";
  const a11y = label ? `role="img" aria-label="${esc(label)}"` : `aria-hidden="true"`;
  return `<svg class="ds-ico" viewBox="0 0 24 24" width="${Number(size) || 16}" height="${Number(size) || 16}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" focusable="false" ${a11y}>${body}</svg>`;
}

// --- the status mapping -----------------------------------------------------------------------------------------

/** The five tones: colour means one of these, always with an icon and a word. */
export const TONES = Object.freeze(["good", "warning", "critical", "info", "neutral"]);

const entry = (key, tone, iconName, word) => Object.freeze({ key, tone, icon: iconName, word });

/**
 * Every status a page shows, by kind (visual_common §2): a campaign's status, what a result can claim (the evidence
 * label) and a data source's health. Words come from the glossary where it has them.
 */
export const STATUS_MAP = Object.freeze({
  campaign: Object.freeze({
    draft: entry("draft", "neutral", "draft", "Draft"),
    live: entry("live", "info", "dot", "Live"),
    waiting: entry("waiting", "warning", "clock", "Waiting for results"),
    measured: entry("measured", "good", "check", "Measured"),
  }),
  evidence: Object.freeze({
    proven: entry("proven", "good", "checkCircle", term("evidenceProven")),
    estimated: entry("estimated", "info", "approx", term("evidenceEstimated")),
    not_yet_proven: entry("not_yet_proven", "neutral", "draft", term("evidenceNotProven")),
  }),
  health: Object.freeze({
    read: entry("read", "good", "check", "Read correctly"),
    connected: entry("connected", "good", "check", "Connected"),
    not_tested: entry("not_tested", "warning", "clock", "Not tested yet"),
    needs_attention: entry("needs_attention", "warning", "alert", "Needs attention"),
    failed: entry("failed", "critical", "critical", "Failed"),
  }),
});

/** Other spellings of the same status (the API's, the campaigns module's, the engine's evidence labels). */
const ALIASES = Object.freeze({
  campaign: { waiting_for_results: "waiting", ok: "measured", neutral: "draft", warn: "waiting" },
  evidence: { causal: "proven", not_proven: "not_yet_proven" },
  health: { ok: "read", ready: "read", warning: "needs_attention", unreadable: "failed", error: "failed", bad: "failed" },
});

/** The fallback for a value a kind does not know: neutral, never a colour that claims something. */
const UNKNOWN = Object.freeze({ key: "unknown", tone: "neutral", icon: "info", word: "" });

/**
 * `{key, tone, icon, word}` for a status. `kind` is "campaign", "evidence" or "health"; `value` its key in any of
 * the spellings above (case and spaces do not matter). `word` replaces the word (a server's own sentence) and keeps
 * the tone and the icon. An unknown value is neutral with no icon meaning, and its own text as the word.
 */
export function statusOf(kind, value, { word = null } = {}) {
  const table = STATUS_MAP[kind];
  if (!table) throw new Error(`components.statusOf: no status kind "${kind}"`);
  const norm = String(value === null || value === undefined ? "" : value)
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  const key = table[norm] ? norm : (ALIASES[kind] || {})[norm];
  const found = table[key] || { ...UNKNOWN, word: String(value || "") };
  return word === null || word === undefined ? { ...found } : { ...found, word: String(word) };
}

// --- buttons ----------------------------------------------------------------------------------------------------

const KINDS = Object.freeze({ primary: "primary", secondary: "secondary", tertiary: "tertiary", danger: "danger" });

/**
 * One button, or a link drawn as one when `href` is given. `kind` is primary (one per page), secondary, tertiary or
 * danger; `size` "sm" is the 32px button. `icon` names an icon from ICONS drawn before the label. `disabled` and
 * `busy` set the states (a busy button is also disabled and says so with aria-busy). `attrs` is extra attribute
 * markup the caller wrote (data-* hooks).
 */
export function button({
  label,
  href = null,
  id = null,
  kind = "primary",
  size = "",
  icon: iconName = "",
  attrs = "",
  disabled = false,
  busy = false,
  type = "button",
} = {}) {
  const cls = ["btn", KINDS[kind] || "primary", size === "sm" ? "sm" : ""].filter(Boolean).join(" ");
  const parts = [`class="${cls}"`];
  if (id) parts.push(`id="${esc(id)}"`);
  if (busy) parts.push(`aria-busy="true"`);
  if (attrs) parts.push(attrs);
  const inner = `${iconName ? icon(iconName) : ""}<span>${esc(label)}</span>`;
  if (href && !disabled && !busy) return `<a ${parts.join(" ")} href="${esc(href)}">${inner}</a>`;
  if (href) return `<a ${parts.join(" ")} role="link" aria-disabled="true">${inner}</a>`;
  const off = disabled || busy ? " disabled" : "";
  return `<button type="${type === "submit" ? "submit" : "button"}" ${parts.join(" ")}${off}>${inner}</button>`;
}

/** An action given as a button's options, or as trusted HTML another helper drew. */
const actionHtml = (action, kind) => {
  if (!action) return "";
  if (typeof action === "string") return action;
  return button({ ...action, kind: action.kind || kind });
};

// --- the page header --------------------------------------------------------------------------------------------

/**
 * The page's header: optional breadcrumbs (only below a top-level place), the H1, a one-line subtitle, and the page's
 * ONE primary action (plus at most one or two secondaries) on the right; below the text on a phone.
 */
export function pageHeader({ title, subtitle = "", crumbs = "", primary = null, secondary = [], attrs = "" } = {}) {
  const actions = [actionHtml(primary, "primary"), ...[].concat(secondary || []).map((a) => actionHtml(a, "secondary"))]
    .filter(Boolean)
    .join("");
  return `<header class="page-header"${attrs ? ` ${attrs}` : ""}><div class="ph-main">${crumbs || ""}<h1 class="h1">${esc(
    title,
  )}</h1>${subtitle ? `<p class="desc">${esc(subtitle)}</p>` : ""}</div>${actions ? `<div class="ph-actions">${actions}</div>` : ""}</header>`;
}

// --- chips ------------------------------------------------------------------------------------------------------

/** A status badge: the mapping's icon (hidden from screen readers) and its word, in the tone's colours. */
export function statusChip(kind, value, { word = null, attrs = "" } = {}) {
  const s = statusOf(kind, value, { word });
  return `<span class="badge tone-${esc(s.tone)}" data-status="${esc(s.key)}" data-tone="${esc(s.tone)}"${
    attrs ? ` ${attrs}` : ""
  }>${icon(s.icon, { size: 12 })}<span>${esc(s.word)}</span></span>`;
}

/** A quiet tag: a grey outline with a word ("Win-back"). Not a status: it carries no colour. */
export function tagChip(label, { attrs = "" } = {}) {
  return `<span class="tag"${attrs ? ` ${attrs}` : ""}>${esc(label)}</span>`;
}

// --- numbers ----------------------------------------------------------------------------------------------------

const finite = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
const pct = (v) => `${Math.max(0, Math.min(100, v)).toFixed(2)}%`;

/**
 * A value with its likely range: a track, the range as a brand bar with rounded ends, a dot at the central
 * estimate, and a tick at zero when the range starts near (or crosses) zero. `text` is the words, always drawn
 * beside it ("Likely 88,187 to 4.18 lakh"): it is the accessible equivalent, so the bar itself is hidden from
 * screen readers. With no usable numbers only the words are drawn.
 */
export function valueRange({ low, high, estimate = null, text = "", attrs = "" } = {}) {
  const words = `<span class="vr-text">${esc(text)}</span>`;
  const open = `<span class="vrange" data-value-range${attrs ? ` ${attrs}` : ""}>`;
  if (!finite(low) || !finite(high)) return `${open}${words}</span>`;
  let lo = Number(low);
  let hi = Number(high);
  if (lo > hi) [lo, hi] = [hi, lo];
  const span = hi - lo;
  const nearZero = lo <= 0 || hi <= 0 || (hi > 0 && lo / hi < 0.35);
  let from;
  let to;
  if (nearZero) {
    from = Math.min(lo, 0);
    to = Math.max(hi, 0);
    const pad = (to - from || Math.abs(hi) || 1) * 0.08;
    to += pad;
    if (from < 0) from -= pad;
  } else {
    const pad = (span || Math.abs(hi) || 1) * 0.35;
    from = lo - pad;
    to = hi + pad;
  }
  const at = (v) => ((v - from) / (to - from)) * 100;
  const left = at(lo);
  const width = Math.max(0, at(hi) - left);
  const mid = finite(estimate) ? Math.min(hi, Math.max(lo, Number(estimate))) : null;
  const zero = from <= 0 && to >= 0 ? `<span class="vr-zero" style="left:${pct(at(0))}"></span>` : "";
  const dot = mid === null ? "" : `<span class="vr-dot" style="left:${pct(at(mid))}"></span>`;
  return `${open}<span class="vr-track" aria-hidden="true">${zero}<span class="vr-bar" style="left:${pct(left)};width:${pct(
    width,
  )}"></span>${dot}</span>${words}</span>`;
}

/**
 * A stat tile: a label (sentence case), the value (large, proportional figures) with an optional unit, an optional
 * caption, and an optional value range (`range`: valueRange's options) under it.
 */
export function statTile({ label, value, unit = "", caption = "", range = null, attrs = "" } = {}) {
  return `<div class="stat"${attrs ? ` ${attrs}` : ""}><p class="stat-label">${esc(label)}</p><p class="stat-value">${esc(value)}${
    unit ? `<span class="stat-unit">${esc(unit)}</span>` : ""
  }</p>${range ? valueRange(range) : ""}${caption ? `<p class="stat-caption">${esc(caption)}</p>` : ""}</div>`;
}

/** A row of stat tiles (2 to 4): each one statTile's options, or a tile another helper drew. */
export function kpiRow(tiles = [], { attrs = "" } = {}) {
  return `<div class="stat-row"${attrs ? ` ${attrs}` : ""}>${tiles
    .map((t) => (typeof t === "string" ? t : statTile(t)))
    .join("")}</div>`;
}

// --- empty states and attention items ---------------------------------------------------------------------------

/** An empty list or card: a 40px line icon, one sentence (`title`, with an optional second line), one action. */
export function emptyState({ icon: iconName = "inbox", title, text = "", action = null, attrs = "" } = {}) {
  return `<div class="empty-state has-icon"${attrs ? ` ${attrs}` : ""}><span class="es-icon">${icon(iconName, {
    size: 22,
  })}</span><p class="es-t">${esc(title)}</p>${text ? `<p>${esc(text)}</p>` : ""}${actionHtml(action, "primary")}</div>`;
}

const TONE_ICON = Object.freeze({ good: "checkCircle", warning: "alert", critical: "critical", info: "info", neutral: "info" });

/**
 * One thing that needs attention: the tone's icon (with the tone named for screen readers), a title, one line of
 * text, an optional small line under it (`meta`, e.g. the campaign it belongs to), and one action - on the right on
 * a wide screen, under the text on a phone. `metaAttrs` is attribute markup the caller wrote for the meta line
 * (M186, DEC-1616: a campaign's recorded name is the person's own text, `data-user-text`, with the full name as
 * its tooltip).
 */
export function attentionItem({
  tone = "warning",
  title,
  text = "",
  meta = "",
  metaAttrs = "",
  action = null,
  attrs = "",
} = {}) {
  const t = TONES.includes(tone) ? tone : "neutral";
  const named = { good: "Done", warning: "Warning", critical: "Problem", info: "Information", neutral: "Note" }[t];
  return `<div class="attn tone-${t}" data-tone="${t}"${attrs ? ` ${attrs}` : ""}><span class="attn-icon">${icon(TONE_ICON[t], {
    size: 18,
    label: named,
  })}</span><div class="attn-body"><p class="attn-title">${esc(title)}</p>${text ? `<p class="attn-text">${esc(text)}</p>` : ""}${
    meta ? `<p class="attn-meta"${metaAttrs ? ` ${metaAttrs}` : ""}>${esc(meta)}</p>` : ""
  }</div>${
    action ? `<div class="attn-action">${actionHtml(action, "secondary")}</div>` : ""
  }</div>`;
}

// --- tabs -------------------------------------------------------------------------------------------------------

/**
 * Underline tabs: `items` are `{id, label, href?, count?, attrs?}`; `current` is the selected id. A tab with `href`
 * is a link (a route per tab, as the campaign, Reports and Data pages have); without, a button that the page wires.
 * role=tablist / role=tab / aria-selected, and a roving tabindex: Left and Right move between tabs, Home and End jump
 * (`wireTabs`), Enter or a click opens one.
 */
export function tabs({ label, items = [], current = null, attrs = "" } = {}) {
  const body = items
    .map((item) => {
      const on = item.id === current;
      const count = finite(item.count) ? ` <span class="ds-count">${esc(item.count)}</span>` : "";
      const common = `class="ds-tab" role="tab" aria-selected="${on}" tabindex="${on ? 0 : -1}" data-tab-id="${esc(item.id)}"${
        item.attrs ? ` ${item.attrs}` : ""
      }`;
      return item.href
        ? `<a ${common} href="${esc(item.href)}">${esc(item.label)}${count}</a>`
        : `<button type="button" ${common}>${esc(item.label)}${count}</button>`;
    })
    .join("");
  return `<div class="ds-tabs" role="tablist" aria-label="${esc(label)}" data-ds-tabs${attrs ? ` ${attrs}` : ""}>${body}</div>`;
}

/** The arrow keys of a tablist: the next tab to focus for `key`, or null when the key is not a tab key. */
export function nextTab(tabList, from, key) {
  const all = [...tabList.querySelectorAll('[role="tab"]')].filter((t) => !t.hasAttribute("disabled"));
  if (!all.length) return null;
  const at = Math.max(0, all.indexOf(from));
  if (key === "ArrowRight") return all[(at + 1) % all.length];
  if (key === "ArrowLeft") return all[(at - 1 + all.length) % all.length];
  if (key === "Home") return all[0];
  if (key === "End") return all[all.length - 1];
  return null;
}

// The documents already wired (a WeakSet, not an attribute: the page's own markup stays exactly as it was).
const wired = new WeakSet();

/** Give every `tabs()` tablist in `doc` its arrow keys, now and later (one delegated listener; safe to call often). */
export function wireTabs(doc = typeof document === "undefined" ? null : document) {
  if (!doc || typeof doc.addEventListener !== "function" || wired.has(doc)) return;
  wired.add(doc);
  doc.addEventListener("keydown", (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const tab = target.closest('[role="tab"]');
    const list = tab && tab.closest("[data-ds-tabs]");
    if (!list) return;
    const next = nextTab(list, tab, event.key);
    if (!next) return;
    event.preventDefault();
    for (const t of list.querySelectorAll('[role="tab"]')) t.setAttribute("tabindex", t === next ? "0" : "-1");
    next.focus();
  });
}

// --- the stepper ------------------------------------------------------------------------------------------------

/**
 * The wizard's steps: numbered circles joined by a line; a done step is a brand circle with a check, the current one
 * a brand ring, the rest grey; labels under the circles. `steps` are labels (or `{label, href}` to make a done step a
 * link back); `current` is the 0-based index of the step on screen. On a phone the labels fold away and one line says
 * "Step 2 of 5 · Customers" (hidden from screen readers, who hear the list itself).
 */
export function stepper({ steps = [], current = 0, label = "Steps", attrs = "" } = {}) {
  const total = steps.length;
  const at = Math.max(0, Math.min(total - 1, Number(current) || 0));
  const items = steps
    .map((step, i) => {
      const text = typeof step === "string" ? step : step.label;
      const href = typeof step === "object" && step && step.href && i < at ? step.href : null;
      const state = i < at ? "done" : i === at ? "current" : "upcoming";
      const dot = state === "done" ? icon("check", { size: 16 }) : `<span aria-hidden="true">${i + 1}</span>`;
      const said = state === "done" ? ` <span class="sr">(done)</span>` : "";
      const inner = `<span class="st-dot">${dot}</span><span class="st-label">${esc(text)}${said}</span>`;
      return `<li class="st-step ${state}"${state === "current" ? ` aria-current="step"` : ""}>${
        href ? `<a href="${esc(href)}">${inner}</a>` : inner
      }</li>`;
    })
    .join("");
  const now = total ? (typeof steps[at] === "string" ? steps[at] : steps[at].label) : "";
  const phone = total ? `<p class="stepper-phone" aria-hidden="true">Step ${at + 1} of ${total} · ${esc(now)}</p>` : "";
  return `<ol class="stepper" aria-label="${esc(label)}"${attrs ? ` ${attrs}` : ""}>${items}</ol>${phone}`;
}
