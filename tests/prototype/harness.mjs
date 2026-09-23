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

/** Per page: the work it still has pending (see track()), and who is waiting for it to end. */
const PENDING = new WeakMap();

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
  track(dom);
  // Every animation in the prototype collapses when reduced motion is on,
  // which keeps the timed build and run flows inside a test's patience.
  dom.window.matchMedia = () => ({ matches: true, addListener() {}, removeListener() {} });
  return dom;
}

/** Count the page's asynchronous work, so settle() can wait for it to end instead of guessing how
 *  long it takes (DEC-875). The prototype has exactly two kinds, and both pass through here:
 *  - file reads: every one goes through Blob.prototype.text;
 *  - timers: the run and build flows, and the hashchange jsdom queues when `go()` sets the hash
 *    (jsdom's session history calls the window's own setTimeout), all use window.setTimeout.
 *  An item stops counting one macrotask after it ends, so whatever it set off in microtasks - the
 *  re-render a read's `.then` draws - has always run by then. */
function track(dom) {
  const w = dom.window;
  const pending = { reads: 0, timers: new Set(), waiters: [], barrier: w.setTimeout.bind(w) };
  PENDING.set(dom, pending);
  const ended = () =>
    setImmediate(() => {
      if (pending.reads === 0 && pending.timers.size === 0) pending.waiters.splice(0).forEach((wake) => wake());
    });
  const readText = w.Blob.prototype.text;
  w.Blob.prototype.text = function text() {
    pending.reads++;
    const read = readText.call(this);
    const done = () => {
      pending.reads--;
      ended();
    };
    read.then(done, done);
    return read;
  };
  const setTimeout_ = w.setTimeout;
  const clearTimeout_ = w.clearTimeout;
  w.setTimeout = function setTimeout(handler, timeout, ...args) {
    const id = setTimeout_.call(
      w,
      (...params) => {
        pending.timers.delete(id);
        try {
          if (typeof handler === "function") handler.apply(w, params);
          else w.eval(String(handler));
        } finally {
          ended();
        }
      },
      timeout,
      ...args,
    );
    pending.timers.add(id);
    return id;
  };
  w.clearTimeout = function clearTimeout(id) {
    if (pending.timers.delete(id)) ended();
    return clearTimeout_.call(w, id);
  };
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

/** Wait until the page has settled: no file read in flight and no timer of its own left to fire -
 *  so a run or build flow has reached its last step, a queued hashchange has re-rendered, and a
 *  read has drawn what it read (see track()). This is the page's own "ready", not a guess of how
 *  long it takes; the deadline only turns a page that never settles into an error.
 *  There is deliberately no `wait(ms)` any more: fixed sleeps sized for a quiet machine are what
 *  failed on a busy one (a run that took longer than 1300 ms, a read that ended after a hashchange
 *  re-render), so every test waits here instead (DEC-875, DEC-876). */
export async function settle(dom, timeoutMs = 10000) {
  const pending = PENDING.get(dom);
  const busy = () => pending.reads > 0 || pending.timers.size > 0;
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const what = `${pending.reads} file read(s) and ${pending.timers.size} timer(s) still pending`;
      reject(new Error(`the page never settled within ${timeoutMs} ms: ${what}`));
    }, timeoutMs);
  });
  try {
    // One zero-delay timer first. It fires after every zero-delay timer set before it (Node runs
    // timers of one delay in the order they were set), which covers the one hop jsdom takes on
    // Node's own clock rather than the window's: following a clicked link. That hop sets the
    // (tracked) hashchange timer, so by the time this one fires, busy() can see it.
    await Promise.race([new Promise((resolve) => pending.barrier(resolve, 0)), late]);
    while (busy()) await Promise.race([new Promise((resolve) => pending.waiters.push(resolve)), late]);
  } finally {
    clearTimeout(timer);
  }
}

/** Choose a real file in a file input, as a user would, and wait until the page has read it.
 *  Reading is asynchronous (Blob.text), and a hashchange still queued from `go()` can re-render the
 *  page before the read ends - under load it often does - so waiting for the first swap of <main>
 *  returned with the file still unread. This waits for the page to settle instead: the read done,
 *  its re-render drawn, and any queued re-render drawn too (DEC-875). The check that <main> was
 *  replaced at all is kept: a change handler that silently did nothing is still an error. */
export async function upload(dom, sel, name, content, timeoutMs = 10000) {
  const input = $(dom, sel);
  if (!input) throw new Error(`no file input: ${sel}`);
  const before = $(dom, "#app main");
  const file = new dom.window.File([content], name, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  await settle(dom, timeoutMs);
  if ($(dom, "#app main") === before) throw new Error(`the page never re-rendered after ${name}`);
}
