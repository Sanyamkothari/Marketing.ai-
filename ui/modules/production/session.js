// Who is signed in, and how their sign-in reaches every call the UI makes (Phase 4b M46).
//
// The token. `POST /auth/login` answers a bearer token once (DEC-711); it is kept in
// `sessionStorage`, not `localStorage`, so it lives exactly as long as the tab: closing the tab signs
// the browser out even if nobody pressed "Sign out", and a second tab signs in on its own. It is never
// put in a URL, a cookie or a log line. Nothing else about the person is stored - who they are and
// what they may do is asked of `GET /auth/me` on every page load, so a role an Admin removed a minute
// ago is gone on the next reload rather than cached in the browser.
//
// How it reaches every call (DEC-790). Three request wrappers already exist - `ui/api.js` (Phase 1),
// `ui/modules/onboarding/api.js` and `ui/modules/generative/api.js` - and each calls the global
// `fetch` directly, by design (none may import another's private helpers). Teaching each of them
// about a token would be three edits to three other workstreams' files, and a fourth wrapper written
// next year would silently forget it. Instead `installFetch` wraps `window.fetch` once: a request to
// the API origin gets `Authorization: Bearer <token>` when a token is held, and a request anywhere
// else (a font, a CDN) is passed through untouched so the token never leaves for another origin.
// With no token - which is every request when `auth_mode=off` - the call is byte-for-byte the call
// the screen made, which is how the UI "works unchanged" with access control off.
//
// The wrapper is installed from `ui/modules/router.js`'s Phase 4b block, through `boot.js`, because
// `app.js` imports that file before its own first render runs: installed any later (from the
// `<script>` tag in `index.html`), the very first `GET /industries` would already have left without
// the header.
//
// When a sign-in is needed (DEC-791). Any API answer `401 AUTH_REQUIRED` - no token, an expired one, a
// session revoked because the person's roles or password changed - clears the token and sends the
// browser to `#/signin/<where it was>`. The screen that made the call still receives its 401 and
// renders its own error box as always; the hash change then replaces it with the sign-in form. A
// `401` with any other code (a wrong password on the sign-in form itself) is left to its screen.

import { API_BASE } from "../../api.js";

export const TOKEN_KEY = "marketing-ai.auth";
export const SIGNIN_ROUTE = "signin";

const INSTALLED = Symbol.for("marketing-ai.production.fetch");

let me = null; // the last `MeResponse` from `GET /auth/me`, or null
let status = "unknown"; // unknown | off | signed-in | signed-out | absent | unavailable
let inflight = null; // the `GET /auth/me` on its way, so two screens asking share one call
let problem = null; // the server's sentence when status is `misconfigured`
const listeners = new Set();

function storage() {
  try {
    return window.sessionStorage;
  } catch {
    return null; // a sandboxed frame or a privacy mode that refuses storage: no sign-in survives a reload
  }
}

/** The held token, or null. An expired one is dropped here so it is never sent. */
export function readToken() {
  const store = storage();
  if (!store) return null;
  let saved = null;
  try {
    saved = JSON.parse(store.getItem(TOKEN_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!saved || typeof saved.token !== "string" || !saved.token) return null;
  if (saved.expires_at && Date.parse(saved.expires_at) <= Date.now()) {
    clearToken();
    return null;
  }
  return saved.token;
}

export function storeToken(token, expiresAt) {
  const store = storage();
  if (store) store.setItem(TOKEN_KEY, JSON.stringify({ token, expires_at: expiresAt || null }));
}

export function clearToken() {
  const store = storage();
  if (store) store.removeItem(TOKEN_KEY);
}

/** True when `target` is a URL on the API this UI talks to - the only place a token may be sent. */
export function isApiUrl(target) {
  let absolute;
  try {
    absolute = new URL(target, window.location.href).href;
  } catch {
    return false;
  }
  return absolute === API_BASE || absolute.startsWith(`${API_BASE}/`);
}

/** The API path of an absolute or relative URL (`/auth/login`), without its query. */
export function apiPath(target) {
  try {
    const absolute = new URL(target, window.location.href);
    const base = new URL(API_BASE);
    const prefix = base.pathname.replace(/\/+$/, "");
    return absolute.pathname.slice(prefix.length) || "/";
  } catch {
    return "";
  }
}

function targetOf(input) {
  if (typeof input === "string") return input;
  if (input && typeof input.href === "string") return input.href; // a URL object
  if (input && typeof input.url === "string") return input.url; // a Request
  return String(input);
}

async function errorCode(response) {
  try {
    const body = await response.clone().json();
    return (body && body.detail && body.detail.code) || null;
  } catch {
    return null;
  }
}

/** The hash the sign-in screen should return to afterwards: where the browser is now. */
export function currentRoute() {
  return window.location.hash.replace(/^#\/?/, "");
}

export function signInHash(next) {
  const target = next === undefined ? currentRoute() : next;
  return target && !target.startsWith(SIGNIN_ROUTE)
    ? `#/${SIGNIN_ROUTE}/${encodeURIComponent(target)}`
    : `#/${SIGNIN_ROUTE}`;
}

/** Send the browser to the sign-in screen, remembering where it was. A no-op when already there. */
export function goToSignIn() {
  if (currentRoute().split("/")[0] === SIGNIN_ROUTE) return;
  window.location.hash = signInHash();
}

/**
 * Wrap `window.fetch` so the token reaches every API call (DEC-790) and a lapsed sign-in sends the
 * browser to the sign-in screen (DEC-791). Idempotent: installing twice wraps once.
 */
export function installFetch(win = window) {
  if (win.fetch && win.fetch[INSTALLED]) return;
  const original = win.fetch.bind(win);
  const wrapped = async function fetchWithSignIn(input, init) {
    const target = targetOf(input);
    if (!isApiUrl(target)) return original(input, init);
    const token = readToken();
    let response;
    if (token) {
      const headers = new Headers((init && init.headers) || (input && input.headers) || undefined);
      if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
      response = await original(input, { ...(init || {}), headers });
    } else {
      response = await original(input, init);
    }
    if (response.status === 401 && apiPath(target) !== "/auth/login") {
      if ((await errorCode(response)) === "AUTH_REQUIRED") {
        clearToken();
        setState(null, "signed-out");
        goToSignIn();
      }
    }
    return response;
  };
  wrapped[INSTALLED] = true;
  win.fetch = wrapped;
}

// --- who is signed in ---------------------------------------------------------------------------

function setState(nextMe, nextStatus) {
  me = nextMe;
  status = nextStatus;
  for (const listener of listeners) {
    try {
      listener(me, status);
    } catch {
      // one broken listener must not stop the others from hearing about a sign-out
    }
  }
}

/** Call `listener(me, status)` now and after every change. Returns the unsubscribe function. */
export function onSession(listener) {
  listeners.add(listener);
  listener(me, status);
  return () => listeners.delete(listener);
}

export const currentMe = () => me;
export const sessionStatus = () => status;
export const sessionProblem = () => problem;

/**
 * Ask the API who this browser is. `off` and `signed-in` both carry a permission list; `absent`
 * means the API has no `/auth/me` at all (a build before Phase 4b), and the UI then behaves exactly
 * as it did before this module existed; `unavailable` is a network failure, which changes nothing.
 * `misconfigured` is a production deployment with sign-in off, which the API refuses to serve at
 * all (`503 AUTH_NOT_CONFIGURED`, DEC-702): the bar says so, in the server's words.
 */
export function loadMe() {
  inflight = askMe().finally(() => {
    inflight = null;
  });
  return inflight;
}

/** `loadMe`, unless an answer is already in hand or on its way (the boot call usually is). */
export function ensureMe() {
  if (inflight) return inflight;
  return status === "unknown" ? loadMe() : Promise.resolve(me);
}

async function askMe() {
  let response;
  try {
    response = await window.fetch(`${API_BASE}/auth/me`);
  } catch {
    setState(null, "unavailable");
    return me;
  }
  if (response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (body && body.principal) setState(body, body.auth_mode === "off" ? "off" : "signed-in");
    else setState(null, "unavailable");
  } else if (response.status === 401) {
    setState(null, "signed-out");
  } else if (response.status === 404) {
    setState(null, "absent");
  } else if (response.status === 503 && (await errorCode(response)) === "AUTH_NOT_CONFIGURED") {
    try {
      problem = (await response.json()).detail.message;
    } catch {
      problem = null;
    }
    setState(null, "misconfigured");
  } else {
    setState(null, "unavailable");
  }
  return me;
}

/** After a successful `POST /auth/login`: keep the token, then learn the permissions it carries. */
export async function adoptSignIn(login) {
  storeToken(login.token, login.expires_at);
  return loadMe();
}

/** Forget the sign-in locally. The caller has already asked the API to revoke it (or it lapsed). */
export function forgetSignIn() {
  clearToken();
  setState(null, "signed-out");
}

// --- what the signed-in person may do ------------------------------------------------------------

/** The `PermissionView` of one route, or null when `/auth/me` did not list it (or nobody is known). */
export function permissionFor(method, path, source = me) {
  if (!source || !Array.isArray(source.permissions)) return null;
  const upper = method.toUpperCase();
  return source.permissions.find((p) => p.method === upper && p.path === path) || null;
}

/**
 * Whether the UI should offer an action. Deliberately permissive when it does not know: with no
 * `/auth/me` answer, or a route `/auth/me` did not list, the control stays as its screen drew it and
 * the server remains the one that refuses (DEC-792) - hiding on a guess would be a second, weaker
 * access model that could disagree with the real one.
 */
export function can(method, path, source = me) {
  const permission = permissionFor(method, path, source);
  return permission ? Boolean(permission.allowed) : true;
}

/** "Only an Approver can approve a champion." - the server's own sentence, or null when allowed. */
export function reasonFor(method, path, source = me) {
  const permission = permissionFor(method, path, source);
  return permission && !permission.allowed ? permission.reason || "Your role cannot do this." : null;
}

/** Test seam: reset module state between cases. Not used by any screen. */
export function _resetForTests() {
  me = null;
  status = "unknown";
  inflight = null;
  problem = null;
  listeners.clear();
}
