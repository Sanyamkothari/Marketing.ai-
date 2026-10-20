// Small helpers every Autonomy screen shares. Copied in spirit from v1 (ui/dom.js, ui/components.js), not
// imported: v2 stands alone. One rule runs through all of them: a value nobody sent is an em dash, never a
// made-up number, and every estimate is shown as its range, never as a bare point.
//
// They draw the design system in styles.css (docs/v2/DESIGN.md; "How to use the system" at its end lists the
// classes): cards, the divided KPI strip, tables that fold to two-line rows on a phone, pills, notices, the
// segmented control, skeleton blocks, empty and error states, the freshness stamp, toasts and dialogs, the one page
// head, and descriptions: the explanations that show only on hover, focus or tap (withInfo, info, describe), so a
// card shows its title, its numbers and at most one sentence (DESIGN.md section 6, "Words").

export const EM_DASH = "—";

/** True only for a value the API actually sent. `0` and `false` are values; `null`, `undefined`, "", NaN and
 * infinities are not. */
export const present = (v) =>
  v !== null && v !== undefined && v !== "" && !(typeof v === "number" && !Number.isFinite(v));

/** A value, or the em dash. `fmt` only runs on a value that is present. */
export const dash = (v, fmt = (x) => String(x)) => (present(v) ? fmt(v) : EM_DASH);

const NUMBER_LOCALE = "en-US";

/**
 * Build an element. `attrs` keys: `class`, `text`, `style` (object), `dataset` (object), `on<event>` (function),
 * booleans (true sets the attribute, false leaves it off) and any other attribute as a string. Children may be
 * strings, numbers, nodes, arrays or null/false (skipped). Text is always set as text, so nothing is parsed as HTML.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "style" && typeof value === "object") setStyle(node, value);
    else if (key === "dataset" && typeof value === "object") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Set inline styles; custom properties (`--kpi-count`) need `setProperty`, plain ones are assigned. */
function setStyle(node, styles) {
  for (const [name, v] of Object.entries(styles)) {
    if (name.startsWith("--") && node.style && typeof node.style.setProperty === "function") node.style.setProperty(name, String(v));
    else node.style[name] = v;
  }
}

/** Append children (same rules as `el`) to `node`. */
export function append(node, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Empty `node` and fill it with `children`. */
export function clear(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

/** A whole number with digit grouping. */
export const int = (n) => dash(n, (x) => Number(x).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: 0 }));

/** A plain number with at most `places` decimals. */
export const num = (n, places = 2) =>
  dash(n, (x) => Number(x).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places }));

/** A fraction as a percentage: 0.0008 -> "0.08%", 0.1 -> "10%". */
export const pct = (fraction, places = 2) =>
  dash(fraction, (x) => `${(Number(x) * 100).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places })}%`);

/**
 * Money in the goal's currency, built by Intl (so no currency sign is typed here). Whole units from 100 up,
 * cents below. An unknown currency code still shows the amount and the code; a missing value is the em dash.
 */
export function money(value, currency = "USD") {
  if (!present(value) || !Number.isFinite(Number(value))) return EM_DASH;
  const amount = Number(value);
  const whole = Math.abs(amount) >= 100;
  const code = String(currency || "").toUpperCase();
  const opts = { style: "currency", currency: code };
  if (whole) Object.assign(opts, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  try {
    return new Intl.NumberFormat(NUMBER_LOCALE, opts).format(amount);
  } catch {
    return `${amount.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: whole ? 0 : 2 })} ${code}`.trim();
  }
}

/**
 * An estimate (`{point, low, high}`) as its range, "low – high". With a currency it is money; with
 * `{format: "pct"}` a percentage; `{format: "x"}` a multiple (ROI); otherwise a plain number. The point is never
 * shown alone: `{withPoint: true}` reads "about P (low – high)". A missing estimate or bound is the em dash.
 */
export function range(estimate, currency, opts = {}) {
  if (!estimate || !present(estimate.low) || !present(estimate.high)) return EM_DASH;
  const fmt =
    opts.format === "pct"
      ? (x) => pct(x, 1)
      : opts.format === "x"
        ? (x) => `${num(x, 2)}×`
        : currency
          ? (x) => money(x, currency)
          : (x) => num(x, 2);
  const span = `${fmt(estimate.low)} – ${fmt(estimate.high)}`;
  if (opts.withPoint && present(estimate.point)) return `about ${fmt(estimate.point)} (${span})`;
  return span;
}

/** "win_back" -> "Win back"; the em dash for nothing. */
export const humanize = (v) =>
  dash(v, (x) => {
    const s = String(x).replace(/_/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  });

/** An ISO instant as "19 Oct 2026, 06:00"; the em dash if missing or unreadable. */
export function when(iso, { time = true } = {}) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  const opts = { day: "numeric", month: "short", year: "numeric" };
  if (time) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return at.toLocaleString("en-GB", opts);
}

/** A two-digit hour of the day: 9 -> "09:00". */
export const hour = (h) => dash(h, (x) => `${String(x).padStart(2, "0")}:00`);

const TONES = new Set(["good", "warning", "critical", "info", "neutral"]);
/** DESIGN.md's names for the same tones. */
const TONE_ALIASES = Object.freeze({ positive: "good", negative: "critical", accent: "info" });
const toneOf = (tone, fallback = "neutral") => {
  const t = TONE_ALIASES[tone] || tone;
  return TONES.has(t) ? t : fallback;
};

/**
 * A small status label: pale tint, dark ink of the same hue. Colour is never the only signal: the word is always
 * there. Tones: good (positive), warning, critical (negative), info (accent), neutral. `{dot: true}` adds a 6px dot.
 */
export function pill(text, tone = "neutral", { dot = false } = {}) {
  return el("span", { class: ["pill", `pill-${toneOf(tone)}`, dot && "pill-dot"] }, text);
}

/** A goal's status in the words every screen uses ("active" reads "Running"). */
export function goalStatusWords(status) {
  return { active: "Running", paused: "Paused", draft: "Not started", done: "Finished" }[status] || humanize(status);
}

/** The tone for a goal or content status, for `pill`. */
export function statusTone(status) {
  return (
    {
      active: "good",
      approved: "good",
      paused: "warning",
      draft: "info",
      rejected: "critical",
      retired: "neutral",
      done: "neutral",
      critical: "critical",
      warning: "warning",
      info: "info",
    }[status] || "neutral"
  );
}

/**
 * A card: optional title (with a heading level), body children and an actions row. `meta` (the card's own period
 * or a freshness stamp) sits right of the title in grey; `flush: true` lets a table or list run edge to edge.
 */
export function card({ title, level = 3, body = [], actions = [], className = "", label, meta, flush = false } = {}) {
  let heading = title ? el(`h${level}`, { class: "card-title" }, title) : null;
  if (heading && present(meta)) heading = el("div", { class: "card-head" }, heading, el("span", { class: "card-meta" }, meta));
  const actionRow = actions.length ? el("div", { class: "card-actions" }, actions) : null;
  return el("section", { class: ["card", flush && "card-flush", className], "aria-label": label || null }, heading, body, actionRow);
}

/**
 * A table with a caption. `columns` is `[{key, label, format?, align?, lead?}]`; `rows` are objects. A missing
 * cell is the em dash. Wrapped in a scroller so it never widens the page on a phone, and (unless `stack: false`)
 * folded into two-line rows under 640px: the first column and the `lead` column (the key figure) on line 1, the others in
 * grey on line 2, each with its header as a label. `empty` is the sentence for a table with no rows.
 */
export function table({ caption, columns, rows, stack = true, empty } = {}) {
  const head = el("thead", {}, el("tr", {}, columns.map((c) => el("th", { scope: "col", class: c.align === "right" ? "num" : null }, c.label))));
  const list = Array.isArray(rows) ? rows : [];
  const body = el(
    "tbody",
    {},
    !list.length && present(empty)
      ? el("tr", {}, el("td", { colspan: String(columns.length), class: "muted" }, empty))
      : list.map((row) =>
          el(
            "tr",
            {},
            columns.map((c) => {
              const raw = row[c.key];
              const value = c.format ? c.format(raw, row) : dash(raw);
              const label = typeof c.label === "string" ? c.label : null;
              return el("td", { class: [c.align === "right" && "num", c.lead && "is-key"], "data-label": label }, value);
            }),
          ),
        ),
  );
  return el("div", { class: "table-wrap" }, el("table", { class: stack ? "table-stack" : null }, caption ? el("caption", {}, caption) : null, head, body));
}

/** What a screen shows when there is nothing yet: a title, a sentence, and maybe one action. */
export function emptyState(title, text, action) {
  return el("div", { class: "empty", role: "status" }, el("p", { class: "empty-title" }, title), text ? el("p", {}, text) : null, action || null);
}

/**
 * A button. `kind` is "primary" (near-black; one per screen), "secondary", "ghost" or "danger" (also spelled
 * "destructive": red text and hairline). `size` is "sm" or "lg"; `block: true` spans the width.
 */
export function button(label, onClick, { kind = "secondary", type = "button", disabled = false, ariaLabel, size, block = false } = {}) {
  const k = kind === "destructive" ? "danger" : kind;
  return el(
    "button",
    { type, class: ["btn", `btn-${k}`, size && `btn-${size}`, block && "btn-block"], onclick: onClick, disabled, "aria-label": ariaLabel || null },
    label,
  );
}

/** A loading line for a screen that is waiting on the API. */
export const loading = (text = "Loading…") => el("p", { class: "loading", role: "status" }, text);

/**
 * A plain-language error box for the block that failed, with the server's message when there is one. Options:
 * `lastGood` (an ISO time: "Last updated 20 Oct 2026, 09:00") and `retry` (a function: a Retry text button).
 */
export function errorBox(title, err, { lastGood, retry } = {}) {
  const detail = err && err.message ? err.message : null;
  const footer =
    present(lastGood) || typeof retry === "function"
      ? el(
          "div",
          { class: "error-actions" },
          present(lastGood) ? el("span", { class: "meta" }, `Last updated ${when(lastGood)}`) : null,
          typeof retry === "function" ? el("button", { type: "button", class: "link-btn", onclick: retry }, "Retry") : null,
        )
      : null;
  return el("div", { class: "error-box", role: "alert" }, el("p", { class: "empty-title" }, title), detail ? el("p", {}, detail) : null, footer);
}

/**
 * A short message in the corner that also reaches screen readers (polite live region; "critical" is assertive).
 * Disappears after a few seconds; the close button dismisses it sooner.
 */
export function toast(message, tone = "info") {
  let host = document.getElementById("toasts");
  if (!host) {
    host = el("div", { id: "toasts", class: "toasts" });
    document.body.append(host);
  }
  const item = el(
    "div",
    { class: ["toast", `toast-${TONES.has(tone) ? tone : "info"}`], role: tone === "critical" ? "alert" : "status" },
    el("span", {}, message),
    el("button", { type: "button", class: "toast-close", "aria-label": "Dismiss", onclick: () => item.remove() }, "×"),
  );
  host.append(item);
  setTimeout(() => item.remove(), tone === "critical" ? 9000 : 5000);
  return item;
}

/** A body with `{{slot}}` placeholders, with each slot drawn as a marked chip so the marketer sees what varies. */
export function slotted(text) {
  const parts = String(text || "").split(/(\{\{\s*[a-zA-Z0-9_]+\s*\}\})/g);
  return parts.filter((p) => p !== "").map((p) => {
    const m = p.match(/^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$/);
    return m ? el("span", { class: "slot", title: "Filled in for each customer" }, humanize(m[1]).toLowerCase()) : p;
  });
}

// ---- the design system's newer pieces (DESIGN.md, "How to use the system") ---------------------------------------

// ---- descriptions: the explanation that shows only on hover, focus or tap ------------------------------------------
//
// Every "how is this measured", what a number means, a threshold or a method note lives in a description, not on the
// page. A description is a short floating box (the only floating thing besides menus, toasts and dialogs) that opens
// on hover after 150 ms, on keyboard focus, and on tap on a touch screen (tap again, tap outside or Esc closes it).
// Its text is always in the DOM (role="tooltip", linked with aria-describedby), so screen readers and tests find it;
// it is `hidden` while closed, so it adds no visible words, and it is position: fixed, so nothing shifts when it opens.

const HOVER_DELAY = 150;
let infoSeq = 0;
/** The one description open right now: {target, tip, close}. */
let openInfo = null;
let infoGlobalsReady = false;

const classesOf = (node) => String(node.getAttribute("class") || "").split(/\s+/).filter(Boolean);
const hasClass = (node, c) => classesOf(node).includes(c);
function addClass(node, c) {
  if (!hasClass(node, c)) node.setAttribute("class", [...classesOf(node), c].join(" "));
}
function removeClass(node, c) {
  if (hasClass(node, c)) node.setAttribute("class", classesOf(node).filter((x) => x !== c).join(" "));
}
const isActionable = (node) => /^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY|LABEL)$/.test(String(node.tagName || "").toUpperCase());

/** Esc closes, a tap or click outside closes, and scrolling or resizing moves the open one with its target. */
function infoGlobals() {
  if (infoGlobalsReady || typeof document === "undefined" || typeof document.addEventListener !== "function") return;
  infoGlobalsReady = true;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && openInfo) openInfo.close(true);
  });
  document.addEventListener(
    "pointerdown",
    (e) => {
      if (!openInfo) return;
      const inside = (n) => n && typeof n.contains === "function" && n.contains(e.target);
      if (!inside(openInfo.target) && !inside(openInfo.tip)) openInfo.close(true);
    },
    true,
  );
  const follow = () => openInfo && placeInfo(openInfo.target, openInfo.tip);
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("scroll", follow, { capture: true, passive: true });
    window.addEventListener("resize", follow, { passive: true });
  }
}

/** Put the box under its target (above when there is no room below), inside the window with an 8px margin. */
function placeInfo(target, tip) {
  if (typeof target.getBoundingClientRect !== "function" || typeof window === "undefined" || !tip.style) return;
  const r = target.getBoundingClientRect();
  const vw = (document.documentElement && document.documentElement.clientWidth) || window.innerWidth;
  const vh = window.innerHeight;
  tip.style.left = "0px";
  tip.style.top = "0px";
  tip.style.maxWidth = `${Math.min(280, vw - 16)}px`;
  const w = tip.offsetWidth || 0;
  const h = tip.offsetHeight || 0;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, vw - w - 8));
  const below = r.bottom + 6;
  const top = below + h + 8 > vh && r.top - 6 - h >= 8 ? r.top - 6 - h : below;
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

/**
 * Wire an existing description box (`tip`, any element holding the words) to its `target`. The building block of
 * `withInfo` and `info`; the shell uses it for the boxes written in index.html. Returns the target.
 *
 * On a label, a number or a pill: hover (150 ms), keyboard focus (it becomes tabbable) and tap all open it. On a
 * button or a link the action wins on click and tap: its description shows on hover and focus, so its aria-label
 * should carry the same words for touch screens.
 */
export function describe(target, tip) {
  if (!target || !tip) return target;
  if (!tip.getAttribute("id")) tip.setAttribute("id", `info-${(infoSeq += 1)}`);
  const tipId = tip.getAttribute("id");
  tip.setAttribute("role", "tooltip");
  addClass(tip, "info-tip");
  tip.hidden = true;
  const ids = String(target.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
  if (!ids.includes(tipId)) target.setAttribute("aria-describedby", [...ids, tipId].join(" "));
  addClass(target, "has-info");
  const glyph = hasClass(target, "info-dot");
  const actionable = isActionable(target) && !glyph;
  if (!isActionable(target) && target.getAttribute("tabindex") === null) target.setAttribute("tabindex", "0");

  const state = { hover: false, focus: false, pinned: false };
  let timer = null;
  let lastPointer = "mouse";
  function close(all = false) {
    clearTimeout(timer);
    if (all) Object.assign(state, { hover: false, focus: false, pinned: false });
    tip.hidden = true;
    removeClass(tip, "is-open");
    if (openInfo && openInfo.tip === tip) openInfo = null;
  }
  function show() {
    if (openInfo && openInfo.tip !== tip) openInfo.close(true);
    infoGlobals();
    tip.hidden = false;
    addClass(tip, "is-open");
    placeInfo(target, tip);
    openInfo = { target, tip, close };
  }
  const sync = () => (state.hover || state.focus || state.pinned ? show() : close());

  target.addEventListener("pointerenter", (e) => {
    if (e && e.pointerType && e.pointerType !== "mouse") return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.hover = true;
      sync();
    }, HOVER_DELAY);
  });
  target.addEventListener("pointerleave", (e) => {
    if (e && e.pointerType && e.pointerType !== "mouse") return;
    clearTimeout(timer);
    state.hover = false;
    sync();
  });
  target.addEventListener("pointerdown", (e) => {
    lastPointer = (e && e.pointerType) || "mouse";
  });
  target.addEventListener("focus", () => {
    // Keyboard focus only: a click or a tap also focuses, and hover or the tap handle those.
    let keyboard = true;
    try {
      if (typeof target.matches === "function") keyboard = target.matches(":focus-visible");
    } catch {
      keyboard = true;
    }
    if (!keyboard) return;
    state.focus = true;
    sync();
  });
  target.addEventListener("blur", () => {
    state.focus = false;
    state.pinned = false;
    sync();
  });
  if (!actionable) {
    target.addEventListener("click", (e) => {
      const type = (e && e.pointerType) || lastPointer;
      if (type === "mouse" && !glyph) return; // the mouse already sees it on hover
      // A tap toggles it; a click on the (i) keeps it open (or closes it if it was pinned already).
      state.pinned = !state.pinned;
      if (!state.pinned) state.hover = false;
      sync();
    });
  }
  target.addEventListener("keydown", (e) => {
    if (e && e.key === "Escape") close(true);
  });
  return target;
}

/** The description box: a `span.info-tip` holding `text` (a string or inline nodes; no links, it cannot be clicked). */
const infoTip = (text, id) => el("span", { class: "info-tip", role: "tooltip", id: id || null, hidden: true }, text);

/**
 * Give `target` (a label, a number, a pill, a button) a description that shows only on hover, focus or tap. The
 * target gets a dotted underline (`.has-info`). Returns a wrapper to put where the target goes (`.info-host`,
 * `display: contents`, so it takes no room and does not change the layout); if the target is already in the page,
 * the box goes right after it and the target itself is returned. An empty `text` adds nothing.
 *
 *     el("p", { class: "kpi-label" }, withInfo(el("span", {}, "Extra profit"), "Orders from people we contact, …"))
 */
export function withInfo(target, text, { id } = {}) {
  if (!target || !present(text)) return target;
  const tip = infoTip(text, id);
  if (target.parentNode && typeof target.after === "function") {
    target.after(tip);
    describe(target, tip);
    return target;
  }
  describe(target, tip);
  return el("span", { class: "info-host" }, target, tip);
}

/**
 * A quiet (i) glyph that holds a description, for when there is no word or number to underline (after a card title
 * or a figure). `label` is what a screen reader says for the glyph. Returns null for an empty `text`.
 */
export function info(text, { label = "What this means", id } = {}) {
  if (!present(text)) return null;
  const dot = el("button", { type: "button", class: "info-dot", "aria-label": label }, el("span", { "aria-hidden": "true" }, "i"));
  const tip = infoTip(text, id);
  describe(dot, tip);
  return el("span", { class: "info-host" }, dot, tip);
}

/**
 * A title's words followed by its (i), with the last word and the glyph kept together, so a title that wraps on a
 * phone never leaves the (i) alone on a line. Returns the children to put in the heading.
 */
export function titleInfo(text, about, { label } = {}) {
  const dot = info(about, { label: label || `About ${text}` });
  if (!dot) return [text];
  const cut = String(text).lastIndexOf(" ");
  return [cut > 0 ? text.slice(0, cut + 1) : "", el("span", { class: "no-wrap" }, cut > 0 ? text.slice(cut + 1) : text, dot)];
}

/**
 * The one page head every screen uses: on the left the title (a noun: "Live", "Proof") with its status pills on the
 * same row and one short grey line under it; on the right the primary action and/or a freshness stamp. `status` is a
 * pill or a list of pills (`pill()`), `info` a description behind an (i) after the title, `sub` one short line (put
 * anything longer into `info`). Returns a `.page-head` block.
 */
export function pageHead({ title, sub, status = [], actions = [], stamp = null, info: about, level = 1 } = {}) {
  const pills = [status].flat().filter(Boolean);
  const titleRow = el(
    "div",
    { class: "page-title-row" },
    el(`h${level}`, { class: "page-title" }, title),
    present(about) ? info(about, { label: `About ${typeof title === "string" ? title : "this page"}` }) : null,
    pills.length ? el("div", { class: "page-status" }, pills) : null,
  );
  const left = el("div", { class: "page-head-main" }, titleRow, present(sub) ? el("p", { class: "page-sub" }, sub) : null);
  const right = [stamp, ...[actions].flat()].filter(Boolean);
  return el("div", { class: "page-head" }, left, right.length ? el("div", { class: "page-actions" }, right) : null);
}

/**
 * The divided KPI strip: one container split by hairlines. `items` is `[{label, value, range?, note?}]`, where
 * `value` and `range` are already formatted strings (money with `range()`, so it always carries its range). A
 * missing value is the grey em dash, never a made-up number. Four items get four columns; a phone gets two.
 */
export function kpiStrip(items = [], { label } = {}) {
  const list = items.filter(Boolean);
  return el(
    "div",
    { class: "kpis", role: "group", "aria-label": label || null, style: { "--kpi-count": String(Math.max(1, list.length)) } },
    list.map((k) => {
      const shown = present(k.value) && k.value !== EM_DASH;
      return el(
        "div",
        { class: "kpi" },
        el("p", { class: "kpi-label" }, k.label),
        el("p", { class: ["kpi-value", !shown && "is-missing"] }, shown ? k.value : EM_DASH),
        present(k.range) ? el("p", { class: "kpi-range tabular" }, k.range) : null,
        present(k.note) ? el("p", { class: "kpi-note" }, k.note) : null,
      );
    }),
  );
}

/**
 * A quiet notice (the product's honest status, for example "Not proven on your business yet: start in Shadow"):
 * pale amber with dark amber text. `tone: "neutral"` is a grey aside instead.
 */
export function notice(children, { tone = "warning", role = null } = {}) {
  return el("div", { class: ["notice", tone === "neutral" && "notice-neutral"], role }, children);
}

/**
 * A grey block at the real size of what is loading (no shimmer, nothing shifts when the data lands). `width` and
 * `height` are CSS lengths; the default is one line of body text across the block.
 */
export function skeleton({ width = "100%", height = "20px", inline = false, className = "" } = {}) {
  return el("span", { class: ["skeleton", inline && "skeleton-inline", className], "aria-hidden": "true", style: { width, height } });
}

/** `n` skeleton lines (the last one shorter), with a screen-reader "Loading" for the block. */
export function skeletonLines(n = 3, { label = "Loading…" } = {}) {
  const lines = Array.from({ length: Math.max(1, n) }, (_, i) => skeleton({ width: i === n - 1 && n > 1 ? "60%" : "100%" }));
  return el("div", { class: "skeleton-stack", role: "status" }, el("span", { class: "visually-hidden" }, label), lines);
}

/** A KPI strip whose labels are known while the values load: each value is a grey block at the value's size. */
export function skeletonKpis(labels = []) {
  return el(
    "div",
    { class: "kpis", role: "status", "aria-label": "Loading…", "aria-busy": "true", style: { "--kpi-count": String(Math.max(1, labels.length)) } },
    labels.map((label) =>
      el("div", { class: "kpi" }, el("p", { class: "kpi-label" }, label), el("p", { class: "kpi-value" }, skeleton({ width: "96px", height: "28px", inline: true }))),
    ),
  );
}

/**
 * The quiet freshness stamp: "Updated 20 Oct 2026, 09:00", with an optional Refresh text button. Nothing (an
 * empty string) when the time is unknown, so no made-up time is shown.
 */
export function freshness(iso, { label = "Updated", onRefresh } = {}) {
  const at = when(iso);
  if (at === EM_DASH && typeof onRefresh !== "function") return "";
  return el(
    "span",
    { class: "stamp" },
    at === EM_DASH ? null : el("span", {}, `${label} ${at}`),
    typeof onRefresh === "function" ? el("button", { type: "button", class: "link-btn", onclick: onRefresh }, "Refresh") : null,
  );
}

let segmentedSeq = 0;

/**
 * A segmented control (the autonomy dial: Shadow | Approve | Autopilot): radios in one rounded track, so arrow keys
 * move the choice. `options` is `[{value, label, text?, hint?, disabled?}]`; `onChange(value)` runs on a choice.
 *
 * Words: when the options carry a `text` (one sentence each), ONE sentence shows under the track, the selected
 * option's, and it follows the choice. Every segment also keeps its own sentence (with its `hint` first) in a
 * description that opens on hover, so the other levels are one hover away and all three sentences stay in the
 * DOM (screen readers hear each one with its radio). `note` overrides the sentence under the track (a string, or a
 * node you update yourself).
 */
export function segmented({ name, legend, options = [], value, onChange, note, disabled = false, className = "" } = {}) {
  const group = name || `seg-${(segmentedSeq += 1)}`;
  const described = options.some((o) => present(o.text));
  const sentence = (v) => {
    const o = options.find((x) => x.value === v);
    return o && present(o.text) ? o.text : "";
  };
  const noteNode = present(note) ? el("p", { class: "segmented-note" }, note) : described ? el("p", { class: "segmented-note", "aria-live": "polite" }, sentence(value)) : null;
  const track = el(
    "fieldset",
    { class: ["segmented", className], disabled },
    present(legend) ? el("legend", {}, legend) : null,
    options.map((o, i) => {
      const id = `${group}-${i}`;
      const input = el("input", {
        type: "radio",
        id,
        name: group,
        value: String(o.value),
        checked: o.value === value,
        disabled: Boolean(o.disabled),
        onchange: (e) => {
          if (!e.target.checked) return;
          if (described && !present(note) && noteNode) noteNode.textContent = sentence(o.value);
          if (typeof onChange === "function") onChange(o.value);
        },
      });
      const label = el("label", { for: id }, o.label);
      const words = [o.hint, o.text].filter(present);
      if (!words.length) return [input, label];
      const tip = el("span", { class: "info-tip", id: `${id}-info` }, words.map((w, j) => (j === 0 && words.length > 1 ? el("strong", {}, `${w}. `) : w)));
      describe(label, tip);
      input.setAttribute("aria-describedby", `${id}-info`);
      return [input, label, tip];
    }),
  );
  if (!noteNode) return track;
  return el("div", { class: "segmented-field" }, track, noteNode);
}

/**
 * A dialog (one of the two things with a shadow). Returns the `<dialog>`; call `.showModal()` to open it. Each
 * action is a node (use `button()`); a button with `value` inside a `<form method="dialog">` closes it.
 */
export function dialog({ title, body = [], actions = [], label } = {}) {
  const node = el(
    "dialog",
    { class: "dialog", "aria-label": label || (typeof title === "string" ? title : null) },
    title ? el("h2", { class: "dialog-title" }, title) : null,
    body,
    actions.length ? el("div", { class: "dialog-actions" }, actions) : null,
  );
  node.addEventListener("close", () => node.remove());
  return node;
}
