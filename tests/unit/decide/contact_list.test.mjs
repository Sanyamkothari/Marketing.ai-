/* Plan M M171 (DEC-1601): the one contact list (`ui/contact_list.js`): which file the single download is, and the
   one sentence that says why fewer customers are contacted than are worth contacting. Run by
   tests/unit/decide/test_contact_list_js.py, or directly: `node --test tests/unit/decide/contact_list.test.mjs`.
   No npm install is needed: the module is pure. The sentence is also checked against a real run's files in
   tests/integration/decide/test_one_contact_list.py. */
import { test } from "node:test";
import assert from "node:assert/strict";

const cl = await import(new URL("../../../ui/contact_list.js", import.meta.url));

const scoring = {
  rows_scored: 32000,
  control_group_rows: 16000,
  kpi: { label: "Customers worth an e-mail", formula: 'count_where_band_in(["High","Medium"])', value: 1645, display: "2K" },
  suppressed: [
    { reason: "opted_out", rows: 120 },
    { reason: "recently_contacted", rows: 30 },
  ],
};
const treat = { treat_rows: 831, holdout_rows: 15990, suppressed_rows: 150, uncontactable_rows: 7, compliance_withheld_rows: 2 };

test("the sentence says how many are worth contacting, how many will be, and why they differ", () => {
  const sentence = cl.contactListSentence({ scoring, treat });
  assert.equal(
    sentence,
    "1,645 customers are worth contacting; 831 will be contacted. Why the difference: " +
      "15,990 customers in the whole list are held back at random as the comparison group (not contacted, so the effect of contacting can be measured); " +
      "120 customers opted out of marketing; 30 customers were contacted recently; " +
      "7 customers cannot be reached on any channel they have agreed to; " +
      "2 customers were taken off by the compliance check before sending.",
  );
});

test("every figure in the sentence is one of the artefacts' own fields", () => {
  const sentence = cl.contactListSentence({ scoring, treat });
  const allowed = new Set([1645, 831, 15990, 120, 30, 7, 2]);
  for (const found of sentence.match(/\d[\d,]*/g)) assert.ok(allowed.has(Number(found.replace(/,/g, ""))), found);
});

test("a part whose figure is missing, null or zero is left out, never filled in", () => {
  const bare = cl.contactListSentence({ scoring: { ...scoring, suppressed: [], control_group_rows: null }, treat: { treat_rows: 831 } });
  assert.equal(bare, "1,645 customers are worth contacting; 831 will be contacted.");
  const zero = cl.contactListSentence({ scoring: { ...scoring, suppressed: [{ reason: "opted_out", rows: 0 }] }, treat: { treat_rows: 831, uncontactable_rows: 0 } });
  assert.doesNotMatch(zero, /opted out|cannot be reached/);
  assert.match(zero, /16,000 customers in the whole list/, "the comparison group falls back to the scoring summary's count");
});

test("the comparison group is the treat list's own holdout count when it has one", () => {
  assert.match(cl.contactListSentence({ scoring, treat: { ...treat, holdout_rows: 14000 } }), /14,000 customers in the whole list/);
  assert.match(cl.contactListSentence({ scoring, treat: { ...treat, holdout_rows: null } }), /16,000 customers in the whole list/);
});

test("with no treat list the sentence says only how many are worth contacting", () => {
  assert.equal(cl.contactListSentence({ scoring, treat: null }), "1,645 customers are worth contacting.");
});

test("a headline that is not a count of customers is never used as one", () => {
  const sum = { ...scoring, kpi: { label: "Revenue at risk", formula: 'sum_where_band_in(["High"], "spend")', value: 99000, display: "99K" } };
  assert.equal(cl.contactListSentence({ scoring: sum, treat }), "831 customers will be contacted.");
  const fractional = { ...scoring, kpi: { ...scoring.kpi, value: 1645.5 } };
  assert.equal(cl.contactListSentence({ scoring: fractional, treat: null }), "");
  assert.equal(cl.contactListSentence({}), "");
});

test("nobody left out says so in one clause, and singular counts read in the singular", () => {
  assert.equal(cl.contactListSentence({ scoring: { ...scoring, kpi: { ...scoring.kpi, value: 831 } }, treat }), "831 customers are worth contacting, and everyone will be contacted.");
  const one = cl.contactListSentence({
    scoring: { ...scoring, kpi: { ...scoring.kpi, value: 2 }, suppressed: [], control_group_rows: 1 },
    treat: { treat_rows: 1 },
  });
  assert.equal(
    one,
    "2 customers are worth contacting; 1 will be contacted. Why the difference: 1 customer in the whole list is held back at random as the comparison group (not contacted, so the effect of contacting can be measured).",
  );
  // explored customers can be contacted although the policy left them out: no gap is claimed
  assert.equal(
    cl.contactListSentence({ scoring: { ...scoring, kpi: { ...scoring.kpi, value: 800 } }, treat }),
    "800 customers are worth contacting; 831 will be contacted.",
  );
});

test("an uplift run's recommended contacts are what is worth contacting, and a reached budget is a reason", () => {
  const policy = { contacts_recommended: 150, stop_reason: "budget", budget_contacts: 150 };
  const sentence = cl.contactListSentence({ scoring: { control_group_rows: 0, suppressed: [] }, treat: { treat_rows: 120 }, policy });
  assert.equal(sentence, "150 customers are worth contacting; 120 will be contacted. Why the difference: the contact budget of 150 customers was reached.");
  const noBudget = cl.contactListSentence({ scoring: null, treat: { treat_rows: 120 }, policy: { ...policy, stop_reason: "all_persuadables" } });
  assert.equal(noBudget, "150 customers are worth contacting; 120 will be contacted.");
});

test("the download is the treat list unless the run said it cannot have one", () => {
  const asked = cl.contactListDownload({ treatHref: "/runs/r/treat_list.csv", scoresHref: "/runs/r/scores.csv", treatError: null });
  assert.deepEqual(asked, { href: "/runs/r/treat_list.csv", kind: "contact", note: "" });
  const refused = cl.contactListDownload({ treatHref: "/runs/r/treat_list.csv", scoresHref: "/runs/r/scores.csv", treatError: new Error("409") });
  assert.equal(refused.href, "/runs/r/scores.csv");
  assert.equal(refused.kind, "scores");
  assert.match(refused.note, /list of every customer scored/);
});

test("one primary download, labelled the same either way; the scored list is offered only beside the treat list", () => {
  const contact = cl.contactListDownload({ treatHref: "/t", scoresHref: "/s" });
  const link = cl.contactDownloadLink(contact);
  assert.match(link, /^<a class="btn primary" href="\/t" download data-download="contact-list" data-download-kind="contact">/);
  assert.match(link, />Download contact list<\/a>$/);
  assert.equal((link.match(/btn primary/g) || []).length, 1);
  assert.match(cl.allScoresHtml(contact, "/s"), /<a class="btn quiet sm" href="\/s" download data-download="scores">All scores \(for analysts\)<\/a>/);
  const fallback = cl.contactListDownload({ treatHref: "/t", scoresHref: "/s", treatError: new Error("no") });
  assert.match(cl.contactDownloadLink(fallback, { icon: false }), />Download contact list<\/a>$/);
  assert.equal(cl.allScoresHtml(fallback, "/s"), "", "the scored list is the main file, so no second link");
});

test("the block escapes what it draws, and draws nothing with nothing to say", () => {
  const html = cl.contactSentenceHtml("A <b>sentence</b>", { note: "a <i>note</i>" });
  assert.doesNotMatch(html, /<b>|<i>/);
  assert.match(html, /data-contact-sentence/);
  assert.match(html, /data-contact-note/);
  assert.equal(cl.contactSentenceHtml("", { note: "" }), "");
  assert.equal(cl.contactSentenceHtml("", null), "");
});
