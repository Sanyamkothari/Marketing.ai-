// The AWS connection screen: which identity Bedrock is called as, chosen by name and checked, never
// typed in.
//
// `engine/aws_connection.py` states the design this screen only renders: the browser chooses a
// *source* - the default credential chain, or an AWS CLI profile by name - and the server resolves
// it from `~/.aws/`, the SSO cache or an IAM role. There is no field here for a key or a secret,
// anywhere, on purpose; the explanation the API sends (`AwsConnectionState.explanation`) is shown
// verbatim, plus one line this screen states on its own so a person never has to read the module
// docstring to learn it. Choosing is only possible when `GET /connection/aws` says `editable: true`
// - a local deployment, from the machine that owns the identities - and this screen never second
// guesses that: when it is false, no control that could change the identity is drawn, only the
// reason (`locked_reason`) and a "Test connection" button, because testing is free and safe from
// anywhere while choosing is not (`api/routes/connection.py`).
//
// "Test connection" never invokes a model - `sts:GetCallerIdentity` plus
// `bedrock:GetFoundationModelAvailability` - so it is offered before Save, not after: a person can
// try a profile and see who it resolves to before committing to it. `identity_masked` on the report
// means exactly what it says - this caller could not edit the connection either, so the account and
// principal are withheld - and the screen says so rather than showing blanks that look like a bug.

import { EM_DASH, errorBox, esc, pageHead, present } from "../../dom.js";
import { injectGenerativeStyles } from "./styles.js";
import { deleteAwsConnection, getAwsConnection, postTestAwsConnection, putAwsConnection } from "./api.js";

injectGenerativeStyles();

const SOURCE_LABEL = { default_chain: "Default credentials", profile: "A named profile" };
const LOCK_LABEL = { deployed: "deployed", remote_client: "another machine" };
const ROLE_LABEL = { generation: "Generation", judge: "Judge", embedding: "Embedding" };
const STATUS_LABEL = { available: "Available", unavailable: "Unavailable", unverified: "Unverified" };
const STATUS_CLASS = { available: "ok", unavailable: "bad", unverified: "warn" };
const STATUS_ICON = { available: "✓", unavailable: "✕", unverified: "?" };

function freshState() {
  return {
    data: null, // AwsConnectionState from the last GET/PUT/DELETE
    loadError: null,
    selectedSource: "default_chain",
    selectedProfile: "",
    saving: false,
    saveError: null,
    resetting: false,
    testing: false,
    testError: null,
    testReport: null, // ConnectionReport from the last POST /connection/aws/test
  };
}

/** The choice this screen currently has selected, in the shape `PUT`/`POST` accept. */
function pendingConnection(s) {
  return s.selectedSource === "profile" ? { source: "profile", profile: s.selectedProfile } : { source: "default_chain" };
}

/** True once the selection on screen differs from the connection the server has saved. */
function pendingChanged(s) {
  const saved = s.data.connection;
  const pending = pendingConnection(s);
  return saved.source !== pending.source || (saved.profile || null) !== (pending.profile || null);
}

// --- the choice, and the two actions that change what is saved ----------------------------------

function choiceHtml(s) {
  const profiles = s.data.profiles || [];
  const isProfile = s.selectedSource === "profile";
  // The profile list appears only once "A named profile" is chosen. Shown beside "Default
  // credentials" - even disabled, which these styles do not distinguish - it reads as a choice, and
  // a profile that happens to be called "default" is then easily mistaken for boto3's default chain,
  // which is a different thing entirely.
  const profileControl = !profiles.length
    ? `<p class="fhint">No AWS CLI profile was found on the machine running Marketing AI. Create one with
        <code class="colchip">aws configure --profile NAME</code> or <code class="colchip">aws configure sso</code>,
        then reload this page.</p>`
    : isProfile
      ? `<div class="field" style="width:280px"><span class="sub">Profile</span><div class="control sel"><select id="c-profile" aria-label="AWS CLI profile">${profiles
          .map((p) => `<option value="${esc(p)}"${p === s.selectedProfile ? " selected" : ""}>${esc(p)}</option>`)
          .join("")}</select></div></div>`
      : "";
  return `<form id="c-form" novalidate style="margin-top:14px">
    <div class="frow">
      <label class="check"><input type="radio" name="c-source" value="default_chain"${
        isProfile ? "" : " checked"
      }> Default credentials</label>
      <label class="check"><input type="radio" name="c-source" value="profile"${isProfile ? " checked" : ""}${
        profiles.length ? "" : " disabled"
      }> A named profile</label>
    </div>
    <div style="margin-top:10px">${profileControl}</div>
    ${s.saveError ? errorBox(s.saveError) : ""}
    <div class="actions">
      <button type="submit" class="run" id="c-save"${s.saving ? " disabled" : ""}>${esc(
        s.saving ? "Saving…" : "Save",
      )}</button>
      <button type="button" class="linkbtn" id="c-reset"${s.resetting ? " disabled" : ""}>${esc(
        s.resetting ? "Resetting…" : "Reset to default",
      )}</button>
    </div>
  </form>`;
}

function lockedHtml(s) {
  return `<div class="gnotice"><span><b>Locked (${esc(
    LOCK_LABEL[s.data.locked_reason] || s.data.locked_reason,
  )})</b> ${esc(s.data.explanation)}</span></div>`;
}

function connectionCardHtml(s) {
  const editable = s.data.editable;
  return `<section class="card"><div class="form-body">
    ${editable ? `<p class="desc">${esc(s.data.explanation)}</p>` : lockedHtml(s)}
    <p class="fhint" style="margin-top:10px"><b>Marketing AI never asks for your AWS keys.</b> There is no field
      for one anywhere in this product - a source is chosen by name and the server resolves it locally.</p>
    ${editable ? choiceHtml(s) : ""}
  </div></section>`;
}

// --- the test, and its report ------------------------------------------------------------------

function modelRow(model) {
  const cls = STATUS_CLASS[model.status] || "warn";
  const icon = STATUS_ICON[model.status] || "?";
  return `<div class="runrow"><div><div class="r1"><span class="pill ${cls}">${icon} ${esc(
    STATUS_LABEL[model.status] || model.status,
  )}</span> ${esc(ROLE_LABEL[model.role] || model.role)}</div><div class="r2">${esc(
    model.model_id,
  )}</div></div><div class="r3" style="text-align:left;white-space:normal;max-width:320px">${esc(
    model.detail,
  )}</div></div>`;
}

/** A hint string with `backtick` commands rendered as copyable, monospace chips. */
function hintHtml(hint) {
  if (!present(hint)) return "";
  const parts = String(hint).split(/`([^`]+)`/g);
  const html = parts.map((part, i) => (i % 2 === 1 ? `<code class="colchip">${esc(part)}</code>` : esc(part))).join("");
  return `<p class="fhint" style="margin-top:10px">${html}</p>`;
}

function reportHtml(report) {
  if (!report.ok) {
    return `<div class="apierr" role="alert"><b>${esc(report.error_code || "ERROR")}</b>${esc(
      report.message || "",
    )}</div>${hintHtml(report.hint)}`;
  }
  const rows = (report.models || []).map(modelRow).join("");
  return `<div class="summary"><span class="ok">✓ Connected</span><span class="muted">${esc(
    SOURCE_LABEL[report.source] || report.source,
  )}${present(report.profile) ? ` · ${esc(report.profile)}` : ""} · ${esc(report.region)}</span></div>
    <div class="kv"><span class="k">Principal</span><span class="v">${
      report.identity_masked ? "Masked" : present(report.principal_arn) ? esc(report.principal_arn) : EM_DASH
    }</span></div>
    <div class="kv"><span class="k">Account</span><span class="v">${
      report.identity_masked ? "Masked" : present(report.account_id) ? esc(report.account_id) : EM_DASH
    }</span></div>
    <div class="kv"><span class="k">Credential method</span><span class="v">${
      present(report.credential_method) ? esc(report.credential_method) : EM_DASH
    }</span></div>
    ${
      report.identity_masked
        ? `<p class="fhint">This caller may not edit the connection, so the account and principal are masked.</p>`
        : ""
    }
    <div class="runs-list" style="margin-top:12px;border:1px solid var(--line);border-radius:8px">${
      rows || `<div class="empty">No models are configured to check.</div>`
    }</div>`;
}

function testCardHtml(s) {
  const changed = s.data.editable && pendingChanged(s);
  return `<section class="card"><h3>Test connection</h3><div class="form-body">
    <p class="fhint" style="margin-top:0">Free: checks who these credentials are and whether each configured model
      is enabled. No model is ever called and nothing is spent.</p>
    <div class="actions" style="border-top:0;margin-top:0;padding-top:0">
      <button type="button" class="run" id="c-test"${s.testing ? " disabled" : ""}>${esc(
        s.testing ? "Testing…" : "Test connection",
      )}</button>
      ${changed ? `<span class="reason">Testing the selection above - not what is saved yet.</span>` : ""}
    </div>
    ${s.testError ? errorBox(s.testError) : ""}
    ${s.testReport ? `<div style="margin-top:14px">${reportHtml(s.testReport)}</div>` : ""}
  </div></section>`;
}

// --- shell ---------------------------------------------------------------------------------------

export function connectionHtml(s) {
  const body = s.data
    ? `<div class="stack">${connectionCardHtml(s)}${testCardHtml(s)}</div>`
    : s.loadError
      ? errorBox(s.loadError)
      : `<p class="loading">Loading the AWS connection…</p>`;
  return `<main class="screen">
    ${pageHead(
      `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">AWS connection</h1><p class="desc">Which AWS identity Bedrock is called as.</p>`,
    )}
    ${body}
  </main>`;
}

// --- controller ------------------------------------------------------------------------------

export function createConnectionController(rerender) {
  const s = freshState();

  async function load() {
    s.loadError = null;
    try {
      s.data = await getAwsConnection();
    } catch (error) {
      s.loadError = error;
      return;
    }
    s.selectedSource = s.data.connection.source;
    s.selectedProfile = s.data.connection.profile || (s.data.profiles || [])[0] || "";
    s.testReport = null;
    s.testError = null;
  }

  async function save(event) {
    event.preventDefault();
    if (s.saving) return;
    s.saving = true;
    s.saveError = null;
    rerender();
    try {
      s.data = await putAwsConnection(pendingConnection(s));
      s.selectedSource = s.data.connection.source;
      s.selectedProfile = s.data.connection.profile || s.selectedProfile;
    } catch (error) {
      s.saveError = error;
    }
    s.saving = false;
    rerender();
  }

  async function reset() {
    if (s.resetting) return;
    s.resetting = true;
    s.saveError = null;
    rerender();
    try {
      s.data = await deleteAwsConnection();
      s.selectedSource = s.data.connection.source;
      s.selectedProfile = s.data.connection.profile || (s.data.profiles || [])[0] || "";
    } catch (error) {
      s.saveError = error;
    }
    s.resetting = false;
    rerender();
  }

  async function test() {
    if (s.testing) return;
    s.testing = true;
    s.testError = null;
    s.testReport = null;
    rerender();
    const body = s.data.editable && pendingChanged(s) ? { connection: pendingConnection(s) } : {};
    try {
      s.testReport = await postTestAwsConnection(body);
    } catch (error) {
      s.testError = error;
    }
    s.testing = false;
    rerender();
  }

  function bind(root) {
    root.querySelectorAll('input[name="c-source"]').forEach((input) =>
      input.addEventListener("change", () => {
        s.selectedSource = input.value;
        rerender();
      }),
    );
    const profileSelect = root.querySelector("#c-profile");
    if (profileSelect) {
      profileSelect.addEventListener("change", (event) => {
        s.selectedProfile = event.target.value;
      });
    }
    const form = root.querySelector("#c-form");
    if (form) form.addEventListener("submit", save);
    const resetButton = root.querySelector("#c-reset");
    if (resetButton) resetButton.addEventListener("click", () => reset());
    const testButton = root.querySelector("#c-test");
    if (testButton) testButton.addEventListener("click", () => test());
  }

  return { state: s, load, bind };
}
