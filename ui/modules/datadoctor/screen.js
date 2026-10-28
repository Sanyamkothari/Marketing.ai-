// The one Data Doctor screen (Plan O M247, DEC-1817; the design §13): one component, opened on a case, in three
// places, behind `data_doctor.unified`:
//
//   the Data page          `#/data/check`: the newest case (or a new one), with no goal; "Choose this goal" sets the
//                          goal on the same case and continues in the "New campaign" wizard, where nothing answered
//                          is asked again;
//   the wizard's Customers the goal's case (the one just continued from the Data page, else the goal's newest) in
//   step                   the card's two mount points `[data-cw-guided-main]` / `[data-cw-guided-aside]`; Approve
//                          only approves: the run starts from "Start the campaign" under the sentence, as on the
//                          Data page;
//   a goal's setup         the Setup tab of `#/uc/<goal>`, in the same two places Guided setup mounts into.
//
// The wizard and a goal's setup reach it through the setup mode `index.js` registers (`mountDataDoctorSetup`), so the
// screen sits where Guided setup used to be and no route or page changes.
//
// The way in is a chat (`chat.js`): the server's `view.conversation` as cards, a composer, the data beside it and ONE
// "Approve and start" card (`approveAndStart`). "See details (for experts)" opens the full plan view below it, as it
// was; "Back to the chat" returns. An answer without a conversation (an older server) draws the plan view.
//
// The case is the server's: every change is sent with the version read and an idempotency key, and the screen is
// drawn again from `GET /cases/{case_id}/screen`. The only state kept here is which case is open, a chat draft and
// what is busy. One flow: nothing in this file asks a person to go anywhere else to finish.

import { ApiError, getIndustries, getProfile, postUpload } from "../../api.js";
import { announceStatus, errorBox, esc } from "../../dom.js";
import { aiServiceStatus, AI_SERVICE_EVENT } from "../../availability.js";
import { button, emptyState } from "../../components.js";
import { canAccess } from "../router.js";
import { createConnectionPicker } from "../connections/picker.js";
import { getModelOffer, postCampaignRun } from "../campaigns/wizard_api.js";
import { DEFAULT_SPEED, SPEEDS, applySpeedMinutes, minutesText, modelChoice, runHref, startSentenceHtml } from "../campaigns/run_rules.js";
import { TABLE_FILE_TYPES } from "../intake/notes.js";
import { injectAskStyles } from "../ask/styles.js";
import { plain } from "../ask/sent.js";
import { userText } from "../../labels.js";
import {
  acknowledgeStop,
  addFile,
  answerQuestion,
  approveCase,
  chooseGoal,
  confirmItem,
  createCase,
  dataRequestUrl,
  decideItem,
  getCases,
  getScreen,
  resumeVisit,
  revokeStanding,
  sendChat,
  getAiStatus,
  giveAiConsent,
  parkItem,
} from "./api.js";
import { injectDataDoctorStyles } from "./styles.js";
import { sectionOf } from "./plan.js";
import { approveAllowed, cardOf, conversationHtml, headHtml, leavesOutHtml, panelHtml, progressText, sentenceWithMore } from "./chat.js";
import { checksHtml, funnelHtml, newLineHtml, planHtml, planModel, queueHtml, todoHtml } from "./plan_view.js";
import {
  approveHtml,
  chatHtml,
  doubtHtml,
  goalsHtml,
  headerHtml,
  leftOutOf,
  missingHtml,
  namesOf,
  requestHtml,
  requestsOf,
  standingHtml,
  technicalHtml,
  timelineHtml,
  uploadHtml,
} from "./view.js";

/** The wizard's Customers step for a goal: where the Data page continues. */
export const wizardHref = (goalId) => `#/campaigns/new/customers/${encodeURIComponent(goalId)}`;

const entries = new Map(); // place|goal|mode -> { main, aside, state, host, detail }
const remembered = new Map(); // goal -> the case just continued from the Data page

/** The case the Data page continued with for `goalId`, so the wizard opens that one. */
export const rememberCase = (goalId, caseId) => remembered.set(goalId, caseId);

function fresh() {
  return {
    caseId: null,
    screen: null,
    loading: true,
    busy: "", // "" | upload | answer | tick | agree | approve | goal | revoke | chat | continue
    uploadNote: "",
    pending: "", // what was clicked while the server answers ("answer:<q>|<option>", "undo:<item>", "goal:<goal>"): that control shows it is working
    panelOpen: false, // on a phone the data panel is one line until it is opened
    sent: new Map(), // files already uploaded for the next check ("name|size|date" -> the upload): a retry does not send them again
    error: null,
    notice: "",
    alertAt: "top", // where the notice or error is drawn: the approval card for what the Approve click said, else the top
    draft: "",
    ai: null, // true | false | null (not known)
    use: null, // the Doctor's AI on this case: { consent, spent, ... } (`GET /cases/{id}/ai`), null while not known
    started: null, // the case whose hand-off was started from here
    speed: DEFAULT_SPEED, // how long the model may learn: quick | balanced | thorough
    speedOpen: false, // the three choices are shown ("Change")
    offerFor: null, // which file and outcome `offer` was asked for
    offer: null, // next month: the model that could be reused (`GET /campaign-runs/model-offer`), else null
    mode: null, // "reuse" | "learn" as the person picked it on Start; null follows the hint
    startInfo: null, // { uploadId, rows } of the file the campaign starts from, for the sentence above Start
    changing: new Set(), // answered questions whose answers are shown again ("Change answer")
    read: new Set(), // changes needing their own yes whose "I have read this sentence" is ticked
    checked: new Map(), // plan sections the person has checked ("I have checked this section") -> what they held then
    knowing: new Map(), // "I don't know" panels open: "q:<question>" / "i:<item>" -> who would know (or null)
    stops: new Set(), // the chat's approval card: stops the person ticked "go on anyway" for (by key)
    confirms: new Set(), // ... and the changes they ticked "I agree" for (by item id)
    tab: null, // the data panel's file (upload id) and side ("after" | "before")
    side: "after",
  };
}

const levelOf = (host) => (host.level ? Number(host.level) : host.place === "data" ? 2 : 3);

// --- drawing ---------------------------------------------------------------------------------------------------

function section(title, inner, attrs, level) {
  if (!inner) return "";
  return `<section class="dds-section" ${attrs}><h${level} class="dds-section-t">${esc(title)}</h${level}>${inner}</section>`;
}

function mainHtml(entry) {
  const { state: s, host } = entry;
  const level = levelOf(host);
  const sub = level + 1;
  const err = s.error ? errorBox(s.error) : "";
  const notice = s.notice ? `<p class="dds-why" role="status" data-dds-notice>${esc(s.notice)}</p>` : "";
  if (s.loading && !s.screen) {
    return `<div class="dds" data-dds data-dds-place="${esc(host.place)}"><p class="loading" role="status">Opening Data Doctor…</p></div>`;
  }
  if (!s.screen && s.busy === "upload") {
    // The first files are being read: the chat is already there and says so (the start card returns if it fails).
    const bubble = `<div class="ddc-row ddc-agent" data-dds-reading><span class="ddc-avatar" aria-hidden="true">DD</span><div class="ddc-stack"><div class="ddc-bubble" data-dds-progress role="status"><span class="ddc-spin" aria-hidden="true"></span><span>${esc(progressText("upload"))}</span> <span class="dds-why">${userText(
      plain(s.uploadFirst || ""),
    )}${s.uploadMore ? esc(` and ${s.uploadMore} more`) : ""}</span></div></div></div>`;
    return `<div class="dds ddc" data-dds data-dds-place="${esc(host.place)}"><section class="ddc-conv" aria-label="Conversation with Data Doctor" data-dds-conversation>${bubble}</section></div>`;
  }
  if (!s.screen) {
    const title = host.goalId ? "Check the customers' file" : "Check my data";
    return `<div class="dds" data-dds data-dds-place="${esc(host.place)}"><section class="card dds-start" data-dds-start>${emptyState({
      icon: "data",
      title,
      text: "Data Doctor reads your file, asks only what it cannot tell, and gets it ready. It never changes your file.",
    })}<div class="card-body">${intakeHtml(entry, { first: true, quiet: true })}</div></section>${err}</div>`;
  }
  const screen = s.screen;
  if (isChat(entry)) return chatMainHtml(entry);
  const c = screen.case;
  const view = screen.view;
  const busy = Boolean(s.busy);
  const ctx = { busy, changing: s.changing, knowing: s.knowing, read: s.read, checked: s.checked };
  const model = planModel(screen, ctx);
  const queue = queueHtml(c, view, ctx);
  const checks = checksHtml(model.findings, {
    used: queue.asking + model.unconfirmed,
    ledger: model.ledger,
    items: model.items,
    level: sub,
  });
  const doubts = (c.doubts || []).map((d) => doubtHtml(d, c)).join("");
  const goalIds = view.goal_id ? [view.goal_id] : view.missing_for || [];
  const kit = dataRequestUrl(goalIds.slice(0, 4));
  const requests = `${requestsOf(c)
    .map((q) => requestHtml(q, c, { kitHref: kit, busy }))
    .join("")}${view.mode === "generic" ? missingHtml(view, kit, c) : ""}`;
  const earlier = standingHtml(view, { busy, level: sub });
  const proceed = continueHtml(entry);
  const more = c.files && c.files.length && !c.read_only ? `<div class="dds-more" data-dds-more>${intakeHtml(entry, { first: false })}</div>` : "";
  const back = chatShown(screen)
    ? `<div class="dds-actions">${button({ label: "Back to the chat", kind: "secondary", attrs: "data-dds-back-to-chat" })}</div>`
    : "";
  return `<div class="dds" data-dds data-dds-place="${esc(host.place)}" data-dds-case="${esc(c.case_id)}">${back}<section class="card" data-dds-header>${headerHtml(
    screen,
    { level },
  )}</section>${notice}${err}${proceed}${newLineHtml(model)}${todoHtml(view.verdict.key, {
    asking: queue.asking,
    unchecked: model.gate.unchecked.length,
    unconfirmed: model.unconfirmed,
    stops: model.gate.stops.length,
  })}${funnelHtml(model.items, model.ledger, { level: sub })}${checks}${section(
    "Questions",
    queue.html,
    "data-dds-questions",
    sub,
  )}${planHtml(screen, model, ctx, { level: sub })}${section("Not sure", doubts, "data-dds-doubts", sub)}${section(
    "More data",
    requests,
    "data-dds-requests",
    sub,
  )}${earlier ? `<section class="card dds-card" data-dds-checklist>${earlier}</section>` : ""}${goalsHtml(screen, {
    busy,
    level: sub,
  })}${approveHtml(screen, { busy: s.busy, level: sub, gate: model.gate })}${
    !(c.files || []).length ? intakeHtml(entry, { first: true }) : more
  }${timelineHtml(screen, { level: sub })}${technicalHtml(screen)}${expertHtml(entry)}${
    host.place === "data" ? `<div class="dds-actions">${button({ label: "Start a new check", kind: "tertiary", attrs: "data-dds-new" })}</div>` : ""
  }</div>`;
}

/** A file from this computer, or - once picked - the connection picker in its place ("Pick from a connection"). */
function intakeHtml(entry, options) {
  const s = entry.state;
  if (s.picker) return `<section class="card dds-card" data-dds-picker>${s.picker.html()}</section>`;
  return uploadHtml({ accept: TABLE_FILE_TYPES, busy: s.uploadNote, pick: true, ...options });
}

/** "Expert view": the way from the Doctor to Manual setup for this goal (its own record is not kept there). */
function expertHtml(entry) {
  const { state: s, host } = entry;
  const goal = (s.screen && s.screen.view && s.screen.view.goal_id) || host.goalId;
  if (!goal) return "";
  const open = host.manual
    ? button({ label: "Expert view", kind: "tertiary", attrs: "data-dds-expert" })
    : button({ label: "Expert view", kind: "tertiary", href: `#/uc/${encodeURIComponent(goal)}/manual`, attrs: "data-dds-expert" });
  return `<div class="dds-actions" data-dds-expert-row>${open}<span class="dds-why">Set this up by hand. Changes made there are not part of the Data Doctor's record.</span></div>`;
}

/** The three ways to train, shown after "Change" on the sentence above Start. */
function speedsHtml(entry) {
  const chosen = entry.state.speed;
  const items = SPEEDS.map((speed) =>
    button({
      label: `${speed.name}: about ${minutesText(speed.minutes)}${speed.recommended ? " (recommended)" : ""}`,
      kind: chosen === speed.id ? "primary" : "secondary",
      size: "sm",
      attrs: `data-dds-speed="${speed.id}" aria-pressed="${chosen === speed.id}"`,
    }),
  ).join("");
  return `<div class="dds-actions" role="group" aria-label="How long to learn" data-dds-speeds>${items}</div>`;
}

/** Next month: "Use last month's model" or "Learn again on the new data", the hint, and why reusing is off. */
function modelChoiceHtml(choice) {
  const pick = (mode, label, disabled) =>
    button({
      label,
      kind: (choice.reuse ? "reuse" : "learn") === mode ? "primary" : "secondary",
      size: "sm",
      disabled,
      attrs: `data-dds-mode="${mode}" aria-pressed="${(choice.reuse ? "reuse" : "learn") === mode}"`,
    });
  const why = choice.reason ? `<p class="dds-why" data-dds-reuse-off>${esc(choice.reason)}</p>` : "";
  const hint = choice.hint ? `<p class="dds-why" data-dds-offer-hint>${esc(choice.hint)}</p>` : "";
  return `<div class="dds-actions" role="group" aria-label="Which model to use" data-dds-modes>${pick(
    "reuse",
    "Use last month's model (fast)",
    !choice.canReuse,
  )}${pick("learn", "Learn again on the new data (slower)", false)}</div>${why}${hint}`;
}

/** The sentence above Start: what is learned from, how many customers, how long it takes, and "Change". */
function startSentence(entry) {
  const { state: s, host } = entry;
  const files = s.screen.case.files || [];
  const startId = s.screen.view.start.upload_id;
  const file = files.find((f) => f.upload_id === startId) || files[0];
  const rows = s.startInfo && s.startInfo.uploadId === startId ? s.startInfo.rows : null;
  const choice = host.place !== "setup" ? modelChoice(s.offer, s.mode) : null; // the Data page and the wizard both offer it
  const reuse = choice ? choice.reuse : host.place === "setup" && Boolean(host.modelVersionId); // reusing: no time to choose
  const change = reuse
    ? ""
    : ` ${button({ label: "Change", kind: "tertiary", size: "sm", attrs: `data-dds-speed-open aria-expanded="${s.speedOpen}"` })}`;
  const sentence = startSentenceHtml({ table: file ? plain(file.file_name) : "", customers: rows, speed: s.speed, reuse });
  return `${choice ? modelChoiceHtml(choice) : ""}<p class="dds-why" data-dds-start-sentence>${sentence}${change}</p>${
    s.speedOpen && !reuse ? speedsHtml(entry) : ""
  }<p class="dds-why" data-dds-start-leave>You can close this page.</p>`;
}

/** What starts the campaign (or fills the goal's setup): the sentence above Start, and the Start button. */
function startParts(entry) {
  const { state: s, host } = entry;
  const view = s.screen && s.screen.view;
  if (!view || !["ready", "ready_with_warnings"].includes(view.verdict.key) || !view.start) return null;
  // Here the campaign starts (the wizard's Start, which also offers last month's model or learning again, or the Data page's); else the goal's setup or the wizard goes on.
  const starts =
    host.place !== "setup" && (host.approved ? true : view.start.target === "runs" && canAccess("POST", "/campaign-runs"));
  const label = starts ? "Start the campaign" : host.approved ? "Continue with this data" : "Continue to the campaign";
  const action = `<div class="dds-actions">${button({
    label,
    kind: "primary",
    attrs: "data-dds-continue",
    disabled: Boolean(s.busy),
    busy: s.busy === "continue",
  })}</div>`;
  return { sentence: starts ? startSentence(entry) : "", action };
}

/** Whether "Approve" goes on to start a campaign here (the Data page, the wizard) or only fills the goal's setup / hands on to the wizard. */
function approveStarts(entry) {
  const { host } = entry;
  return host.place !== "setup" && (host.approved ? true : canAccess("POST", "/campaign-runs"));
}

/** Above the approve button: what happens after the click (speed picked above; with last month's model on offer it is chosen after). */
function approveSentence(entry) {
  const { state: s } = entry;
  if (modelChoice(s.offer, s.mode)) return "";
  const files = s.screen.case.files || [];
  const file = files[0];
  // The minutes are the speed buttons' (one place, one set of numbers): the sentence does not say them again.
  const ready = cardOf(s.screen, "ready");
  const sentence = startSentenceHtml({ table: file ? plain(file.file_name) : "", speed: s.speed, time: !(ready && (ready.speeds || []).length) });
  return `<p class="dds-why" data-dds-approve-start-sentence>${sentence}</p><p class="dds-why" data-dds-start-leave>You can close this page.</p>`;
}

/** Ready for the goal: one action on - start the campaign (Data page, wizard) or fill the goal's setup (setup). */
function continueHtml(entry) {
  const parts = startParts(entry);
  if (!parts) return "";
  const left = leftOutOf(entry.state.screen).length;
  const without = left
    ? `<p class="dds-why" data-dds-continue-without>${esc(
        `Continuing uses the data without the ${left === 1 ? "change" : `${left} changes`} left out above. Someone can still agree to ${left === 1 ? "it" : "them"} first.`,
      )}</p>`
    : "";
  return `<section class="card dds-continue" data-dds-ready><p class="dds-why">The data is ready for this goal. Approved, checked and saved with this check.</p>${without}${parts.sentence}${parts.action}</section>`;
}

// --- the chat-first screen ----------------------------------------------------------------------------------------

/** Whether the server sent a conversation to draw (an older answer has none: today's plan view is drawn then). */
const chatShown = (screen) => Boolean(screen && screen.case && (screen.case.files || []).length && !screen.case.read_only && screen.view.conversation && screen.view.conversation.length);

/** The chat is drawn unless "See details" opened the full plan view. */
const isChat = (entry) => !entry.detail && chatShown(entry.state.screen);

function chatMainHtml(entry) {
  const { state: s, host } = entry;
  const screen = s.screen;
  const level = levelOf(host);
  const err = s.error ? errorBox(s.error) : "";
  const notice = s.notice ? `<p class="dds-why" role="status" data-dds-notice>${esc(s.notice)}</p>` : "";
  const ready = cardOf(screen, "ready");
  // What the Approve click says (the sentence changed, a safety check changed, a refusal) is drawn in the approval card,
  // next to the button the person just clicked; at the top of a long page it would be out of sight.
  // The card's learning times are the server's; the sentence above Approve says the same ones (before a goal is chosen
  // no offer has brought them in yet).
  // The learning time answered in See details opens the buttons on it (once per answer: a later click on a button wins).
  if (ready && ready.speed && s.speedFrom !== ready.speed) {
    s.speedFrom = ready.speed;
    s.speed = ready.speed;
  }
  if (ready && (ready.speeds || []).length) applySpeedMinutes(Object.fromEntries(ready.speeds.map((sp) => [sp.id, sp.minutes])));
  const alertInCard = s.alertAt === "card" && Boolean(ready && ["approve", "start"].includes(ready.stage));
  const parts = ready && ready.stage === "start" ? startParts(entry) : null;
  const starts = approveStarts(entry);
  const goalIds = screen.view.goal_id ? [screen.view.goal_id] : screen.view.missing_for || [];
  const openSent = new Set([...entry.main.querySelectorAll("details[data-ag-sent][open]")].map((el) => el.getAttribute("data-ag-sent")));
  const conversation = conversationHtml(screen, {
    busy: s.busy,
    pending: s.pending,
    stops: s.stops,
    confirms: s.confirms,
    speed: s.speed,
    startHtml: parts ? `${parts.sentence}${parts.action}` : "",
    starts,
    approveHtml: ready && ready.stage === "approve" && starts ? approveSentence(entry) : "",
    alertHtml: alertInCard ? `${notice}${err}` : "",
    openSent,
    kitHref: (ids) => dataRequestUrl((ids.length ? ids : goalIds).slice(0, 4)),
    composer: {
      ai: s.ai,
      use: s.use,
      draft: s.draft,
      busy: s.busy,
      canConnect: canAccess("PUT", "/ai-service/{slot}"),
      canConsent: canAccess("POST", "/cases/{case_id}/ai-consent"),
    },
  });
  const intake = screen.case.read_only ? "" : intakeHtml(entry, { first: false });
  const panel = panelHtml(screen, { tab: s.tab, side: s.side, intake, busy: Boolean(s.busy), open: s.panelOpen });
  return `<div class="dds ddc" data-dds data-dds-chat-view data-dds-place="${esc(host.place)}" data-dds-case="${esc(screen.case.case_id)}">${headHtml(screen, { level, titled: host.place !== "wizard" })}${alertInCard ? "" : `${notice}${err}`}<div class="ddc-layout">${conversation}${panel}</div>${
    host.place === "data" ? `<div class="dds-actions">${button({ label: "Start a new check", kind: "tertiary", attrs: "data-dds-new" })}</div>` : ""
  }</div>`;
}

function asideHtml(entry) {
  const { state: s, host } = entry;
  if (!s.screen || isChat(entry)) return ""; // the chat view has its own composer and data panel
  const openSent = new Set(
    [...entry.aside.querySelectorAll("details[data-ag-sent][open]")].map((el) => el.getAttribute("data-ag-sent")),
  );
  return chatHtml(s.screen, {
    ai: s.ai,
    draft: s.draft,
    busy: s.busy === "chat",
    canConnect: canAccess("PUT", "/ai-service/{slot}"),
    openSent,
    level: levelOf(host) + 1,
    use: s.use,
    canConsent: canAccess("POST", "/cases/{case_id}/ai-consent"),
  });
}

function draw(entry) {
  const openTimeline = Boolean(entry.main.querySelector("details[data-dds-timeline][open]"));
  // Folded parts (sure fixes, questions that can wait, more notes) stay open across a repaint.
  const kept = new Set([...entry.main.querySelectorAll("details[data-dds-keep][open]")].map((el) => el.getAttribute("data-dds-keep")));
  entry.main.innerHTML = mainHtml(entry);
  if (openTimeline) {
    const timeline = entry.main.querySelector("details[data-dds-timeline]");
    if (timeline) timeline.open = true;
  }
  for (const el of entry.main.querySelectorAll("details[data-dds-keep]")) {
    if (kept.has(el.getAttribute("data-dds-keep"))) el.open = true;
  }
  entry.aside.innerHTML = asideHtml(entry);
  entry.aside.hidden = !entry.state.screen || isChat(entry);
  followChat(entry);
  if (entry.state.picker) entry.state.picker.bind(entry.main);
}

/**
 * New cards in the conversation scroll into view, as a chat does. The approval card always stays last, so it is not
 * counted: a reply that lands above it is what the person is taken to (the newest message, else the first new card).
 */
function followChat(entry) {
  const cards = [...entry.main.querySelectorAll("[data-dds-conversation] > :not([data-dds-composer]):not([data-dds-reading])")].filter((el) => !el.querySelector("[data-dds-ready]"));
  const seen = entry.cardsSeen;
  entry.cardsSeen = cards.length;
  if (!(seen > 0 && cards.length > seen)) return;
  const fresh = cards.slice(seen);
  const said = fresh.filter((el) => el.querySelector("[data-dds-msg]"));
  const target = said.length ? said[said.length - 1] : fresh[0];
  if (target && typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "nearest" });
}

// --- behaviour -------------------------------------------------------------------------------------------------

async function refresh(entry) {
  const s = entry.state;
  if (!s.caseId) {
    s.screen = null;
    return;
  }
  s.screen = await getScreen(s.caseId);
  s.use = await getAiStatus(s.caseId).catch(() => null);
  if (s.ai === null) await readAi(entry); // not known yet: ask again (the answer is shared for a few seconds)
  await readStartInfo(entry);
  await readOffer(entry);
}

/** Next month: can last month's model score this file? Not in a goal's setup; only once the file is ready to start. */
async function readOffer(entry) {
  const s = entry.state;
  const start = s.screen && s.screen.view && s.screen.view.start;
  if (!start || entry.host.place === "setup" || !["ready", "ready_with_warnings"].includes(s.screen.view.verdict.key)) {
    s.offer = null;
    s.offerFor = null;
    return;
  }
  const asked = `${start.goal_id}|${start.upload_id}|${start.outcome}`; // asked again only when one of these changes
  if (s.offerFor === asked) return;
  s.offer = null;
  s.offerFor = asked;
  try {
    s.offer = await getModelOffer(start.goal_id, start.upload_id, s.screen.case.client_id, start.outcome);
  } catch {
    s.offerFor = null; // no offer (the first month, or it cannot be asked): Start only learns; ask again next time
  }
}

/** How many customers the file the campaign starts from holds, for the sentence above Start (no sentence without). */
async function readStartInfo(entry) {
  const s = entry.state;
  const start = s.screen && s.screen.view && s.screen.view.start;
  if (!start || entry.host.place === "setup" || (s.startInfo && s.startInfo.uploadId === start.upload_id)) return;
  s.startInfo = { uploadId: start.upload_id, rows: null };
  try {
    const profile = await getProfile(start.upload_id);
    s.startInfo.rows = Number.isFinite(profile && profile.row_count) ? profile.row_count : null;
  } catch {
    // the sentence leaves the number out
  }
}

/** Run one change, then draw the case as the server has it now. A stale version reloads and says so. */
async function act(entry, busy, work, { announce = "", pending = "" } = {}) {
  const s = entry.state;
  if (s.busy) return;
  s.busy = busy;
  s.pending = pending;
  s.error = null;
  s.notice = "";
  s.alertAt = busy === "approve" ? "card" : "top";
  draw(entry);
  try {
    await work();
    await refresh(entry);
    if (announce) announceStatus(announce);
  } catch (error) {
    if (error instanceof ApiError && (error.code === "CASE_VERSION_CONFLICT" || error.code === "APPROVAL_SENTENCE_CHANGED")) {
      s.notice =
        error.code === "CASE_VERSION_CONFLICT"
          ? "This check changed meanwhile. Here it is as it is now; nothing you did was lost."
          : "What you would approve has changed. Read it again below.";
      try {
        await refresh(entry);
      } catch (again) {
        s.error = again;
      }
    } else {
      s.error = error;
    }
  }
  s.busy = "";
  s.pending = "";
  draw(entry);
}

const theCase = (entry) => entry.state.screen.case;

/** The goal an upload is read for: the host's goal, else the first goal on offer (as Data Doctor's page did). */
async function uploadGoal(host) {
  if (host.goalId) return host.goalId;
  const payload = await getIndustries();
  const industry =
    (payload.industries || []).find((i) => i.id === payload.default_industry) || (payload.industries || [])[0];
  for (const stage of (industry && industry.stages) || []) {
    for (const card of stage.use_cases || []) {
      if (card.status === "available" && card.ai_type !== "generative") return card.id;
    }
  }
  throw new ApiError(0, "DOCTOR_NO_USE_CASES", "There is no goal to read the file for.", null);
}

/** Put the files the server now holds (uploaded, or imported from a connection) into the open case, or a new one. */
async function bringIn(entry, name, getUploads, first = name, more = 0) {
  const s = entry.state;
  const host = entry.host;
  s.uploadNote = `Reading ${name}…`;
  s.uploadFirst = first;
  s.uploadMore = more;
  await act(
    entry,
    "upload",
    async () => {
      const ids = (await getUploads()).map((answer) => answer.upload_id);
      if (!s.caseId) {
        const made = await createCase(ids, name);
        s.caseId = made.case_id;
        let c = await resumeVisit(made.case_id);
        if (host.goalId) c = await chooseGoal(c, host.goalId);
      } else {
        let c = theCase(entry);
        const held = new Set((c.files || []).map((f) => f.upload_id));
        for (const id of ids) if (!held.has(id)) c = await addFile(c, id);
        await resumeVisit(s.caseId);
      }
    },
    { announce: "File read" },
  );
  s.uploadNote = "";
  if (!s.error) s.sent.clear();
  draw(entry);
}

/** One or several files from this computer, read together as one check. */
function upload(entry, files) {
  const list = [...files];
  const name = list.length > 1 ? `${list[0].name} and ${list.length - 1} more` : list[0].name;
  return bringIn(entry, name, async () => {
    const goal = await uploadGoal(entry.host);
    const sent = entry.state.sent;
    for (const file of list) {
      const key = `${file.name}|${file.size}|${file.lastModified}`;
      if (sent.has(key)) continue;
      try {
        sent.set(key, await postUpload(file, goal, entry.host.mode || "train"));
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        const done = sent.size ? ` ${sent.size === 1 ? "1 file" : `${sent.size} files`} before it went through and will not be sent again.` : "";
        // A 4xx about the file says it could not be read; a network or sign-in problem keeps its own words.
        const about = error.status >= 400 && error.status < 500 && ![401, 403, 429].includes(error.status) ? "could not be read." : "was not sent.";
        throw new ApiError(error.status, error.code, `${file.name} ${about} ${error.message}${done} Choose ${file.name} again to go on.`, error.body);
      }
    }
    return [...sent.values()];
  }, list[0].name, list.length - 1);
}

/** "Pick from a connection": the picker's import answers what `POST /uploads` does, so it carries on as a file does. */
async function pickFromConnection(entry) {
  const s = entry.state;
  if (s.picker) return;
  try {
    const useCaseId = await uploadGoal(entry.host);
    s.picker = createConnectionPicker({
      useCaseId,
      mode: entry.host.mode || "train",
      redraw: () => draw(entry),
      onCancel: () => {
        s.picker = null;
        draw(entry);
      },
      onImported: (imported, label) => {
        s.picker = null;
        bringIn(entry, label, async () => [imported]);
      },
    });
  } catch (error) {
    s.error = error;
    s.alertAt = "top";
    draw(entry);
    return;
  }
  draw(entry);
  s.picker.start();
}

/**
 * The hand-off. Goal setup gets the prepared file and the chosen learning time from `host.approved` (it fills its
 * form). On the Data page and in the wizard the campaign run starts from here, and the person lands on its progress
 * page: they can close the browser.
 */
async function proceed(entry) {
  const s = entry.state;
  const view = s.screen && s.screen.view;
  if (!view || !view.start) return;
  const start = view.start;
  const runsHere = start.target === "runs" && canAccess("POST", "/campaign-runs");
  // The wizard starts the run from here too: only this screen knows next month's choice (last month's model or learn again).
  if (!entry.host.approved || (entry.host.place === "wizard" && runsHere)) {
    if (!runsHere) {
      rememberCase(start.goal_id, s.caseId);
      window.location.hash = wizardHref(start.goal_id);
      return;
    }
    await startCampaignRun(entry, start);
    return;
  }
  s.busy = "continue";
  draw(entry);
  let profile = null;
  try {
    profile = await getProfile(start.upload_id);
  } catch (error) {
    s.error = error;
    s.alertAt = "card";
    s.busy = "";
    draw(entry);
    return;
  }
  s.busy = "";
  s.started = s.caseId;
  draw(entry);
  entry.host.approved({
    mode: entry.host.mode || "train",
    upload: { upload_id: start.upload_id, profile },
    primaryKey: start.primary_key,
    target: start.outcome,
    overrides: {},
    speed: s.speed,
    caseId: s.caseId,
  });
}

/** `POST /campaign-runs` with the prepared file, the goal and the learning time; then its progress page. */
async function startCampaignRun(entry, start) {
  const s = entry.state;
  s.busy = "continue";
  s.error = null;
  s.alertAt = "card"; // a refused start is told in the card with the Start button, not at the top of a long page
  draw(entry);
  try {
    const choice = modelChoice(s.offer, s.mode);
    const created = await postCampaignRun({
      use_case: start.goal_id,
      upload_id: start.upload_id,
      primary_key: start.primary_key,
      target: start.outcome, // also when reusing: the server checks the model learned this outcome
      ...(choice && choice.reuse ? { model_version_id: choice.modelVersionId } : { speed: s.speed }),
      case_id: s.caseId,
    });
    s.started = s.caseId;
    s.busy = "";
    window.location.hash = runHref(created.record.campaign_run_id);
  } catch (error) {
    s.error = error;
    s.busy = "";
    draw(entry);
  }
}

async function approve(entry) {
  const sentence = entry.state.screen.view.approval_sentence;
  await act(entry, "approve", () => approveCase(theCase(entry), sentence), { announce: "Approved" });
  const screen = entry.state.screen;
  const view = screen && screen.view;
  // Approving never starts a campaign run: that is the one "Start the campaign" button below the sentence, on the
  // Data page and in the wizard alike. Only the goal's setup (which just fills its form, nothing runs) goes straight
  // on, unless a change that needs its own yes is left out: then continuing without it is its own choice.
  if (
    view &&
    ["ready", "ready_with_warnings"].includes(view.verdict.key) &&
    view.start &&
    entry.host.approved &&
    entry.host.place === "setup" &&
    !leftOutOf(screen).length
  ) {
    await proceed(entry);
  }
}

/**
 * The one "Approve and start" click of the chat: the person's own yeses (the changes in the plan that need one, and any
 * left-out change they ticked "Also include"), then Approve, then the start that was already there. Every server rule
 * stays: each own yes is sent with its exact sentence, each stop is said yes to by its key, and Approve carries the
 * sentence the person read.
 *
 * The order is the one the server needs. Agreeing to a change that was not in the plan puts it there, which counts the
 * plan's checks again and drops yeses said to stops, so the own yeses go first; the stops are read from the plan as it
 * stands then, and a stop the person did not tick ends the chain (the card shows it). The sentence is read again after
 * the yeses: when it moved only by the changes they just agreed to ("with 4 changes" -> "with 5 changes"), the click
 * goes on and approves the sentence as it reads now; any other difference approves nothing, and the card shows the new
 * sentence to read.
 */
async function approveAndStart(entry) {
  const s = entry.state;
  const card = cardOf(s.screen, "ready");
  if (s.busy || !card || card.stage !== "approve" || !approveAllowed(card, s)) return;
  const latest = async () => {
    s.screen = await getScreen(s.caseId);
    return s.screen;
  };
  let go = false;
  await act(
    entry,
    "approve",
    async () => {
      let added = 0; // left-out changes the person ticked: each one the yes puts into the plan
      const recorded = new Set();
      for (const yes of card.confirmations) {
        if (!yes.can_agree || !(yes.required || s.confirms.has(yes.item_id))) continue;
        await confirmItem(theCase(entry), yes.item_id, yes.sentence);
        recorded.add(yes.item_id);
        if (!yes.required) added += 1;
        s.confirms.delete(yes.item_id);
        await latest();
      }
      const now = cardOf(await latest(), "ready");
      if (!now || now.stage !== "approve") return; // the plan moved under the click: the screen shows where it is
      const only = now.sentence === card.sentence || (added > 0 && now.sentence === sentenceWithMore(card.sentence, added));
      if (!only || now.blocked || now.hold || (now.confirmations || []).some((c) => c.required && c.can_agree && !recorded.has(c.item_id))) {
        s.notice = only
          ? "Your yes is recorded. Something else needs a look first, then click Approve and start."
          : "Your yes is recorded. What you approve has changed, so read the sentence again, then click Approve and start.";
        return;
      }
      for (const stop of now.stops) {
        if (stop.acknowledgeable === false || !s.stops.has(stop.key)) {
          s.notice = "A safety check changed. Read it below, tick it if you agree, then click Approve and start.";
          return;
        }
        await acknowledgeStop(theCase(entry), stop.key, "Ticked in the approval card.");
        s.stops.delete(stop.key);
        await latest();
      }
      // Approve carries the sentence that was checked above, never the server's fresh one: if acknowledging a stop moved it,
      // nothing is approved and the card shows the new sentence to read.
      const last = cardOf(s.screen, "ready");
      if (!last || last.stage !== "approve" || last.sentence !== now.sentence) {
        s.notice = "Your yes is recorded. What you approve has changed, so read the sentence again, then click Approve and start.";
        return;
      }
      await approveCase(theCase(entry), now.sentence);
      go = true;
    },
    { announce: "Approved" },
  );
  const view = s.screen && s.screen.view;
  if (!go || s.error || !view || !["ready", "ready_with_warnings"].includes(view.verdict.key) || !view.start) return;
  if (modelChoice(s.offer, s.mode)) {
    // Next month: last month's model or learning again is the person's choice, made on the card that is now "Ready to start".
    s.notice = "Approved. Choose how to start below.";
    draw(entry);
    return;
  }
  await proceed(entry);
}

async function pickGoal(entry, goalId) {
  const s = entry.state;
  await act(entry, "goal", () => chooseGoal(theCase(entry), goalId), { announce: "Goal chosen", pending: `goal:${goalId}` });
  if (!s.error && entry.host.place === "data") {
    rememberCase(goalId, s.caseId);
    window.location.hash = wizardHref(goalId);
  }
}

function bind(entry) {
  const root = entry.main;
  root.addEventListener("click", (event) => {
    const target = event.target && event.target.closest ? event.target.closest("button, a") : null;
    if (!target || target.disabled) return;
    const d = target.dataset;
    if (d.ddsUndo) {
      const item = (theCase(entry).items || []).find((i) => i.item_id === d.ddsUndo);
      act(entry, "tick", () => decideItem(theCase(entry), d.ddsUndo, false), { announce: item && item.state === "accepted" ? "Change undone" : "Change skipped", pending: `undo:${d.ddsUndo}` });
    } else if (d.ddsUse) act(entry, "tick", () => decideItem(theCase(entry), d.ddsUse, true), { announce: "Change added", pending: `use:${d.ddsUse}` });
    else if (target.hasAttribute("data-dds-panel-toggle")) togglePanel(entry);
    else if (d.ddsPrompt) talk(entry, d.ddsPrompt);
    else if (target.hasAttribute("data-dds-approve-start")) approveAndStart(entry);
    else if (target.hasAttribute("data-dds-see-details")) {
      entry.detail = true;
      draw(entry);
    } else if (target.hasAttribute("data-dds-back-to-chat")) {
      entry.detail = false;
      draw(entry);
    } else if (d.ddsFileTab) {
      entry.state.tab = d.ddsFileTab;
      draw(entry);
    } else if (d.ddsSide) {
      entry.state.side = d.ddsSide;
      draw(entry);
    } else if (target.hasAttribute("data-dds-ai-consent")) {
      act(entry, "consent", () => giveAiConsent(entry.state.caseId), { announce: "The Doctor's AI may now read masked summaries" });
    } else if (target.hasAttribute("data-dds-dont-know") && d.ddsQ) {
      // "I don't know" first asks who would know; the safe choice is one click further, never a guess.
      openWho(entry, `q:${d.ddsQ}`, `[data-dds-who-pick]`);
    } else if (d.ddsAnswer && d.ddsQ) {
      // An answer that asks for a typed date reads it from the field beside the button; without one it asks for it.
      const field = target.closest("li")?.querySelector("input[data-dds-value]");
      const typed = field ? field.value : "";
      if (field && !typed) {
        field.focus();
        return;
      }
      act(
        entry,
        "answer",
        async () => {
          await answerQuestion(theCase(entry), d.ddsQ, d.ddsAnswer, typed);
          entry.state.changing.delete(d.ddsQ);
          entry.state.knowing.delete(`q:${d.ddsQ}`);
        },
        { announce: "Answer saved", pending: `answer:${d.ddsQ}|${d.ddsAnswer}` },
      );
    } else if (d.ddsItemDk) openWho(entry, `i:${d.ddsItemDk}`, `[data-dds-who-pick]`);
    else if (d.ddsWhoPick) {
      entry.state.knowing.set(d.ddsWhoFor, d.ddsWhoPick);
      draw(entry);
      focusOn(entry, `[data-dds-who-pick="${d.ddsWhoPick}"]`, d.ddsWhoFor);
    } else if (d.ddsWhoClose) {
      entry.state.knowing.delete(d.ddsWhoClose);
      draw(entry);
    } else if (d.ddsCopy) copyMessage(entry, d.ddsCopy);
    else if (d.ddsPark) {
      act(
        entry,
        "park",
        async () => {
          await parkItem(theCase(entry), d.ddsPark, d.ddsParkRole);
          entry.state.knowing.delete(`i:${d.ddsPark}`);
        },
        { announce: "Step parked until they answer" },
      );
    } else if (d.ddsResume) act(entry, "resume", () => decideItem(theCase(entry), d.ddsResume, false), { announce: "Step is open again" });
    else if (d.ddsAck) act(entry, "ack", () => acknowledgeStop(theCase(entry), d.ddsAck), { announce: "Noted: you said yes to this" });
    else if (d.ddsGoto) goTo(entry, d.ddsGoto);
    else if (d.ddsChange) toggleChange(entry, d.ddsChange);
    else if (d.ddsAgree) {
      const found = entry.state.screen.view.approve_class.find((a) => a.item_id === d.ddsAgree);
      if (found && entry.state.read.has(found.item_id)) {
        act(
          entry,
          "agree",
          async () => {
            await confirmItem(theCase(entry), found.item_id, found.sentence);
            entry.state.read.delete(found.item_id);
          },
          { announce: "Agreed" },
        );
      }
    } else if (target.hasAttribute("data-dds-approve-button")) approve(entry);
    else if (d.ddsRevoke) act(entry, "revoke", () => revokeStanding(theCase(entry), d.ddsRevoke), { announce: "Answer revoked" });
    else if (d.ddsGoal) pickGoal(entry, d.ddsGoal);
    else if (target.hasAttribute("data-dds-continue")) proceed(entry);
    else if (d.ddsSpeed) {
      entry.state.speed = d.ddsSpeed;
      draw(entry);
    } else if (d.ddsMode) {
      entry.state.mode = d.ddsMode;
      draw(entry);
    } else if (target.hasAttribute("data-dds-speed-open")) {
      entry.state.speedOpen = !entry.state.speedOpen;
      draw(entry);
    }
    else if (target.hasAttribute("data-dds-pick-connection")) pickFromConnection(entry);
    else if (target.hasAttribute("data-dds-expert") && entry.host.manual) entry.host.manual();
    else if (target.hasAttribute("data-dds-new")) {
      entry.state = { ...fresh(), loading: false, ai: entry.state.ai };
      draw(entry);
    }
  });
  root.addEventListener("change", (event) => {
    const input = event.target;
    if (!input) return;
    if (input.matches && input.matches("input[data-dds-tick]")) {
      const ticked = (entry.state.screen.case.items || []).find((i) => i.item_id === input.dataset.ddsTick);
      if (ticked) entry.state.checked.delete(sectionOf(ticked)); // what changed there is looked at again
      act(entry, "tick", () => decideItem(theCase(entry), input.dataset.ddsTick, input.checked));
    } else if (input.matches && input.matches("input[data-dds-sec-ok]")) {
      const id = input.dataset.ddsSecOk;
      if (input.checked) entry.state.checked.set(id, input.dataset.ddsSecSig || "");
      else entry.state.checked.delete(id);
      draw(entry);
      focusOn(entry, `input[data-dds-sec-ok="${id}"]`);
    } else if (input.matches && (input.matches("input[data-dds-stop]") || input.matches("input[data-dds-confirm]"))) {
      // The approval card's ticks: kept in the state, and "Approve and start" turns on or off in place (the focus stays).
      const stop = input.matches("input[data-dds-stop]");
      const set = stop ? entry.state.stops : entry.state.confirms;
      const id = stop ? input.dataset.ddsStop : input.dataset.ddsConfirm;
      if (input.checked) set.add(id);
      else set.delete(id);
      const go = root.querySelector("[data-dds-approve-start]");
      const ready = cardOf(entry.state.screen, "ready");
      if (go) go.disabled = Boolean(entry.state.busy) || !approveAllowed(ready, entry.state);
      const note = root.querySelector("[data-dds-approve-note]");
      if (note && ready) note.innerHTML = leavesOutHtml(ready, { names: namesOf(entry.state.screen.case), confirms: entry.state.confirms, starts: approveStarts(entry) });
    } else if (input.matches && input.matches("input[data-dds-read]")) {
      // "I have read this sentence": kept for the next drawing, and turns its "I agree" on or off in place, so
      // the focus stays on the box.
      const itemId = input.dataset.ddsRead;
      if (input.checked) entry.state.read.add(itemId);
      else entry.state.read.delete(itemId);
      const card = input.closest("[data-dds-approval]");
      const agree = card && card.querySelector("[data-dds-agree]");
      if (agree) agree.disabled = Boolean(entry.state.busy) || !input.checked;
    } else if (input.type === "file" && input.files && input.files[0]) {
      upload(entry, input.files);
    }
  });
  entry.aside.addEventListener("click", (event) => {
    const target = event.target && event.target.closest ? event.target.closest("button") : null;
    if (target && !target.disabled && target.hasAttribute("data-dds-ai-consent")) {
      act(entry, "consent", () => giveAiConsent(entry.state.caseId), { announce: "The Doctor's AI may now read masked summaries" });
    }
  });
  // The composer: in the side card of the plan view, and in the conversation of the chat view (one of them is drawn).
  for (const where of [entry.aside, entry.main]) {
    where.addEventListener("input", (event) => {
      if (event.target && event.target.id === "dds-chat-input") entry.state.draft = event.target.value;
    });
    where.addEventListener("submit", (event) => {
      if (!event.target || event.target.id !== "dds-chat-form") return;
      event.preventDefault();
      talk(entry, entry.state.draft);
    });
  }
}

/** On a phone: the one-line "Your data · 4,000 customers · Show" opens the panel and "Hide" folds it again, in place. */
function togglePanel(entry) {
  const s = entry.state;
  s.panelOpen = !s.panelOpen;
  const panel = entry.main.querySelector("[data-dds-panel]");
  if (!panel) return;
  panel.classList.toggle("is-open", s.panelOpen);
  const toggle = panel.querySelector("[data-dds-panel-toggle]");
  if (toggle) toggle.setAttribute("aria-expanded", String(s.panelOpen));
  const word = panel.querySelector("[data-dds-panel-act]");
  if (word) word.textContent = s.panelOpen ? "Hide" : "Show";
}

/** Send a message to Data Doctor (typed, or a suggested prompt); with no AI service the reply says so. */
async function talk(entry, message) {
  const text = String(message || "").trim();
  if (!text || entry.state.busy) return;
  await act(entry, "chat", async () => {
    await sendChat(theCase(entry), text);
    entry.state.draft = "";
  });
  focusOn(entry, "#dds-chat-input");
}

/** Open the "Who would know?" panel of a question ("q:<id>") or a step ("i:<id>"), and move to its first choice. */
function openWho(entry, key, firstControl) {
  if (!entry.state.knowing.has(key)) entry.state.knowing.set(key, null);
  draw(entry);
  focusOn(entry, firstControl, key);
}

/** Put the focus back on a control after a repaint (inside one panel when `key` is given). */
function focusOn(entry, selector, key = null) {
  const root = key ? [...entry.main.querySelectorAll("[data-dds-who]")].find((el) => el.dataset.ddsWho === key) : entry.main;
  const found = root && root.querySelector(selector);
  if (found && typeof found.focus === "function") found.focus();
}

/** Copy the ready message of a panel. The text is in a box a person can also select by hand. */
async function copyMessage(entry, key) {
  const panel = [...entry.main.querySelectorAll("[data-dds-who]")].find((el) => el.dataset.ddsWho === key);
  const box = panel && panel.querySelector("[data-dds-who-message]");
  if (!box) return;
  try {
    await navigator.clipboard.writeText(box.value);
    announceStatus("Message copied");
  } catch {
    box.focus();
    box.select();
    announceStatus("Message selected. Copy it with your keyboard.");
  }
}

/** Bring a step of the plan into view: its section opens its folded part first. */
function goTo(entry, itemId) {
  const found = [...entry.main.querySelectorAll("[data-dds-item], [data-dds-approval]")].find(
    (el) => el.dataset.ddsItem === itemId || el.dataset.ddsApproval === itemId,
  );
  if (!found) return;
  const fold = found.closest("details");
  if (fold) fold.open = true;
  if (typeof found.scrollIntoView === "function") found.scrollIntoView({ block: "center" });
  const control = found.querySelector("input, button, a");
  if (control) control.focus();
}

/** "Change answer": show an answered question's answers again (or hide them), keeping the focus on the control. */
function toggleChange(entry, questionId) {
  const changing = entry.state.changing;
  if (changing.has(questionId)) changing.delete(questionId);
  else changing.add(questionId);
  draw(entry);
  const again = [...entry.main.querySelectorAll("[data-dds-change]")].find((b) => b.dataset.ddsChange === questionId);
  if (again) again.focus();
}

async function readAi(entry) {
  const status = await aiServiceStatus();
  const slot = status && status.slots && status.slots.product;
  entry.state.ai = slot ? Boolean(slot.connected) : null;
}

/** Which case to open: the host's, the one just continued from the Data page, else the goal's (or any) newest. */
async function caseToOpen(host) {
  if (host.caseId) return host.caseId;
  if (host.goalId && remembered.has(host.goalId)) return remembered.get(host.goalId);
  const listed = await getCases(host.goalId || null);
  const found = (listed && listed.cases) || [];
  return found.length ? found[0].case_id : null;
}

async function open(entry) {
  const s = entry.state;
  try {
    const [caseId] = await Promise.all([caseToOpen(entry.host), readAi(entry)]);
    s.caseId = caseId;
    if (caseId) {
      await resumeVisit(caseId).catch(() => null); // a case resting mid-step goes on to its next rest
      await refresh(entry);
    }
  } catch (error) {
    s.error = error;
    s.alertAt = "top";
  }
  s.loading = false;
  draw(entry);
}

/**
 * Mount the screen into `slots` (`{ main, aside }`). `host` is where it opens: `{ place: "data" | "wizard" | "setup",
 * goalId?, mode?, uc?, approved?(fill), level? }`. The same elements are kept per place, goal and mode and moved into
 * each fresh mount point, so a page that repaints keeps the case, the busy state and the chat draft.
 */
export function mountDataDoctor(slots, host) {
  injectAskStyles();
  injectDataDoctorStyles();
  const goalId = host.goalId || (host.uc && host.uc.id) || null;
  const full = { ...host, goalId };
  const key = `${full.place}|${goalId || ""}|${full.mode || ""}`;
  let entry = entries.get(key);
  const isNew = !entry;
  if (!entry) {
    const main = document.createElement("div");
    const aside = document.createElement("div");
    main.className = "dds-root";
    aside.className = "dds-aside-root";
    entry = { main, aside, state: fresh(), host: full };
    entries.set(key, entry);
    bind(entry);
    if (typeof window !== "undefined") {
      window.addEventListener(AI_SERVICE_EVENT, () => readAi(entry).then(() => draw(entry)));
    }
  }
  entry.host = full;
  // The chat is the way in; `host.detail` opens the full plan view instead ("See details" does the same by hand).
  if (isNew || full.detail !== undefined) entry.detail = Boolean(full.detail);
  // A case continued from the Data page since this place last drew is the one to show.
  const wanted = goalId && remembered.get(goalId);
  // ... or the case a link names (`host.caseId`, "What the Doctor did" on a finished campaign).
  const linked = Boolean(full.caseId) && full.caseId !== entry.state.caseId && full.caseId !== entry.linked;
  const reopen = !isNew && Boolean((wanted && wanted !== entry.state.caseId) || linked);
  entry.linked = full.caseId || null;
  // Files chosen on the Data page ("Connect your data") start a new check: no earlier case is opened first.
  const handed = Boolean(full.files && full.files.length);
  if (reopen || handed) entry.state = { ...fresh(), loading: !handed, ai: entry.state.ai };
  draw(entry);
  slots.main.replaceChildren(entry.main);
  if (slots.aside) slots.aside.replaceChildren(entry.aside);
  else entry.main.appendChild(entry.aside);
  if (handed) {
    readAi(entry).then(() => draw(entry), () => null);
    upload(entry, full.files);
  } else if (isNew || reopen) open(entry);
  if (full.pick) pickFromConnection(entry);
  return entry;
}

/** Test seam: forget every mounted screen and remembered case. Not used by any screen. */
export function _resetDataDoctorForTests() {
  entries.clear();
  remembered.clear();
}
