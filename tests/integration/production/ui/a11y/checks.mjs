/* The structural accessibility checks, over a DOM tree (Plan M, M181 part A).

   Written here rather than taken from axe-core (it is not in package.json and installs are not allowed), and kept
   small enough to read in one sitting. They check what a page's markup alone can say, in jsdom, without a style
   sheet: an element is "not there" when it, or an ancestor, has the `hidden` attribute or an inline
   `display:none` / `visibility:hidden`.

   Rules (the ids are what `known.json` lists):
     control-name     every form control (input, select, textarea, role textbox/checkbox/radio/switch/combobox/
                      slider/spinbutton/searchbox) has an accessible name: aria-labelledby, aria-label, a <label>
                      (for= or wrapping) or, last, title. A placeholder is NOT a name.
     button-name      every button, input[type=button|submit|reset], role=button, role=tab, role=menuitem and
                      <summary> has text, aria-label, aria-labelledby, title, or an image with alt inside.
     link-name        every <a href> and role=link has the same.
     image-alt        every <img> has an alt attribute (alt="" marks it decorative); every svg/[role=img] that is not
                      aria-hidden has a name.
     one-h1           exactly one visible <h1> (or role=heading aria-level=1) on the page.
     heading-jump     a heading that is three or more levels deeper than the one before it, or a first heading
                      deeper than h2. A jump of two levels is recorded in `headings`, not failed.
     tabindex-positive  tabindex above 0 (it takes the element out of the page's natural tab order)
     duplicate-id     an id used twice (a label's `for` and aria-labelledby then point at the wrong element).

   `check(root)` returns `{ violations: [{rule, target, text}], headings: [level...], interactive: n }`. */

const NAMELESS_INPUT_TYPES = new Set(["hidden"]);
const BUTTON_INPUT_TYPES = new Set(["button", "submit", "reset"]);
const CONTROL_ROLES = new Set(["textbox", "checkbox", "radio", "switch", "combobox", "slider", "spinbutton", "searchbox", "listbox"]);
const BUTTON_ROLES = new Set(["button", "tab", "menuitem", "menuitemcheckbox", "menuitemradio"]);

const tag = (el) => el.tagName.toLowerCase();
const clean = (text) => String(text || "").replace(/\s+/g, " ").trim();

/** Whether the element is out of the page: hidden itself or inside something hidden. */
export function isGone(el) {
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    if (node.hasAttribute("hidden")) return true;
    const inline = (node.getAttribute("style") || "").replace(/\s+/g, "").toLowerCase();
    if (inline.includes("display:none") || inline.includes("visibility:hidden")) return true;
    const name = tag(node);
    if (name === "script" || name === "style" || name === "template" || name === "noscript") return true;
  }
  return false;
}

/** Text a person using a screen reader gets from `el`'s content: not aria-hidden parts, image alts, svg titles. */
export function contentName(el) {
  const pieces = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      pieces.push(node.nodeValue);
      return;
    }
    if (node.nodeType !== 1 || node.getAttribute("aria-hidden") === "true" || isGone(node)) return;
    const own = clean(node.getAttribute("aria-label"));
    if (own) {
      pieces.push(own);
      return;
    }
    const name = tag(node);
    if (name === "img") {
      pieces.push(node.getAttribute("alt") || "");
      return;
    }
    if (name === "svg") {
      const title = [...node.children].find((c) => tag(c) === "title");
      pieces.push(title ? title.textContent : node.getAttribute("aria-label") || "");
      return;
    }
    for (const child of node.childNodes) walk(child);
  };
  for (const child of el.childNodes) walk(child);
  return clean(pieces.join(" "));
}

const labelledBy = (el) => {
  const ids = clean(el.getAttribute("aria-labelledby")).split(" ").filter(Boolean);
  const doc = el.ownerDocument;
  return clean(ids.map((id) => (doc.getElementById(id) ? contentName(doc.getElementById(id)) || clean(doc.getElementById(id).textContent) : "")).join(" "));
};

/** The accessible name of a form control: labelledby, aria-label, <label>, title (placeholder is not a name). */
export function controlName(el) {
  const byIds = labelledBy(el);
  if (byIds) return byIds;
  const own = clean(el.getAttribute("aria-label"));
  if (own) return own;
  const labels = el.labels ? [...el.labels] : [];
  const fromLabel = clean(labels.map((l) => contentName(l) || clean(l.textContent)).join(" "));
  if (fromLabel) return fromLabel;
  return clean(el.getAttribute("title"));
}

/** The accessible name of a button or link: labelledby, aria-label, own content, input value, title. */
export function actionName(el) {
  const byIds = labelledBy(el);
  if (byIds) return byIds;
  const own = clean(el.getAttribute("aria-label"));
  if (own) return own;
  if (tag(el) === "input") return clean(el.getAttribute("value")) || clean(el.getAttribute("title"));
  return contentName(el) || clean(el.getAttribute("title"));
}

/** A short, stable description of an element, for a known-defects list: tag, type, name/data attribute, id, classes. */
export function describe(el) {
  let out = tag(el);
  const type = el.getAttribute("type");
  if (type && tag(el) === "input") out += `[type=${type}]`;
  const role = el.getAttribute("role");
  if (role) out += `[role=${role}]`;
  const named = ["name", ...el.getAttributeNames().filter((n) => n.startsWith("data-")).sort()].filter((n) => el.hasAttribute(n));
  for (const attribute of named.slice(0, 2)) {
    const value = el.getAttribute(attribute).replace(/\d{3,}/g, "N").slice(0, 24);
    out += value ? `[${attribute}=${value}]` : `[${attribute}]`;
  }
  if (el.id && !/\d{3,}/.test(el.id)) out += `#${el.id.slice(0, 32)}`;
  const classes = [...el.classList].slice(0, 2);
  if (classes.length) out += `.${classes.join(".")}`;
  return out.slice(0, 96);
}

const headingLevel = (el) => {
  const name = tag(el);
  if (/^h[1-6]$/.test(name)) return Number(name[1]);
  if (el.getAttribute("role") === "heading") return Number(el.getAttribute("aria-level")) || 2;
  return 0;
};

/** Run every rule on `root` (an element). */
export function check(root) {
  const violations = [];
  const add = (rule, el) => violations.push({ rule, target: describe(el), text: clean(el.textContent).slice(0, 40) });
  const all = [...root.querySelectorAll("*")].filter((el) => !isGone(el));
  let interactive = 0;

  for (const el of all) {
    const name = tag(el);
    const role = el.getAttribute("role");
    const type = (el.getAttribute("type") || "text").toLowerCase();

    const isControl =
      (name === "input" && !NAMELESS_INPUT_TYPES.has(type) && !BUTTON_INPUT_TYPES.has(type) && type !== "image") ||
      name === "select" ||
      name === "textarea" ||
      CONTROL_ROLES.has(role);
    const isButton = name === "button" || (name === "input" && BUTTON_INPUT_TYPES.has(type)) || name === "summary" || BUTTON_ROLES.has(role);
    const isLink = (name === "a" && el.hasAttribute("href")) || role === "link";

    if (isControl) {
      interactive += 1;
      if (!controlName(el)) add("control-name", el);
    }
    if (isButton) {
      interactive += 1;
      if (!actionName(el)) add("button-name", el);
    }
    if (isLink) {
      interactive += 1;
      if (!actionName(el)) add("link-name", el);
    }
    if (Number(el.getAttribute("tabindex")) > 0) add("tabindex-positive", el);
    if (name === "input" && type === "image") {
      interactive += 1;
      if (!clean(el.getAttribute("alt")) && !actionName(el)) add("button-name", el);
    }
    if (name === "img" && !el.hasAttribute("alt") && !clean(el.getAttribute("aria-label")) && el.getAttribute("role") !== "presentation") {
      add("image-alt", el);
    }
    if ((name === "svg" || role === "img") && el.getAttribute("aria-hidden") !== "true" && name !== "img") {
      const decorative = role === "presentation" || role === "none";
      if (!decorative && role === "img" && !actionName(el) && !labelledBy(el)) add("image-alt", el);
    }
  }

  const headings = all.filter((el) => headingLevel(el) > 0 && el.getAttribute("aria-hidden") !== "true");
  const levels = headings.map(headingLevel);
  const h1 = levels.filter((n) => n === 1).length;
  if (h1 !== 1) violations.push({ rule: "one-h1", target: `${h1} h1 elements`, text: "" });
  levels.forEach((level, i) => {
    const before = i === 0 ? 0 : levels[i - 1];
    if (i === 0 ? level > 2 : level - before >= 3) add("heading-jump", headings[i]);
  });

  const seen = new Set();
  const repeated = new Set();
  for (const el of all) {
    if (!el.id) continue;
    if (seen.has(el.id)) repeated.add(el.id);
    seen.add(el.id);
  }
  for (const id of [...repeated].sort()) violations.push({ rule: "duplicate-id", target: `#${id.replace(/\d{3,}/g, "N").slice(0, 40)}`, text: "" });

  return { violations, headings: levels, interactive };
}

/** `{ "rule|target": count }` for a list of violations. */
export function tally(violations) {
  const counts = {};
  for (const v of violations) {
    const key = `${v.rule}|${v.target}`;
    counts[key] = (counts[key] || 0) + 1;
  }
  return counts;
}

/**
 * Compare what a page shows with what it is allowed (its known defects).
 * `worse`: keys shown more often than allowed (a NEW defect is one with an allowance of 0); `better`: keys shown less
 * often than allowed (the list can be lowered).
 */
export function against(found, allowed) {
  const worse = [];
  const better = [];
  for (const [key, count] of Object.entries(found)) {
    const limit = allowed[key] || 0;
    if (count > limit) worse.push({ key, count, allowed: limit });
  }
  for (const [key, limit] of Object.entries(allowed)) {
    const count = found[key] || 0;
    if (count < limit) better.push({ key, count, allowed: limit });
  }
  return { ok: worse.length === 0, worse, better };
}
