/* "What the AI looked at" and the access line under the chat input (ui/modules/agent/sent.js), in
   jsdom. The session bodies are the REAL recorded ones (PB_FIXTURES, written by
   tests/integration/agent/test_guided_setup_ui.py) with the API's newer fields added by hand:
   `sent` on an assistant message and `data_access` / `third_party` on `chat`. The recorded API
   does not send them yet, so an unchanged recording is also the "older API" case. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");

const HOSTILE = `<img src=x onerror="window.__pwned=1"> [click](http://evil.example) **bold** <script>window.__pwned=1</script>`;
const PREVIEW = "monthly_spend: 12.5, 30, 48.1\nemail: [email hidden], [email hidden]";
const SENT = [
  { tool: "value_counts", args: { column: "region" }, preview: "north: 410\nsouth: 388\neast: 12", chars: 33, mode: "masked_data" },
  { tool: "compare_columns", args: { left: "monthly_spend", right: "churned" }, preview: PREVIEW, chars: PREVIEW.length, mode: "masked_data" },
  { tool: "find_values", args: { column: "<b>notes</b>" }, preview: HOSTILE, chars: HOSTILE.length, mode: "masked_data" },
  { tool: "brand_new_tool", args: { column: "x" }, preview: "", chars: 0, mode: "masked_data" },
];

/** One scenario = one upload id; `chat` is what the API says about the AI service. */
const scenarios = {
  masked: { chat: { backend: "bedrock", generation_model_id: "m", data_access: "masked_data", third_party: false }, sent: SENT },
  summaries: {
    chat: { backend: "openai", generation_model_id: "m", data_access: "summaries_only", third_party: true },
    sent: [{ ...SENT[0], mode: "summaries_only" }],
  },
  emptysent: { chat: { backend: "bedrock", generation_model_id: "m", data_access: "masked_data", third_party: false }, sent: [] },
  practice: { chat: { backend: "fake", generation_model_id: "practice", data_access: "masked_data", third_party: false }, sent: [] },
  older: { chat: null, sent: undefined }, // the recorded body as the API answers today
  olderreal: { chat: { backend: "bedrock", generation_model_id: "m" }, sent: undefined },
};

function body(name, action) {
  const copy = structuredClone(fixture(action === "start" ? "main_start" : "main_chat"));
  const scenario = scenarios[name];
  if (scenario.chat) copy.chat = scenario.chat;
  if (action === "messages") {
    const reply = copy.session.transcript.at(-1);
    if (scenario.sent !== undefined) reply.sent = scenario.sent;
    else delete reply.sent;
  }
  return copy;
}

const uploadId = (name) => `u_${name}`;
let nextUpload = "masked";
const server = (request) => {
  const { method, path } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "POST" && path === "/uploads") {
    return { status: 201, body: { ...fixture("upload_main"), upload_id: uploadId(nextUpload) } };
  }
  const session = /^\/uploads\/u_(\w+)\/agent-session(?:\/(\w+))?$/.exec(path);
  if (session && method === "POST" && scenarios[session[1]]) {
    const action = session[2] || "start";
    if (action === "start") return { status: 201, body: body(session[1], "start") };
    if (action === "messages") return { status: 200, body: body(session[1], "messages") };
  }
  return null;
};

const { w } = installPage(server, { hash: "#/uc/x" });
const router = await import("../../../../../ui/modules/router.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");
await import("../../../../../ui/modules/agent/index.js");
void router;

const app = $("#app");
let controller;
const rerender = () => {
  app.innerHTML = useCaseHtml(uc, controller.state);
  controller.bind(app);
};

/** Upload a file, wait for the session, then send one chat message and wait for the reply. */
async function chat(name) {
  nextUpload = name;
  if (!controller) {
    controller = createController(uc, rerender);
    await controller.refreshLists();
    controller.sync();
    rerender();
  } else if ($("#ag-restart")) {
    $("#ag-restart").click();
  }
  const input = $("#ag-file");
  const file = new w.File(["a,b\n1,2\n"], `${name}.csv`, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(() => $("#ag-question") && !$(".ag .loading"), 3000, `the ${name} session`);
  $("#ag-question").value = "What about monthly_spend?";
  $("#ag-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#ag-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $$("#ag-chat .gmsg.bot").length && !$("#ag-chat .loading"), 3000, "the reply");
}

const flat = (el) => el.textContent.replace(/\s+/g, " ").trim();
const reply = () => $$("#ag-chat .gmsg.bot").at(-1);

test("a reply that sent things shows a collapsed 'What the AI looked at (N)' disclosure", async () => {
  await chat("masked");
  const details = reply().querySelector("details[data-ag-sent]");
  assert.ok(details, "the disclosure is under the reply");
  assert.equal(details.open, false, "collapsed by default");
  assert.equal(details.querySelector("summary").textContent, `What the AI looked at (${SENT.length})`);
  assert.equal($$("#ag-chat details").length, 1, "one disclosure, on the reply that sent");
});

test("it lists each tool in plain words with its columns, and the masked preview in a scrollable block", async () => {
  const items = $$("#ag-chat [data-ag-sent-item]");
  assert.equal(items.length, SENT.length);
  assert.match(flat(items[0]), /^Counted the values of a column Column: region/);
  assert.match(flat(items[1]), /^Compared two columns Columns: monthly_spend, churned/);
  assert.match(flat(items[2]), /^Searched a column/);
  assert.match(flat(items[3]), /^brand_new_tool/, "an unknown tool is shown by its name");
  assert.equal(items[1].querySelector("pre").textContent, PREVIEW, "the preview is what was sent, unchanged");
  assert.equal(items[3].querySelector("pre"), null, "an empty preview draws no block");
  assert.ok(flat($("#ag-chat [data-ag-sent]")).endsWith("Personal details that our checks recognise are hidden before anything is sent."));
  assert.doesNotMatch(flat(items[0]), /value_counts/, "the raw tool name is not shown for a known tool");
});

test("the disclosure is keyboard-accessible and the preview block is capped and scrollable", async () => {
  const summary = $("#ag-chat [data-ag-sent] > summary");
  assert.equal(summary.tagName, "SUMMARY", "a real <summary>: focusable and toggled by Enter / Space");
  const css = $("#agent-styles").textContent;
  const pre = /\.ag-sent-pre\{([^}]*)\}/.exec(css)[1];
  assert.match(pre, /max-height:\d+px/);
  assert.match(pre, /overflow:auto/);
  assert.match(pre, /monospace/);
  assert.match(pre, /overflow-wrap:anywhere/, "a long unbroken value wraps: the page never scrolls sideways");
  assert.equal($("#ag-chat pre").getAttribute("tabindex"), "0", "a scrollable block can be reached by keyboard");
  assert.match(css, /\.ag-chat \.gmsg\{max-width:90%;overflow-wrap:anywhere\}/);
});

test("hostile preview text and column names stay text: no element, no script, no link", async () => {
  const list = $("#ag-chat .ag-sent-list");
  assert.equal(list.querySelectorAll("img, script, a, b, i, strong").length, 0, list.innerHTML);
  const hostile = $$("#ag-chat [data-ag-sent-item]")[2];
  assert.equal(hostile.querySelector("pre").textContent, HOSTILE);
  assert.ok(flat(hostile).includes("<b>notes</b>"), "a hostile column name is text too");
  assert.equal(w.__pwned, undefined);
});

test("masked data: the status line under the input, and no third-party sentence", async () => {
  const line = $("#ag-chat [data-ag-access]");
  assert.equal(flat(line), "The AI can look at your data, with the personal details our checks recognise hidden.");
  assert.equal(line.querySelector("a"), null);
  assert.equal($("[data-ag-practice]"), null, "a real service is not labelled practice");
  assert.ok(line.compareDocumentPosition($("#ag-ask")) & w.Node.DOCUMENT_POSITION_PRECEDING, "it sits under the input row");
});

test("summaries only, from another company's service: the line, the sentence and the Connections link", async () => {
  await chat("summaries");
  const line = $("#ag-chat [data-ag-access]");
  assert.equal(
    flat(line),
    "The AI sees only summaries of your data, never values. This AI service is run by another company. Change on Connections",
  );
  const link = line.querySelector("a");
  assert.equal(link.getAttribute("href"), "#/connections");
  assert.equal(link.textContent, "Change on Connections");
  assert.equal(reply().querySelector("summary").textContent, "What the AI looked at (1)");
});

test("a disclosure the person opened stays open when the page is drawn again", async () => {
  await chat("masked");
  const details = reply().querySelector("details[data-ag-sent]");
  details.open = true;
  $("#ag-question").value = "And the region?";
  $("#ag-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#ag-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $("#ag-chat .loading"), 3000, "the thinking line");
  assert.equal($("#ag-chat details[data-ag-sent]").open, true, "still open while the next answer is awaited");
  await until(() => !$("#ag-chat .loading"), 3000, "the reply");
  assert.equal(reply().querySelector("details[data-ag-sent]").open, true, "and after it");
  reply().querySelector("details[data-ag-sent]").open = false;
  $("#ag-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.equal(reply().querySelector("details[data-ag-sent]").open, false, "a closed one is not forced open");
});

test("an empty `sent` draws no disclosure", async () => {
  await chat("emptysent");
  assert.equal($("#ag-chat details"), null);
  assert.ok($("#ag-chat [data-ag-access]"), "the access line is still there");
});

test("the practice service shows only the practice label, even when the API sends the new fields", async () => {
  await chat("practice");
  assert.equal($("#ag-chat [data-ag-access]"), null);
  assert.match(flat($("[data-ag-practice]")), /^Practice answers/);
  assert.equal($("#ag-chat details"), null);
});

test("an older API (no `sent`, no `data_access`) shows nothing extra, and the practice label stays", async () => {
  await chat("older");
  assert.equal($("#ag-chat details"), null);
  assert.equal($("#ag-chat [data-ag-access]"), null);
  assert.ok($("[data-ag-practice]"));
  await chat("olderreal");
  assert.equal($("#ag-chat details"), null);
  assert.equal($("#ag-chat [data-ag-access]"), null, "an unknown mode is never claimed");
});
