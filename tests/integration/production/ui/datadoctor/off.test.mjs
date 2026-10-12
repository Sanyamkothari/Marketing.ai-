/* Plan O M247 (DEC-1817): with `data_doctor.unified` OFF (`GET /auth/me` without `data_doctor`, recorded from the real
   app), today's screens are drawn unchanged: the Data page's Check my data tab is its description card with the way to
   Data Doctor's page, the wizard's Customers step mounts Guided setup itself, and nothing of the Data Doctor screen is
   asked of the API. */
import { test } from "node:test";
import assert from "node:assert/strict";

if (!process.env.DDS_FIXTURES) {
  test("the Data Doctor switch-off jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  const { $, installPage, settle, until } = await import("../harness.mjs");
  const { GOAL, makeWorld, serverFor } = await import("./common.mjs");

  const world = makeWorld({ at: "question", me: "me_off" });
  const { calls } = installPage(serverFor(world), { hash: "#/data/check" });
  await import("../../../../../ui/app.js");
  await import("../../../../../ui/modules/production/index.js");
  await import("../../../../../ui/modules/agent/index.js");
  await import("../../../../../ui/modules/decide/index.js");
  await import("../../../../../ui/modules/data/index.js");

  const asked = () => calls.filter((c) => c.path.startsWith("/cases") || c.path.startsWith("/data-doctor"));

  test("the Data page's Check my data tab is today's card, with the way to Data Doctor's page", async () => {
    await until(() => $(".dt-check"), 4000, "the Check my data tab");
    await settle(4);
    assert.ok(!$("[data-dds]") && !$("[data-dt-doctor-screen]"));
    const open = $('.dt-check a[href="#/doctor"]');
    assert.ok(open && open.textContent.includes("Open Data Doctor"));
  });

  test("the wizard's Customers step mounts Guided setup itself", async () => {
    window.location.hash = `#/campaigns/new/customers/${GOAL}`;
    await until(() => $("[data-cw-customers] [data-cw-guided-main] .ag"), 5000, "Guided setup in the wizard");
    await settle(4);
    assert.ok(!$("[data-dds]"), "no Data Doctor screen");
  });

  test("nothing of the Data Doctor screen was asked of the API", () => {
    assert.deepEqual(asked(), []);
  });
}
