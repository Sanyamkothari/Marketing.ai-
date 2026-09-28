// "Pick from a connection" in Manual setup's Step 1 (UI audit §8.4 item 10; DEC-1109 follow-up): the
// same picker Guided setup uses (`picker.js`), offered through the upload-source seam
// (`registerUploadSource`, modules/router.js). The import is `POST /connections/{id}/import`, which
// answers exactly what `POST /uploads` does, so `ui/usecase.js` fills Step 1 with it as with a file.
//
// `entry` draws the offer beside the file control: "or Pick from a connection" once at least one
// usable connection exists, a short line linking to the Connections page when none does, nothing
// while the list loads or when it cannot be read (a role that may not read connections). The list is
// read once per route: coming back from `#/connections` with a new connection reads it again.
//
// `panel` draws the picker in the file control's place. The Setup form repaints its whole `<main>` on
// every change, so the picker's element is kept here and moved into each fresh placeholder, as the
// raw-tables panel is (`modules/onboarding/setup.js`): a listing or a preview survives the repaint.

import { getConnections } from "./api.js";
import { createConnectionPicker } from "./picker.js";
import { injectConnectionStyles } from "./styles.js";

const known = { answer: null, loading: false };
const entry = { container: null, ctx: null };
let open = null; // { key, element, picker, ctx } while the picker is drawn

if (typeof window !== "undefined") {
  window.addEventListener("hashchange", () => {
    known.answer = null;
  });
}

async function loadConnections() {
  if (known.loading) return;
  known.loading = true;
  try {
    const answer = await getConnections();
    const all = answer.connections || [];
    known.answer = { usable: all.filter((c) => c.available).length, total: all.length };
  } catch {
    known.answer = { failed: true };
  }
  known.loading = false;
  drawEntry();
}

function entryHtml(ctx) {
  const answer = known.answer;
  if (!answer || answer.failed) return "";
  if (answer.usable) {
    return `or <button type="button" class="btn quiet sm" data-upload-conn${
      ctx.disabled ? " disabled" : ""
    }>Pick from a connection</button>`;
  }
  const text = answer.total
    ? `None of your connections can be read on this server: see <a href="#/connections">Connections</a>.`
    : `No connections yet: <a href="#/connections">add one</a> to pick a table or file from it.`;
  return `<span class="cp-none">${text}</span>`;
}

function drawEntry() {
  const { container, ctx } = entry;
  if (!container || !ctx || !container.isConnected) return;
  container.innerHTML = entryHtml(ctx);
  const button = container.querySelector("[data-upload-conn]");
  if (button) {
    button.addEventListener("click", () => {
      open = null; // every pick starts from the list of connections
      ctx.open();
    });
  }
}

/** The offer beside Step 1's file control. */
export function connectionEntry(container, ctx) {
  injectConnectionStyles();
  entry.container = container;
  entry.ctx = ctx;
  if (known.answer === null) loadConnections();
  drawEntry();
}

function drawPanel() {
  if (!open) return;
  open.element.innerHTML = open.picker.html();
  open.picker.bind(open.element);
}

/** The picker, in the file control's place, until it imports or is left. */
export function connectionPanel(container, ctx) {
  injectConnectionStyles();
  const key = `${ctx.uc.id}|${ctx.mode}`;
  if (!open || open.key !== key) {
    const element = document.createElement("div");
    const picker = createConnectionPicker({
      useCaseId: ctx.uc.id,
      mode: ctx.mode,
      redraw: drawPanel,
      onImported: (upload, label) => {
        const done = open.ctx;
        open = null;
        done.onUpload(upload, label);
      },
      onCancel: () => {
        const done = open.ctx;
        open = null;
        done.onCancel();
      },
    });
    open = { key, element, picker, ctx };
    picker.start();
  }
  open.ctx = ctx;
  container.replaceChildren(open.element);
  drawPanel();
}
