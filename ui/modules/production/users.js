// The Users screen (Phase 4b M46, Admin): list, add, change roles, disable or enable, set a password.
//
// Everything on it is a call to `api/routes/auth.py`, and every rule it shows is the server's:
// * Roles are a set (DEC-703). Viewer is implied by any role, so the checkboxes list all four and the
//   server adds Viewer; Admin does not imply Approver or Analyst, and the hint under the checkboxes
//   says so, because "the Admin cannot approve" otherwise reads like a bug.
// * The last enabled Admin cannot be disabled or demoted (`409 LAST_ADMIN`, DEC-712); the server's
//   sentence is shown in place, under the row that was being changed.
// * Changing someone's roles, disabling them or setting their password signs them out everywhere
//   (DEC-711). When the person changed is the one signed in, `GET /auth/me` is asked again at once, so
//   a removed role disappears from the screen, or - when the session was revoked - the browser goes
//   straight to the sign-in form (DEC-791) rather than failing on the next click.
//
// Passwords are read from their inputs on submit and never kept in state or echoed back; setting
// your own password here needs your current one, exactly as `POST /users/{id}/password` requires.

import { EM_DASH, errorBox, esc, fmtStamp, pageHead } from "../../dom.js";
import { getUsers, patchUser, postPassword, postUser } from "./api.js";
import { can, currentMe, loadMe, reasonFor } from "./session.js";
import { ROLE_LABEL, roleChips } from "./userbar.js";

export const ROLES = ["viewer", "analyst", "approver", "admin"];

export const ROLE_HINT = {
  viewer: "see every result",
  analyst: "upload, build, train, score and generate",
  approver: "approve champions and campaign copy",
  admin: "users, the audit log and settings",
};

const state = {
  users: null,
  loadError: null,
  createError: null,
  creating: false,
  created: null, // username of the last user added, for the confirmation line
  editing: null, // { userId, mode: "roles" | "password" }
  rowError: null, // { userId, error }
  rowNotice: null, // { userId, text }
  busy: false,
};

export const tabsHtml = (current) =>
  `<div class="tabs-bar" style="margin-bottom:8px"><div class="tabs">${[
    ["users", "Users", "#/admin/users"],
    ["audit", "Audit log", "#/admin/audit"],
  ]
    .map(([key, label, href]) => `<a class="tab${key === current ? " on" : ""}" href="${href}">${label}</a>`)
    .join("")}</div></div>`;

export const adminHead = (title, desc) =>
  pageHead(
    `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">${esc(title)}</h1><p class="desc">${desc}</p>`,
  );

function roleBoxes(name, checked) {
  return `<div class="pb-roles" role="group" aria-label="Roles">${ROLES.map(
    (role) =>
      `<label title="${esc(ROLE_HINT[role])}"><input type="checkbox" name="${esc(name)}" value="${role}"${
        checked.includes(role) ? " checked" : ""
      }>${esc(ROLE_LABEL[role])}</label>`,
  ).join("")}</div><p class="fhint" style="margin:6px 0 0">Viewer is implied by any role. Admin does not include Approver or Analyst: grant them separately.</p>`;
}

function createCard() {
  const refused = reasonFor("POST", "/users");
  if (refused) return `<section class="card"><h3>Add a user</h3><p class="empty">${esc(refused)}</p></section>`;
  return `<section class="card"><h3>Add a user</h3><div class="form-body">
    <form id="pb-create" class="pb-form wide" novalidate autocomplete="off">
      <div class="frow">
        <label class="pb-field field"><span class="sub">Username</span><input class="pb-input" id="pb-new-username" autocapitalize="none" spellcheck="false" required></label>
        <label class="pb-field field"><span class="sub">Display name (optional)</span><input class="pb-input" id="pb-new-display"></label>
        <label class="pb-field field"><span class="sub">First password · 12+ characters</span><input class="pb-input" id="pb-new-password" type="password" autocomplete="new-password" required></label>
      </div>
      ${roleBoxes("pb-new-role", ["viewer"])}
      <div class="actions"><button type="submit" class="run" id="pb-create-submit"${state.creating ? " disabled" : ""}>${
        state.creating ? "Adding…" : "Add user"
      }</button><span class="reason">Give the password to the person another way; they can change it after signing in.</span></div>
    </form>
    ${state.createError ? errorBox(state.createError) : ""}
    ${state.created ? `<div class="pb-ok" role="status">${esc(state.created)} was added.</div>` : ""}
  </div></section>`;
}

function editPanel(user, me) {
  const editing = state.editing;
  if (!editing || editing.userId !== user.user_id) return "";
  const own = me && me.principal.user_id === user.user_id;
  const body =
    editing.mode === "roles"
      ? `<form class="pb-form wide" data-form="roles" data-user="${esc(user.user_id)}">${roleBoxes(
          "pb-edit-role",
          user.roles,
        )}<div class="pb-row-actions"><button type="submit" class="run" data-save>Save roles</button><button type="button" class="linkbtn" data-cancel>Cancel</button>${
          own ? `<span class="reason">Changing your own roles signs you out.</span>` : ""
        }</div></form>`
      : `<form class="pb-form wide" data-form="password" data-user="${esc(user.user_id)}" autocomplete="off"><div class="frow">${
          own
            ? `<label class="pb-field field"><span class="sub">Your current password</span><input class="pb-input" data-current type="password" autocomplete="current-password"></label>`
            : ""
        }<label class="pb-field field"><span class="sub">New password · 12+ characters</span><input class="pb-input" data-password type="password" autocomplete="new-password"></label></div><div class="pb-row-actions"><button type="submit" class="run" data-save>Set password</button><button type="button" class="linkbtn" data-cancel>Cancel</button><span class="reason">${
          own ? "This signs you out everywhere." : "This signs them out everywhere."
        }</span></div></form>`;
  return `<tr class="pb-edit-row"><td colspan="6" class="pb-edit">${body}</td></tr>`;
}

function rowMessage(user) {
  const error = state.rowError && state.rowError.userId === user.user_id ? state.rowError.error : null;
  const notice = state.rowNotice && state.rowNotice.userId === user.user_id ? state.rowNotice.text : null;
  if (!error && !notice) return "";
  return `<tr><td colspan="6">${error ? errorBox(error) : `<div class="pb-ok" role="status">${esc(notice)}</div>`}</td></tr>`;
}

function userRow(user, me) {
  const own = me && me.principal.user_id === user.user_id;
  const mayChange = can("PATCH", "/users/{user_id}");
  const actions = mayChange
    ? `<div class="pb-row-actions"><button type="button" class="linkbtn" data-edit="roles" data-user="${esc(
        user.user_id,
      )}">Change roles</button><button type="button" class="linkbtn" data-toggle="${
        user.disabled ? "enable" : "disable"
      }" data-user="${esc(user.user_id)}">${user.disabled ? "Enable" : "Disable"}</button><button type="button" class="linkbtn" data-edit="password" data-user="${esc(
        user.user_id,
      )}">Set password</button></div>`
    : `<span class="pb-small">${esc(reasonFor("PATCH", "/users/{user_id}") || EM_DASH)}</span>`;
  return `<tr data-row="${esc(user.user_id)}">
    <td>${esc(user.username)}${own ? ` <span class="pb-small">(you)</span>` : ""}</td>
    <td>${esc(user.display_name || EM_DASH)}</td>
    <td>${roleChips(user.roles)}</td>
    <td>${user.disabled ? `<span class="pill bad">Disabled</span>` : `<span class="pill ok">Active</span>`}</td>
    <td>${esc(fmtStamp(user.created_at))}<div class="pb-small">by ${esc(user.created_by)}</div></td>
    <td>${actions}</td>
  </tr>${editPanel(user, me)}${rowMessage(user)}`;
}

function listCard(me) {
  if (state.loadError) return `<section class="card"><h3>People</h3>${errorBox(state.loadError)}</section>`;
  if (!state.users) return `<section class="card"><h3>People</h3><p class="loading" style="padding:16px 20px">Loading…</p></section>`;
  if (!state.users.length) {
    return `<section class="card"><h3>People</h3><p class="empty">Nobody has an account yet. Add the first user above, or run <code class="colchip">python -m scripts.create_user</code> on the server.</p></section>`;
  }
  return `<section class="card"><h3>People · ${state.users.length}</h3><div class="tbl-wrap"><table>
    <thead><tr><th>Username</th><th>Name</th><th>Roles</th><th>Status</th><th>Added</th><th></th></tr></thead>
    <tbody>${state.users.map((user) => userRow(user, me)).join("")}</tbody></table></div></section>`;
}

export function usersHtml() {
  const me = currentMe();
  const refused = reasonFor("GET", "/users");
  const head = adminHead(
    "Users",
    "Who can sign in and what each may do. Every change here is written to the audit log.",
  );
  if (refused) return `<main class="screen">${head}${tabsHtml("users")}<div class="apierr" role="alert"><b>ROLE_REQUIRED</b>${esc(refused)}</div></main>`;
  return `<main class="screen">${head}${tabsHtml("users")}<div class="stack">${createCard()}${listCard(me)}</div></main>`;
}

/** Load the list. Keeps the previous list on screen while it reloads. */
export async function loadUsers() {
  try {
    const body = await getUsers();
    state.users = body.users || [];
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
}

function selectedRoles(form, name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

/** After a change to the signed-in person: re-read `/auth/me` - a revoked session goes to sign-in. */
async function afterChange(userId) {
  const me = currentMe();
  if (me && me.principal.user_id === userId) await loadMe();
  await loadUsers();
}

async function run(userId, action, repaint, notice) {
  state.busy = true;
  state.rowError = null;
  state.rowNotice = null;
  try {
    await action();
    state.editing = null;
    state.rowNotice = notice ? { userId, text: notice } : null;
    await afterChange(userId);
  } catch (error) {
    state.rowError = { userId, error };
  }
  state.busy = false;
  repaint();
}

export function bindUsers(root, repaint) {
  const create = root.querySelector("#pb-create");
  if (create) {
    create.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = {
        username: create.querySelector("#pb-new-username").value.trim(),
        password: create.querySelector("#pb-new-password").value,
        roles: selectedRoles(create, "pb-new-role"),
      };
      const display = create.querySelector("#pb-new-display").value.trim();
      if (display) payload.display_name = display;
      state.creating = true;
      state.createError = null;
      state.created = null;
      repaint();
      try {
        const user = await postUser(payload);
        state.created = user.username;
        await loadUsers();
      } catch (error) {
        state.createError = error;
      }
      state.creating = false;
      repaint();
    });
  }
  // Delegated from `<main>`, which every paint replaces, so listeners never pile up on `#app`.
  const main = root.querySelector("main");
  if (!main) return;
  main.addEventListener("click", (event) => {
    const target = event.target;
    if (!target || !target.closest) return;
    const edit = target.closest("[data-edit]");
    if (edit) {
      state.editing = { userId: edit.dataset.user, mode: edit.dataset.edit };
      state.rowError = null;
      state.rowNotice = null;
      repaint();
      return;
    }
    if (target.closest("[data-cancel]")) {
      state.editing = null;
      repaint();
      return;
    }
    const toggle = target.closest("[data-toggle]");
    if (toggle && !state.busy) {
      const userId = toggle.dataset.user;
      const disabled = toggle.dataset.toggle === "disable";
      run(userId, () => patchUser(userId, { disabled }), repaint, disabled ? "Disabled and signed out." : "Enabled.");
    }
  });
  main.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || !form.dataset || !form.dataset.form) return;
    event.preventDefault();
    if (state.busy) return;
    const userId = form.dataset.user;
    if (form.dataset.form === "roles") {
      const roles = selectedRoles(form, "pb-edit-role");
      run(userId, () => patchUser(userId, { roles }), repaint, "Roles saved; their sessions were signed out.");
    } else {
      const payload = { password: form.querySelector("[data-password]").value };
      const current = form.querySelector("[data-current]");
      if (current) payload.current_password = current.value;
      run(userId, () => postPassword(userId, payload), repaint, "Password set; their sessions were signed out.");
    }
  });
}

/** Test seam: forget screen state between cases. */
export function _resetUsersForTests() {
  Object.assign(state, {
    users: null,
    loadError: null,
    createError: null,
    creating: false,
    created: null,
    editing: null,
    rowError: null,
    rowNotice: null,
    busy: false,
  });
}
