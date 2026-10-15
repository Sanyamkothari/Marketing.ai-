/* Plan M M182 (DEC-1612): a deployment's own brand, drawn in both layouts.

   The whole page is loaded as ui/index.html loads it, against the recorded Hillstrom demo, with `GET /auth/me`
   answering the REAL bodies the app wrote for a deployment with brand_name "Acme Growth", a logo address, the logo
   name "Acme" and the colour #0B5D3B (BRAND_FIXTURES). What must show: the name in the wordmark, the logo as an image
   with its alt text, the logo link's name, the tab's title, and the accent colour on <html>. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { CUSTOM, installShellPage, loadModules, me, untilStable, visit, withoutBrand } from "./support.mjs";
import { settle } from "../harness.mjs";

const world = { me: me("custom_classic"), approvals: null };
// Starts on Home: an unknown address now draws "Page not found", whose title is not the one tested first.
const { w, state } = installShellPage({ hash: "#/", world });
// The page's <title>, as ui/index.html has one (the harness page has none).
const title = document.createElement("title");
title.textContent = "Marketing AI · Minfy";
document.head.appendChild(title);
await loadModules();
await untilStable(state);
const session = await import("../../../../../ui/modules/production/session.js");
const brand = await import("../../../../../ui/brand.js");

async function useMe(body) {
  world.me = body;
  await session.loadMe();
  await settle(3);
  await untilStable(state);
}

const bar = () => document.getElementById("pb-bar");
const rootStyle = () => document.documentElement.style.getPropertyValue("--brand-blue");

function assertCustomBar(why) {
  const wordmark = bar().querySelector("a.wordmark");
  assert.equal(wordmark.textContent, CUSTOM.name, `${why}: the wordmark is the brand's name`);
  assert.ok(wordmark.querySelector("[data-user-text]"), `${why}: a custom name is marked as the deployment's own words`);
  const link = bar().querySelector("a.tb-logo");
  assert.equal(link.getAttribute("aria-label"), `${CUSTOM.logo_alt}, home`, `${why}: the logo link's name`);
  const img = link.querySelector("img.logo.logo-img");
  assert.ok(img, `${why}: the logo is an image`);
  assert.equal(img.getAttribute("src"), CUSTOM.logo_url);
  assert.equal(img.getAttribute("alt"), CUSTOM.logo_alt);
  assert.equal(bar().querySelector("svg.logo"), null, `${why}: the built-in Minfy logo is not drawn`);
  assert.equal(bar().textContent.includes("Minfy"), false, `${why}: nothing of the default brand is left in the bar`);
  assert.equal(bar().querySelectorAll("a.wordmark").length, 1);
}

test("the classic bar draws the brand's name, logo, alt text and colour", async () => {
  assert.equal(document.documentElement.getAttribute("data-ui-layout"), null);
  assertCustomBar("classic");
  assert.equal(rootStyle(), CUSTOM.color, "--brand-blue is the brand's colour on <html>");
  assert.ok(document.getElementById("brand-styles"), "the image's fit rule is added once, because a logo address is set");
  assert.equal(document.querySelectorAll("#brand-styles").length, 1);
  const rules = document.getElementById("brand-styles").textContent;
  assert.ok(rules.includes("html .logo .b{fill:#1E57BD}") && rules.includes("html .logo .s{stroke:#1E57BD}"), "Minfy's own mark keeps Minfy's blue");
  assert.ok(rules.includes("html .run-pct{border-color:rgba(11,93,59,0.2)}"), "the run-progress pill's border is the colour's tint");
});

test("the tab's title carries the brand's name, whoever sets it", async () => {
  assert.equal(document.title, `${CUSTOM.name} · ${CUSTOM.logo_alt}`, "the first title is rewritten");
  document.title = "Settings · Marketing AI";
  await settle(3);
  assert.equal(document.title, `Settings · ${CUSTOM.name}`);
  await visit(state, "#/settings");
  assert.match(document.title, new RegExp(` · ${CUSTOM.name}$`), "a page that sets its own title is rewritten too");
  document.title = "Nothing to swap here";
  await settle(3);
  assert.equal(document.title, "Nothing to swap here", "a title that does not end with the product's name is left alone");
});

test("the campaigns layout draws the same brand (signed in or not)", async () => {
  await useMe(me("custom_campaigns"));
  assert.equal(document.documentElement.getAttribute("data-ui-layout"), "campaigns");
  assert.ok(bar().querySelector("nav.sh-side"), "the sidebar is drawn");
  assertCustomBar("campaigns");
  await useMe(me("custom_viewer_campaigns"));
  assertCustomBar("campaigns, signed in as a Viewer");
  assert.equal(rootStyle(), CUSTOM.color);
});

test("signing out keeps the brand: the sign-in screen is the deployment's", async () => {
  await useMe(null); // GET /auth/me answers 401 with nobody signed in
  assert.equal(bar().querySelector("nav.sh-side"), null, "the signed-out shell has no sidebar");
  assertCustomBar("signed out");
  await useMe(me("custom_campaigns"));
});

test("a logo that does not load falls back to the built-in logo for the rest of the page", async () => {
  const img = bar().querySelector("img.logo-img");
  img.dispatchEvent(new w.Event("error"));
  await settle(3);
  assert.equal(bar().querySelector("img.logo-img"), null);
  const svg = bar().querySelector("a.tb-logo svg.logo");
  assert.ok(svg, "the built-in logo is drawn instead");
  assert.equal(svg.getAttribute("aria-label"), CUSTOM.logo_alt, "and it carries the brand's logo name");
  await useMe(me("custom_campaigns")); // the same answer again does not bring the broken address back
  assert.equal(bar().querySelector("img.logo-img"), null);
});

test("only the name and the logo name set: the built-in logo, named, and the default colour", async () => {
  await useMe(me("custom_name_classic"));
  assert.equal(document.documentElement.getAttribute("data-ui-layout"), null);
  const wordmark = bar().querySelector("a.wordmark");
  assert.equal(wordmark.textContent, "Acme Growth");
  assert.equal(bar().querySelector("a.tb-logo").getAttribute("aria-label"), "Acme, home");
  assert.equal(bar().querySelector("a.tb-logo svg.logo").getAttribute("aria-label"), "Acme");
  assert.equal(bar().querySelector("img.logo-img"), null);
  assert.equal(rootStyle(), "", "the default colour sets nothing on <html>");
  assert.equal(document.documentElement.hasAttribute("style"), false, "and leaves no empty style attribute");
});

test("a malformed brand is today's product, part by part", async () => {
  await useMe({
    ...me("default_classic"),
    brand: { name: "   ", logo_url: "javascript:alert(1)", logo_alt: 5, color: "red" },
  });
  const wordmark = bar().querySelector("a.wordmark");
  assert.equal(wordmark.textContent, "Marketing AI");
  assert.equal(wordmark.querySelector("[data-user-text]"), null);
  assert.equal(bar().querySelector("a.tb-logo").getAttribute("aria-label"), "Minfy, home");
  assert.ok(bar().querySelector("a.tb-logo svg.logo"));
  assert.equal(rootStyle(), "");
  assert.equal(document.documentElement.hasAttribute("style"), false);
});

test("an answer with no brand field changes nothing", async () => {
  // (the logo address failed to load earlier on this page, so the built-in logo stays; the name and colour are the test)
  const known = () => [bar().querySelector("a.wordmark").textContent, rootStyle(), bar().querySelector("a.tb-logo").getAttribute("aria-label")];
  await useMe(me("custom_classic"));
  assert.deepEqual(known(), [CUSTOM.name, CUSTOM.color, `${CUSTOM.logo_alt}, home`]);
  await useMe(withoutBrand(me("default_classic")));
  assert.deepEqual(known(), [CUSTOM.name, CUSTOM.color, `${CUSTOM.logo_alt}, home`], "an API that sends no brand leaves what was known");
});

test("the default brand puts today's product back, and forgets the remembered one", async () => {
  await useMe(me("default_classic"));
  const wordmark = bar().querySelector("a.wordmark");
  assert.equal(wordmark.innerHTML, "Marketing AI", "plain text, no mark, as it always was");
  assert.equal(bar().querySelector("a.tb-logo").getAttribute("aria-label"), "Minfy, home");
  assert.equal(rootStyle(), "");
  assert.equal(document.documentElement.hasAttribute("style"), false);
  assert.equal(brand.isDefaultBrand(), true);
  assert.equal(document.getElementById("brand-styles"), null, "no style of ours is left on the default page");
  assert.equal(w.localStorage.getItem(brand.STORAGE_KEY), null, "nothing is remembered for the default");
});
