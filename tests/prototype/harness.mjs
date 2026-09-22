/* Loads marketing-ai-prototype.html into jsdom and drives its hash router.
   The prototype is one classic script, so its top-level `function`s land on
   `window` and its top-level `const`s only in the global lexical scope —
   read those through `ev()`. */
import fs from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

export const ROOT = new URL("../../", import.meta.url);
export const FILE = new URL("marketing-ai-prototype.html", ROOT);
/** A file from the repository, as text: lets a test pin the prototype to the product. */
export const repoText = (rel) => fs.readFileSync(new URL(rel, ROOT), "utf8");
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
  // Every browser has Blob.prototype.text(); jsdom does not. Same contract, over FileReader.
  if (!dom.window.Blob.prototype.text) {
    dom.window.Blob.prototype.text = function text() {
      return new Promise((resolve, reject) => {
        const reader = new dom.window.FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsText(this);
      });
    };
  }
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

/** Choose a real file in a file input, as a user would, and wait until the page has read it.
 *  Reading is asynchronous (Blob.text), so this waits for the re-render the read ends in -
 *  the page swaps its <main> element - rather than guessing how long a read takes. */
export async function upload(dom, sel, name, content, timeoutMs = 3000) {
  const input = $(dom, sel);
  if (!input) throw new Error(`no file input: ${sel}`);
  const before = $(dom, "#app main");
  const file = new dom.window.File([content], name, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  const started = Date.now();
  while ($(dom, "#app main") === before) {
    if (Date.now() - started > timeoutMs) throw new Error(`the page never re-rendered after ${name}`);
    await wait(10);
  }
}
