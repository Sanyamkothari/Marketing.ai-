/* Plan O M247 (DEC-1817): the one Data Doctor screen, shape by shape, in jsdom, on answers the real visit gave.

   Each question, approval and doubt shape (the design §6.5-§6.8, §7.2, §13.2): a question with why it is asked, what
   each answer changes and "I don't know"; a small choice decided for the person; a change that needs its own yes with
   the exact sentence ("I agree to this sentence"); Approve sent with its sentence word for word; a doubt in three
   parts; a request for more data with the list to send; a remembered answer with its date and Revoke. Then the chat
   with no AI service (the way to Settings › AI service, never Connections, and everything else still works), the
   accessibility checks of Plan M M181, the touch targets, and the visible-text check. */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the Data Doctor screen's jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  await import("./screen_cases.mjs");
}
