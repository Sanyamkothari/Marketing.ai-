/* Plan M M180 (DEC-1610): "Show technical details" with sign-in off (the app's real `GET /auth/me`, $SP_FIXTURES):
   the switch on the Settings page, the class on <html> and the one CSS rule that hides every technical mark, the
   choice kept in this browser (localStorage, or memory when storage is refused), the change event, the layout's
   default (on in the classic layout, off in the campaigns layout until a person chooses), and the marks that
   ui/dom.js techDetails() and ui/technical.js put on "Technical details" sections. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const world = { runs: fixture("runs_empty"), waiting: null };
const { w } = await installWholePage({ hash: "#/settings", world });
const technical = await import("../../../../../ui/technical.js");
const { techDetails } = await import("../../../../../ui/dom.js");

const root = () => w.document.documentElement;
const hidden = () => root().classList.contains(technical.HIDE_CLASS);
const shown = (el) => w.getComputedStyle(el).display !== "none";

/** A technical section of each kind, and one plain sentence, on the page. */
function plant() {
  const holder = w.document.createElement("div");
  holder.setAttribute("data-planted", "");
  holder.innerHTML = [
    techDetails([["Run", "r_1"]]),
    '<details class="tech"><summary>Details</summary><p>code</p></details>',
    '<a href="#/uc/x/model/r_1" data-technical>Model</a>',
    "<details><summary>Technical details</summary><p>by hand</p></details>",
    "<p data-plain>Customers to contact</p>",
  ].join("");
  $("#app").appendChild(holder);
  return holder;
}

test("the real `GET /auth/me` with sign-in off carries no stored choice", () => {
  const me = fixture("me_off");
  assert.equal(me.auth_mode, "off");
  assert.equal(me.preferences, null, "nobody to remember it for: the browser keeps it");
});

test("Settings has one labelled checkbox, keyboard reachable, on by default in the classic layout", async () => {
  await until(() => $("[data-settings-view] input[data-tech-switch]"), 3000, "the switch on Settings");
  const input = $("[data-settings-view] input[data-tech-switch]");
  assert.equal(input.type, "checkbox");
  assert.equal(input.getAttribute("role"), "switch");
  assert.equal(input.closest("label").querySelector(".ts-label").textContent, "Show technical details");
  assert.match(input.closest("label").textContent, /not what you are allowed to do/);
  assert.ok(input.tabIndex >= 0 && !input.disabled, "Tab reaches it; Space changes it");
  assert.equal(input.checked, true);
  assert.equal(technical.showTechnical(), true);
  assert.equal(hidden(), false);
  assert.equal(root().hasAttribute("class"), false, "the classic page's <html> is untouched");
  assert.equal($$("main .btn.primary").length <= 1, true, "still at most one primary button");
});

test("turning it off hides every technical mark through one CSS rule; on shows them all again", async () => {
  const holder = plant();
  await settle(2);
  const events = [];
  const stop = technical.onTechnicalChange((on) => events.push(on));
  try {
    const marks = [...holder.querySelectorAll(".tech, [data-technical], details")];
    assert.equal(marks.length, 4);
    assert.ok(holder.querySelector("details:not(.tech)").hasAttribute("data-technical-details"), "marked as it arrived");
    assert.ok(marks.every(shown), "all on screen while the switch is on");
    $("[data-settings-view] input[data-tech-switch]").click();
    await settle(2);
    assert.equal(hidden(), true);
    assert.deepEqual(events, [false]);
    assert.ok(marks.every((el) => !shown(el)), "each kind of mark is hidden by the style sheet");
    assert.ok(shown(holder.querySelector("[data-plain]")), "plain content stays");
    assert.equal(w.localStorage.getItem(technical.STORAGE_KEY), "off", "sign-in off: this browser remembers");
    $("[data-settings-view] input[data-tech-switch]").click();
    await settle(2);
    assert.equal(hidden(), false);
    assert.deepEqual(events, [false, true]);
    assert.ok(marks.every(shown));
    assert.equal(w.localStorage.getItem(technical.STORAGE_KEY), "on");
    assert.equal(root().hasAttribute("class"), false, "back to the page as it was");
  } finally {
    stop();
    holder.remove();
  }
});

test("every switch on the page follows the value, and a redrawn Settings page shows it", async () => {
  await technical.setShowTechnical(false);
  assert.equal($("[data-settings-view] input[data-tech-switch]").checked, false);
  w.location.hash = "#/results";
  await settle(4);
  w.location.hash = "#/settings";
  await until(() => $("[data-settings-view] input[data-tech-switch]"), 3000, "Settings again");
  assert.equal($("[data-settings-view] input[data-tech-switch]").checked, false);
  await technical.setShowTechnical(true);
  assert.equal($("[data-settings-view] input[data-tech-switch]").checked, true);
});

test("when the browser refuses storage the choice lives in memory, and nothing throws", async () => {
  const real = Object.getOwnPropertyDescriptor(w, "localStorage");
  Object.defineProperty(w, "localStorage", {
    configurable: true,
    get() {
      throw new w.DOMException("denied", "SecurityError");
    },
  });
  try {
    technical._resetTechnicalForTests();
    assert.equal(await technical.setShowTechnical(false), false);
    assert.equal(technical.showTechnical(), false);
    assert.equal(hidden(), true);
    technical._resetTechnicalForTests(); // what the page knows survives without storage
    assert.equal(technical.showTechnical(), false);
    assert.equal(await technical.setShowTechnical(true), true);
  } finally {
    Object.defineProperty(w, "localStorage", real);
    technical._resetTechnicalForTests();
  }
});

test("the default is off in the campaigns layout until the person chooses (DEC-1610 (c))", async () => {
  w.localStorage.removeItem(technical.STORAGE_KEY);
  technical._resetTechnicalForTests();
  const events = [];
  const stop = technical.onTechnicalChange((on) => events.push(on));
  try {
    assert.equal(technical.showTechnical(), true, "classic: on");
    // What M174's ui/layout.js does when the deployment says "campaigns".
    root().setAttribute("data-ui-layout", "campaigns");
    w.dispatchEvent(new w.CustomEvent("marketing-ai:layout-changed", { detail: "campaigns" }));
    assert.equal(technical.showTechnical(), false);
    assert.equal(hidden(), true);
    assert.deepEqual(events, [false]);
    await technical.setShowTechnical(true); // a person's choice wins over the default
    assert.equal(technical.showTechnical(), true);
    assert.equal(hidden(), false);
  } finally {
    stop();
    root().removeAttribute("data-ui-layout");
    w.localStorage.removeItem(technical.STORAGE_KEY);
    technical._resetTechnicalForTests();
  }
  assert.equal(technical.showTechnical(), true);
});
