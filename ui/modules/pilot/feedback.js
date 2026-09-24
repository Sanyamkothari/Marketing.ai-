// "Send feedback" (Plan E M64; v1: from the Help menu, no floating button).
//
// The Help menu's "Send feedback" entry (`#pe-fb-btn`, drawn by `index.js` into the top bar's help
// slot) opens one dialog, anchored under the bar (a bottom sheet on a phone). It sends the page's
// route - never a value from the page - with a category and the person's words to
// `POST /pilot/feedback`, which stores it with the platform's data, masks any phone number or e-mail
// address typed into it and records the write in the audit trail. The confirmation says when
// something was masked. Escape or Cancel closes it and puts focus back where it was.

import { errorBox, esc } from "../../dom.js";
import { postFeedback } from "./api.js";

const CATEGORIES = [
  ["confusing", "Something is confusing"],
  ["wrong", "Something looks wrong"],
  ["idea", "An idea"],
  ["praise", "Something works well"],
  ["other", "Other"],
];

const CLOSE_AFTER_MS = 2500;

let panel = null;
let returnTo = null;
let closer = null;

function route() {
  const hash = window.location.hash || "#/";
  return hash.split("?")[0].replace(/[^#/A-Za-z0-9_\-.:~]/g, "");
}

/** The Help menu's entry (the top bar redraws it, so it is found by id each time). */
export const feedbackMenuItem = () =>
  `<button type="button" id="pe-fb-btn" aria-haspopup="dialog" aria-controls="pe-fb">Send feedback</button>`;

function status() {
  return panel.querySelector("[data-pe-fb-status]");
}

function place() {
  const bar = document.getElementById("pb-bar");
  const help = bar ? bar.querySelector("[data-menu='tn-help']") : null;
  const barBox = bar ? bar.getBoundingClientRect() : null;
  const helpBox = help ? help.getBoundingClientRect() : null;
  panel.style.top = barBox && barBox.bottom > 0 ? `${Math.round(barBox.bottom + 8)}px` : "";
  panel.style.right =
    helpBox && helpBox.width > 0 ? `${Math.max(16, Math.round(window.innerWidth - helpBox.right))}px` : "";
}

export function closeFeedback({ focus = true } = {}) {
  if (!panel || panel.hidden) return;
  clearTimeout(closer);
  panel.hidden = true;
  const target = returnTo && returnTo.isConnected ? returnTo : null;
  returnTo = null;
  if (focus && target) target.focus();
}

export function openFeedback() {
  mountFeedback();
  clearTimeout(closer);
  const bar = document.getElementById("pb-bar");
  const help = bar ? bar.querySelector("[data-menu='tn-help']") : null;
  const active = document.activeElement;
  returnTo = active && active.id !== "pe-fb-btn" && active !== document.body ? active : help;
  // Close the Help menu the way the top bar does it, so the menu and its focus stay consistent.
  const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true });
  escape.peInternal = true;
  document.dispatchEvent(escape);
  status().textContent = "";
  panel.hidden = false;
  place();
  panel.querySelector("select").focus();
}

export function mountFeedback() {
  if (typeof document === "undefined" || panel) return;
  panel = document.createElement("form");
  panel.id = "pe-fb";
  panel.className = "pe-fb dialog";
  panel.hidden = true;
  panel.noValidate = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "pe-fb-title");
  panel.innerHTML = `
    <h2 id="pe-fb-title">Feedback on this screen</h2>
    <label for="pe-fb-cat">What kind of feedback?</label>
    <div class="control sel"><select id="pe-fb-cat" name="category">${CATEGORIES.map(
      ([value, label]) => `<option value="${value}">${esc(label)}</option>`,
    ).join("")}</select></div>
    <label for="pe-fb-text">Tell us more</label>
    <textarea id="pe-fb-text" class="pe-textarea" name="text" maxlength="1000" aria-describedby="pe-fb-hint"></textarea>
    <p class="pe-hint" id="pe-fb-hint">Please don't include customer details.</p>
    <div class="pe-row"><span class="pe-status" data-pe-fb-status role="status"></span>
      <button type="button" class="btn quiet" data-pe-fb-cancel>Cancel</button>
      <button type="submit" class="btn primary">Send</button></div>`;

  panel.querySelector("[data-pe-fb-cancel]").addEventListener("click", () => closeFeedback());
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.peInternal) {
      event.stopPropagation();
      closeFeedback();
    }
  });
  panel.addEventListener("submit", async (event) => {
    event.preventDefault();
    const category = panel.querySelector("select").value;
    const text = panel.querySelector("textarea").value;
    status().textContent = "Sending…";
    try {
      const saved = await postFeedback({ screen: route(), category, text });
      const masked = saved && saved.redacted && saved.redacted.length ? " Contact details in your text were masked." : "";
      status().textContent = `Thank you, it was recorded.${masked}`;
      panel.querySelector("textarea").value = "";
      closer = setTimeout(() => closeFeedback(), CLOSE_AFTER_MS);
    } catch (error) {
      status().innerHTML = errorBox(error);
    }
  });
  // Escape anywhere else (focus left the dialog) closes it too; a click outside leaves it open, so
  // a half-written note is never lost by accident.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.peInternal && panel && !panel.hidden) closeFeedback();
  });
  document.addEventListener("click", (event) => {
    const trigger = event.target && event.target.closest && event.target.closest("#pe-fb-btn");
    if (trigger) {
      event.preventDefault();
      openFeedback();
    }
  });
  window.addEventListener("resize", () => {
    if (panel && !panel.hidden) place();
  });
  document.body.appendChild(panel);
}
