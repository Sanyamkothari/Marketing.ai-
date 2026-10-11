/* Colour contrast of the palette, from the style sheets themselves (Plan M, M181 part A).

   The UI has no external style sheet: its colours are custom properties on `:root` in `ui/index.html`, and rules in
   that file's <style> and in the template strings of the .js files under ui/. This module reads them all:

     tokens(html)          `{ "--ink": "#111827", ... }`, with `var(--x)` aliases resolved
     rulesIn(text)         `[{selector, props}]` for every `selector{prop:value;...}` block in a CSS or JS string
     pairsOf(rules, tok)   the text/background pairs the rules actually use:
                             - a rule that sets both `color` and a background colour is one pair;
                             - a rule that sets only `color` is text on the background of the nearest rule that names
                               one of its compounds (".stage-pill .n" on ".stage-pill"), else on the page surface
                               (--surface) and on the soft panel (--soft), the two grounds text sits on;
     ratio(a, b)           the WCAG 2.x contrast ratio of two #rrggbb colours
     required(props)       4.5, or 3 for large text (24px, or 18.66px and bold)

   What it cannot see, it skips rather than guesses: gradients, rgba() with alpha, and `currentColor`. A disabled or inactive control is exempt, as
   WCAG 1.4.3 exempts it. */
import fs from "node:fs";
import path from "node:path";

export const UI_DIR = new URL("../../../../../ui/", import.meta.url);

const HEX = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

/** "#abc" and "#aabbcc" as [r, g, b], or null. */
export function rgbOf(hex) {
  if (!HEX.test(hex || "")) return null;
  let h = hex.slice(1);
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

const channel = (v) => {
  const s = v / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};

export function luminance(hex) {
  const [r, g, b] = rgbOf(hex);
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG contrast ratio, 1 to 21. */
export function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "");

/** `--name: value` declarations of the first `:root{...}` in an HTML or CSS string, aliases resolved. */
export function tokens(text) {
  const root = stripComments(text).match(/:root\s*\{([^}]*)\}/);
  if (!root) return {};
  const raw = {};
  for (const m of root[1].matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);?/gi)) raw[m[1]] = m[2].trim();
  const resolved = {};
  const resolve = (value, depth = 0) => {
    if (depth > 8) return null;
    const v = value.trim();
    const alias = v.match(/^var\((--[a-z0-9-]+)\)$/i);
    if (alias) return raw[alias[1]] === undefined ? null : resolve(raw[alias[1]], depth + 1);
    return v;
  };
  for (const [name, value] of Object.entries(raw)) resolved[name] = resolve(value);
  return resolved;
}

/** A property value as one #rrggbb colour, or null when it is anything else (gradient, alpha, local variable). */
export function colourOf(value, tok) {
  if (!value) return null;
  let v = value.trim().replace(/\s*!important$/i, "");
  const alias = v.match(/^var\((--[a-z0-9-]+)\)$/i);
  if (alias) v = tok[alias[1]] || "";
  if (/^#[0-9a-f]{3}$/i.test(v) || /^#[0-9a-f]{6}$/i.test(v)) return v.length === 4 ? `#${[...v.slice(1)].map((c) => c + c).join("")}` : v;
  return null;
}

const DECLARATION = /^\s*(?:[a-z0-9-]+\s*:\s*[^;{}]+;?\s*)+$/i;

/** Every `selector{declarations}` block in a CSS or JavaScript string; JavaScript that is not CSS is skipped. */
export function rulesIn(text) {
  const rules = [];
  const clean = stripComments(text);
  for (const m of clean.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].replace(/\s+/g, " ").trim().split("}").pop().trim();
    const body = m[2];
    if (!selector || /\$\{|`|;|=>|function|\bif\b|\bfor\b|\breturn\b/.test(selector) || /\$\{/.test(body)) continue;
    if (!/^[.#\[*:>&a-z@]/i.test(selector) || selector.startsWith("@")) continue;
    if (!DECLARATION.test(body)) continue;
    const props = {};
    for (const d of body.matchAll(/([a-z0-9-]+)\s*:\s*([^;]+);?/gi)) props[d[1].toLowerCase()] = d[2].trim();
    rules.push({ selector, props });
  }
  return rules;
}

/** 4.5 for normal text, 3 for large (24px, or 18.66px and bold) or when the rule says nothing about its size. */
export function required(props) {
  const size = Number((props["font-size"] || "").match(/^([\d.]+)px$/)?.[1] || 0);
  const weight = props["font-weight"] || "";
  const bold = weight === "bold" || Number(weight) >= 700;
  return size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
}

const INACTIVE = /:disabled|\[disabled\]|\[aria-disabled|\.disabled\b|::placeholder|::selection/;

/** The compounds of a selector, without a trailing pseudo-element: ".a .b::after" is [".a", ".b"]. */
const compoundsOf = (selector) =>
  selector
    .replace(/::?(before|after|first-line|first-letter)$/, "")
    .split(/\s*[>+~]\s*|\s+/)
    .filter(Boolean);

/** The custom properties a rule defines (`--c`, `--t` on `.t-P`), resolved against the tokens. */
function localDefinitions(rules, tok) {
  const found = [];
  for (const { selector, props } of rules) {
    const own = {};
    for (const [name, value] of Object.entries(props)) {
      if (!name.startsWith("--")) continue;
      const colour = colourOf(value, tok);
      if (colour) own[name] = colour;
    }
    if (Object.keys(own).length) found.push({ selector, own });
  }
  return found;
}

const varsIn = (value) => [...String(value || "").matchAll(/var\((--[a-z0-9-]+)\)/gi)].map((m) => m[1]);

/**
 * The text/background pairs the rules use.
 *  - A rule with a text colour and a background is one pair.
 *  - A rule with only a text colour sits on the background of the nearest rule that names one of its compounds alone
 *    and sets a background (".stage-pill .n" sits on ".stage-pill"'s dark fill); with none, on `surfaces`.
 *  - A colour held in a custom property that other rules set locally (`.block .go{color:var(--c)}`, with `--c` set
 *    by `.t-P`, `.t-G` and `.t-H`) is one pair for each of those rules, named "selector [definer]".
 */
export function pairsOf(rules, tok, surfaces = ["--surface", "--soft"]) {
  const definers = localDefinitions(rules, tok);
  const grounds = new Map();
  for (const { selector, props } of rules) {
    const bg = colourOf(props["background-color"] || props.background, tok);
    if (bg && !INACTIVE.test(selector)) grounds.set(selector, { bg, text: props["background-color"] || props.background });
  }
  const out = [];
  for (const { selector, props } of rules) {
    if (INACTIVE.test(selector)) continue;
    const fgRaw = props.color;
    if (!fgRaw || /^(inherit|currentcolor|transparent|unset|initial)$/i.test(fgRaw)) continue;
    const bgRaw = props["background-color"] || props.background;
    const need = required(props);
    const wanted = [...new Set([...varsIn(fgRaw), ...varsIn(bgRaw)])].filter((name) => tok[name] === undefined);
    // [label suffix, tokens as this variant sees them]
    let variants = [["", tok]];
    if (wanted.length) {
      variants = definers
        .filter((d) => wanted.every((name) => d.own[name]))
        // ".chip.type.t-H{color:var(--ink2)}" replaces the variable for the teal chip: that rule is checked itself.
        .filter((d) => !rules.some((r) => r.selector === `${selector}${d.selector}` && r.props.color))
        .map((d) => [` [${d.selector}]`, { ...tok, ...d.own }]);
    }
    for (const [suffix, view] of variants) {
      const fg = colourOf(fgRaw, view);
      if (!fg) continue;
      const bg = colourOf(bgRaw, view);
      const name = `${selector}${suffix}`;
      if (bg) {
        out.push({ selector: name, fg, bg, fgText: fgRaw, bgText: bgRaw, need });
      } else if (!bgRaw) {
        const parts = compoundsOf(selector);
        let ground = null;
        for (let i = parts.length - 1; i >= 0 && !ground; i -= 1) ground = grounds.get(parts[i]) || null;
        if (ground) out.push({ selector: name, fg, bg: ground.bg, fgText: fgRaw, bgText: "its ground", need, implied: true });
        else for (const s of surfaces) out.push({ selector: name, fg, bg: colourOf(`var(${s})`, view), fgText: fgRaw, bgText: "its ground", need, implied: true });
      }
    }
  }
  return out;
}

/** Every style source of the UI: index.html, and every .js file of ui/ that holds a CSS-looking block. */
export function styleSources() {
  const found = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(js|html|css)$/.test(entry.name)) {
        const file = path.relative(new URL("../../../../../", import.meta.url).pathname, full);
        found.push({ file, text: fs.readFileSync(full, "utf8") });
      }
    }
  };
  walk(UI_DIR.pathname);
  return found;
}
