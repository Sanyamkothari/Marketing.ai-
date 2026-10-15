// "I don't know" opens "Who would know?": five people who usually can answer, and a message ready to copy. Nothing
// here is sent anywhere. The message holds the question, why it matters and what Data Doctor does meanwhile, in words
// our rules wrote.

/** Who would know, by the id the case stores (`PARK_ROLES` in `engine/doctor/case.py`). */
export const WHO = Object.freeze([
  { id: "crm_admin", label: "CRM admin", knows: "how customer records are kept and numbered" },
  { id: "database_admin", label: "Database admin", knows: "how the tables and columns are filled" },
  { id: "compliance", label: "Compliance", knows: "who may be contacted and what may be used" },
  { id: "finance", label: "Finance", knows: "what the amounts mean and which ones count" },
  { id: "campaign_manager", label: "Campaign manager", knows: "who was contacted before and why" },
]);

export const whoOf = (id) => WHO.find((w) => w.id === id) || null;

/**
 * The message to send, as plain lines. `what` is the question or the step; `why` the reason it matters (may be
 * empty); `meanwhile` what Data Doctor does until there is an answer.
 */
export function messageFor(roleId, { what, why = "", meanwhile = "" }) {
  const who = whoOf(roleId);
  const lines = [
    `Hello${who ? ` (${who.label})` : ""},`,
    "",
    `I am getting our customer data ready for a campaign, and one point needs someone who knows ${
      who ? who.knows : "the data well"
    }.`,
    "",
    `The point: ${what}`,
  ];
  if (why) lines.push(`Why it matters: ${why}`);
  lines.push("What I need: a short answer, or the name of the person who can give it.");
  if (meanwhile) lines.push("", `Until I hear back: ${meanwhile}`);
  lines.push("", "Thank you.");
  return lines.join("\n");
}
