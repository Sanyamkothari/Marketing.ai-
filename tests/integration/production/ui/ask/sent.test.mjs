/* "What the AI looked at" and the access line (ui/modules/ask/sent.js), on plain objects: the cases the deleted
   Guided-setup suite held, ported to the module's own functions. sent.js is still what the Data Doctor chat and
   Ask your data draw under each reply. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { sentHtml, statusHtml, SENT_FOOTER, STATUS_MASKED, STATUS_SUMMARIES, STATUS_THIRD_PARTY } from "../../../../../ui/modules/ask/sent.js";

const dom = (html) => {
  const { window } = new JSDOM(`<!doctype html><body>${html}</body>`, { runScripts: "outside-only" });
  return window;
};
const flat = (el) => el.textContent.replace(/\s+/g, " ").trim();

const HOSTILE = `<img src=x onerror="window.__pwned=1"> [click](http://evil.example) **bold** <script>window.__pwned=1</script>`;
const PREVIEW = "monthly_spend: 12.5, 30, 48.1\nemail: [email hidden], [email hidden]";
const SENT = [
  { tool: "value_counts", args: { column: "region" }, preview: "north: 410\nsouth: 388", chars: 22, mode: "masked_data" },
  { tool: "compare_columns", args: { left: "monthly_spend", right: "churned" }, preview: PREVIEW, chars: PREVIEW.length, mode: "masked_data" },
  { tool: "find_values", args: { column: "<b>notes</b>" }, preview: HOSTILE, chars: HOSTILE.length, mode: "masked_data" },
  { tool: "brand_new_tool", args: { column: "x" }, preview: "", chars: 0, mode: "masked_data" },
];

test("a reply that sent things is a collapsed disclosure with each tool in plain words and the preview as sent", () => {
  const w = dom(sentHtml(SENT, { id: "m3" }));
  const details = w.document.querySelector("details[data-ag-sent]");
  assert.equal(details.open, false, "collapsed by default");
  assert.equal(details.querySelector("summary").textContent, `What the AI looked at (${SENT.length})`);
  const items = [...w.document.querySelectorAll("[data-ag-sent-item]")];
  assert.match(flat(items[0]), /^Counted the values of a column/);
  assert.match(flat(items[1]), /monthly_spend, churned/);
  assert.match(flat(items[3]), /^brand_new_tool/, "an unknown tool is shown by its name");
  assert.equal(items[1].querySelector("pre").textContent, PREVIEW, "the preview is what was sent, unchanged");
  assert.equal(items[3].querySelector("pre"), null, "an empty preview draws no block");
  assert.ok(flat(details).endsWith(SENT_FOOTER));
});

test("hostile preview text and column names stay text: no element, no script, no link", () => {
  const w = dom(sentHtml(SENT));
  const list = w.document.querySelector(".ag-sent-list");
  assert.equal(list.querySelectorAll("img, script, a, b, i, strong").length, 0, list.innerHTML);
  const hostile = w.document.querySelectorAll("[data-ag-sent-item]")[2];
  assert.equal(hostile.querySelector("pre").textContent, HOSTILE);
  assert.ok(flat(hostile).includes("<b>notes</b>"), "a hostile column name is text too");
  assert.equal(w.__pwned, undefined);
});

test("the disclosure is keyboard-accessible: a real summary, and a preview block that takes focus", () => {
  const w = dom(sentHtml(SENT));
  assert.equal(w.document.querySelector("details > summary").tagName, "SUMMARY");
  const pre = w.document.querySelector("pre");
  assert.equal(pre.getAttribute("tabindex"), "0", "a scrollable block can be reached by keyboard");
  assert.equal(pre.getAttribute("role"), "region");
});

test("the preview block is capped and scrolls, and a long value wraps (the page never scrolls sideways)", () => {
  const source = readFileSync(new URL("../../../../../ui/modules/ask/styles.js", import.meta.url), "utf8");
  const rule = /\.ag-sent-pre\{([^}]*)\}/.exec(source);
  assert.ok(rule, "the styles carry the preview rule");
  assert.match(rule[1], /max-height:\d+px/);
  assert.match(rule[1], /overflow:auto/);
  assert.match(rule[1], /overflow-wrap:anywhere/);
});

test("a disclosure the person opened is drawn open again (by its id), and a closed one is not forced open", () => {
  assert.match(sentHtml(SENT, { id: "m3", open: true }), /<details[^>]*data-ag-sent="m3"[^>]* open>/);
  assert.doesNotMatch(sentHtml(SENT, { id: "m3", open: false }), / open>/);
});

test("masked data: the status line, and no third-party sentence and no link", () => {
  const w = dom(statusHtml({ data_access: "masked_data", third_party: false }));
  const line = w.document.querySelector("[data-ag-access]");
  assert.equal(flat(line), STATUS_MASKED);
  assert.equal(line.querySelector("a"), null);
});

test("summaries only, from another company's service: the line, the sentence and the Connections link", () => {
  const w = dom(statusHtml({ data_access: "summaries_only", third_party: true }));
  const line = w.document.querySelector("[data-ag-access]");
  assert.equal(flat(line), `${STATUS_SUMMARIES} ${STATUS_THIRD_PARTY} Change on Connections`);
  assert.equal(line.querySelector("a").getAttribute("href"), "#/connections/ai/product");
});

test("an empty `sent` draws nothing, and an older API (no `sent`, no `data_access`) claims nothing", () => {
  assert.equal(sentHtml([]), "");
  assert.equal(sentHtml(undefined), "");
  assert.equal(statusHtml(null), "");
  assert.equal(statusHtml({ backend: "bedrock", generation_model_id: "m" }), "", "an unknown mode is never claimed");
  assert.equal(statusHtml({ available: false, reason: "AI_NOT_CONNECTED" }), "", "nothing about a service that is not there");
});
