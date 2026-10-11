/* What a person can read on a page, for the visible-text check (Plan M, M170).

   Visible text is every text node, plus the attributes a person reads (aria-label, title, placeholder),
   EXCEPT what sits in:
     - a technical section: any element with class "tech" (today always `<details class="tech">`),
       any element with [data-technical], and any <details> whose summary says "Technical details".
       The <summary> of a technical <details> is still read, because it is on screen whether the section
       is open or closed, unless it says only "Technical details" or "Details" (the generic labels);
     - a hidden element: the `hidden` attribute, or an inline display:none / visibility:hidden;
     - <script>, <style>, <template>, <noscript>.
   The page's style sheet is not loaded in jsdom, so a class that hides an element in CSS cannot be seen
   here; pages that hide content use `hidden` (or a closed <details>, whose contents count: they are one
   click away from a marketer and must not carry engine words either, unless marked technical).

   Plan M M180 (DEC-1610) adds a MODE, the person's "Show technical details" switch (ui/technical.js):
     - "on" (the default): exactly the rules above, as M170 wrote them - the marks are read;
     - "off": the marks are NOT read. What is left out is what the page's real style sheet hides, read from the
       computed style (`display: none` or `visibility: hidden`), with <html> carrying the switch's class and its
       CSS rule injected by technical.js; plus the `hidden` attribute and the tags that never show text. So the
       "off" text is what a person with the switch off really sees, and a technical section the CSS fails to hide
       is counted. */

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
  return isHidden(element) || isTechnical(element);
}

/** Whether this element carries a technical mark (class "tech", [data-technical], a "Technical details" <details>). */
export function isTechnical(element) {
  if (element.classList.contains("tech") || element.hasAttribute("data-technical")) return true;
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

/** Whether the page's style sheet hides this element (M180's "off" mode): its computed style. */
export function hiddenByStyle(element) {
  const view = element.ownerDocument && element.ownerDocument.defaultView;
  if (!view || typeof view.getComputedStyle !== "function") return false;
  const style = view.getComputedStyle(element);
  return style.display === "none" || style.visibility === "hidden";
}

/** Whether `element` or one of its ancestors is hidden by the style sheet, the attribute, or an inline style. */
export function hiddenOnScreen(element) {
  for (let el = element; el && el.nodeType === 1; el = el.parentElement) {
    if (isHidden(el) || hiddenByStyle(el)) return true;
  }
  return false;
}

/** Every element carrying a technical mark under `root`: the elements the "on" mode leaves out by their mark. */
export function technicalElements(root) {
  return [...root.querySelectorAll("*")].filter((el) => isTechnical(el));
}

/** The visible text under `root`, one piece per line. `mode` is "on" (M170's marks) or "off" (the real CSS). */
export function visibleText(root, { mode = "on" } = {}) {
  if (mode !== "on" && mode !== "off") throw new Error(`visibleText: mode is "on" or "off", not ${mode}`);
  const pieces = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      const text = node.nodeValue.replace(/\s+/g, " ").trim();
      if (text) pieces.push(text);
      return;
    }
    if (node.nodeType !== 1) return;
    if (mode === "off") {
      if (isHidden(node) || hiddenByStyle(node)) return;
    } else if (isExcluded(node)) {
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
