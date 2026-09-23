// The feedback button on every screen (Plan E M64).
//
// One fixed button, outside `#app` so no repaint removes it. It sends the page's route - never a
// value from the page - with a category and the person's words to `POST /pilot/feedback`, which
// stores it with the platform's data, masks any phone number or e-mail address typed into it and
// records the write in the audit trail. The confirmation says when something was masked.

import { errorBox, esc } from "../../dom.js";
import { postFeedback } from "./api.js";

const CATEGORIES = [
  ["confusing", "Something is confusing"],
  ["wrong", "Something looks wrong"],
  ["idea", "An idea"],
  ["praise", "Something works well"],
  ["other", "Other"],
];

function route() {
  const hash = window.location.hash || "#/";
  return hash.split("?")[0].replace(/[^#/A-Za-z0-9_\-.:~]/g, "");
}

export function mountFeedback() {
  if (typeof document === "undefined" || document.getElementById("pe-fb-btn")) return;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.id = "pe-fb-btn";
  trigger.className = "pe-fb-btn";
  trigger.textContent = "Feedback";
  trigger.setAttribute("aria-haspopup", "dialog");

  const panel = document.createElement("form");
  panel.id = "pe-fb";
  panel.className = "pe-fb";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Feedback on this screen");
  panel.innerHTML = `
    <b>Feedback on this screen</b>
    <label class="pe-note" for="pe-fb-cat">What kind of feedback?</label>
    <select id="pe-fb-cat" name="category">${CATEGORIES.map(
      ([value, label]) => `<option value="${value}">${esc(label)}</option>`,
    ).join("")}</select>
    <label class="pe-note" for="pe-fb-text">Tell us more (please do not include customer details)</label>
    <textarea id="pe-fb-text" name="text" maxlength="1000"></textarea>
    <div class="pe-row"><span class="pe-note" data-pe-fb-status></span>
      <button type="button" class="pe-btn" data-pe-fb-cancel>Cancel</button>
      <button type="submit" class="pe-btn primary">Send</button></div>`;

  const status = () => panel.querySelector("[data-pe-fb-status]");
  trigger.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) panel.querySelector("select").focus();
  });
  panel.querySelector("[data-pe-fb-cancel]").addEventListener("click", () => {
    panel.hidden = true;
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
    } catch (error) {
      status().innerHTML = errorBox(error);
    }
  });
  document.body.appendChild(panel);
  document.body.appendChild(trigger);
}
