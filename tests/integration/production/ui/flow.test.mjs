/* The whole page, as index.html loads it - app.js (which imports router.js, and through its Phase 4b
   block boot.js), then the generative module, then ui/modules/production/index.js - against an API
   with sign-in on (auth_mode=local). Every body the fake API answers with is a real one
   (PB_FIXTURES, written by test_production_ui_js.py). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, settle, signedInServer, until } from "./harness.mjs";

const login = fixture("login_ok");
const ids = fixture("ids");
const tokens = {};
const answers = {
  "GET /industries": () => ({ status: 200, body: fixture("industries") }),
  "GET /connection/aws": () => ({ status: 200, body: fixture("connection") }),
  "GET /users": () => ({ status: 200, body: fixture("users") }),
  // as the real app answered: the last Admin can be neither disabled nor demoted; anyone else can be disabled
  "PATCH /users/{id}": (request) =>
    "roles" in request.body
      ? { status: 409, body: fixture("last_admin_demote") }
      : request.path.endsWith(`/${ids.admin}`)
        ? { status: 409, body: fixture("last_admin") }
        : { status: 200, body: fixture("user_disabled") },
  "POST /users": (request) => ({
    status: 201,
    body: { ...fixture("users").users[3], username: request.body.username, roles: request.body.roles },
  }),
  "GET /audit/events": () => ({ status: 200, body: fixture("audit_page") }),
  "POST /auth/logout": () => ({ status: 204, body: null }),
};
const server = signedInServer({
  tokens,
  routes: new Proxy(
    {
      login: (request) =>
        request.body.password === "the right one"
          ? ((tokens[login.token] = fixture("me_admin")), { status: 200, body: login })
          : { status: 401, body: fixture("login_refused") },
    },
    {
      get(target, key) {
        if (key in target) return target[key];
        const [method, path] = String(key).split(" ");
        const normalised = `${method} ${(path || "").replace(/^\/users\/[^/]+$/, "/users/{id}")}`;
        return answers[normalised];
      },
    },
  ),
});
const { w, calls } = installPage(server, { hash: "#/" });

await import("../../../../ui/app.js");
await import("../../../../ui/modules/generative/index.js");
await import("../../../../ui/modules/production/index.js");
const session = await import("../../../../ui/modules/production/session.js");

const bar = () => ($("#pb-bar") || {}).textContent || "";
const submit = (form) => form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));

test("with sign-in on, the first thing on screen is the sign-in form", async () => {
  await until(() => $("#pb-signin"), 3000, "the sign-in form");
  assert.equal(w.location.hash, "#/signin");
  assert.match(bar(), /Not signed in/);
  const industries = calls.find((c) => c.path === "/industries");
  assert.equal(industries.auth, null, "nothing to send yet");
});

test("a wrong password shows the server's sentence, keeps the username, forgets the password", async () => {
  $("#pb-username").value = "admin-person";
  $("#pb-password").value = "not it";
  submit($("#pb-signin"));
  await until(() => $(".apierr"), 2000, "the refusal");
  assert.match($(".apierr").textContent, /The username or password is not right\./);
  assert.equal($("#pb-username").value, "admin-person");
  assert.equal($("#pb-password").value, "");
  assert.equal(w.document.body.innerHTML.includes("not it"), false);
});

test("signing in keeps the token for the tab and every later call carries it", async () => {
  $("#pb-password").value = "the right one";
  submit($("#pb-signin"));
  await until(() => $(".stage-pill"), 3000, "the overview after signing in");
  assert.equal(session.readToken(), login.token);
  assert.equal(w.sessionStorage.getItem(session.TOKEN_KEY).includes(login.token), true);
  assert.equal(w.localStorage.length, 0, "never in localStorage");
  const later = calls.filter((c) => c.path === "/industries").pop();
  assert.equal(later.auth, `Bearer ${login.token}`);
  assert.match(bar(), /Signed in as admin-person/);
  assert.match(bar(), /Admin/);
  assert.ok($('#pb-bar a[href="#/admin/users"]'));
  assert.ok($('#pb-bar a[href="#/admin/audit"]'));
});

test("the Users screen lists everyone; Disable is quiet red and asks twice; the last Admin cannot be demoted", async () => {
  w.location.hash = "#/admin/users";
  await until(() => $$("tr[data-row]").length === 4, 3000, "four users");
  assert.match($('tr[data-row] td').textContent, /admin-person/);
  assert.match($("tr[data-row]").textContent, /\(you\)/);
  const own = () => $(`tr[data-row="${ids.admin}"]`);
  const other = () => $(`tr[data-row="${ids.viewer}"]`);
  assert.equal(own(), $("tr[data-row]"), "your own row comes first");
  assert.equal(own().querySelector('[data-toggle="disable"]'), null, "no Disable on your own row");
  assert.ok(own().querySelector('[data-edit="roles"]'), "your roles can still be changed");

  const disable = other().querySelector('[data-toggle="disable"]');
  assert.deepEqual([...disable.classList].sort(), ["btn", "pb-quiet-bad", "quiet", "sm"], "quiet, red text, not filled");
  disable.click();
  await until(() => other().querySelector('[data-toggle="disable"][data-confirm]'), 2000, "the confirm step");
  assert.equal(calls.filter((c) => c.method === "PATCH").length, 0, "the first click only asks");
  assert.match(other().textContent, /Disable viewer-person and sign them out\?/);
  const yes = other().querySelector('[data-toggle="disable"][data-confirm]');
  assert.ok(yes.classList.contains("danger") && yes.classList.contains("confirm"), "only the confirm step is filled red");
  yes.click();
  await until(() => $(".pb-ok"), 2000, "the confirmation");
  assert.match($(".pb-ok").textContent, /Disabled and signed out\./);
  let patch = calls.filter((c) => c.method === "PATCH").pop();
  assert.equal(patch.path, `/users/${ids.viewer}`);
  assert.deepEqual(patch.body, { disabled: true });
  assert.equal(patch.auth, `Bearer ${login.token}`);

  own().querySelector('[data-edit="roles"]').click();
  await until(() => $('form[data-form="roles"]'), 2000, "the roles form");
  const form = $('form[data-form="roles"]');
  form.querySelector('input[value="admin"]').checked = false;
  form.querySelector('input[value="viewer"]').checked = true;
  submit(form);
  await until(() => $(".apierr"), 2000, "the LAST_ADMIN refusal");
  assert.match($(".apierr").textContent, /This is the last active Admin\./);
  patch = calls.filter((c) => c.method === "PATCH").pop();
  assert.equal(patch.path, `/users/${ids.admin}`);
  assert.deepEqual(patch.body, { roles: ["viewer"] });
  assert.equal(patch.auth, `Bearer ${login.token}`);
  assert.equal($(".pb-ok"), null, "the earlier confirmation is gone");
});

test("adding a user sends the roles ticked and never keeps the password on screen", async () => {
  $("#pb-new-username").value = "new-analyst";
  $("#pb-new-password").value = "a long enough password";
  $('input[name="pb-new-role"][value="analyst"]').checked = true;
  submit($("#pb-create"));
  await until(() => $(".pb-ok"), 2000, "the confirmation");
  const post = calls.filter((c) => c.method === "POST" && c.path === "/users").pop();
  assert.deepEqual(post.body, { username: "new-analyst", password: "a long enough password", roles: ["viewer", "analyst"] });
  assert.match($(".pb-ok").textContent, /new-analyst was added\./);
  assert.equal(w.document.body.innerHTML.includes("a long enough password"), false);
});

test("the audit viewer shows a page, filters it, pages it, and offers the same query as CSV", async () => {
  w.location.hash = "#/admin/audit";
  await until(() => $$("tbody tr").length === 2, 3000, "a page of events");
  const page = fixture("audit_page");
  assert.match($(".pb-pager").textContent, new RegExp(`1–2 of ${page.total}`));
  assert.match($("tbody").textContent, new RegExp(page.events[0].action.replace(".", "\\.")));
  // the row reads plain words; the raw action code is only in the row's Details
  const row = $(`tr[data-event="${page.events[0].event_id}"]`);
  const visible = row.cloneNode(true);
  visible.querySelectorAll("details").forEach((d) => d.remove());
  assert.match(visible.textContent, /Changed a person/);
  assert.equal(visible.textContent.includes(page.events[0].action), false, "no raw code outside Details");
  assert.equal(visible.querySelector(".mono"), null);
  assert.match(row.querySelector("details").textContent, new RegExp(page.events[0].action.replace(".", "\\.")));
  const form = $("#pb-audit-filters");
  form.elements.namedItem("action").value = "users.";
  form.elements.namedItem("outcome").value = "failed";
  form.elements.namedItem("from").value = "2026-09-01";
  form.elements.namedItem("to").value = "2026-09-23";
  submit(form);
  await settle();
  await until(() => $("#pb-audit-csv"), 2000, "the CSV link");
  const query = calls.filter((c) => c.path === "/audit/events").pop().query;
  assert.deepEqual(query, {
    action: "users.",
    outcome: "failed",
    since: "2026-09-01T00:00:00Z",
    until: "2026-09-24T00:00:00Z",
    limit: "50",
    offset: "0",
  });
  const csv = new URL($("#pb-audit-csv").href);
  assert.equal(csv.pathname, "/audit/events.csv");
  assert.deepEqual(Object.fromEntries(csv.searchParams), {
    action: "users.",
    outcome: "failed",
    since: "2026-09-01T00:00:00Z",
    until: "2026-09-24T00:00:00Z",
  });
  $("#pb-audit-next").click();
  await settle();
  assert.equal(calls.filter((c) => c.path === "/audit/events").pop().query.offset, "50");
});

test("a Viewer's AWS connection screen explains, in place, why they cannot test it", async () => {
  tokens["tok-viewer"] = fixture("me_viewer");
  session.storeToken("tok-viewer", null);
  await session.loadMe();
  w.location.hash = "#/generative/connection";
  await until(() => $("#c-test"), 3000, "the connection screen");
  await settle(2);
  assert.equal($("#c-test").disabled, true);
  assert.ok($$(".pb-why").some((n) => n.textContent === "Only an Admin can test the AWS connection."));
  assert.match(bar(), /Signed in as viewer-person/);
  assert.equal($('#pb-bar a[href="#/admin/users"]'), null, "no Users link for a Viewer");
});

test("a Viewer sent to the Users screen reads why, not an empty table", async () => {
  w.location.hash = "#/admin/users";
  await until(() => $(".apierr"), 3000, "the refusal");
  assert.match($(".apierr").textContent, /Only an Admin can see the users\./);
});

test("signing out revokes the session on the server and forgets it here", async () => {
  $("#pb-signout").click();
  await until(() => $("#pb-signin"), 3000, "the sign-in form again");
  const logout = calls.filter((c) => c.path === "/auth/logout").pop();
  assert.equal(logout.auth, "Bearer tok-viewer");
  assert.equal(session.readToken(), null);
  assert.match(bar(), /Not signed in/);
});
