// "Value proof": the Value Proof Packs, one row per campaign (Plan M, M178). Every sentence about a result is the
// server's (`GET /pilot/proof`: the pack's headline); this file chooses the words for what the result can claim
// and the short name, and draws the table. Pure: nothing here reads the document or calls the API.

import { button, emptyState, statusChip, valueRange } from "../../components.js";
import { EM_DASH, dataTable, esc, sortNote } from "../../dom.js";
import { term, USER_TEXT_ATTRIBUTE } from "../../labels.js";
import { proofUrl } from "./api.js";

/** The server's label for what a pack's numbers can claim, as the one word a marketer reads and why. */
export function claimOf(label) {
  const text = String(label || "");
  if (/^causal/i.test(text)) {
    return { key: "proven", word: term("evidenceProven"), tone: "ok", why: "The customers held back were chosen at random." };
  }
  if (/^random by your statement/i.test(text)) {
    return {
      key: "estimated",
      word: term("evidenceEstimated"),
      tone: "neutral",
      why: "You said the groups were chosen at random, and we could not check it.",
    };
  }
  // Anything else, or nothing, claims nothing: the safe reading of a label this page does not know.
  return {
    key: "not-proven",
    word: term("evidenceNotProven"),
    tone: "neutral",
    why: "The groups were not chosen at random, so this shows what each group did, not what the campaign changed.",
  };
}

const LONGEST_NAME = 56;

/** A campaign's name without its trailing description ("Hillstrom e-mail (spend) - public dataset, ..." -> "Hillstrom e-mail (spend)"). */
export function shortName(name) {
  const full = String(name || "").trim();
  if (!full) return EM_DASH;
  const cut = full.split(/\s[-–—]\s/)[0].trim() || full;
  if (cut.length <= LONGEST_NAME) return cut;
  const room = cut.slice(0, LONGEST_NAME - 1);
  return `${room.slice(0, Math.max(room.lastIndexOf(" "), 20)).trim()}…`;
}

export const packHref = (campaignId) => `#/pilot/proof/${encodeURIComponent(campaignId)}`;

/** The evidence label as a status chip (icon and word, never colour alone) with its one-line reason under it. */
const claimHtml = (entry) => {
  const claim = claimOf(entry.claim_label);
  return `<span data-claim="${esc(claim.key)}">${statusChip("evidence", claim.key)}</span><span class="td-sub rp-why">${esc(claim.why)}</span>`;
};

const SIGN = /^[-−]$/;
// The server writes each end with `format_inr` (engine/pilot/roi.py): "₹81,034", "-₹67,912", and from one lakh up the
// words beside it, "₹1,23,456 (1.23 lakh)" or "₹2,50,00,000 (2.50 crore)". The words after the low end are skipped
// here; the text drawn is still the server's whole sentence.
const NET =
  /\bNet value\s+([-−]?)₹([\d,]+(?:\.\d+)?)(?:\s*\([\d.]+\s+(?:lakh|crore)\))?\s+to\s+([-−]?)₹([\d,]+(?:\.\d+)?)/;
const figure = (sign, digits) => (SIGN.test(sign) ? -1 : 1) * Number(digits.replace(/,/g, ""));

/**
 * The server's headline (one sentence about the extra outcomes, then "Net value ₹low to ₹high ...") split in two for the
 * eye: the effect, and the net value with the two figures it names for the range bar. The words are the server's,
 * unchanged and in the same order; nothing is computed. A headline that does not carry a net value range comes back
 * whole with no figures, and is drawn as plain text.
 */
export function splitHeadline(headline) {
  const text = String(headline || "");
  const found = NET.exec(text);
  if (!found) return { effect: text, net: "", low: null, high: null };
  const low = figure(found[1], found[2]);
  const high = figure(found[3], found[4]);
  const effect = text.slice(0, found.index).trim();
  const net = text.slice(found.index).trim();
  return Number.isFinite(low) && Number.isFinite(high) ? { effect, net, low, high } : { effect: text, net: "", low: null, high: null };
}

/** "What it shows": the effect in words, then the net value with its range as an interval bar and the words beside it. */
const shownHtml = (entry) => {
  const { effect, net, low, high } = splitHeadline(entry.headline);
  if (!net) return esc(entry.headline || EM_DASH);
  return `${effect ? `<span class="rp-effect">${esc(effect)}</span> ` : ""}<span class="rp-net">${valueRange({ low, high, text: net })}</span>`;
};

/** The primary action of the page header on this tab: the newest ready pack, or nothing when no pack is ready. */
export function newestPackAction(list) {
  const ready = ((list && Array.isArray(list.proofs) && list.proofs) || []).filter((entry) => entry.status === "ready");
  return ready.length ? { label: "Open the newest Value Proof Pack", href: packHref(ready[0].campaign_id), attrs: "data-proof-newest" } : null;
}

/** The "Value proof" tab. `list` is `GET /pilot/proof`'s answer (null when this API has none). */
export function valueProofHtml(list) {
  const proofs = (list && Array.isArray(list.proofs) && list.proofs) || [];
  if (!proofs.length) {
    return `<section class="card" data-proofs-empty>${emptyState({
      icon: "report",
      title: "No Value Proof Pack yet",
      text: "Once a campaign's results are measured, its Value Proof Pack appears here: what it changed, what it cost and what finance can rely on.",
      action: { label: "Check a past campaign", href: "#/reports/check", kind: "secondary" },
    })}</section>`;
  }
  const rows = proofs.map((entry) => {
    const isReady = entry.status === "ready";
    const name = shortName(entry.name);
    const title = name !== String(entry.name || "").trim() ? ` title="${esc(entry.name)}"` : "";
    return [
      `<span class="rp-name"${title} data-proof-campaign="${esc(entry.campaign_id)}" ${USER_TEXT_ATTRIBUTE}>${esc(name)}</span>`,
      isReady ? shownHtml(entry) : `<span class="rp-na">${esc(entry.message || EM_DASH)}</span>`,
      isReady ? claimHtml(entry) : EM_DASH,
      isReady
        ? `<span class="rp-acts">${button({ label: "Open", href: packHref(entry.campaign_id), kind: "secondary", size: "sm", attrs: "data-proof-open" })}${button({
            label: "PDF",
            href: proofUrl(entry.campaign_id, "pdf"),
            kind: "tertiary",
            size: "sm",
            icon: "download",
            attrs: "download data-proof-pdf",
          })}</span>`
        : EM_DASH,
    ];
  });
  return `<section class="card" data-proofs><h3>Value Proof Packs${sortNote("newest first")}</h3>${dataTable(
    [{ label: "Campaign" }, { label: "What it shows" }, { label: "What it can claim" }, { label: "Pack", num: true }],
    rows,
    { cls: "rp-table" },
  )}</section>`;
}
