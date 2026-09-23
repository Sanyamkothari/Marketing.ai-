/* DEC-790/791/793: the bearer token reaches every API call - including the ones Phase 1 and Phase 3a
   make through their own request wrappers - never leaves for another origin, and a 401
   AUTH_REQUIRED sends the browser to the sign-in screen. With no token (auth_mode=off) every call is
   exactly the call the screen made. */
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { fixture, installPage, settle } from "./harness.mjs";

let answer = () => ({ status: 200, body: {} });
const page = installPage((request) => answer(request), { hash: "#/uc/some-use-case" });
const { w, calls } = page;

const session = await import("../../../../ui/modules/production/session.js");
const phase1 = await import("../../../../ui/api.js");
const generative = await import("../../../../ui/modules/generative/api.js");
const downloads = await import("../../../../ui/modules/production/downloads.js");
session.installFetch(w);
downloads.installDownloads(w.document);

beforeEach(() => {
  calls.length = 0;
  session._resetForTests();
  session.clearToken();
  answer = () => ({ status: 200, body: {} });
  w.location.hash = "#/uc/some-use-case";
});

test("with no token a call is sent exactly as the screen made it", async () => {
  await phase1.getIndustries();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/industries");
  assert.equal(calls[0].auth, null);
});

test("a held token reaches Phase 1's and Phase 3a's calls alike", async () => {
  session.storeToken("tok-123", new Date(Date.now() + 60_000).toISOString());
  await phase1.getIndustries();
  await phase1.postRun({ use_case: "x" });
  await generative.getAwsConnection();
  assert.deepEqual(
    calls.map((c) => [c.method, c.path, c.auth]),
    [
      ["GET", "/industries", "Bearer tok-123"],
      ["POST", "/runs", "Bearer tok-123"],
      ["GET", "/connection/aws", "Bearer tok-123"],
    ],
  );
  // The screen's own headers survive next to the token.
  assert.equal(calls[1].body.use_case, "x");
});

test("the token never goes to another origin", async () => {
  session.storeToken("tok-123", null);
  await w.fetch("https://fonts.example.org/x.css");
  await w.fetch("http://localhost.evil.example/runs");
  await w.fetch("http://localhost/runs");
  assert.deepEqual(
    calls.map((c) => c.auth),
    [null, null, "Bearer tok-123"],
  );
});

test("an expired token is dropped rather than sent", async () => {
  session.storeToken("old", new Date(Date.now() - 1000).toISOString());
  assert.equal(session.readToken(), null);
  await phase1.getIndustries();
  assert.equal(calls[0].auth, null);
});

test("401 AUTH_REQUIRED clears the token and goes to sign-in, remembering where it was", async () => {
  session.storeToken("revoked", null);
  answer = () => ({ status: 401, body: { detail: { code: "AUTH_REQUIRED", message: "Sign in to do this." } } });
  await assert.rejects(phase1.getIndustries(), (error) => error.code === "AUTH_REQUIRED");
  assert.equal(session.readToken(), null);
  assert.equal(w.location.hash, "#/signin/uc%2Fsome-use-case");
  assert.equal(session.sessionStatus(), "signed-out");
});

test("a wrong password on the sign-in form is left to that form", async () => {
  answer = () => ({ status: 401, body: fixture("login_refused") });
  const { postLogin } = await import("../../../../ui/modules/production/api.js");
  await assert.rejects(postLogin("someone", "wrong"), (error) => error.code === "BAD_CREDENTIALS");
  assert.equal(w.location.hash, "#/uc/some-use-case");
});

test("a 403 is a screen's business, not a sign-in", async () => {
  session.storeToken("tok", null);
  answer = () => ({ status: 403, body: { detail: { code: "ROLE_REQUIRED", message: "Only an Analyst can start a run." } } });
  await assert.rejects(phase1.postRun({}), (error) => error.message === "Only an Analyst can start a run.");
  assert.equal(session.readToken(), "tok");
  assert.equal(w.location.hash, "#/uc/some-use-case");
});

test("GET /auth/me decides the status: signed in, off, absent", async () => {
  answer = () => ({ status: 200, body: fixture("me_analyst") });
  await session.loadMe();
  assert.equal(session.sessionStatus(), "signed-in");
  assert.equal(session.can("POST", "/runs"), true);
  assert.equal(session.can("POST", "/models/{model_id}/approve"), false);
  assert.equal(session.reasonFor("POST", "/models/{model_id}/approve"), "Only an Approver can approve a champion.");
  answer = () => ({ status: 200, body: fixture("me_off") });
  await session.loadMe();
  assert.equal(session.sessionStatus(), "off");
  assert.equal(session.reasonFor("POST", "/models/{model_id}/approve"), null);
  answer = () => ({ status: 404, body: { detail: "Not Found" } });
  await session.loadMe();
  assert.equal(session.sessionStatus(), "absent");
  assert.equal(session.can("POST", "/runs"), true, "no /auth/me: nothing is hidden on a guess");
});

test("a download link fetches with the token and saves under the server's file name", async () => {
  session.storeToken("tok-dl", null);
  answer = () => ({
    status: 200,
    body: "customer_id,score\n",
    headers: { "Content-Disposition": 'attachment; filename="scores_r1.csv"' },
  });
  const saved = [];
  const realClick = w.HTMLAnchorElement.prototype.click;
  w.HTMLAnchorElement.prototype.click = function click() {
    if (this.href.startsWith("blob:")) saved.push(this.download);
    else realClick.call(this);
  };
  try {
    w.document.body.insertAdjacentHTML("beforeend", `<a id="dl" href="http://localhost/runs/r1/scores.csv">Download</a>`);
    w.document.getElementById("dl").dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    await settle();
    assert.deepEqual(calls.map((c) => [c.path, c.auth]), [["/runs/r1/scores.csv", "Bearer tok-dl"]]);
    assert.deepEqual(saved, ["scores_r1.csv"]);
  } finally {
    w.HTMLAnchorElement.prototype.click = realClick;
    w.document.getElementById("dl").remove();
  }
});

test("a refused download is explained beside its link", async () => {
  session.storeToken("tok-dl", null);
  answer = () => ({ status: 403, body: { detail: { code: "ROLE_REQUIRED", message: "Only an Admin can download the audit log." } } });
  w.document.body.insertAdjacentHTML("beforeend", `<p><a id="dl2" href="http://localhost/audit/events.csv">CSV</a></p>`);
  const link = w.document.getElementById("dl2");
  link.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  await settle();
  assert.match(link.nextElementSibling.textContent, /Only an Admin can download the audit log\./);
  link.parentElement.remove();
});

test("with no token a download link is left to the browser", async () => {
  w.document.body.insertAdjacentHTML("beforeend", `<a id="dl3" href="http://localhost/runs/r1/scores.csv">Download</a>`);
  const event = new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
  // Stop jsdom from trying to navigate; what matters is whether our listener prevented the default.
  w.document.addEventListener("click", (e) => { if (!e.defaultPrevented) { e.preventDefault(); e.__leftAlone = true; } }, { once: true });
  w.document.getElementById("dl3").dispatchEvent(event);
  await settle();
  assert.equal(event.__leftAlone, true);
  assert.equal(calls.length, 0);
  w.document.getElementById("dl3").remove();
});

test("file names come from Content-Disposition, plain or RFC 5987", () => {
  assert.equal(downloads.filenameFrom('attachment; filename="a b.csv"'), "a b.csv");
  assert.equal(downloads.filenameFrom("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.csv"), "résumé.csv");
  assert.equal(downloads.filenameFrom(null), null);
});

test("after signing in the browser returns to where it was, and only ever inside this UI", async () => {
  const { nextRoute } = await import("../../../../ui/modules/production/signin.js");
  assert.equal(nextRoute(["signin", "uc%2Fsome-use-case%2Frun%2Fr1"]), "#/uc/some-use-case/run/r1");
  assert.equal(nextRoute(["signin"]), "#/");
  assert.equal(nextRoute(["signin", "signin"]), "#/");
  assert.equal(nextRoute(["signin", encodeURIComponent("https://evil.example/")]), "#/");
  assert.equal(nextRoute(["signin", encodeURIComponent("//evil.example/")]), "#/");
  assert.equal(nextRoute(["signin", "%E0%A4%A"]), "#/", "a malformed escape is not an error");
});

test("a production deployment with sign-in off is named in the bar, in the server's words", async () => {
  const message = "Sign-in is off on a production deployment; set MARKETING_AI_AUTH_MODE=local.";
  answer = () => ({ status: 503, body: { detail: { code: "AUTH_NOT_CONFIGURED", message, path: "MARKETING_AI_AUTH_MODE" } } });
  await session.loadMe();
  assert.equal(session.sessionStatus(), "misconfigured");
  const { userBarHtml } = await import("../../../../ui/modules/production/userbar.js");
  const html = userBarHtml(session.currentMe(), session.sessionStatus());
  assert.match(html, /Sign-in is not configured/);
  assert.ok(html.includes(message));
});
