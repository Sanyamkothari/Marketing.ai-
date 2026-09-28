// "Pick from a connection" (Plan H M80): step 1 of a use case, filled from a saved connection instead
// of a file on this computer. Choose a connection → open folders or schemas → pick a CSV / Parquet file
// or a table → see its first rows (personal data masked by the server) → Import. The import is an
// ordinary upload (`POST /connections/{id}/import` answers exactly what `POST /uploads` does), so the
// caller carries on precisely as after a file upload.
//
// The picker owns its own state and draws into whatever its host paints; the host calls `html()` in
// its markup and `bind(root)` after painting, and the picker calls `redraw()` when it changed.

import { dataTable, errorBox, esc, fmtSize } from "../../dom.js";
import { browseConnection, getConnections, importFrom, previewFrom } from "./api.js";
import { statusBadge } from "./page.js";
import { injectConnectionStyles } from "./styles.js";

/**
 * `{ useCaseId, mode, redraw(), onImported(upload, label), onCancel() }` → `{ html(), bind(root), busy() }`.
 */
export function createConnectionPicker({ useCaseId, mode, redraw, onImported, onCancel }) {
  injectConnectionStyles();
  const s = {
    phase: "loading", // loading | choose | browse | preview | importing
    connections: [],
    connection: null,
    listing: null,
    pick: null,
    preview: null,
    error: null,
    loading: false,
  };

  const set = (patch) => {
    Object.assign(s, patch);
    redraw();
  };

  async function load() {
    try {
      const answer = await getConnections();
      const usable = (answer.connections || []).filter((c) => c.available);
      set({ phase: "choose", connections: usable, error: null });
    } catch (error) {
      set({ phase: "choose", connections: [], error });
    }
  }

  async function open(path) {
    set({ loading: true, error: null });
    try {
      const listing = await browseConnection(s.connection.connection_id, path || "");
      set({ phase: "browse", listing, loading: false, pick: null, preview: null });
    } catch (error) {
      set({ loading: false, error });
    }
  }

  async function choose(item) {
    set({ phase: "preview", pick: item, preview: null, loading: true, error: null });
    try {
      const preview = await previewFrom(s.connection.connection_id, item);
      set({ preview, loading: false });
    } catch (error) {
      set({ loading: false, error });
    }
  }

  async function importIt() {
    set({ phase: "importing", error: null });
    try {
      const upload = await importFrom(s.connection.connection_id, s.pick, useCaseId, mode);
      onImported(upload, `${s.pick.table || s.pick.name} (from ${s.connection.name})`);
    } catch (error) {
      set({ phase: "preview", error });
    }
  }

  const cancel = `<button type="button" class="btn quiet sm" data-cp-cancel>Upload a file instead</button>`;

  function chooseHtml() {
    if (!s.connections.length) {
      return `<p class="cp-note">No connections yet. Set one up on the <a href="#/connections">Connections</a> page, then come back.</p><div class="btn-row">${cancel}</div>`;
    }
    const items = s.connections
      .map(
        (c) =>
          `<button type="button" class="cp-item" data-cp-connection="${esc(c.connection_id)}"><span class="cp-name">${esc(
            c.name,
          )} <span class="cp-meta">${esc(c.kind_label)}</span></span>${statusBadge(c)}</button>`,
      )
      .join("");
    return `<div class="cp-head"><span>Choose a connection</span></div><div class="cp-list" role="list">${items}</div><div class="btn-row">${cancel}</div>`;
  }

  function browseHtml() {
    const listing = s.listing;
    const where = listing.path ? listing.path : "Top";
    const up =
      listing.parent !== null && listing.parent !== undefined
        ? `<button type="button" class="btn quiet sm" data-cp-open="${esc(listing.parent)}">Up one level</button>`
        : "";
    const items = (listing.items || [])
      .map((item) => {
        if (item.kind === "folder" || item.kind === "schema") {
          return `<button type="button" class="cp-item" data-cp-open="${esc(item.path)}"><span class="cp-name">${esc(
            item.name,
          )}</span><span class="cp-meta">Open ›</span></button>`;
        }
        const size = item.size_bytes !== null && item.size_bytes !== undefined ? fmtSize(item.size_bytes) : "";
        return `<button type="button" class="cp-item" data-cp-pick="${esc(JSON.stringify([item.path, item.schema_name, item.table, item.name]))}"${
          item.importable ? "" : " disabled"
        }><span class="cp-name">${esc(item.name)}</span><span class="cp-meta">${esc(
          item.importable ? size || "Pick" : "Not a CSV or Parquet file",
        )}</span></button>`;
      })
      .join("");
    const empty = (listing.items || []).length ? "" : `<p class="cp-note">Nothing here.</p>`;
    const more = listing.truncated ? `<p class="cp-note">Only the first 500 are listed.</p>` : "";
    return `<div class="cp-head"><span>In <b>${esc(s.connection.name)}</b>: ${esc(where)}</span>${up}</div>${
      empty || `<div class="cp-list" role="list">${items}</div>`
    }${more}<div class="btn-row"><button type="button" class="btn quiet sm" data-cp-back>Choose another connection</button>${cancel}</div>`;
  }

  function previewHtml() {
    const label = s.pick.table ? `${s.pick.schema_name}.${s.pick.table}` : s.pick.path;
    const table = s.preview
      ? dataTable(
          (s.preview.columns || []).map((c, i) => ({ label: c, more: i >= 5 })),
          (s.preview.rows || []).map((r) => r.map((v) => esc(v))),
        )
      : "";
    const note = s.preview
      ? `<p class="cp-note">${esc(
          `The first ${s.preview.rows.length} rows. Personal details are hidden here; the import copies everything.`,
        )}</p>`
      : "";
    const busy = s.phase === "importing";
    return `<div class="cp-head"><span>Preview of <b>${esc(label)}</b></span></div>${table}${note}<div class="btn-row"><button type="button" class="btn primary sm" data-cp-import${
      busy || s.loading || !s.preview ? " disabled" : ""
    }>${busy ? "Importing…" : "Import"}</button><button type="button" class="btn quiet sm" data-cp-open="${esc(
      s.listing ? s.listing.path : "",
    )}"${busy ? " disabled" : ""}>Pick another</button></div>`;
  }

  return {
    busy: () => s.phase === "importing",
    html() {
      let body;
      if (s.phase === "loading") body = `<div class="loading" role="status">Loading your connections…</div>`;
      else if (s.phase === "choose") body = chooseHtml();
      else if (s.phase === "browse") body = browseHtml();
      else body = previewHtml();
      const loading = s.loading ? `<div class="loading" role="status">Reading…</div>` : "";
      const error = s.error ? errorBox(s.error) : "";
      return `<div class="cp" data-cp>${body}${loading}${error}</div>`;
    },
    bind(root) {
      const on = (selector, fn) => root.querySelectorAll(selector).forEach((el) => el.addEventListener("click", () => fn(el)));
      on("[data-cp-connection]", (el) => {
        s.connection = s.connections.find((c) => c.connection_id === el.dataset.cpConnection) || null;
        if (s.connection) open("");
      });
      on("[data-cp-open]", (el) => open(el.dataset.cpOpen));
      on("[data-cp-pick]", (el) => {
        const [path, schema, table, name] = JSON.parse(el.dataset.cpPick);
        choose({ path, schema_name: schema, table, name });
      });
      on("[data-cp-import]", () => importIt());
      on("[data-cp-back]", () => set({ phase: "choose", listing: null, pick: null, preview: null, error: null }));
      on("[data-cp-cancel]", () => onCancel());
    },
    start() {
      if (s.phase === "loading") load();
    },
  };
}
