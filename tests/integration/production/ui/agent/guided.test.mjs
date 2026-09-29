/* Guided setup (ui/modules/agent/, Plan G M75) on the use-case page, in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/agent/test_guided_setup_ui.py). The real Setup form
   (ui/usecase.js) is drawn the way app.js draws it; the agent module registers itself as a setup mode
   exactly as index.html's PLAN-G block loads it. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const uc = fixture("use_case");
const ucGenerative = fixture("use_case_generative");

// --- the fake API: each upload id is one recorded flow ------------------------------------------

/** A session whose words carry markup and markdown: what a hostile column name or cell could do. */
const HOSTILE = `<img src=x onerror="window.__pwned=1"> ![logo](http://evil.example/x.png) [click here](http://evil.example) **bold** https://evil.example/path`;
function hostile(body) {
  const copy = structuredClone(body);
  const proposal = copy.session.proposals.find((p) => p.kind === "recipe_step");
  proposal.title = `<img src=x onerror="window.__pwned=1">Turn <b>it</b> into numbers`;
  proposal.reason = "[click](javascript:alert(1)) ![x](http://evil.example/x.png)";
  copy.session.questions[0].text = `<script>window.__pwned=1</script>Hide it?`;
  copy.session.agent_name = "<i>helper</i>";
  return copy;
}
const TRICKY = "u_tricky";
const hostileChat = (() => {
  const copy = hostile(fixture("main_chat"));
  copy.session.transcript[copy.session.transcript.length - 1].text = HOSTILE;
  return copy;
})();

const flows = {
  [ids.main]: { start: "main_start", messages: "main_chat", answers: "main_answer", decisions: "main_decided", preview: "main_preview", apply: "main_apply" },
  [ids.conflict]: { start: "conflict_start", answers: "conflict_answer", decisions: "conflict_decided", apply: "conflict_apply" },
  [ids.stopped]: { start: "stopped_start" },
};
const sent = { decisions: [], runs: [], messages: [], starts: 0 };
let nextUpload = "main";

const server = (request) => {
  const { method, path, body } = request;
  if (method === "GET" && path === "/runs") return { status: 200, body: fixture("runs_empty") };
  if (method === "GET" && path === "/models") return { status: 200, body: fixture("models_empty") };
  if (method === "POST" && path === "/uploads") {
    if (nextUpload === "tricky") return { status: 201, body: { ...fixture("upload_main"), upload_id: TRICKY } };
    return { status: 201, body: fixture(`upload_${nextUpload}`) };
  }
  if (method === "GET" && path === `/uploads/${ids.derived}/profile`) return { status: 200, body: fixture("derived_profile") };
  if (method === "POST" && path === "/runs") {
    sent.runs.push(body);
    return { status: 202, body: fixture("run_created") };
  }
  if (method === "GET" && path === `/runs/${ids.run}`) return { status: 200, body: fixture("run") };
  const session = /^\/uploads\/([^/]+)\/agent-session(?:\/(\w+))?$/.exec(path);
  if (session && method === "POST") {
    const [, uploadId, action = "start"] = session;
    if (action === "start") sent.starts += 1;
    if (uploadId === TRICKY) {
      if (action === "start") return { status: 201, body: hostile(fixture("main_start")) };
      if (action === "messages") return { status: 200, body: hostileChat };
    }
    const flow = flows[uploadId];
    if (!flow || !flow[action]) return null;
    if (action === "decisions") sent.decisions.push({ uploadId, body });
    if (action === "messages") sent.messages.push(body);
    const answer = fixture(flow[action]);
    if (action === "apply" && answer.detail) return { status: 409, body: answer };
    return { status: action === "start" ? 201 : 200, body: answer };
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/uc/x" });
const router = await import("../../../../../ui/modules/router.js");
const { createController, useCaseHtml } = await import("../../../../../ui/usecase.js");

const app = $("#app");
let controller;
let shown;
/** app.js's `paint`, less the animation: the whole screen redrawn and its events bound again. */
const rerender = () => {
  app.innerHTML = useCaseHtml(shown, controller.state);
  controller.bind(app);
};
async function showUseCase(useCase) {
  shown = useCase;
  controller = createController(useCase, rerender);
  await controller.refreshLists();
  controller.sync();
  rerender();
}

async function chooseFile(name) {
  nextUpload = name;
  const starts = sent.starts;
  const input = $("#ag-file");
  assert.ok(input, "the Guided setup upload control is drawn");
  const file = new w.File(["a,b\n1,2\n"], `${name}.csv`, { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  await until(
    () => sent.starts > starts && !$(".ag .loading") && ($(".ag [data-ag-group]") || $("[data-ag-stop]")),
    3000,
    `the ${name} session`,
  );
}

const text = (sel) => ($(sel) ? $(sel).textContent.replace(/\s+/g, " ").trim() : "");
const box = (id) => $(`[data-ag-proposal="${id}"]`);
const session = (name) => fixture(name).session;

// --- the tests ----------------------------------------------------------------------------------

let manualBefore = "";

test("with no setup mode registered, the Setup view is exactly today's", async () => {
  await showUseCase(uc);
  assert.equal($(".setup-tabs"), null);
  manualBefore = $(".setup-grid").outerHTML;
  assert.ok($("#f-file"), "the prepared-file upload is there");
});

test("the agent module adds a tab strip that opens on Guided setup; Manual setup is still today's screen", async () => {
  await import("../../../../../ui/modules/agent/index.js");
  rerender();
  const tabs = $$(".setup-tabs [data-setup-tab]");
  assert.deepEqual(
    tabs.map((b) => b.textContent),
    ["Guided setup (recommended)", "Manual setup"],
  );
  // Plan H M82 (DEC-1114): Guided setup is where the page starts; Manual setup is one click away.
  assert.equal(controller.state.setupTab, null, "nothing picked yet");
  assert.equal($('[data-setup-tab="guided"]').getAttribute("aria-pressed"), "true", "Guided setup is where the page starts");
  assert.ok($("#ag-file"), "the Guided upload step is drawn first");
  assert.equal($("#f-file"), null, "Manual setup's form is not");
  $('[data-setup-tab="manual"]').click();
  assert.equal($('[data-setup-tab="manual"]').getAttribute("aria-pressed"), "true");
  assert.equal($(".setup-grid").outerHTML, manualBefore, "Manual setup is byte-identical");
});

test("a generative use case gets no tab strip", async () => {
  assert.deepEqual(router.setupModes(ucGenerative), []);
  const keep = [shown, controller];
  await showUseCase(ucGenerative);
  assert.equal($(".setup-tabs"), null);
  [shown, controller] = keep;
  rerender();
  assert.ok($(".setup-tabs"), "and the predictive one still has it");
});

test("the tab strip never switches Train / Score, and the mode switch still works on the Guided tab", async () => {
  $('[data-setup-tab="guided"]').click();
  assert.equal(controller.state.setupTab, "guided");
  assert.equal(controller.state.mode, "train", "a tab click is not a mode click");
  assert.equal($('[data-setup-tab="guided"]').getAttribute("aria-pressed"), "true");
  assert.equal($("#f-file"), null, "Manual setup's form is not drawn on the Guided tab");
  assert.ok($("#ag-file"), "the Guided upload step is");
  assert.equal($$(".ag .fstep").length, 3, "upload, then two steps still to come");
  assert.equal($("#ag-approve"), null, "nothing to approve before there is a file");
  assert.equal($("#ag-chat"), null, "no chat before there is a session");
  $('.seg button[data-mode="score"]').click();
  assert.equal(controller.state.mode, "score");
  assert.equal(controller.state.setupTab, "guided");
  assert.match(text(".nomodel"), /No trained model yet/);
  $('.seg button[data-mode="train"]').click();
  assert.equal(controller.state.mode, "train");
});

test("a stopped session says why and offers no Approve", async () => {
  await chooseFile("stopped");
  const reason = session("stopped_start").stop_reason;
  assert.ok(text("[data-ag-stop]").includes(reason.replace(/\s+/g, " ").trim()));
  assert.equal($("#ag-approve"), null);
  assert.equal($(".ag [data-ag-group]"), null, "no checklist for data that cannot work");
  assert.ok($("#ag-restart"), "another file can be uploaded");
});

test("titles, reasons, questions and chat replies are plain text: no markup, no markdown", async () => {
  $("#ag-restart").click();
  await chooseFile("tricky");
  const main = $(".ag");
  const chat = $("#ag-chat");
  // Everything the session wrote: the checklist, the questions, the summary and the chat (the upload
  // step's own "Download template" link is this screen's, not the session's).
  for (const root of [...$$(".ag [data-ag-group]"), $("[data-ag-summary]"), chat]) {
    assert.equal(root.querySelectorAll("img, script, b, i, strong, em, a").length, 0, root.outerHTML.slice(0, 200));
  }
  assert.match(main.textContent, /<img src=x onerror="window\.__pwned=1">Turn <b>it<\/b> into numbers/);
  assert.match(main.textContent, /\[click\]\(javascript:alert\(1\)\)/);
  assert.match(main.textContent, /<script>window\.__pwned=1<\/script>Hide it\?/);
  assert.equal($("#ag-chat h3").textContent, "<i>helper</i>");
  $("#ag-question").value = "hello";
  $("#ag-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#ag-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $("#ag-chat .gmsg.bot") && !$("#ag-chat .loading"), 2000, "the reply");
  const reply = $$("#ag-chat .gmsg.bot").at(-1);
  assert.equal(reply.firstChild.nodeType, w.Node.TEXT_NODE);
  assert.ok(reply.textContent.startsWith(HOSTILE), "the reply is shown as the text it is");
  assert.equal($("#ag-chat").querySelectorAll("img, a, strong, b").length, 0, "nothing in it became markup");
  assert.equal(w.__pwned, undefined);
});

test("the main flow: proposals grouped and ticked by confidence, questions, summary and chat", async () => {
  await chooseFile("main");
  const start = session("main_start");
  const groups = $$(".ag [data-ag-group]").map((g) => g.dataset.agGroup);
  assert.deepEqual(groups, ["questions", "roles", "fixes", "settings"]);
  assert.match(text('[data-ag-group="roles"] h4'), /Columns & outcome/);
  assert.match(text('[data-ag-group="settings"] h4'), /Auto \(recommended\)/);
  for (const proposal of start.proposals) {
    const element = box(proposal.proposal_id);
    assert.ok(element, `${proposal.title} is listed`);
    assert.equal(element.checked, proposal.confidence === "sure", `${proposal.title} starts as the helper is sure`);
    const item = element.closest(".ag-item");
    assert.ok(item.textContent.includes(proposal.title) && item.textContent.includes(proposal.reason));
    assert.equal(/Please check/.test(item.textContent), proposal.confidence === "check");
  }
  const question = start.questions.find((q) => q.blocking);
  assert.match(text(`[data-ag-q="${question.question_id}"]`), /Needs an answer/);
  assert.equal($("#ag-approve").disabled, true, "Approve waits for the blocking question");
  assert.match(text(".ag .reason"), /Answer the questions/);
  for (const line of [...start.summary.assumptions, ...start.summary.intended_actions, ...start.summary.hidden_columns]) {
    assert.ok(text("[data-ag-summary]").includes(line), line);
  }
  assert.equal($("[data-ag-dirty]").hidden, false, "the summary says it is brought up to date later");
  assert.equal($("[data-ag-practice]"), null, "no answers are labelled as anything but the AI service's");
  assert.equal(sent.decisions.length, 0, "nothing is decided until preview or approve");
});

test("a chat reply shows the evidence it cites; the question the person asked is sent as typed", async () => {
  $("#ag-question").value = ids.chat;
  $("#ag-question").dispatchEvent(new w.Event("input", { bubbles: true }));
  $("#ag-ask").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $$("#ag-chat .gmsg.bot").length && !$("#ag-chat .loading"), 2000, "the reply");
  assert.deepEqual(sent.messages.at(-1), { text: ids.chat });
  const chat = session("main_chat");
  const reply = chat.transcript.at(-1);
  const bubble = $$("#ag-chat .gmsg.bot").at(-1);
  assert.ok(bubble.textContent.startsWith(reply.text));
  const cited = chat.tool_results.find((r) => r.evidence_id === reply.evidence_ids[0]);
  const source = bubble.querySelector("[data-ag-sources]").textContent;
  assert.ok(source.startsWith(`Source: ${cited.tool.replace(/_/g, " ")}`), source);
  for (const [key, value] of Object.entries(cited.result)) {
    if (typeof value === "number" && Number.isInteger(value) && source.includes(key)) {
      assert.ok(source.includes(`${key.replace(/_/g, " ")} ${value.toLocaleString("en-US")}`), `${key} in ${source}`);
    }
  }
  const user = $$("#ag-chat .gmsg.user").at(-1);
  assert.equal(user.querySelector("[data-ag-sources]"), null, "a person's own message cites nothing");
});

test("answering a question, then Preview sends every pending decision as its box says", async () => {
  const question = session("main_chat").questions.find((q) => q.blocking);
  $(`[data-ag-question="${question.question_id}"][data-ag-option="hide"]`).click();
  await until(() => !$(`[data-ag-q="${question.question_id}"] .pill`), 2000, "the answer");
  assert.equal($(`[data-ag-question="${question.question_id}"][data-ag-option="hide"]`).getAttribute("aria-pressed"), "true");
  assert.equal($("#ag-approve").disabled, false);
  $("#ag-preview").click();
  await until(() => $("[data-ag-preview]") || $("[data-ag-nochange]"), 2000, "the preview");
  const expected = session("main_answer")
    .proposals.filter((p) => p.state === "pending")
    .map((p) => ({ proposal_id: p.proposal_id, state: p.confidence === "sure" ? "accepted" : "rejected" }));
  assert.equal(sent.decisions.length, 1);
  assert.deepEqual(sent.decisions[0], { uploadId: ids.main, body: { decisions: expected, accept_recommended: false } });
  const preview = fixture("main_preview");
  const touched = preview.receipt.steps.map((s) => s.column).filter((c) => preview.columns_after.includes(c));
  assert.ok($$("[data-ag-preview] th").some((th) => touched.includes(th.textContent)));
  for (const line of session("main_decided").summary.decisions) assert.ok(text("[data-ag-summary]").includes(line), line);
  assert.equal($("[data-ag-dirty]").hidden, true, "the summary now matches the boxes");
});

test("Approve applies, fills the Setup form from the API, and Run is the form's own", async () => {
  $("#ag-approve").click();
  await until(() => $("[data-ag-applied]") && $("#f-run"), 3000, "the approval");
  assert.equal(sent.decisions.length, 1, "nothing left to decide, so no second decisions call");
  const applied = fixture("main_apply");
  const s = controller.state;
  assert.equal(s.upload.upload_id, applied.upload_id);
  assert.deepEqual(s.upload.profile, fixture("derived_profile"));
  assert.equal(s.pk, applied.primary_key);
  assert.equal(s.target, applied.target);
  assert.match(text("[data-ag-applied]"), new RegExp(`${applied.receipt.rows_out.toLocaleString("en-US")} rows`));
  assert.equal($$(".btn.primary").length, 1, "one primary action: Run");
  assert.equal($("#ag-approve"), null);
  assert.equal($("#f-run").disabled, false, text(".reason"));
  $("#f-setup").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => sent.runs.length === 1, 2000, "POST /runs");
  // Painted in the same task that starts the Running view's poll; this test ends here, so stop it.
  await until(() => $(".uc-running") || $(".results"), 2000, "the run view");
  controller.stop();
  const body = sent.runs[0];
  assert.equal(body.use_case, uc.id);
  assert.equal(body.mode, "train");
  assert.equal(body.upload_id, applied.upload_id);
  assert.equal(body.primary_key, applied.primary_key);
  assert.equal(body.target, applied.target);
  assert.deepEqual(body.overrides, applied.overrides, "the approved overrides, through the form's own state");
});

test("after the run, Manual setup shows the prepared file with every choice filled in", async () => {
  controller.state.view = "setup";
  rerender();
  $('[data-setup-tab="manual"]').click();
  assert.match(text(".fname"), /\(prepared\)/);
  assert.equal($("#f-pk").value, fixture("main_apply").primary_key);
  assert.equal($("#f-target").value, fixture("main_apply").target);
  $('[data-setup-tab="guided"]').click();
  assert.ok($("[data-ag-applied]"), "the Guided tab still shows what was approved");
});

test("an Approve the Run button's checks refuse shows those checks, and fills nothing", async () => {
  const before = controller.state.upload.upload_id;
  $("#ag-restart").click();
  await chooseFile("conflict");
  const target = session("conflict_start").proposals.find((p) => p.kind === "role" && p.path === "target");
  box(target.proposal_id).click();
  assert.equal(box(target.proposal_id).checked, false);
  const question = session("conflict_start").questions.find((q) => q.blocking);
  $(`[data-ag-question="${question.question_id}"][data-ag-option="hide"]`).click();
  await until(() => !$(`[data-ag-q="${question.question_id}"] .pill`), 2000, "the answer");
  assert.equal(box(target.proposal_id).checked, false, "the untick survives the answer");
  $("#ag-approve").click();
  await until(() => $("[data-ag-checks]"), 2000, "the refusal");
  const decided = sent.decisions.filter((d) => d.uploadId === ids.conflict);
  assert.equal(decided.length, 1);
  assert.deepEqual(
    decided[0].body.decisions.find((d) => d.proposal_id === target.proposal_id),
    { proposal_id: target.proposal_id, state: "rejected" },
  );
  assert.ok($('[data-ag-checks] [data-code="TARGET_MISSING"]'));
  assert.equal($("[data-ag-applied]"), null);
  assert.equal(controller.state.upload.upload_id, before, "the Setup form was not touched");
});
