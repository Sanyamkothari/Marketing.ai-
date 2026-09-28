/* docs/UI_AUDIT.md §8.4 item 2, the reload half: a run opened from Results keeps Results marked in the
   top bar (ui/chrome.js `placeFor`) after the page is reloaded on the run, because where it was
   opened from is kept in this tab's sessionStorage. Its own process, so the bar is mounted fresh on a
   page whose storage already holds the mark, as after a reload. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, installPage, settle } from "./harness.mjs";

const { w } = installPage(async () => null, { hash: "#/uc/telco-churn/output/r7" });
w.sessionStorage.setItem("marketing-ai:run-opened-from", JSON.stringify(["telco-churn/r7"]));

const chrome = await import("../../../../ui/chrome.js");
chrome.mountChrome(w.document);

const on = () => (($("#pb-bar .tn-item.on") || {}).textContent || "").trim().replace(/\s*\d+$/, "");

test("reloaded on a run opened from Results, the bar still marks Results", async () => {
  await settle(2);
  assert.equal(on(), "Results");
});

test("a run nobody opened from Results is Home, as its route says", () => {
  assert.equal(chrome.placeFor("#/uc/telco-churn/output/r8"), "home");
  assert.equal(chrome.placeFor("#/uc/other-use-case/run/r7"), "home", "the mark is per use case and run");
});
