/* The one approval screen (roadmap 2.5), in jsdom, on answers the real visit gave: the plan in plain words, each
   decision with yes / change and its typed input, problems with yes / no (a hard stop apart), swaps old beside new, and
   ONE Approve button that is on only when everything needed is answered. */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the one approval screen jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  await import("./review_cases.mjs");
}
