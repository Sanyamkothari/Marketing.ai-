/* Plan M M182 (DEC-1612): the last custom brand is remembered in this browser, so a page load draws it at once.

   Before `GET /auth/me` answers (and when it cannot, as on the sign-in screen with sign-in on, which answers 401), the
   page would otherwise show the default name and colour for a moment on every load. localStorage is a
   convenience here, never a requirement: a refused or garbled store is today's product. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { CUSTOM, installShellPage, loadModules, me, untilStable } from "./support.mjs";

const KEY = "marketing-ai.brand";
const world = { me: null, approvals: null }; // nobody is signed in: GET /auth/me answers 401, with no brand in it
const { w, state } = installShellPage({ hash: "#/__start", world });
w.localStorage.setItem(KEY, JSON.stringify(me("custom_classic").brand));
await loadModules();
await untilStable(state);

test("a remembered brand is drawn although the answer carries none (signed out)", () => {
  const bar = document.getElementById("pb-bar");
  assert.equal(bar.querySelector("a.wordmark").textContent, CUSTOM.name);
  assert.equal(bar.querySelector("a.tb-logo img.logo-img").getAttribute("alt"), CUSTOM.logo_alt);
  assert.equal(document.documentElement.style.getPropertyValue("--brand-blue"), CUSTOM.color);
});

test("what is remembered goes through the same checks as an answer", async () => {
  const fresh = (query) => import(`../../../../../ui/brand.js?${query}`);
  w.localStorage.setItem(KEY, "{not json");
  assert.equal((await fresh("garbled")).isDefaultBrand(), true, "garbage in the store is today's product");
  w.localStorage.setItem(KEY, JSON.stringify({ name: "x".repeat(200), logo_url: "http://insecure.example/a.svg", color: "#12" }));
  const partly = await fresh("partly");
  assert.equal(partly.isDefaultBrand(), true, "every part that fails the checks is the default");
  w.localStorage.setItem(KEY, JSON.stringify({ name: "Kept", color: "#0b5d3b" }));
  const kept = (await fresh("kept")).currentBrand();
  assert.equal(kept.name, "Kept");
  assert.equal(kept.color, "#0B5D3B", "a colour is kept in capitals");
  assert.equal(kept.logo_alt, "Minfy");
});

test("a refused store is no store: the brand still comes from the answer", async () => {
  Object.defineProperty(w, "localStorage", {
    configurable: true,
    get() {
      throw new w.DOMException("blocked", "SecurityError");
    },
  });
  const fresh = await import("../../../../../ui/brand.js?blocked");
  assert.equal(fresh.isDefaultBrand(), true);
  fresh.setBrand(me("custom_classic").brand, { fromServer: true }); // must not throw
  assert.equal(fresh.currentBrand().name, CUSTOM.name);
});
