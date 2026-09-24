// "What does this mean?" beside every warning and every setting (Plan E M63; v1 toggletip).
//
// The words come from `GET /pilot/help` - the same catalogue the data readiness report prints - so a
// warning reads the same on the screen and in the PDF. Nothing here is written by hand per code.
//
// Other workstreams own the screens that show warnings (the validation list, the onboarding checks,
// the uplift checks) and settings (`settings.js`), and this module may not edit them. So it watches
// the page instead: after each paint, every warning the catalogue explains - a pill whose text is the
// code, or any element carrying the code in `data-code` (screens now show the plain title and keep the
// code there) - and every advanced-setting control (`[data-path]`) gets one small "?" button, kept on
// the same line as the last word of its label (inside the `.check` label for a checkbox). The button
// opens one shared popover below the label: a title line, the meaning, what to do, and a close button;
// on a phone it is a bottom sheet. A code or setting the catalogue does not carry gets no button
// rather than an invented sentence.

import { esc } from "../../dom.js";

const DONE = "peHelp";
const CODE_DONE = "peHelpCode";
const CODE_DONE_ATTR = "data-pe-help-code";
let catalogue = null;
let pop = null;
let openedBy = null;
let observer = null;

function settingKey(path) {
  return String(path).replace(/\[\d+\]/g, "[*]");
}

const narrow = () => typeof window.matchMedia === "function" && window.matchMedia("(max-width: 700px)").matches;

const clean = (text) => String(text || "").replace(/\s+/g, " ").replace(/[?]$/, "").trim();

function button(label, kind, key, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pe-q";
  b.innerHTML = `<span aria-hidden="true">?</span>`;
  b.title = "What does this mean?";
  b.setAttribute("aria-label", `What does this mean? ${label}`);
  b.setAttribute("aria-expanded", "false");
  b.setAttribute("aria-controls", "pe-pop");
  b.dataset.peKind = kind;
  b.dataset.peKey = key;
  b.dataset.peTitle = title || label;
  return b;
}

/** The last text node under `el` that has a word in it (skipping any "?" already there). */
function lastWordNode(el) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      /\S/.test(node.nodeValue) && !(node.parentElement && node.parentElement.closest(".pe-q"))
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT,
  });
  let last = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) last = node;
  return last;
}

/** Put `btn` at the end of `el`, in one unbreakable span with the label's last word. */
function attach(el, btn) {
  const node = lastWordNode(el);
  if (!node) {
    el.appendChild(btn);
    return;
  }
  const text = node.nodeValue;
  // In a flex label (`.check`) every text run is its own item, spaced by the gap: keep the whole
  // label text with the "?" there, so no gap opens inside the label.
  const parent = node.parentElement;
  const flex = parent && /flex/.test(window.getComputedStyle(parent).display);
  const match = flex ? /(\S(?:.*\S)?)(\s*)$/s.exec(text) : /(\S+)(\s*)$/.exec(text);
  const wrap = document.createElement("span");
  wrap.className = "pe-qw";
  wrap.textContent = match[1];
  wrap.appendChild(btn);
  node.nodeValue = text.slice(0, match.index);
  node.parentNode.insertBefore(wrap, node.nextSibling);
}

function close({ focus = false } = {}) {
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  if (openedBy) {
    openedBy.setAttribute("aria-expanded", "false");
    if (focus && openedBy.isConnected) openedBy.focus();
  }
  openedBy = null;
}

function ensurePop() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "pe-pop";
  pop.id = "pe-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-labelledby", "pe-pop-title");
  pop.hidden = true;
  document.body.appendChild(pop);
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const trigger = target.closest(".pe-q");
    if (trigger) {
      // A "?" inside a checkbox's label must not tick the box.
      event.preventDefault();
      event.stopPropagation();
      if (openedBy === trigger && !pop.hidden) close();
      else open(trigger);
      return;
    }
    if (target.closest("[data-pe-pop-close]")) {
      close({ focus: true });
      return;
    }
    if (!pop.hidden && !pop.contains(target)) close();
  });
  window.addEventListener("hashchange", () => close());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && pop && !pop.hidden && !event.peInternal) close({ focus: true });
  });
  return pop;
}

function place(box, trigger) {
  if (narrow()) {
    box.classList.add("sheet");
    box.style.left = "";
    box.style.top = "";
    return;
  }
  box.classList.remove("sheet");
  // Document coordinates: the popover stays under its label when the page scrolls.
  const anchor = (trigger.closest(".pe-qw") || trigger).getBoundingClientRect();
  const width = Math.min(360, window.innerWidth - 32);
  const scrollX = window.scrollX || window.pageXOffset || 0;
  const scrollY = window.scrollY || window.pageYOffset || 0;
  const left = Math.max(16, Math.min(anchor.left, window.innerWidth - width - 16));
  box.style.left = `${left + scrollX}px`;
  box.style.top = `${anchor.bottom + scrollY + 8}px`;
}

function open(trigger) {
  const kind = trigger.dataset.peKind;
  const key = trigger.dataset.peKey;
  let title = trigger.dataset.peTitle || "";
  let body = "";
  if (kind === "code") {
    const entry = catalogue.codes[key];
    if (!entry) return;
    title = entry.title;
    body = `<p>${esc(entry.meaning)}</p><p class="pe-fix"><b>What to do</b>${esc(entry.fix)}</p>`;
  } else {
    const entry = catalogue.settings[key];
    if (!entry) return;
    body = `<p>${esc(entry.meaning)}</p>`;
  }
  const box = ensurePop();
  if (openedBy && openedBy !== trigger) openedBy.setAttribute("aria-expanded", "false");
  box.innerHTML = `<div class="pe-pop-head"><p class="pe-pop-title" id="pe-pop-title">${esc(
    title,
  )}</p><button type="button" class="pe-x" data-pe-pop-close aria-label="Close">×</button></div>${body}`;
  box.hidden = false;
  openedBy = trigger;
  trigger.setAttribute("aria-expanded", "true");
  place(box, trigger);
}

/** Where a warning's "?" goes: after a pill; inside the heading of a larger block. */
function codeAnchor(el) {
  if (el.classList.contains("pill")) return { el, after: true };
  const pill = el.querySelector(".pill");
  if (pill) return { el: pill, after: true };
  const heading = el.querySelector("b, strong, h3, h4, h5, .vhead");
  return { el: heading || el, after: false };
}

/** Decorate one subtree. Exported for the tests, which run it on fixture markup. */
export function decorate(root) {
  if (!catalogue || !root || !root.querySelectorAll) return 0;
  let added = 0;
  root.querySelectorAll(".pill, [data-code]").forEach((el) => {
    if (el.dataset[CODE_DONE] || el.closest(".apierr, .pe-pop, .tech")) return;
    if (el.parentElement && el.parentElement.closest(`[${CODE_DONE_ATTR}]`)) return;
    const code = el.dataset.code || (el.textContent || "").trim();
    if (!catalogue.codes[code]) return;
    el.dataset[CODE_DONE] = "1";
    const title = catalogue.codes[code].title;
    const btn = button(title, "code", code, title);
    const anchor = codeAnchor(el);
    if (anchor.after) anchor.el.insertAdjacentElement("afterend", btn);
    else attach(anchor.el, btn);
    added += 1;
  });
  root.querySelectorAll("[data-path]").forEach((control) => {
    const key = settingKey(control.dataset.path);
    if (!catalogue.settings[key]) return;
    const field = control.closest(".field") || control.parentElement;
    if (!field || field.dataset[DONE]) return;
    field.dataset[DONE] = "1";
    const check = control.type === "checkbox" ? control.closest(".check") : null;
    const label = check || field.querySelector(".sub") || field.querySelector("label") || field;
    const group = control.closest(".algos");
    const head = group ? group.querySelector(".sub") : null;
    const name = clean((head || label).textContent).split(" · ")[0] || key;
    attach(label, button(name, "setting", key, name));
    added += 1;
  });
  return added;
}

export function installHelp(helpCatalogue) {
  catalogue = helpCatalogue;
  if (!catalogue || typeof document === "undefined") return;
  ensurePop();
  const app = document.getElementById("app") || document.body;
  decorate(app);
  if (observer) observer.disconnect();
  observer = new MutationObserver(() => decorate(app));
  observer.observe(app, { childList: true, subtree: true });
}
