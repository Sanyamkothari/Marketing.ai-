/* The brand: the product's name, logo and accent colour, set per deployment (Plan M M182, DEC-1612).

   Four deployment settings (`brand_name`, `brand_logo_url`, `brand_logo_alt`, `brand_color`, engine/settings.py)
   reach the browser in the `brand` object of `GET /auth/me`, the call the UI already makes at start-up, with
   sign-in on or off. This module follows the production session (as `layout.js` does) and, once the answer
   arrives, applies it:

     - the wordmark's text and the logo in the top bar (`chrome.js` and `shell.js` draw both through
       `brandNameHtml()`, `brandLogoAlt()` and `brandLogoHtml()`), and the logo link's name, "<alt>, home";
     - the browser tab's title: every page ends it with the product's name ("Campaigns · Marketing AI") and the
       first one is "Marketing AI · Minfy"; a watcher on <title> swaps the name, so the forty places that set a
       title are untouched;
     - `--brand-blue` on <html>, the one token every link, focus ring, active item and badge reads (and the tab's
       icon, which is drawn in the same colour), plus the one colour that is a fixed tint of it in ui/index.html (the
       run-progress pill's border), written into the `#brand-styles` <style> for a custom colour only.

   With the defaults (the answer of a deployment that set nothing, an older API with no `brand`, or no answer
   yet) nothing is changed: the three functions return today's markup character for character, the <html>
   element gets no style, no <style> is added and the title is left alone. tests/integration/test_brand_ui.py
   proves it by drawing every page twice, once from a copy of ui/ with this milestone removed.

   Three choices worth knowing:
     - The built-in logo is Minfy's own: it keeps Minfy's blue whatever `brand_color` says (two rules in
       `#brand-styles`, written for a custom colour only, so ui/index.html is the same bytes as before). A
       deployment that changes the colour and wants its own mark sets `brand_logo_url`.
     - A custom name is drawn inside `data-user-text` (ui/labels.js userText): it is the deployment's own
       wording, not ours, so the glossary's visible-text check does not count it. The default name is plain text.
     - The last custom brand is remembered in this browser (localStorage, never required), so the next page load
       draws the right name and colour at once instead of the default until `GET /auth/me` answers, and the
       sign-in screen (which cannot ask `/auth/me`) is drawn in the brand too. The server's answer replaces it.

   This file imports `dom.js` (the page-changed event) and `labels.js` (the mark on a person's own words), both
   leaves, and the session on demand; it never imports the router or the bar, so `chrome.js`, `shell.js` and
   `app.js` can import it without a cycle (DEC-790). The bar repaints on `BRAND_EVENT`. */

import { esc } from "./dom.js";
import { userText } from "./labels.js";

/** Today's product: what a deployment that set nothing gets, and what a malformed answer falls back to. */
export const DEFAULT_BRAND = Object.freeze({
  name: "Marketing AI",
  logo_url: "",
  logo_alt: "Minfy",
  color: "#1E57BD",
});

/** Fired on window after the brand changes; `event.detail` is the new brand. */
export const BRAND_EVENT = "marketing-ai:brand-changed";
export const STORAGE_KEY = "marketing-ai.brand";
export const STYLE_ID = "brand-styles";
export const TOKEN = "--brand-blue";

/** The title every page ends with, and the first page's, before the brand is applied. */
const firstTitle = (name, alt) => `${name} · ${alt}`;
/** The colour inside the tab's icon (ui/index.html) and the same colour in the page's icon link. */
const ICON_COLOUR = "%23" + DEFAULT_BRAND.color.slice(1);

const COLOR = /^#[0-9a-f]{6}$/i;
// The API refuses anything else; the browser holds the same line, so a bad answer cannot put script or a data URI in an <img>.
const LOGO_URL = /^(?:https:\/\/[^\s"'<>\\`]+|\/(?!\/)[^\s"'<>\\`]*)$/;

let brand = { ...DEFAULT_BRAND };
// Every name and first title the page has carried (today's included): a title written under an earlier brand is
// swapped to the one now in force, also when the brand changes again or goes back to the default.
const seenNames = new Set([DEFAULT_BRAND.name]);
const seenFirst = new Set([firstTitle(DEFAULT_BRAND.name, DEFAULT_BRAND.logo_alt)]);
let failedLogo = new Set();
let iconHref = null; // the page's own icon link, remembered before the first change
let watching = false;
let titleObserver = null;

/** `raw` (an answer's `brand`, or a remembered one) as a complete brand: what is wrong or missing is the default. */
export function cleanBrand(raw) {
  const out = { ...DEFAULT_BRAND };
  if (!raw || typeof raw !== "object") return out;
  if (typeof raw.name === "string" && raw.name.trim() && raw.name.trim().length <= 80) out.name = raw.name.trim();
  if (typeof raw.logo_url === "string" && LOGO_URL.test(raw.logo_url.trim())) out.logo_url = raw.logo_url.trim();
  if (typeof raw.logo_alt === "string" && raw.logo_alt.trim() && raw.logo_alt.trim().length <= 120) out.logo_alt = raw.logo_alt.trim();
  if (typeof raw.color === "string" && COLOR.test(raw.color.trim())) out.color = raw.color.trim().toUpperCase();
  return out;
}

const sameBrand = (a, b) => a.name === b.name && a.logo_url === b.logo_url && a.logo_alt === b.logo_alt && a.color === b.color;

/** The brand now in force (a copy). */
export const currentBrand = () => ({ ...brand });
/** True when the brand is today's product, in every part. */
export const isDefaultBrand = () => sameBrand(brand, DEFAULT_BRAND);

/** The product's name as plain text (a tab title, a loading heading). */
export const brandName = () => brand.name;

/** The wordmark's content: today's text as it is; a custom name inside the mark that keeps it out of the glossary check. */
export function brandNameHtml() {
  return brand.name === DEFAULT_BRAND.name ? esc(brand.name) : userText(brand.name);
}

/** What the logo is called ("Minfy" unless set): the logo link reads "<this>, home". */
export const brandLogoAlt = () => brand.logo_alt;

const logoUrl = () => (brand.logo_url && !failedLogo.has(brand.logo_url) ? brand.logo_url : "");

/**
 * The logo's markup: `builtin` (dom.js's Minfy logo) as it is by default, with its name replaced when only the name
 * is set; an <img class="logo logo-img"> with the alt text when an address is set.
 */
export function brandLogoHtml(builtin) {
  const url = logoUrl();
  if (url) return `<img class="logo logo-img" src="${esc(url)}" alt="${esc(brand.logo_alt)}">`;
  if (brand.logo_alt === DEFAULT_BRAND.logo_alt) return builtin;
  return builtin.replace(`aria-label="${DEFAULT_BRAND.logo_alt}"`, `aria-label="${esc(brand.logo_alt)}"`);
}

/** A page title with the product's name swapped for the brand's (the identity when the name is today's). */
export function brandTitle(text) {
  const value = String(text);
  if (seenFirst.has(value)) return firstTitle(brand.name, brand.logo_alt);
  // The longest name the title ends with: the brand's own (done), or an earlier one (swapped).
  let longest = "";
  for (const name of seenNames) {
    if (name.length > longest.length && value.endsWith(` · ${name}`)) longest = name;
  }
  if (!longest || longest === brand.name) return value;
  return `${value.slice(0, -(longest.length + 3))} · ${brand.name}`;
}

// --- the page ---------------------------------------------------------------------------------------------------

function doc() {
  return typeof document !== "undefined" && document.documentElement ? document : null;
}

function retitle() {
  const d = doc();
  if (!d) return;
  const next = brandTitle(d.title);
  if (next !== d.title) d.title = next;
}

function watchTitle(d) {
  if (watching) return;
  const Observer = typeof window !== "undefined" ? window.MutationObserver : null;
  if (!d.head || !Observer) return;
  watching = true;
  // The whole <head>, not just <title>: a page that sets `document.title` before the element exists creates it.
  titleObserver = new Observer(() => retitle());
  titleObserver.observe(d.head, { childList: true, characterData: true, subtree: true });
}

function setIcon(d, colour) {
  const link = d.querySelector('link[rel="icon"]');
  if (!link) return;
  if (iconHref === null) iconHref = link.getAttribute("href") || "";
  if (!iconHref.includes(ICON_COLOUR)) return;
  link.setAttribute("href", colour === DEFAULT_BRAND.color ? iconHref : iconHref.replace(ICON_COLOUR, `%23${colour.slice(1)}`));
}

const FIT_RULE = ".logo-img{object-fit:contain;object-position:left center}";

/** `#rrggbb` as "r,g,b". */
const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(",");

/** The rules a brand needs beyond the token: none for today's brand, so no <style> is added or it is removed. */
function brandRules() {
  const rules = [];
  if (brand.logo_url) rules.push(FIT_RULE);
  if (brand.color !== DEFAULT_BRAND.color) {
    const own = DEFAULT_BRAND.color;
    // Minfy's own mark keeps Minfy's blue; the run-progress pill's border is a 20% tint of the accent colour.
    rules.push(`html .logo .b{fill:${own}}`, `html .logo .s{stroke:${own}}`);
    rules.push(`html .run-pct{border-color:rgba(${rgb(brand.color)},0.2)}`);
  }
  return rules.join("");
}

function syncStyle(d) {
  const text = brandRules();
  let el = d.getElementById(STYLE_ID);
  if (!text) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    if (!d.head) return;
    el = d.createElement("style");
    el.id = STYLE_ID;
    d.head.appendChild(el);
  }
  if (el.textContent !== text) el.textContent = text;
}

function applyToPage() {
  const d = doc();
  if (!d) return;
  const root = d.documentElement;
  if (brand.color === DEFAULT_BRAND.color) {
    if (root.style.getPropertyValue(TOKEN)) {
      root.style.removeProperty(TOKEN);
      if (!root.getAttribute("style")) root.removeAttribute("style");
    }
  } else {
    root.style.setProperty(TOKEN, brand.color);
  }
  if (iconHref !== null || brand.color !== DEFAULT_BRAND.color) setIcon(d, brand.color);
  syncStyle(d);
  seenNames.add(brand.name);
  seenFirst.add(firstTitle(brand.name, brand.logo_alt));
  if (watching || brand.name !== DEFAULT_BRAND.name || brand.logo_alt !== DEFAULT_BRAND.logo_alt) {
    watchTitle(d);
    retitle();
  }
}

function announce() {
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new window.CustomEvent(BRAND_EVENT, { detail: currentBrand() }));
  }
}

function remember(value) {
  try {
    if (sameBrand(value, DEFAULT_BRAND)) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // a private window or blocked storage: the brand is drawn from the server's answer each time
  }
}

function recalled() {
  try {
    return cleanBrand(JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null"));
  } catch {
    return { ...DEFAULT_BRAND };
  }
}

/**
 * Make `raw` the brand: anything wrong in it is today's value for that part. Applies it to the page, repaints the bar
 * (`BRAND_EVENT`) and, for the server's answer, remembers it in this browser. Does nothing when nothing changed.
 */
export function setBrand(raw, { fromServer = false } = {}) {
  const next = cleanBrand(raw);
  if (fromServer) remember(next);
  if (sameBrand(next, brand)) return;
  brand = next;
  applyToPage();
  announce();
}

/** Test seam: back to the state of a fresh page. */
export function _resetBrandForTests() {
  brand = { ...DEFAULT_BRAND };
  failedLogo = new Set();
  iconHref = null;
  watching = false;
  if (titleObserver) titleObserver.disconnect();
  titleObserver = null;
  seenNames.clear();
  seenNames.add(DEFAULT_BRAND.name);
  seenFirst.clear();
  seenFirst.add(firstTitle(DEFAULT_BRAND.name, DEFAULT_BRAND.logo_alt));
  const d = doc();
  if (d) {
    d.documentElement.style.removeProperty(TOKEN);
    if (!d.documentElement.getAttribute("style")) d.documentElement.removeAttribute("style");
    const style = d.getElementById(STYLE_ID);
    if (style) style.remove();
  }
}

// A logo that does not load (a wrong address) is dropped for the rest of the page, so the bar never shows a broken
// image; the built-in logo is drawn instead.
function onImageError(event) {
  const target = event.target;
  if (!target || typeof target.matches !== "function" || !target.matches("img.logo-img")) return;
  if (!brand.logo_url || failedLogo.has(brand.logo_url)) return;
  failedLogo.add(brand.logo_url);
  announce();
}

// --- start ------------------------------------------------------------------------------------------------------

// What this browser remembers, at once; then the session's answer, as `layout.js` follows it. An API without the
// field (a build before M182), a signed-out answer or no session module changes nothing.
const d0 = doc();
if (d0 && typeof window !== "undefined") {
  d0.addEventListener("error", onImageError, true);
  const known = recalled();
  if (!sameBrand(known, DEFAULT_BRAND)) {
    brand = known;
    applyToPage();
  }
}

export const followSession = import("./modules/production/session.js").then(
  (session) =>
    session.onSession((me) => {
      if (me && me.brand && typeof me.brand === "object") setBrand(me.brand, { fromServer: true });
    }),
  () => null,
);
