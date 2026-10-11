// "Value proof": the Value Proof Packs, one row per campaign (Plan M, M178). Every sentence about a result is the
// server's (`GET /pilot/proof`: the pack's headline); this file chooses the words for what the result can claim
// and the short name, and draws the table. Pure: nothing here reads the document or calls the API.

import { EM_DASH, dataTable, emptyState, esc, headActions, sortNote } from "../../dom.js";
import { term } from "../../labels.js";
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

const claimHtml = (entry) => {
  const claim = claimOf(entry.claim_label);
  const pill = claim.tone === "ok" ? `<span class="pill ok">${esc(claim.word)}</span>` : `<span class="chip neutral">${esc(claim.word)}</span>`;
  return `<span data-claim="${esc(claim.key)}">${pill}</span><span class="rp-why">${esc(claim.why)}</span>`;
};

/** The "Value proof" tab. `list` is `GET /pilot/proof`'s answer (null when this API has none). */
export function valueProofHtml(list) {
  const proofs = (list && Array.isArray(list.proofs) && list.proofs) || [];
  if (!proofs.length) {
    return `<section class="card" data-proofs-empty>${emptyState({
      title: "No Value Proof Pack yet",
      text: "Once a campaign's results are measured, its Value Proof Pack appears here: what it changed, what it cost and what finance can rely on.",
      action: { label: "Check a past campaign", href: "#/reports/check", kind: "secondary" },
    })}</section>`;
  }
  const ready = proofs.filter((entry) => entry.status === "ready");
  const rows = proofs.map((entry) => {
    const isReady = entry.status === "ready";
    const name = shortName(entry.name);
    const title = name !== String(entry.name || "").trim() ? ` title="${esc(entry.name)}"` : "";
    return [
      `<span class="rp-name"${title} data-proof-campaign="${esc(entry.campaign_id)}">${esc(name)}</span>`,
      isReady ? esc(entry.headline || EM_DASH) : esc(entry.message || EM_DASH),
      isReady ? claimHtml(entry) : EM_DASH,
      isReady
        ? `<span class="rp-acts"><a class="btn secondary sm" href="${esc(packHref(entry.campaign_id))}" data-proof-open>Open</a><a class="btn quiet sm" href="${esc(
            proofUrl(entry.campaign_id, "pdf"),
          )}" download data-proof-pdf>PDF</a></span>`
        : EM_DASH,
    ];
  });
  const primary = ready.length
    ? headActions({ primary: { label: "Open the newest Value Proof Pack", href: packHref(ready[0].campaign_id), attrs: "data-proof-newest" } })
    : "";
  return `${primary}<section class="card" data-proofs><h3>Value Proof Packs${sortNote("newest first")}</h3>${dataTable(
    [{ label: "Campaign" }, { label: "What it shows" }, { label: "What it can claim" }, { label: "Pack", num: true }],
    rows,
    { cls: "rp-table" },
  )}</section>`;
}
