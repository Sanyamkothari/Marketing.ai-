/* Plan M M177 (DEC-1607): the "New campaign" wizard, step by step, in jsdom - the cases are `wizard_cases.mjs`.

   They answer with bodies the real API gave, which tests/integration/test_campaign_wizard_ui.py records into
   $WZ_FIXTURES before it runs this file. A runner that globs ui/ tests without them gets one skipped test saying
   so, never a failure of a suite that is not its own. */
import { test } from "node:test";

if (process.env.WZ_FIXTURES) {
  await import("./wizard_cases.mjs");
} else {
  test("the wizard's jsdom suite (run it through tests/integration/test_campaign_wizard_ui.py)", {
    skip: "WZ_FIXTURES is not set",
  }, () => {});
}
