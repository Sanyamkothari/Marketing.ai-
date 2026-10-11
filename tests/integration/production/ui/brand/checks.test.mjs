/* Plan M M182 (DEC-1612): a custom brand adds no accessibility defect and no engine word to any page.

   The pages are the visible-text check's own (a11y/pages.mjs reads its PAGES list; a subset is drawn here). Each is drawn
   in both layouts, with the default brand and with a custom one, and the markup is looked at twice:
     - the accessibility rules (a11y/checks.mjs): what the page breaks with the custom brand must be exactly what it
       breaks with the default (the brand's logo is an image with alt text inside a link with a name, and so on);
     - the visible-text check (glossary/check.mjs): no never-shown word on the page with either brand.

   The brand's name is the deployment's own wording, like a file name the person supplied, not ours: it is drawn inside
   `data-user-text` (ui/labels.js userText), so a deployment named "Uplift Model Studio" does not fail the check that
   guards OUR copy. A planted name proves that is what lets it pass. (The mark is on a custom name only; the default
   name is plain text, byte for byte as before.) */
import { test } from "node:test";
import assert from "node:assert/strict";
import { installShellPage, loadModules, me, untilStable, visit } from "./support.mjs";
import { check, tally } from "../a11y/checks.mjs";
import { loadPages } from "../a11y/pages.mjs";
import { violations } from "../glossary/check.mjs";
import { visibleText } from "../glossary/collect.mjs";
import { findNeverShown } from "../../../../../ui/labels.js";
import { settle } from "../harness.mjs";

const WANTED = ["home", "results", "settings", "overview", "campaigns-list", "data-sources", "reports-value-proof", "approvals"];
const PAGES = loadPages().filter((p) => WANTED.includes(p.id) && !p.steps);
assert.equal(PAGES.length, WANTED.length, "the pages this test draws are still in the visible-text check");

const world = { me: me("default_classic"), approvals: null };
const { state } = installShellPage({ hash: "#/__start", world });
await loadModules();
await untilStable(state);
const session = await import("../../../../../ui/modules/production/session.js");

async function useMe(body) {
  world.me = body;
  await session.loadMe();
  await settle(3);
  await untilStable(state);
}

async function drawn(page) {
  await visit(state, page.hash);
  const text = visibleText(document.body);
  return { counts: tally(check(document.body).violations), found: violations(text, {}).found, text };
}

for (const layout of ["classic", "campaigns"]) {
  test(`${layout}: a custom brand adds no accessibility defect and no engine word to any page`, async () => {
    const results = {};
    for (const [brand, fixture] of [
      ["default", `default_${layout}`],
      ["custom", `custom_${layout}`],
    ]) {
      await useMe(me(fixture));
      const wordmark = document.getElementById("pb-bar").querySelector("a.wordmark").textContent;
      assert.equal(wordmark, brand === "custom" ? "Acme Growth" : "Marketing AI");
      for (const page of PAGES) {
        const one = await drawn(page);
        const bar = document.getElementById("pb-bar");
        assert.ok(bar.textContent.includes(wordmark), `${layout} / ${brand} / ${page.id}: the brand is on the page`);
        results[`${brand}|${page.id}`] = one;
      }
    }
    for (const page of PAGES) {
      const base = results[`default|${page.id}`];
      const custom = results[`custom|${page.id}`];
      assert.deepEqual(custom.counts, base.counts, `${layout} / ${page.id}: the custom brand changes the accessibility defects`);
      assert.deepEqual(custom.found, {}, `${layout} / ${page.id}: an engine word with the custom brand`);
      assert.deepEqual(base.found, {}, `${layout} / ${page.id}: an engine word with the default brand`);
    }
  });
}

test("a deployment named with an engine word does not fail the check on our copy, because the name is marked as theirs", async () => {
  const nasty = "Uplift Model Studio";
  assert.notDeepEqual(findNeverShown(nasty), {}, "the planted name really is one the check would flag");
  for (const layout of ["classic", "campaigns"]) {
    const body = me(`custom_${layout}`);
    await useMe({ ...body, brand: { ...body.brand, name: nasty } });
    const bar = document.getElementById("pb-bar");
    assert.equal(bar.querySelector("a.wordmark").textContent, nasty);
    for (const page of PAGES.slice(0, 3)) {
      const one = await drawn(page);
      assert.deepEqual(one.found, {}, `${layout} / ${page.id}: the deployment's own name was counted as ours`);
      assert.equal(one.text.includes(nasty), false, `${layout} / ${page.id}: the name is not part of the text the check reads`);
    }
  }
});
