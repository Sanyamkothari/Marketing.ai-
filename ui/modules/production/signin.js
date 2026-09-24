// The sign-in screen and the "change your own password" screen (Phase 4b M46).
//
// Sign-in is shown when the API says a sign-in is needed: `GET /auth/me` - or any other call - answers
// `401 AUTH_REQUIRED` with `auth_mode=local` (DEC-791), and the browser is sent to
// `#/signin/<where it was>`. After `POST /auth/login` succeeds the token is kept for the tab
// (`session.js`) and the browser goes back to where it was. A wrong password, an unknown username
// and a disabled account all read the same server sentence (DEC-720); this screen adds nothing that
// could tell them apart.
//
// Neither password is ever put in screen state, a URL or a re-render: each is read from its input
// only when the form is submitted, and a failed attempt clears the password field. Only the
// username survives a failed attempt, so a person does not have to retype it.
//
// With `auth_mode=off` there is nothing to sign in to (`POST /auth/login` answers `409 AUTH_OFF`),
// so the screen says so instead of offering a form that cannot work.

import { errorBox, esc, pageHead, techDetails } from "../../dom.js";
import { postLogin, postPassword } from "./api.js";
import { adoptSignIn, currentMe, ensureMe, forgetSignIn, sessionStatus, SIGNIN_ROUTE } from "./session.js";
import { mainRole } from "./userbar.js";

const state = { username: "", error: null, busy: false, notice: null };
const account = { error: null, mismatch: false, busy: false };

/** The minimum length `POST /users/{id}/password` accepts (the same 12 the Users screen asks for). */
export const MIN_PASSWORD = 12;

/** A screen of its own, centred: the header (breadcrumb-free, logo on the right), then one 440px column. */
const centred = (head, body = "") =>
  `<main class="screen">${pageHead("")}<div class="pb-center">${head}${body}</div></main>`;

/** Where to go after signing in: the route the browser was sent away from, or the overview. */
export function nextRoute(parts) {
  const raw = parts && parts[0] === SIGNIN_ROUTE && parts[1] ? parts.slice(1).join("/") : "";
  let decoded = "";
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    decoded = "";
  }
  // Only ever a hash inside this UI: a crafted link cannot send a fresh sign-in anywhere else.
  if (!decoded || decoded.startsWith(SIGNIN_ROUTE) || /^[a-z]+:|^\/\//i.test(decoded)) return "#/";
  return `#/${decoded.replace(/^#?\/?/, "")}`;
}

/** Leave a message for the sign-in screen to show once (e.g. "Your password was changed"). */
export function noticeForSignIn(text) {
  state.notice = text;
}

/** The sign-in-off answer, shared by the sign-in and the password screens. */
const offBody = (lead) =>
  `<section class="card notice-card" role="status"><h3>Sign-in is turned off</h3><p>${esc(
    lead,
  )}</p><div class="notice-actions"><a class="btn primary" href="#/">Go to Home</a></div><div class="card-body">${techDetails(
    [["Turn sign-in on", "MARKETING_AI_AUTH_MODE=local"]],
    "For administrators",
  )}</div></section>`;

export function signInHtml(parts) {
  const status = sessionStatus();
  const me = currentMe();
  if (status === "off") {
    return centred(
      `<h1 class="h1">Sign in</h1>`,
      offBody("Sign-in is turned off on this installation. You can use everything without an account."),
    );
  }
  if (status === "signed-in" && me) {
    const who = me.principal.display_name || me.principal.username;
    const role = mainRole(me.principal.roles);
    return centred(
      `<h1 class="h1">You are signed in</h1><p class="desc">As <b>${esc(who)}</b>${role ? ` (${esc(role)})` : ""}.</p>`,
      `<div class="btn-row next"><a class="btn primary" href="${esc(nextRoute(parts))}">Continue</a></div>`,
    );
  }
  const missing = state.error && state.error.code === "MISSING";
  const refused = state.error && !missing;
  return centred(
    `<h1 class="h1">Sign in</h1><p class="desc">Sign in to see results and to do what your role allows. Ask an Admin if you need an account or a new password.</p>`,
    `<section class="card pb-signin"><div class="card-body">
    ${state.notice ? `<p class="field-ok" role="status">${esc(state.notice)}</p>` : ""}
    <form id="pb-signin" class="pb-form" novalidate>
      <label class="pb-field"><span class="sub">Username</span><span class="control"><input id="pb-username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required value="${esc(
        state.username,
      )}"${missing ? ` aria-describedby="pb-signin-err"` : ""}></span></label>
      <label class="pb-field"><span class="sub">Password</span><span class="control"><input id="pb-password" name="password" type="password" autocomplete="current-password" required${
        state.error ? ` aria-describedby="pb-signin-err" aria-invalid="true"` : ""
      }></span></label>
      ${missing ? `<p class="field-err" id="pb-signin-err" role="alert">${esc(state.error.message)}</p>` : ""}
      ${refused ? `<div id="pb-signin-err">${errorBox(state.error)}</div>` : ""}
      <div class="actions"><button type="submit" class="btn primary run" id="pb-signin-submit"${state.busy ? " disabled" : ""}>${
        state.busy ? "Signing in…" : "Sign in"
      }</button></div>
    </form>
  </div></section>`,
  );
}

export function bindSignIn(root, parts, repaint) {
  const form = root.querySelector("#pb-signin");
  if (!form) return;
  const focus = root.querySelector(state.username ? "#pb-password" : "#pb-username");
  if (focus) focus.focus();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = form.querySelector("#pb-username").value.trim();
    const password = form.querySelector("#pb-password").value;
    state.username = username;
    if (!username || !password) {
      state.error = { code: "MISSING", message: "Enter both your username and your password." };
      repaint();
      return;
    }
    state.busy = true;
    state.error = null;
    repaint();
    try {
      const login = await postLogin(username, password);
      await adoptSignIn(login);
      state.busy = false;
      state.notice = null;
      state.username = "";
      window.location.hash = nextRoute(parts);
      // Start the page again as the person who just signed in: the client list, the demo status and
      // the help catalogue were read at load, before there was a token, and would otherwise keep
      // their signed-out answers (DEC-955). The token is in session storage and survives the reload.
      reloadAfterSignIn();
    } catch (error) {
      state.busy = false;
      state.error = error;
      repaint();
    }
  });
}

// --- your own password ---------------------------------------------------------------------------

export function accountHtml() {
  const status = sessionStatus();
  const me = currentMe();
  if (status === "off") {
    return centred(
      `<h1 class="h1">Change your password</h1>`,
      offBody("Sign-in is turned off on this installation, so there is no account or password to change."),
    );
  }
  if (status !== "signed-in" || !me || me.principal.kind !== "user") {
    return centred(
      `<h1 class="h1">Change your password</h1><p class="desc">Sign in first to change your password.</p>`,
      `<div class="btn-row next"><a class="btn primary" href="#/signin/account">Sign in</a></div>`,
    );
  }
  return centred(
    `<h1 class="h1">Change your password</h1><p class="desc">Changing it signs you out everywhere, this tab included. Then sign in again with the new one.</p>`,
    `<section class="card pb-signin"><div class="card-body">
    <form id="pb-account" class="pb-form" novalidate>
      <label class="pb-field"><span class="sub">Current password</span><span class="control"><input id="pb-current" type="password" autocomplete="current-password" required></span></label>
      <label class="pb-field"><span class="sub">New password</span><span class="control"><input id="pb-new" type="password" autocomplete="new-password" minlength="${MIN_PASSWORD}" required aria-describedby="pb-new-len"></span><span class="field-help" id="pb-new-len" data-length-check>At least ${MIN_PASSWORD} characters</span></label>
      <label class="pb-field"><span class="sub">New password, again</span><span class="control"><input id="pb-again" type="password" autocomplete="new-password" required${
        account.mismatch ? ` aria-describedby="pb-again-err" aria-invalid="true"` : ""
      }></span>${
        account.mismatch
          ? `<span class="field-err" id="pb-again-err" role="alert">The two new passwords are not the same.</span>`
          : ""
      }</label>
      ${account.error ? errorBox(account.error) : ""}
      <div class="actions"><button type="submit" class="btn primary run" id="pb-account-submit"${account.busy ? " disabled" : ""}>${
        account.busy ? "Saving…" : "Change password"
      }</button></div>
    </form>
  </div></section>`,
  );
}

/** The live "12+ characters" line under the new password: muted until it is met, then confirmed. */
export function lengthCheck(value) {
  const length = String(value || "").length;
  if (length >= MIN_PASSWORD) return { ok: true, text: `At least ${MIN_PASSWORD} characters: done` };
  const left = MIN_PASSWORD - length;
  return {
    ok: false,
    text: length
      ? `At least ${MIN_PASSWORD} characters: ${left} more to go`
      : `At least ${MIN_PASSWORD} characters`,
  };
}

export function bindAccount(root, repaint) {
  const form = root.querySelector("#pb-account");
  if (!form) return;
  const fresh = form.querySelector("#pb-new");
  const hint = form.querySelector("[data-length-check]");
  if (fresh && hint) {
    fresh.addEventListener("input", () => {
      const check = lengthCheck(fresh.value);
      hint.textContent = check.text;
      hint.className = check.ok ? "field-ok" : "field-help";
    });
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = form.querySelector("#pb-current").value;
    const next = form.querySelector("#pb-new").value;
    const again = form.querySelector("#pb-again").value;
    if (next !== again) {
      account.mismatch = true;
      account.error = null;
      repaint();
      return;
    }
    account.busy = true;
    account.mismatch = false;
    account.error = null;
    repaint();
    try {
      await postPassword(currentMe().principal.user_id, { password: next, current_password: current });
      account.busy = false;
      // The server has revoked every session of this person, this one included (DEC-711).
      forgetSignIn();
      noticeForSignIn("Your password was changed. Sign in with the new one.");
      window.location.hash = "#/signin";
    } catch (error) {
      account.busy = false;
      account.error = error;
      repaint();
    }
  });
}

/** Both screens wait for the boot-time `GET /auth/me`, so they never flash the wrong state. */
export const ready = () => ensureMe();

/** A full reload, where the browser offers one (jsdom does not; there nothing was loaded before). */
function reloadAfterSignIn() {
  try {
    if (!/jsdom/i.test(window.navigator.userAgent || "")) window.location.reload();
  } catch {
    // no reload: the screens repaint on the route change as before
  }
}
