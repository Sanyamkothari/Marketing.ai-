/* A fake API for the M48/M49 screens, answering from a route table with REAL bodies (PB_FIXTURES,
   written by test_production_ops_ui_js.py), behind the same sign-in rule as `signedInServer`: no
   known bearer token, `401 AUTH_REQUIRED`. Also records every multipart body, which `harness.mjs`
   does not (it records JSON bodies only), and every file the page saved. */
import { installPage, signedInServer } from "../harness.mjs";

export const TOKEN_KEY = "marketing-ai.auth";

/** `/schedules/{id}/fire` → a regex with one named group per `{name}`, anchored at both ends. */
function compile(pattern) {
  const source = pattern.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{(\w+)\}/g, "(?<$1>[^/]+)");
  return new RegExp(`^${source}$`);
}

export const ok = (body) => ({ status: 200, body });
export const created = (body) => ({ status: 201, body });
export const refused = (status, body) => ({ status, body });

/**
 * Install the page with the fake API and a signed-in token already in the tab. `table` rows are
 * `[method, pattern, handler(request, params)]`; the first match answers.
 */
export function installOps({ tokens, token, table, hash }) {
  const rows = table.map(([method, pattern, handler]) => ({ method, regex: compile(pattern), handler }));
  const routes = new Proxy(
    {},
    {
      get(_target, key) {
        const [method, path] = String(key).split(" ");
        for (const row of rows) {
          const match = row.method === method && row.regex.exec(path || "");
          if (match) return (request) => row.handler(request, { ...match.groups });
        }
        return undefined;
      },
    },
  );
  const page = installPage(signedInServer({ tokens, routes }), { hash });
  const { w } = page;
  if (token) w.sessionStorage.setItem(TOKEN_KEY, JSON.stringify({ token, expires_at: null }));
  const forms = [];
  const inner = w.fetch;
  w.fetch = async (input, init = {}) => {
    if (init && init.body instanceof FormData) forms.push({ url: String(input), form: init.body });
    return inner(input, init);
  };
  const saved = [];
  URL.createObjectURL = (blob) => {
    saved.push(blob);
    return "blob:saved";
  };
  URL.revokeObjectURL = () => {};
  return { ...page, forms, saved };
}

/** Submit a form the way a person does: a cancelable, bubbling submit event. */
export const submit = (w, form) => form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));

/** Set a `<select>` or input by name inside `form`, and fire `change` when asked. */
export function setField(w, form, name, value, { change = false } = {}) {
  const field = form.elements.namedItem(name);
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value;
  if (change) field.dispatchEvent(new w.Event("change", { bubbles: true }));
}

/** Give a file input a file, as a person choosing one would (jsdom has no DataTransfer). */
export function chooseFile(input, name, text, type = "text/csv") {
  Object.defineProperty(input, "files", { configurable: true, value: [new File([text], name, { type })] });
}
