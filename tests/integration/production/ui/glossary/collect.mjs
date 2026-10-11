/* What a person can read on a page, for the visible-text check (Plan M, M170).

   Visible text is every text node, plus the attributes a person reads (aria-label, title, placeholder),
   EXCEPT what sits in:
     - a technical section: any element with class "tech" (today always `<details class="tech">`),
       any element with [data-technical], and any <details> whose summary says "Technical details".
       The <summary> of a technical <details> is still read, because it is on screen whether the section
       is open or closed, unless it says only "Technical details" or "Details" (the generic labels);
     - a hidden element: the `hidden` attribute, or an inline display:none / visibility:hidden;
     - a name the person supplied (M172, DEC-1602): any element with [data-user], which pages write with
       ui/labels.js `userText(value)` around a file name, a campaign label or a column of their data, and
       nothing else. It is their wording, not ours, so the glossary cannot rename it;
     - <script>, <style>, <template>, <noscript>.
   The page's style sheet is not loaded in jsdom, so a class that hides an element in CSS cannot be seen
   here; pages that hide content use `hidden` (or a closed <details>, whose contents count: they are one
   click away from a marketer and must not carry engine words either, unless marked technical). */

import { USER_TEXT_ATTRIBUTE } from "../../../../../ui/labels.js";

const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "HEAD"]);
const READ_ATTRIBUTES = ["aria-label", "title", "placeholder"];

const GENERIC_SUMMARY = /^\s*(technical details|details)\s*$/i;

const summaryOf = (element) => [...element.children].find((c) => c.tagName.toUpperCase() === "SUMMARY");

/** Whether this element is not on screen at all (hidden, or a tag that never shows text). */
function isHidden(element) {
  if (SKIP_TAGS.has(element.tagName.toUpperCase())) return true;
  if (element.hasAttribute("hidden")) return true;
  const inline = (element.getAttribute("style") || "").replace(/\s+/g, "").toLowerCase();
  return inline.includes("display:none") || inline.includes("visibility:hidden");
}

/** Whether this element and everything inside it is left out of the visible text. */
export function isExcluded(element) {
  if (isHidden(element)) return true;
  if (element.classList.contains("tech") || element.hasAttribute("data-technical")) return true;
  if (element.hasAttribute(USER_TEXT_ATTRIBUTE)) return true;
  if (element.tagName.toUpperCase() === "DETAILS") {
    const summary = summaryOf(element);
    if (summary && /^\s*technical details\s*$/i.test(summary.textContent)) return true;
  }
  return false;
}

/** The summary of an excluded <details> that a person still reads (it shows when the section is closed). */
function keptSummary(element) {
  if (element.tagName.toUpperCase() !== "DETAILS" || isHidden(element)) return null;
  const summary = summaryOf(element);
  if (!summary || GENERIC_SUMMARY.test(summary.textContent) || isHidden(summary)) return null;
  return summary;
}

/** The visible text under `root`, one piece per line. */
export function visibleText(root) {
  const pieces = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      const text = node.nodeValue.replace(/\s+/g, " ").trim();
      if (text) pieces.push(text);
      return;
    }
    if (node.nodeType !== 1) return;
    if (isExcluded(node)) {
      const summary = keptSummary(node);
      if (summary) walk(summary);
      return;
    }
    for (const name of READ_ATTRIBUTES) {
      const value = node.getAttribute(name);
      if (value && value.trim()) pieces.push(value.replace(/\s+/g, " ").trim());
    }
    for (const child of node.childNodes) walk(child);
  };
  walk(root);
  return pieces.join("\n");
}
