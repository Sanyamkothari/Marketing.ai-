/* The chat-first Data Doctor screen (Part C of the agent-style Data Doctor), in jsdom, on answers the real visit gave:
   the conversation's cards, the composer with and without an AI service, the data panel, and the ONE "Approve and
   start" card that chains the own yeses, the stops, Approve and the start. */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the chat-first Data Doctor jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  await import("./chat_cases.mjs");
}
