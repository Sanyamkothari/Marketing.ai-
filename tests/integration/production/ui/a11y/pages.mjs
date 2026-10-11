/* The pages the accessibility checks draw (Plan M, M181 part A).

   They are the pages of the visible-text check, read from its own source (`../glossary/visible_text.test.mjs`,
   the `PAGES` list and the constants above it), so a page added there is checked here with no second list to keep.
   That file cannot be imported (importing it would run its tests), so the text between its first constant and the
   closing `];` of PAGES is evaluated with the few helpers it uses. If the file changes shape so that this fails,
   the failure says so; fix the extraction here rather than copying the list.

   The layouts: "classic" always, and "campaigns" when `ui/layout.js` (Plan M, M174) is in the tree. The campaigns
   layout is switched on the way the application does it: `GET /auth/me` carries `ui_layout`. */
import fs from "node:fs";
import assert from "node:assert/strict";
import { $, settle } from "../harness.mjs";

const SOURCE = new URL("../glossary/visible_text.test.mjs", import.meta.url);
const LAYOUT_JS = new URL("../../../../../ui/layout.js", import.meta.url);

/** The layouts this tree has. */
export function availableLayouts() {
  return fs.existsSync(LAYOUT_JS) ? ["classic", "campaigns"] : ["classic"];
}

/** The `PAGES` list of the visible-text check: `[{id, hash, steps?, expect, selector?}]`. */
export function loadPages() {
  const text = fs.readFileSync(SOURCE, "utf8");
  const start = text.search(/^const UC = /m);
  const open = text.indexOf("export const PAGES = [");
  const close = text.indexOf("\n];", open);
  if (start < 0 || open < 0 || close < 0 || start > open) {
    throw new Error("a11y/pages.mjs cannot find PAGES in glossary/visible_text.test.mjs; update the extraction");
  }
  const code = `${text.slice(start, close + 3).replace("export const PAGES", "const PAGES")}\nreturn PAGES;`;
  const pages = new Function("$", "settle", "assert", code)($, settle, assert);
  if (!Array.isArray(pages) || pages.length === 0 || !pages.every((p) => p.id && p.hash)) {
    throw new Error("a11y/pages.mjs read PAGES from glossary/visible_text.test.mjs but it is not a list of pages");
  }
  return pages;
}
