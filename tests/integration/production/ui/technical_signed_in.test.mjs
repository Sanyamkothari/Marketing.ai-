/* Plan M M180 (DEC-1610): "Show technical details" for a SIGNED-IN person, over REAL `GET /auth/me` answers
   ($PB_FIXTURES, which now carry `preferences`): the switch sits in the profile menu (ui/chrome.js, M180 marks), its
   value comes from the server, a change is saved with `PUT /auth/me/preferences` (never in this browser's storage),
   and a refusal puts the switch back. The choice never changes what the bar offers. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, signedInServer, until } from "./harness.mjs";

const saved = [];
let refuse = false;
const viewer = fixture("me_viewer");
const people = {
  "tok-viewer": { ...viewer, preferences: { show_technical: null } },
  "tok-analyst": { ...fixture("me_analyst"), preferences: { show_technical: false } },
};
const { w } = installPage(
  signedInServer({
    tokens: people,
    routes: {
      "PUT /auth/me/preferences": (request, me) => {
        if (refuse) return { status: 500, body: { detail: { code: "INTERNAL", message: "boom", path: null } } };
        saved.push({ user: me.principal.user_id, body: request.body });
        me.preferences = { show_technical: request.body.show_technical };
        return { status: 200, body: me.preferences };
      },
    },
  }),
  { hash: "#/" },
);

const session = await import("../../../../ui/modules/production/session.js");
const chrome = await import("../../../../ui/chrome.js");
const { mountUserBar } = await import("../../../../ui/modules/production/userbar.js");
const technical = await import("../../../../ui/technical.js");
session.installFetch(w);
mountUserBar(w.document);
chrome.mountChrome(w.document);

const signIn = async (token) => {
  session.storeToken(token, new Date(Date.now() + 600_000).toISOString());
  await session.loadMe();
  chrome.refreshChrome();
  await settle(3);
};
const menuSwitch = () => $("#pb-bar #tn-user input[data-tech-switch]");
const hidden = () => w.document.documentElement.classList.contains(technical.HIDE_CLASS);

test("the profile menu has a labelled switch; a person who has not chosen gets the classic default (on)", async () => {
  assert.deepEqual(viewer.preferences, { show_technical: null }, "the real `GET /auth/me` carries the choice, not made yet");
  await signIn("tok-viewer");
  await until(() => menuSwitch(), 2000, "the switch in the profile menu");
  const input = menuSwitch();
  assert.equal(input.type, "checkbox");
  assert.equal(input.closest("label").textContent.trim(), "Show technical details");
  assert.equal(input.checked, true);
  assert.equal(hidden(), false);
  // It sits above "Sign out", and the menu still offers what it did.
  const pop = $("#pb-bar #tn-user");
  const order = [...pop.children].map((el) => (el.querySelector("[data-tech-switch]") ? "switch" : el.id || el.className));
  assert.ok(order.indexOf("switch") < order.indexOf("pb-signout"), order.join(","));
  assert.ok(pop.querySelector('a[href="#/account"]'));
});

const key = (target, name) => {
  const event = new w.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
};

test("the switch is a menu entry: the arrow keys reach it, as they reach the menu's links and buttons", async () => {
  const button = $('#pb-bar [data-menu="tn-user"]');
  button.focus();
  assert.equal(key(button, "ArrowDown").defaultPrevented, true, "Down on the profile button opens the menu");
  assert.equal($("#tn-user").hidden, false);
  const visited = [];
  for (let i = 0; i < 8; i += 1) {
    visited.push(w.document.activeElement);
    key(w.document.activeElement, "ArrowDown");
  }
  assert.ok(visited.includes(menuSwitch()), "ArrowDown through the profile menu lands on the switch");
  // From the switch, Down and Up move on to its neighbours and back (never stuck on it).
  menuSwitch().focus();
  key(menuSwitch(), "ArrowDown");
  assert.notEqual(w.document.activeElement, menuSwitch());
  key(w.document.activeElement, "ArrowUp");
  assert.equal(w.document.activeElement, menuSwitch(), "Up comes back to the switch");
  key(w.document.activeElement, "Escape");
  assert.equal($("#tn-user").hidden, true);
});

test("turning it off is saved on the server for this person, not in the browser, and hides technical details", async () => {
  const before = $$("#pb-bar a").map((a) => a.getAttribute("href"));
  try {
    w.localStorage.removeItem(technical.STORAGE_KEY);
  } catch {
    // no storage in this window: nothing to clear
  }
  const input = menuSwitch();
  input.click(); // a real click on the checkbox: the change event reaches technical.js
  await until(() => saved.length === 1, 2000, "the PUT");
  assert.deepEqual(saved[0], { user: viewer.principal.user_id, body: { show_technical: false } });
  assert.equal(technical.showTechnical(), false);
  assert.equal(hidden(), true);
  assert.equal(w.localStorage.getItem(technical.STORAGE_KEY), null, "a signed-in choice is the server's, not this browser's");
  // The switch hides; it never changes what the bar offers (roles decide that).
  chrome.refreshChrome();
  await settle(2);
  assert.deepEqual($$("#pb-bar a").map((a) => a.getAttribute("href")), before);
});

test("the next sign-in reads the saved choice, and another person has their own", async () => {
  await session.loadMe(); // a reload: the server's answer now says false
  await settle(2);
  assert.equal(technical.showTechnical(), false);
  assert.equal(menuSwitch().checked, false);
  await signIn("tok-analyst"); // saved false on the server
  assert.equal(technical.showTechnical(), false);
  people["tok-analyst"].preferences = { show_technical: true };
  await signIn("tok-analyst");
  assert.equal(technical.showTechnical(), true);
  assert.equal(hidden(), false);
});

test("a refused save puts the switch back", async () => {
  refuse = true;
  try {
    const result = await technical.setShowTechnical(false);
    assert.equal(result, true, "the server's value stands");
    assert.equal(technical.showTechnical(), true);
    assert.equal(menuSwitch().checked, true);
    assert.equal(hidden(), false);
  } finally {
    refuse = false;
  }
});

test("signed out, the browser's choice applies again", async () => {
  session.forgetSignIn();
  await settle(2);
  w.localStorage.setItem(technical.STORAGE_KEY, "off");
  technical._resetTechnicalForTests();
  assert.equal(technical.showTechnical(), false);
  w.localStorage.removeItem(technical.STORAGE_KEY);
  technical._resetTechnicalForTests();
  assert.equal(technical.showTechnical(), true);
});
