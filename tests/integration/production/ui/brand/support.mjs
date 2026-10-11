/* Shared by the M182 brand tests: the real `GET /auth/me` answers the Python side wrote into $BRAND_FIXTURES
   (tests/integration/test_brand_ui.py), and the whole page drawn against the recorded Hillstrom demo. */
import fs from "node:fs";
import path from "node:path";

const FIXTURES = process.env.BRAND_FIXTURES;

export { installShellPage, loadModules, untilStable, visit } from "../shell/page.mjs";

/** One `GET /auth/me` body the Python side captured from the real app. */
export function me(name) {
  if (!FIXTURES) throw new Error("BRAND_FIXTURES is not set: run these through tests/integration/test_brand_ui.py");
  return JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
}

/** What the custom fixtures carry (tests/integration/test_brand_ui.py CUSTOM). */
export const CUSTOM = Object.freeze({
  name: "Acme Growth",
  logo_url: "https://cdn.example.com/acme-logo.svg",
  logo_alt: "Acme",
  color: "#0B5D3B",
});

/** `body` without its `brand` field: an API from before M182. */
export function withoutBrand(body) {
  const { brand: _dropped, ...rest } = body;
  return rest;
}
