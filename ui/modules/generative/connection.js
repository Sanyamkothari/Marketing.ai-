// The AI service connection screen: which identity the AI service (Bedrock) is called as, chosen by
// name and checked, never typed in.
//
// `engine/aws_connection.py` states the design this screen only renders: the browser chooses a
// *source* - the default credential chain, or an AWS CLI profile by name - and the server resolves
// it from `~/.aws/`, the SSO cache or an IAM role. There is no field here for a key or a secret,
// anywhere, on purpose; the subtitle says so once, so a person never has to read the module docstring
// to learn it. Choosing is only possible when `GET /connection/aws` says `editable: true` - a local
// deployment, from the machine that owns the identities - and this screen never second guesses
// that: when it is false, no control that could change the identity is drawn, only a calm notice (the
// API's `locked_reason` and `explanation` under Technical details) and a "Test connection" button,
// because testing is free and safe from anywhere while choosing is not (`api/routes/connection.py`).
//
// "Test connection" never invokes a model - `sts:GetCallerIdentity` plus
// `bedrock:GetFoundationModelAvailability` - so it is the screen's primary action, offered before
// Save: a person can try a profile and see who it resolves to before committing to it. The report
// leads with a plain verdict ("Connected", "2 of 3 AI models available"); the identity and the model
// ids sit under Technical details. `identity_masked` on the report means exactly what it says - this
// caller could not edit the connection either, so the account and principal are withheld - and the
// screen says so rather than showing blanks that look like a bug.
//
// v1 UI: plain words first, AWS vocabulary only under "Technical details".

import { EM_DASH, crumbs, errorBox, esc, pageHead, present } from "../../dom.js";
import { injectGenerativeStyles } from "./styles.js";
import { deleteAwsConnection, getAwsConnection, postTestAwsConnection, putAwsConnection } from "./api.js";

injectGenerativeStyles();

const SOURCE_LABEL = { default_chain: "The server's own access", profile: "A named profile on the server" };
const LOCK_LABEL = { deployed: "set on the server", remote_client: "only changeable on the server's own machine" };
const ROLE_LABEL = { generation: "Writing", judge: "Checking", embedding: "Searching documents" };
const STATUS_LABEL = { available: "Available", unavailable: "Not available", unverified: "Not checked" };
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

/** Monospace rows under one closed "Technical details": label, then an already-escaped value. */
function techRows(rows, summary = "Technical details") {
  const body = rows
    .filter(([, html]) => html)
    .map(([label, html]) => `<div><dt>${esc(label)}</dt><dd><span class="mono">${html}</span></dd></div>`)
    .join("");
  return body ? `<details class="tech"><summary>${esc(summary)}</summary><dl class="tech-list">${body}</dl></details>` : "";
}

/** A hint string with `backtick` commands rendered as monospace chips. */
function hintHtml(hint) {
  if (!present(hint)) return "";
  const parts = String(hint).split(/`([^`]+)`/g);
  return parts.map((part, i) => (i % 2 === 1 ? `<code class="colchip">${esc(part)}</code>` : esc(part))).join("");
}

// --- the choice, and the two actions that change what is saved ----------------------------------

function choiceHtml(s) {
  const profiles = s.data.profiles || [];
  const isProfile = s.selectedSource === "profile";
  const changed = pendingChanged(s);
  // The profile list appears only once "A named profile" is chosen. Shown beside the default - even
  // disabled - it reads as a choice, and a profile that happens to be called "default" is then easily
  // mistaken for the default chain, which is a different thing entirely.
  const profileControl = !profiles.length
    ? `<p class="fhint">No named profile was found on the server. Your IT team can create one; then reload this page.</p>`
    : isProfile
      ? `<div class="field gprofile"><span class="sub">Profile</span><div class="control sel"><select id="c-profile" aria-label="Profile">${profiles
          .map((p) => `<option value="${esc(p)}"${p === s.selectedProfile ? " selected" : ""}>${esc(p)}</option>`)
          .join("")}</select></div></div>`
      : "";
  return `<form id="c-form" novalidate>
    <fieldset class="gsource"><legend class="flabel">Reach the AI service with</legend>
      <label class="check"><input type="radio" name="c-source" value="default_chain"${isProfile ? "" : " checked"}><span>${esc(
        SOURCE_LABEL.default_chain,
      )}<small>Recommended. Uses whatever access your IT team set up for the server.</small></span></label>
      <label class="check"><input type="radio" name="c-source" value="profile"${isProfile ? " checked" : ""}${
        profiles.length ? "" : " disabled"
      }><span>${esc(SOURCE_LABEL.profile)}<small>For a server that can reach more than one account.</small></span></label>
      ${profileControl}
    </fieldset>
    ${s.saveError ? errorBox(s.saveError) : ""}
    <div class="gsave-row">
      <button type="submit" class="btn secondary" id="c-save"${s.saving || !changed ? " disabled" : ""}>${esc(
        s.saving ? "Saving…" : "Save",
      )}</button>
      <span class="reason">${esc(changed ? "Not saved yet." : "")}</span>
      <span class="spacer"></span>
      <button type="button" class="btn quiet" id="c-reset"${s.resetting ? " disabled" : ""}>${esc(
        s.resetting ? "Resetting…" : "Reset to default",
      )}</button>
    </div>
  </form>`;
}

function savedTech(s) {
  const saved = s.data.connection;
  return techRows([
    ["Credential source", esc(saved.source)],
    ["AWS CLI profile", present(saved.profile) ? esc(saved.profile) : ""],
    ["Locked", s.data.editable ? "" : esc(s.data.locked_reason)],
    ["How it is resolved", esc(s.data.explanation)],
    [
      "Create a profile",
      (s.data.profiles || []).length ? "" : hintHtml("`aws configure --profile NAME` or `aws configure sso`"),
    ],
  ]);
}

function connectionCardHtml(s) {
  if (!s.data.editable) {
    return `<section class="card notice-card" role="status" data-locked><h3>This is managed by your IT team on the server.</h3><p>The way Marketing AI reaches the AI service is ${esc(
      LOCK_LABEL[s.data.locked_reason] || "set on the server",
    )}, so it cannot be changed from this screen. You can still test it below.</p><div class="card-body">${savedTech(
      s,
    )}</div></section>`;
  }
  return `<section class="card"><h3>Where the AI service is reached from</h3><div class="card-body">
    ${choiceHtml(s)}
    ${savedTech(s)}
  </div></section>`;
}

// --- the test, and its report ------------------------------------------------------------------

function modelRow(model) {
  const cls = STATUS_CLASS[model.status] || "warn";
  const icon = STATUS_ICON[model.status] || "?";
  return `<div class="runrow"><div><div class="r1"><span class="pill ${cls}">${icon} ${esc(
    STATUS_LABEL[model.status] || model.status,
  )}</span> ${esc(ROLE_LABEL[model.role] || model.role)}</div><div class="r2 mono">${esc(
    model.model_id,
  )}</div></div><div class="r3">${esc(model.detail)}</div></div>`;
}

function reportHtml(report) {
  if (!report.ok) {
    return `<div class="apierr" role="alert" data-code="${esc(report.error_code || "ERROR")}"><b>The connection test did not succeed.</b>${
      present(report.hint) ? `<p class="apierr-fix">For your IT team: ${hintHtml(report.hint)}</p>` : ""
    }<details class="tech"><summary>Details</summary><p class="mono"><code>${esc(
      report.error_code || "ERROR",
    )}</code> ${esc(report.message || "")}</p></details></div>`;
  }
  const models = report.models || [];
  const available = models.filter((m) => m.status === "available").length;
  const rows = models.map(modelRow).join("");
  const masked = "Hidden: only an Admin sees this";
  return `<div class="greport" role="status">
    <p class="gverdict"><span class="ok">✓</span> Connected</p>
    <p class="gverdict-sub">${
      models.length
        ? `${esc(String(available))} of ${esc(String(models.length))} AI models available.`
        : "No AI models are configured to check."
    }</p>
    ${techRows([
      ["Credential source", esc(SOURCE_LABEL[report.source] || report.source)],
      ["AWS CLI profile", present(report.profile) ? esc(report.profile) : ""],
      ["Region", esc(report.region)],
      [
        "Principal",
        report.identity_masked ? masked : present(report.principal_arn) ? esc(report.principal_arn) : EM_DASH,
      ],
      ["Account", report.identity_masked ? masked : present(report.account_id) ? esc(report.account_id) : EM_DASH],
      ["Credential method", present(report.credential_method) ? esc(report.credential_method) : EM_DASH],
    ])}
    ${rows ? `<details class="tech"><summary>AI models checked</summary><div class="runs-list gmodels">${rows}</div></details>` : ""}
  </div>`;
}

function testCardHtml(s) {
  const changed = s.data.editable && pendingChanged(s);
  return `<section class="card"><h3>Check the connection</h3><div class="card-body">
    <p class="fhint">Free: checks that Marketing AI can reach the AI service and which AI models are switched on. No text is written and nothing is spent.</p>
    <div class="gtest-row">
      <button type="button" class="btn primary" id="c-test"${s.testing ? " disabled" : ""}>${esc(
        s.testing ? "Testing…" : "Test connection",
      )}</button>
      ${changed ? `<span class="reason">Tests the choice above, which is not saved yet.</span>` : ""}
    </div>
    ${s.testError ? errorBox(s.testError) : ""}
    ${s.testReport ? reportHtml(s.testReport) : ""}
  </div></section>`;
}

// --- shell ---------------------------------------------------------------------------------------

export function connectionHtml(s) {
  const body = s.data
    ? `<div class="stack">${connectionCardHtml(s)}${testCardHtml(s)}</div>`
    : s.loadError
      ? errorBox(s.loadError, { retry: true })
      : `<div class="stack"><span class="skel skel-card"></span></div>`;
  return `<main class="screen gscreen">
    ${pageHead(
      `${crumbs([{ label: "AI service connection" }])}<h1 class="h1">AI service connection</h1><p class="desc">Lets Marketing AI write text (assistant answers, root-cause notes, campaign copy) using your company's AI service. No sign-in details or keys are typed here.</p>`,
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
    if (s.saving || !pendingChanged(s)) return;
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
        rerender();
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
