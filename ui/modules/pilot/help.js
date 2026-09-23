// "What does this mean?" beside every warning and every setting (Plan E M63).
//
// The words come from `GET /pilot/help` - the same catalogue the data readiness report prints - so a
// warning reads the same on the screen and in the PDF. Nothing here is written by hand per code.
//
// Other workstreams own the screens that show warnings (the validation list, the onboarding checks,
// the uplift checks) and settings (`settings.js`), and this module may not edit them. So it watches
// the page instead: after each paint, every pill whose text is a code the catalogue explains, and
// every advanced-setting control (`[data-path]`), gets one small "?" button. The button opens one
// shared popover. A code or setting the catalogue does not carry gets no button rather than an
// invented sentence.

import { esc } from "../../dom.js";

const DONE = "peHelp";
let catalogue = null;
let pop = null;
let observer = null;

function settingKey(path) {
  return String(path).replace(/\[\d+\]/g, "[*]");
}

function button(label, kind, key) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pe-q";
  b.textContent = "?";
  b.title = "What does this mean?";
  b.setAttribute("aria-label", `What does this mean? ${label}`);
  b.dataset.peKind = kind;
  b.dataset.peKey = key;
  return b;
}

function ensurePop() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "pe-pop";
  pop.id = "pe-pop";
  pop.setAttribute("role", "dialog");
  pop.hidden = true;
  document.body.appendChild(pop);
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest && event.target.closest(".pe-q");
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      open(trigger);
      return;
    }
    if (!pop.hidden && !pop.contains(event.target)) pop.hidden = true;
  });
  window.addEventListener("hashchange", () => {
    pop.hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && pop && !pop.hidden) pop.hidden = true;
  });
  return pop;
}

function open(trigger) {
  const kind = trigger.dataset.peKind;
  const key = trigger.dataset.peKey;
  let html = "";
  if (kind === "code") {
    const entry = catalogue.codes[key];
    if (!entry) return;
    html = `<b>${esc(entry.title)}</b><p>${esc(entry.meaning)}</p><p class="pe-fix"><b>What to do</b>${esc(entry.fix)}</p>`;
  } else {
    const entry = catalogue.settings[key];
    if (!entry) return;
    html = `<p>${esc(entry.meaning)}</p>`;
  }
  const box = ensurePop();
  box.innerHTML = html;
  box.hidden = false;
  const rect = trigger.getBoundingClientRect();
  const width = Math.min(360, window.innerWidth - 32);
  const left = Math.max(16, Math.min(rect.left, window.innerWidth - width - 16));
  box.style.left = `${left}px`;
  box.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 40)}px`;
}

/** Decorate one subtree. Exported for the tests, which run it on fixture markup. */
export function decorate(root) {
  if (!catalogue || !root || !root.querySelectorAll) return 0;
  let added = 0;
  root.querySelectorAll(".pill").forEach((pill) => {
    if (pill.dataset[DONE]) return;
    const code = (pill.textContent || "").trim();
    if (!catalogue.codes[code]) return;
    pill.dataset[DONE] = "1";
    pill.insertAdjacentElement("afterend", button(catalogue.codes[code].title, "code", code));
    added += 1;
  });
  root.querySelectorAll("[data-path]").forEach((control) => {
    const key = settingKey(control.dataset.path);
    if (!catalogue.settings[key]) return;
    const field = control.closest(".field") || control.parentElement;
    if (!field || field.dataset[DONE]) return;
    field.dataset[DONE] = "1";
    const label = field.querySelector(".sub") || field.querySelector("label") || field;
    label.appendChild(button(key, "setting", key));
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
