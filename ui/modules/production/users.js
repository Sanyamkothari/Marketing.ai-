// The Users screen (Phase 4b M46, Admin): list, add, change roles, disable or enable, set a password.
//
// Everything on it is a call to `api/routes/auth.py`, and every rule it shows is the server's:
// * Roles are a set (DEC-703). Viewer is implied by any role, so the checkboxes list all four and the
//   server adds Viewer; Admin does not imply Approver or Analyst, and the line under the checkboxes
//   says so, because "the Admin cannot approve" otherwise reads like a bug.
// * The last enabled Admin cannot be disabled or demoted (`409 LAST_ADMIN`, DEC-712); the server's
//   sentence is shown in place, under the row that was being changed.
// * Changing someone's roles, disabling them or setting their password signs them out everywhere
//   (DEC-711). When the person changed is the one signed in, `GET /auth/me` is asked again at once, so
//   a removed role disappears from the screen, or - when the session was revoked - the browser goes
//   straight to the sign-in form (DEC-791) rather than failing on the next click.
//
// v1: the list comes first; "Add user" is the one primary action and opens the form (at once when
// nobody has an account). Disabling someone is set apart in red and asks twice. "Added" names who
// added the person, never their id; the bootstrap account reads "Set up at install".
//
// Passwords are read from their inputs on submit and never kept in state or echoed back; setting
// your own password here needs your current one, exactly as `POST /users/{id}/password` requires.

import { EM_DASH, errorBox, esc, fmtDate, headActions, noticeCard } from "../../dom.js";
import { getUsers, patchUser, postPassword, postUser } from "./api.js";
import { dangerConfirm, personName, refusal, rememberPeople, rowsTable, screenHead, spanRow } from "./controls.js";
import { can, currentMe, loadMe, reasonFor, sessionStatus } from "./session.js";
import { ROLE_LABEL, roleChips } from "./userbar.js";

export const ROLES = ["viewer", "analyst", "approver", "admin"];

export const ROLE_HINT = {
  viewer: "Sees every result and report.",
  analyst: "Uploads data, trains and scores models, and writes campaign copy.",
  approver: "Approves new models and campaign copy.",
  admin: "Manages people, the audit log, privacy and settings.",
};

const COLUMNS = 5;

const state = {
  users: null,
  loadError: null,
  adding: false, // the Add user form is open
  createError: null,
  creating: false,
  created: null, // username of the last user added, for the confirmation line
  editing: null, // { userId, mode: "roles" | "password" }
  confirmDisable: null, // user id awaiting "Yes, disable"
  rowError: null, // { userId, error }
  rowNotice: null, // { userId, text }
  busy: false,
};

/** The Admin screens' header: Home › Admin › <title>. */
export const adminHead = (title, desc, actions = "") =>
  screenHead({ trail: [{ label: "Admin", href: "#/admin/users" }], title, desc, actions });

/** Kept for callers of the earlier tab strip; the top bar's Admin menu now links the Admin screens. */
export const tabsHtml = () => "";

function roleBoxes(name, checked) {
  return `<fieldset class="pb-fieldset"><legend class="sub">Roles</legend><div class="pb-roles" role="group" aria-label="Roles">${ROLES.map(
    (role) =>
      `<label><input type="checkbox" name="${esc(name)}" value="${role}"${checked.includes(role) ? " checked" : ""}><span>${esc(
        ROLE_LABEL[role],
      )}</span><span class="pb-hint">${esc(ROLE_HINT[role])}</span></label>`,
  ).join("")}</div><p class="pb-hint">Everyone can see results. Admin does not include Approver or Analyst: tick them separately.</p></fieldset>`;
}

const input = (id, label, { type = "text", attrs = "", hint = "" } = {}) =>
  `<label class="field pb-field"><span class="sub">${esc(label)}</span><span class="control"><input id="${id}" type="${type}" ${attrs}></span>${
    hint ? `<span class="pb-hint">${esc(hint)}</span>` : ""
  }</label>`;

function addCard() {
  const refused = reasonFor("POST", "/users");
  const open = state.adding || (state.users && !state.users.length);
  const body = refused
    ? `<p class="pb-note">${esc(refused)}</p>`
    : `<form id="pb-create" class="pb-stack" novalidate autocomplete="off">
      <div class="frow">
        ${input("pb-new-username", "Username", { attrs: 'autocapitalize="none" spellcheck="false" required' })}
        ${input("pb-new-display", "Display name (optional)")}
        ${input("pb-new-password", "First password", {
          type: "password",
          attrs: 'autocomplete="new-password" required',
          hint: "At least 12 characters.",
        })}
      </div>
      ${roleBoxes("pb-new-role", ["viewer"])}
      <div class="pb-form-actions"><button type="submit" class="btn primary" id="pb-create-submit"${state.creating ? " disabled" : ""}>${
        state.creating ? "Adding…" : "Add user"
      }</button><button type="button" class="btn quiet" data-add-close>Cancel</button><span class="reason">Give the password to the person another way; they can change it after signing in.</span></div>
    </form>
    ${state.createError ? errorBox(state.createError) : ""}`;
  return `<section class="card" id="pb-add"${open ? "" : " hidden"}><h3>Add a user</h3><div class="card-body">${body}</div></section>`;
}

function editPanel(user, me) {
  const editing = state.editing;
  if (!editing || editing.userId !== user.user_id) return "";
  const own = me && me.principal.user_id === user.user_id;
  const body =
    editing.mode === "roles"
      ? `<form class="pb-stack" data-form="roles" data-user="${esc(user.user_id)}">${roleBoxes(
          "pb-edit-role",
          user.roles,
        )}<div class="pb-row-actions"><button type="submit" class="btn primary sm" data-save>Save roles</button><button type="button" class="btn quiet sm" data-cancel>Cancel</button>${
          own ? `<span class="reason">Changing your own roles signs you out.</span>` : ""
        }</div></form>`
      : `<form class="pb-stack" data-form="password" data-user="${esc(user.user_id)}" autocomplete="off"><div class="frow">${
          own
            ? `<label class="field pb-field"><span class="sub">Your current password</span><span class="control"><input data-current type="password" autocomplete="current-password"></span></label>`
            : ""
        }<label class="field pb-field"><span class="sub">New password</span><span class="control"><input data-password type="password" autocomplete="new-password"></span><span class="pb-hint">At least 12 characters.</span></label></div><div class="pb-row-actions"><button type="submit" class="btn primary sm" data-save>Set password</button><button type="button" class="btn quiet sm" data-cancel>Cancel</button><span class="reason">${
          own ? "This signs you out everywhere." : "This signs them out everywhere."
        }</span></div></form>`;
  return `<tr class="pb-edit-row"><td colspan="${COLUMNS}" class="pb-edit">${body}</td></tr>`;
}

function rowMessage(user) {
  const error = state.rowError && state.rowError.userId === user.user_id ? state.rowError.error : null;
  const notice = state.rowNotice && state.rowNotice.userId === user.user_id ? state.rowNotice.text : null;
  if (!error && !notice) return "";
  return spanRow(COLUMNS, error ? errorBox(error) : `<div class="pb-ok" role="status">${esc(notice)}</div>`);
}

function actionsFor(user) {
  if (!can("PATCH", "/users/{user_id}")) {
    return `<span class="pb-small">${esc(reasonFor("PATCH", "/users/{user_id}") || EM_DASH)}</span>`;
  }
  const id = esc(user.user_id);
  if (state.confirmDisable === user.user_id) {
    return `<div class="pb-row-actions">${dangerConfirm(
      `Disable ${user.username} and sign them out?`,
      `<button type="button" class="btn danger confirm sm" data-toggle="disable" data-confirm data-user="${id}">Yes, disable</button>`,
      "data-toggle-keep",
    )}</div>`;
  }
  const toggle = user.disabled
    ? `<button type="button" class="btn secondary sm" data-toggle="enable" data-user="${id}">Enable</button>`
    : `<button type="button" class="btn danger sm" data-toggle="disable" data-user="${id}">Disable</button>`;
  return `<div class="pb-row-actions"><button type="button" class="btn quiet sm" data-edit="roles" data-user="${id}">Change roles</button><button type="button" class="btn quiet sm" data-edit="password" data-user="${id}">Set password</button><span class="spacer"></span>${toggle}</div>`;
}

/** "24 Sept 2026 by admin-person", or "Set up at install" for the account the installer made. */
function addedText(user) {
  if (user.created_by === "system:bootstrap") return "Set up at install";
  const when = fmtDate(user.created_at);
  const who = user.created_by ? personName(user.created_by) : null;
  return who ? `${when} by ${who}` : when;
}

function userRow(user, me) {
  const own = me && me.principal.user_id === user.user_id;
  const name = user.display_name && user.display_name !== user.username ? `<div class="pb-small">${esc(user.display_name)}</div>` : "";
  return {
    attrs: `data-row="${esc(user.user_id)}"`,
    cells: [
      `${esc(user.username)}${own ? ` <span class="pb-small">(you)</span>` : ""}${name}`,
      roleChips(user.roles),
      user.disabled
        ? `<span class="pill bad" data-status="disabled">Disabled</span>`
        : `<span class="pill ok" data-status="active">Active</span>`,
      esc(addedText(user)),
      actionsFor(user),
    ],
    after: `${editPanel(user, me)}${rowMessage(user)}`,
  };
}

/** Active people first, then by username. */
const byStatusThenName = (a, b) =>
  Number(Boolean(a.disabled)) - Number(Boolean(b.disabled)) || String(a.username).localeCompare(String(b.username));

function listCard(me) {
  if (state.loadError) {
    return `<section class="card"><h3>People</h3><div class="card-body">${errorBox(state.loadError, {
      title: "We could not load the people list.",
      retry: true,
    })}</div></section>`;
  }
  if (!state.users) return `<section class="card"><h3>People</h3><p class="loading pb-pad">Loading…</p></section>`;
  const created = state.created ? `<div class="card-body"><div class="pb-ok" role="status">${esc(state.created)} was added.</div></div>` : "";
  if (!state.users.length) {
    return `<section class="card"><h3>People</h3><div class="empty-state"><p class="es-t">Nobody has an account yet.</p><p>Add the first person with the form below. Everyone you add signs in with their own username and password.</p></div>
      <div class="card-body"><details class="tech"><summary>For administrators</summary><p>An account can also be created on the server with <code>python -m scripts.create_user</code>.</p></details></div></section>`;
  }
  const rows = [...state.users].sort(byStatusThenName).map((user) => userRow(user, me));
  return `<section class="card"><h3>People · ${state.users.length} <span class="sort-note">(active first, then by name)</span></h3>${created}${rowsTable(
    [{ label: "Person" }, { label: "Roles" }, { label: "Status" }, { label: "Added" }, { label: "", sr: "Actions" }],
    rows,
    { cls: "pb-users" },
  )}</section>`;
}

function signInOffNotice() {
  if (sessionStatus() !== "off") return "";
  return noticeCard({
    title: "Sign-in is turned off",
    text: "Everyone uses Marketing AI without an account on this installation, so the people below cannot sign in yet. An administrator can turn sign-in on.",
  });
}

export function usersHtml() {
  const me = currentMe();
  const refused = reasonFor("GET", "/users");
  const mayAdd = !refused && !reasonFor("POST", "/users");
  const head = adminHead(
    "Users",
    "Who can sign in and what each person may do. Every change here is written to the audit log.",
    refused
      ? ""
      : headActions({
          primary: mayAdd
            ? {
                label: "Add user",
                id: "pb-add-open",
                kind: state.adding || (state.users && !state.users.length) ? "secondary" : "primary",
                attrs: `aria-controls="pb-add" aria-expanded="${state.adding}"`,
              }
            : null,
        }),
  );
  if (refused) return `<main class="screen pb-screen">${head}${refusal(refused)}</main>`;
  return `<main class="screen pb-screen">${head}<div class="stack">${signInOffNotice()}${listCard(me)}${addCard()}</div></main>`;
}

/** Load the list. Keeps the previous list on screen while it reloads. */
export async function loadUsers() {
  try {
    const body = await getUsers();
    state.users = body.users || [];
    rememberPeople(state.users);
    state.loadError = null;
  } catch (error) {
    state.loadError = error;
  }
}

function selectedRoles(form, name) {
  return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((field) => field.value);
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
  state.confirmDisable = null;
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
        state.adding = false;
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
    if (target.closest("#pb-add-open")) {
      state.adding = !state.adding;
      state.createError = null;
      repaint();
      if (state.adding) {
        const field = document.getElementById("pb-new-username");
        if (field) field.focus();
      }
      return;
    }
    if (target.closest("[data-add-close]")) {
      state.adding = false;
      repaint();
      return;
    }
    const edit = target.closest("[data-edit]");
    if (edit) {
      state.editing = { userId: edit.dataset.user, mode: edit.dataset.edit };
      state.confirmDisable = null;
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
    if (target.closest("[data-toggle-keep]")) {
      state.confirmDisable = null;
      repaint();
      return;
    }
    const toggle = target.closest("[data-toggle]");
    if (toggle && !state.busy) {
      const userId = toggle.dataset.user;
      const disabled = toggle.dataset.toggle === "disable";
      if (disabled && !("confirm" in toggle.dataset)) {
        // the first click only asks: disabling signs the person out everywhere
        state.confirmDisable = userId;
        state.editing = null;
        state.rowError = null;
        state.rowNotice = null;
        repaint();
        return;
      }
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
    adding: false,
    createError: null,
    creating: false,
    created: null,
    editing: null,
    confirmDisable: null,
    rowError: null,
    rowNotice: null,
    busy: false,
  });
}
