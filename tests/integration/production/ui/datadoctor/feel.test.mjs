/* Plain words, a phone and feedback in the chat-first Data Doctor (see feel_cases.mjs). */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the Data Doctor plain-words, phone and feedback jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  await import("./feel_cases.mjs");
}
