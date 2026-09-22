/* Loads marketing-ai-prototype.html into jsdom and drives its hash router.
   The prototype is one classic script, so its top-level `function`s land on
   `window` and its top-level `const`s only in the global lexical scope —
   read those through `ev()`. */
import fs from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

export const FILE = new URL("../../marketing-ai-prototype.html", import.meta.url);
export const HTML = fs.readFileSync(FILE, "utf8");

export function load(hash = "#/") {
  // jsdom has no layout, so scrollTo and the font stylesheet only make noise.
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {});
  const dom = new JSDOM(HTML, {
    url: "http://localhost/" + hash,
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  dom.window.scrollTo = () => {};
  // Every animation in the prototype collapses when reduced motion is on,
  // which keeps the timed build and run flows inside a test's patience.
  dom.window.matchMedia = () => ({ matches: true, addListener() {}, removeListener() {} });
  return dom;
}

/** Navigate the hash router and re-render synchronously. */
export function go(dom, hash) {
  dom.window.location.hash = hash;
  dom.window.render();
  return dom.window.document;
}

/** Evaluate an expression in the page's global scope. */
export const ev = (dom, expr) => dom.window.eval(expr);

export const $ = (dom, sel) => dom.window.document.querySelector(sel);
export const $$ = (dom, sel) => [...dom.window.document.querySelectorAll(sel)];
export const text = (dom, sel) => ($(dom, sel) || {}).textContent || "";
export const body = (dom) => dom.window.document.getElementById("app").textContent;

export function click(dom, sel) {
  const el = typeof sel === "string" ? $(dom, sel) : sel;
  if (!el) throw new Error(`nothing to click: ${sel}`);
  el.click();
  return el;
}

export function set(dom, sel, value) {
  const el = typeof sel === "string" ? $(dom, sel) : sel;
  if (!el) throw new Error(`no control: ${sel}`);
  if (el.type === "checkbox") el.checked = value;
  else el.value = value;
  el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  el.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  return el;
}

export const wait = (ms) => new Promise((r) => setTimeout(r, ms));
