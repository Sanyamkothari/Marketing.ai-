/* A use case that is only the document assistant opens straight on the assistant (DEC-1264), and the
   address it was opened at is replaced rather than kept: Back from the assistant leaves the use case
   instead of landing on #/uc/<id> and being sent forward to the assistant again. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { fixture, installPage, settle, until } from "./harness.mjs";

const ID = "ai-onboarding-assistant";
const ASSISTANT = `#/generative/assistant/${ID}`;
/** The fields `app.js` reads to decide; the rest of the screen is the generative module's own. */
const useCase = {
  id: ID,
  name: "AI onboarding assistant",
  ai_type: "generative",
  marker: "g",
  description: "Answers questions from the documents it was given.",
  config: { generative: { kind: "assistant" } },
  pages: {},
};
const server = (request) => {
  if (request.path === "/industries") return { status: 200, body: fixture("industries") };
  if (request.path === `/use-cases/${ID}`) return { status: 200, body: useCase };
  return null;
};
const { w } = installPage(server, { hash: "#/" });

await import("../../../../ui/app.js");
await import("../../../../ui/modules/generative/index.js");

test("opening the use case lands on the assistant, and Back returns to where the person came from", async () => {
  await until(() => w.document.querySelector(".stage-pill"), 3000, "the overview");
  const before = w.history.length;
  w.location.hash = `#/uc/${ID}`;
  await until(() => w.location.hash === ASSISTANT, 3000, "the assistant's address");
  assert.equal(w.history.length, before + 1, "one entry for the visit: the use case's address was replaced");
  w.history.back();
  await until(() => w.location.hash === "#/", 3000, "the overview again");
  await settle(10);
  assert.equal(w.location.hash, "#/", "Back is not bounced forward to the assistant");
});
