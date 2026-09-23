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

import { errorBox, esc, pageHead } from "../../dom.js";
import { postLogin, postPassword } from "./api.js";
import { adoptSignIn, currentMe, ensureMe, forgetSignIn, sessionStatus, SIGNIN_ROUTE } from "./session.js";
import { roleChips } from "./userbar.js";

const state = { username: "", error: null, busy: false, notice: null };
const account = { error: null, busy: false };

const back = `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a>`;

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

export function signInHtml(parts) {
  const status = sessionStatus();
  const me = currentMe();
  if (status === "off") {
    return `<main class="screen">${pageHead(
      `${back}<h1 class="h1">Sign in</h1><p class="desc">Access control is off on this deployment, so there is nothing to sign in to: everyone acts as the local operator, with every role. An operator turns it on with <code class="colchip">MARKETING_AI_AUTH_MODE=local</code>.</p>`,
    )}</main>`;
  }
  if (status === "signed-in" && me) {
    return `<main class="screen">${pageHead(
      `${back}<h1 class="h1">You are signed in</h1><p class="desc">As <b>${esc(
        me.principal.display_name || me.principal.username,
      )}</b> ${roleChips(me.principal.roles)}</p>`,
    )}<p><a class="linkbtn" href="${esc(nextRoute(parts))}">Continue</a></p></main>`;
  }
  return `<main class="screen">${pageHead(
    `${back}<h1 class="h1">Sign in</h1><p class="desc">Sign in to see results and to do what your role allows. Ask an Admin if you need an account or a new password.</p>`,
  )}<section class="card pb-signin"><h3>Your account</h3><div class="form-body">
    ${state.notice ? `<div class="pb-ok" role="status">${esc(state.notice)}</div>` : ""}
    <form id="pb-signin" class="pb-form" novalidate>
      <label class="pb-field"><span class="sub">Username</span><input class="pb-input" id="pb-username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required value="${esc(
        state.username,
      )}"></label>
      <label class="pb-field"><span class="sub">Password</span><input class="pb-input" id="pb-password" name="password" type="password" autocomplete="current-password" required></label>
      <div class="actions"><button type="submit" class="run" id="pb-signin-submit"${state.busy ? " disabled" : ""}>${
        state.busy ? "Signing in…" : "Sign in"
      }</button></div>
    </form>
    ${state.error ? errorBox(state.error) : ""}
  </div></section></main>`;
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
      state.error = { code: "MISSING", message: "Give both your username and your password." };
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
  if (status !== "signed-in" || !me || me.principal.kind !== "user") {
    return `<main class="screen">${pageHead(
      `${back}<h1 class="h1">Change password</h1><p class="desc">${
        status === "off"
          ? "Access control is off on this deployment, so there is no account to change."
          : "Sign in first to change your password."
      }</p>`,
    )}</main>`;
  }
  return `<main class="screen">${pageHead(
    `${back}<h1 class="h1">Change password</h1><p class="desc">For <b>${esc(
      me.principal.username,
    )}</b>. Changing it signs you out everywhere, this tab included; sign in again with the new one.</p>`,
  )}<section class="card pb-signin"><h3>New password</h3><div class="form-body">
    <form id="pb-account" class="pb-form" novalidate>
      <label class="pb-field"><span class="sub">Current password</span><input class="pb-input" id="pb-current" type="password" autocomplete="current-password" required></label>
      <label class="pb-field"><span class="sub">New password · at least 12 characters</span><input class="pb-input" id="pb-new" type="password" autocomplete="new-password" minlength="12" required></label>
      <label class="pb-field"><span class="sub">New password, again</span><input class="pb-input" id="pb-again" type="password" autocomplete="new-password" required></label>
      <div class="actions"><button type="submit" class="run" id="pb-account-submit"${account.busy ? " disabled" : ""}>${
        account.busy ? "Saving…" : "Change password"
      }</button></div>
    </form>
    ${account.error ? errorBox(account.error) : ""}
  </div></section></main>`;
}

export function bindAccount(root, repaint) {
  const form = root.querySelector("#pb-account");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = form.querySelector("#pb-current").value;
    const next = form.querySelector("#pb-new").value;
    const again = form.querySelector("#pb-again").value;
    if (next !== again) {
      account.error = { code: "MISMATCH", message: "The two new passwords are not the same." };
      repaint();
      return;
    }
    account.busy = true;
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
