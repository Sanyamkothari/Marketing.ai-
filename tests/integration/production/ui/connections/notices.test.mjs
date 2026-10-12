/* "Needs an AI service" notices (ui/availability.js, ui/overview.js) in jsdom: they follow the
   Deliverable AI slot of `GET /ai-service` (a hand-written fake built to the HTTP contract), in every
   mode, not the demo. An inherited slot counts as connected; a call that fails claims nothing. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, settle, until } from "../harness.mjs";

const slot = (over = {}) => ({ connected: false, source: "none", provider: null, has_key: false, editable: true, ...over });
let answer = { status: 200, body: { slots: { product: slot(), deliverable: slot() }, providers: [] } };
let asked = 0;
const setSlots = (product, deliverable) => {
  answer = { status: 200, body: { slots: { product: slot(product), deliverable: slot(deliverable) }, providers: [] } };
};

const RAG = { id: "asst", name: "Assistant", ai_type: "generative", config: { generative: { kind: "rag_assistant" } } };
const NONE = { id: "plain", name: "Plain", ai_type: "generative", config: { generative: { kind: "none" } } };
const PREDICT = { id: "pred", name: "Predict", ai_type: "predictive", config: {} };

const server = (request) => {
  const { method, path } = request;
  if (method === "GET" && path === "/ai-service") {
    asked += 1;
    return answer;
  }
  if (method === "GET" && path === "/pilot/demo") return { status: 200, body: { demo_mode: false, seeded: false, manifest: null } };
  if (method === "GET" && path === "/runs") return { status: 200, body: { runs: [{ run_id: "r1" }] } };
  const uc = /^\/use-cases\/(\w+)$/.exec(path);
  if (method === "GET" && uc) return { status: 200, body: [RAG, NONE, PREDICT].find((u) => u.id === uc[1]) };
  return null;
};

installPage(server, { hash: "#/" });
const availability = await import("../../../../../ui/availability.js");
const { overviewHtml } = await import("../../../../../ui/overview.js");
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");

test("before the answer nothing is claimed either way", () => {
  assert.equal(availability.needsAiNoticeNow(RAG), false);
  assert.equal(availability.aiWritingAvailableNow(RAG), false, "not offered on a guess");
});

test("no Deliverable AI: an AI-writing use case needs the notice, in any mode", async () => {
  assert.equal(await availability.needsAiNotice(RAG), true);
  assert.equal(availability.needsAiNoticeNow(RAG), true);
  assert.equal(availability.aiWritingAvailableNow(RAG), false);
  assert.equal(await availability.needsAiNotice(PREDICT), false, "a use case that writes no text is unaffected");
  assert.equal(await availability.needsAiNotice(NONE), false);
});

test("the notice says what is missing and offers the way to connect it, by role", () => {
  availability.registerAiServiceAccess(() => true);
  const holder = document.createElement("div");
  holder.innerHTML = availability.aiNoticeCard({ id: "asst", name: "Assistant" });
  const card = holder.querySelector("[data-ai-notice-card]");
  assert.equal(text(card.querySelector("h3")), "Needs AI service connection");
  assert.match(text(card), /none is connected yet/);
  assert.doesNotMatch(text(card), /demo|sample|practice|fake/i);
  const link = card.querySelector("a.btn.primary");
  assert.equal(link.getAttribute("href"), "#/connections/ai/deliverable");
  assert.equal(text(link), "Connect the Deliverable AI");
  availability.registerAiServiceAccess(() => false);
  holder.innerHTML = availability.aiNoticeCard({ id: "asst", name: "Assistant" });
  assert.equal(holder.querySelector('a[href="#/connections/ai/deliverable"]'), null);
  assert.match(text(holder), /Ask your administrator/);
  availability.registerAiServiceAccess(null);
});

test("the Product AI alone does not switch the notice off unless the Deliverable AI uses it", async () => {
  setSlots({ connected: true, source: "saved", provider: "openai" }, { connected: false, source: "none" });
  availability.forgetAiServiceStatus();
  assert.equal(await availability.needsAiNotice(RAG), true, "the API says the Deliverable AI is not connected");
});

test("a Deliverable AI that uses the Product AI's service counts as connected", async () => {
  setSlots({ connected: true, source: "saved", provider: "openai" }, { connected: true, source: "inherited", provider: "openai", inherits_product: true });
  availability.forgetAiServiceStatus();
  assert.equal(await availability.needsAiNotice(RAG), false);
  assert.equal(availability.needsAiNoticeNow(RAG), false);
  assert.equal(availability.aiWritingAvailableNow(RAG), true);
});

test("the answer is reused for a moment, and connecting or disconnecting makes the next question ask again", async () => {
  const before = asked;
  await availability.needsAiNotice(RAG);
  await availability.needsAiNotice(RAG);
  assert.equal(asked, before, "reused");
  let heard = 0;
  window.addEventListener(availability.AI_SERVICE_EVENT, () => (heard += 1));
  setSlots({}, {});
  availability.forgetAiServiceStatus();
  assert.equal(heard, 0, "listeners are told once the new answer is in, not before");
  assert.equal(await availability.needsAiNotice(RAG), true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(heard, 1);
  assert.equal(asked, before + 1, "one question, asked straight away");
  assert.equal(availability.needsAiNoticeNow(RAG), true, "settled: a screen opened next needs no wait");
});

test("an API that cannot answer claims nothing: no notice, and the writing is offered (the server decides)", async () => {
  answer = { status: 500, body: { detail: { code: "X", message: "x" } } };
  availability.forgetAiServiceStatus();
  assert.equal(await availability.needsAiNotice(RAG), false);
  assert.equal(availability.aiWritingAvailableNow(RAG), true);
  answer = { status: 200, body: { slots: { product: slot(), deliverable: slot() }, providers: [] } };
  availability.forgetAiServiceStatus();
});

// --- Home ------------------------------------------------------------------------------------------

const PAYLOAD = {
  default_industry: "one",
  industries: [
    {
      id: "one",
      name: "One",
      journey_label: "Journey",
      legend: [],
      stages: [
        {
          name: "Stage",
          marker: "G",
          use_cases: [
            { id: "asst", name: "Assistant", ai_type: "generative", status: "available" },
            { id: "pred", name: "Predict", ai_type: "predictive", status: "available" },
          ],
        },
      ],
    },
  ],
};
const draw = () => {
  document.querySelector("#app").innerHTML = overviewHtml(PAYLOAD);
};
// As `app.js` does: redraw the route when a module says something it read has changed.
window.addEventListener("marketing-ai:modules-changed", draw);
const tags = (id) => text($(`a.uc[href="#/uc/${id}"] .uc-tags`));

test("Home tags an AI-writing card 'Needs AI service' while no Deliverable AI is connected, with the demo off", async () => {
  answer = { status: 200, body: { slots: { product: slot({ connected: true }), deliverable: slot() }, providers: [] } };
  availability.forgetAiServiceStatus();
  draw(); // starts reading what only the browser can know
  await until(() => tags("asst") === "Needs AI service", 3000, "the tag");
  assert.equal(tags("pred"), "", "a card that writes no text is not tagged");
  assert.doesNotMatch(text($("#app")), /Sample data ready/);
});

test("connecting one makes Home read again, and the tag goes", async () => {
  setSlots({ connected: true }, { connected: true, source: "saved" });
  availability.forgetAiServiceStatus(); // what the AI service screen does after a save
  await settle(4);
  draw();
  await until(() => tags("asst") === "", 3000, "the tag to go");
  assert.equal($$("a.uc .uc-tags").length, 0);
});

// --- Plan M M192 (DEC-1622): in the campaigns layout, no AI service says so and links Settings › AI service ------

const inCampaigns = (on) => {
  if (on) document.documentElement.setAttribute("data-ui-layout", "campaigns");
  else document.documentElement.removeAttribute("data-ui-layout");
};

test("campaigns layout: the notice says 'No AI service is connected' and opens Settings › AI service", () => {
  inCampaigns(true);
  try {
    availability.registerAiServiceAccess(() => true);
    const holder = document.createElement("div");
    holder.innerHTML = availability.aiNoticeCard({ id: "asst", name: "Assistant" });
    const card = holder.querySelector("[data-ai-notice-card]");
    assert.equal(text(card.querySelector("h3")), "Needs AI service connection");
    assert.equal(text(card.querySelector("p")), "No AI service is connected. Connect one in Settings › AI service.");
    const link = card.querySelector("a.btn.primary");
    assert.equal(link.getAttribute("href"), "#/settings/ai");
    assert.doesNotMatch(text(card), /Deliverable|Product AI/, "plain words: no engineering name");
    availability.registerAiServiceAccess(() => false);
    holder.innerHTML = availability.aiNoticeCard({ id: "asst", name: "Assistant" });
    assert.match(text(holder), /No AI service is connected\. Ask an admin to connect one in Settings › AI service\./);
    assert.equal(holder.querySelector('a[href="#/settings/ai"]'), null, "no way to a form they could not save");
    assert.equal(holder.querySelector('a[href^="#/connections/ai/"]'), null);
  } finally {
    availability.registerAiServiceAccess(null);
    inCampaigns(false);
  }
  // The classic layout keeps its words and its link.
  const holder = document.createElement("div");
  holder.innerHTML = availability.aiNoticeCard({ id: "asst", name: "Assistant" });
  assert.equal(holder.querySelector("a.btn.primary").getAttribute("href"), "#/connections/ai/deliverable");
});

test("the one sentence: a link for whoever may connect one, 'Ask an admin' for anyone else", () => {
  const holder = document.createElement("div");
  holder.innerHTML = availability.aiNotConnectedHtml({ canConnect: true });
  assert.equal(text(holder), "No AI service is connected. Connect one in Settings › AI service.");
  const link = holder.querySelector("[data-ai-not-connected] a");
  assert.equal(link.getAttribute("href"), "#/settings/ai");
  assert.equal(text(link), "Settings › AI service");
  holder.innerHTML = availability.aiNotConnectedHtml({ canConnect: false });
  assert.equal(text(holder), "No AI service is connected. Ask an admin to connect one in Settings › AI service.");
  assert.equal(holder.querySelector("a"), null);
});

test("a 409 AI_NOT_CONNECTED answer becomes that sentence in the campaigns layout, and nothing else does", () => {
  const refused = { code: "AI_NOT_CONNECTED", status: 409, message: "No AI service is connected." };
  assert.equal(availability.aiNotConnectedFor(refused), "", "the classic layout keeps its error box");
  inCampaigns(true);
  try {
    const holder = document.createElement("div");
    holder.innerHTML = availability.aiNotConnectedFor(refused, { canConnect: true });
    assert.equal(holder.querySelector("[data-ai-not-connected] a").getAttribute("href"), "#/settings/ai");
    assert.equal(availability.aiNotConnectedFor({ code: "INTERNAL", status: 500 }), "", "another error stays an error");
    assert.equal(availability.aiNotConnectedFor(null), "");
  } finally {
    inCampaigns(false);
  }
});
