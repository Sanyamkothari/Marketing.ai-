/* Review of "What the AI looked at" (ui/modules/agent/sent.js): hostile or odd `sent` items. Each test is a
   failing repro. They call the module's own functions on plain objects (no fixtures are needed), and
   run in the same `node --test` glob as sent.test.mjs. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { sentHtml, statusHtml, toolPhrase, SENT_FOOTER, STATUS_MASKED } from "../../../../../ui/modules/agent/sent.js";

const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();

test("a tool named like an Object.prototype member is shown by its name, never as native code", () => {
  for (const tool of ["constructor", "toString", "hasOwnProperty", "__proto__", "valueOf"]) {
    const shown = text(sentHtml([{ tool, preview: "p", chars: 1 }]));
    assert.ok(!/native code|\[object/.test(shown), `${tool} -> ${shown}`);
    assert.ok(shown.includes(tool), `${tool} -> ${shown}`);
    assert.equal(toolPhrase(tool), tool);
  }
});

test("bidirectional and other invisible controls in a preview or a column name are not passed to the page", () => {
  const html = sentHtml([
    { tool: "value_counts", args: { column: "region‮evil" }, preview: "abc‮def ⁦ghi ​jkl \u0000", chars: 12 },
  ]);
  assert.ok(!/[‪-‮⁦-⁩​-‏\u0000]/.test(html), "a right-to-left override can reorder what the person reads");
});

test("the preview block that can take focus has a role, so its accessible name is announced", () => {
  const html = sentHtml([{ tool: "value_counts", args: {}, preview: "a: 1", chars: 4 }]);
  const pre = /<pre[^>]*>/.exec(html)[0];
  assert.match(pre, /tabindex="0"/);
  assert.match(pre, /aria-label=/);
  assert.match(pre, /role="(region|group)"/, `aria-label on a role-less <pre> is ignored or flagged: ${pre}`);
});

test("500 items do not become 900 KB of markup in one reply", () => {
  const items = Array.from({ length: 500 }, (_, i) => ({ tool: "value_counts", args: { column: `c${i}` }, preview: "x".repeat(1500), chars: 1500 }));
  const html = sentHtml(items);
  assert.ok((html.match(/<li class="ag-sent-item"/g) || []).length <= 12, "the server keeps at most 12 items per reply; the page should not draw more");
});

test("the wording does not promise that personal details are all hidden when the API says only masked data is sent", () => {
  // docs/AGENTS.md 7.7: in masked_data mode a name inside a sentence, an address, another country's ID number are NOT hidden.
  const claim = `${SENT_FOOTER} ${STATUS_MASKED} ${text(statusHtml({ backend: "bedrock", data_access: "masked_data", third_party: false }))}`;
  assert.match(claim, /recogni[sz]|detect|spot|can find|known patterns|as far as/i, `unqualified claim: ${claim}`);
});
