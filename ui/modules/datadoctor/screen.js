// The one Data Doctor screen (Plan O M247, DEC-1817; the design §13): one component, opened on a case, in three
// places, behind `data_doctor.unified`:
//
//   the Data page          `#/data/check`: the newest case (or a new one), with no goal; "Choose this goal" sets the
//                          goal on the same case and continues in the "New campaign" wizard, where nothing answered
//                          is asked again;
//   the wizard's Customers the goal's case (the one just continued from the Data page, else the goal's newest) in
//   step                   the card's two mount points `[data-cw-guided-main]` / `[data-cw-guided-aside]`; Approve,
//                          once the data is ready, starts the list exactly as Guided setup's Approve did
//                          (`host.approved(fill)`);
//   a goal's setup         the Setup tab of `#/uc/<goal>`, in the same two places Guided setup mounts into.
//
// The wizard and a goal's setup reach it through Guided setup's own mount (`../agent/guided.js` `mountGuided` hands
// over when the switch is on), so the screen sits exactly where Guided setup sat and no route or page changes.
// With the switch off nothing here is drawn and Guided setup is unchanged.
//
// The case is the server's: every change is sent with the version read and an idempotency key, and the screen is
// drawn again from `GET /cases/{case_id}/screen`. The only state kept here is which case is open, a chat draft and
// what is busy. One flow: nothing in this file asks a person to go anywhere else to finish.

import { ApiError, getIndustries, getProfile, postUpload } from "../../api.js";
import { announceStatus, errorBox, esc } from "../../dom.js";
import { aiServiceStatus, AI_SERVICE_EVENT } from "../../availability.js";
import { button, emptyState } from "../../components.js";
import { canAccess } from "../router.js";
import { TABLE_FILE_TYPES } from "../intake/notes.js";
import { injectAgentStyles } from "../agent/styles.js";
import {
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
} from "./api.js";
import { injectDataDoctorStyles } from "./styles.js";
import {
  activeSection,
  approvalHtml,
  approveHtml,
  chatHtml,
  checklistHtml,
  doubtHtml,
  goalsHtml,
  headerHtml,
  leftOutOf,
  missingHtml,
  questionHtml,
  questionsOf,
  requestHtml,
  requestsOf,
  standingHtml,
  technicalHtml,
  timelineHtml,
  uploadHtml,
} from "./view.js";

/** The wizard's Customers step for a goal: where the Data page continues. */
export const wizardHref = (goalId) => `#/campaigns/new/customers/${encodeURIComponent(goalId)}`;

const entries = new Map(); // place|goal|mode -> { main, aside, state, host }
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
    error: null,
    notice: "",
    draft: "",
    ai: null, // true | false | null (not known)
    started: null, // the case whose hand-off was started from here
    changing: new Set(), // answered questions whose answers are shown again ("Change answer")
    read: new Set(), // changes needing their own yes whose "I have read this sentence" is ticked
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
  const accept = TABLE_FILE_TYPES;
  const err = s.error ? errorBox(s.error) : "";
  const notice = s.notice ? `<p class="dds-why" role="status" data-dds-notice>${esc(s.notice)}</p>` : "";
  if (s.loading && !s.screen) {
    return `<div class="dds" data-dds data-dds-place="${esc(host.place)}"><p class="loading" role="status">Opening Data Doctor…</p></div>`;
  }
  if (!s.screen) {
    const title = host.goalId ? "Check the customers' file" : "Check my data";
    return `<div class="dds" data-dds data-dds-place="${esc(host.place)}"><section class="card dds-start" data-dds-start>${emptyState({
      icon: "data",
      title,
      text: "Data Doctor reads your file, asks only what it cannot tell, and gets it ready. It never changes your file.",
    })}<div class="card-body">${uploadHtml({ accept, busy: s.uploadNote, first: true, quiet: true })}</div></section>${err}</div>`;
  }
  const screen = s.screen;
  const c = screen.case;
  const view = screen.view;
  const busy = Boolean(s.busy);
  const standing = view.standing || [];
  const remembered = new Map((view.remembered || []).map((r) => [r.question_id, r]));
  const questions = questionsOf(c)
    .map((q) =>
      questionHtml(q, c, { busy, standing, changing: s.changing.has(q.question_id), remembered: remembered.get(q.question_id) || null }),
    )
    .join("");
  const items = new Map((c.items || []).map((i) => [i.item_id, i]));
  const approvals = (view.approve_class || [])
    .map((a) => approvalHtml(a, items.get(a.item_id), c, { busy, read: s.read.has(a.item_id) }))
    .join("");
  const doubts = (c.doubts || []).map((d) => doubtHtml(d, c)).join("");
  const goalIds = view.goal_id ? [view.goal_id] : view.missing_for || [];
  const kit = dataRequestUrl(goalIds.slice(0, 4));
  const requests = `${requestsOf(c)
    .map((q) => requestHtml(q, c, { kitHref: kit, busy }))
    .join("")}${view.mode === "generic" ? missingHtml(view, kit, c) : ""}`;
  const checklist = `${checklistHtml(screen, { busy, level: sub })}${standingHtml(view, { busy, level: sub })}`;
  const proceed = continueHtml(entry);
  const more = c.files && c.files.length && !c.read_only ? `<div class="dds-more" data-dds-more>${uploadHtml({ accept, busy: s.uploadNote, first: false })}</div>` : "";
  return `<div class="dds" data-dds data-dds-place="${esc(host.place)}" data-dds-case="${esc(c.case_id)}"><section class="card" data-dds-header>${headerHtml(
    screen,
    { level },
  )}</section>${notice}${err}${proceed}${section("Questions", questions, "data-dds-questions", sub)}${section(
    "Needs your own yes",
    approvals,
    "data-dds-approvals",
    sub,
  )}${section("Not sure", doubts, "data-dds-doubts", sub)}${section("More data", requests, "data-dds-requests", sub)}${
    checklist ? `<section class="card dds-card" data-dds-checklist>${checklist}</section>` : ""
  }${goalsHtml(screen, { busy, level: sub })}${approveHtml(screen, { busy: s.busy, level: sub })}${
    !(c.files || []).length ? uploadHtml({ accept, busy: s.uploadNote, first: true }) : more
  }${timelineHtml(screen, { level: sub })}${technicalHtml(screen)}${
    host.place === "data" ? `<div class="dds-actions">${button({ label: "Start a new check", kind: "tertiary", attrs: "data-dds-new" })}</div>` : ""
  }</div>`;
}

/** Ready for the goal: one action on - start the list (wizard, setup) or continue in the wizard (Data page). */
function continueHtml(entry) {
  const { state: s, host } = entry;
  const view = s.screen && s.screen.view;
  if (!view || !["ready", "ready_with_warnings"].includes(view.verdict.key) || !view.start) return "";
  const label = host.approved ? "Continue with this data" : "Continue to the campaign";
  const left = leftOutOf(s.screen).length;
  const without = left
    ? `<p class="dds-why" data-dds-continue-without>${esc(
        `Continuing uses the data without the ${left === 1 ? "change" : `${left} changes`} left out above. Someone can still agree to ${left === 1 ? "it" : "them"} first.`,
      )}</p>`
    : "";
  return `<section class="card dds-continue" data-dds-ready><p class="dds-why">The data is ready for this goal. Approved, checked and saved with this check.</p>${without}<div class="dds-actions">${button(
    { label, kind: "primary", attrs: "data-dds-continue", disabled: Boolean(s.busy), busy: s.busy === "continue" },
  )}</div></section>`;
}

function asideHtml(entry) {
  const { state: s, host } = entry;
  if (!s.screen) return "";
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
  });
}

function draw(entry) {
  const openTimeline = Boolean(entry.main.querySelector("details[data-dds-timeline][open]"));
  entry.main.innerHTML = mainHtml(entry);
  if (openTimeline) {
    const timeline = entry.main.querySelector("details[data-dds-timeline]");
    if (timeline) timeline.open = true;
  }
  entry.aside.innerHTML = asideHtml(entry);
  entry.aside.hidden = !entry.state.screen;
}

// --- behaviour -------------------------------------------------------------------------------------------------

async function refresh(entry) {
  const s = entry.state;
  if (!s.caseId) {
    s.screen = null;
    return;
  }
  s.screen = await getScreen(s.caseId);
  if (s.ai === null) await readAi(entry); // not known yet: ask again (the answer is shared for a few seconds)
}

/** Run one change, then draw the case as the server has it now. A stale version reloads and says so. */
async function act(entry, busy, work, { announce = "" } = {}) {
  const s = entry.state;
  if (s.busy) return;
  s.busy = busy;
  s.error = null;
  s.notice = "";
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

async function upload(entry, file) {
  const s = entry.state;
  const host = entry.host;
  s.uploadNote = `Reading ${file.name}…`;
  await act(
    entry,
    "upload",
    async () => {
      const answer = await postUpload(file, await uploadGoal(host), host.mode || "train");
      if (!s.caseId) {
        const made = await createCase([answer.upload_id], file.name);
        s.caseId = made.case_id;
        let c = await resumeVisit(made.case_id);
        if (host.goalId) c = await chooseGoal(c, host.goalId);
      } else {
        await addFile(theCase(entry), answer.upload_id);
        await resumeVisit(s.caseId);
      }
    },
    { announce: "File read" },
  );
  s.uploadNote = "";
  draw(entry);
}

/** The hand-off: start the list with the prepared file (wizard, setup), or continue in the wizard (Data page). */
async function proceed(entry) {
  const s = entry.state;
  const view = s.screen && s.screen.view;
  if (!view || !view.start) return;
  const start = view.start;
  if (!entry.host.approved) {
    rememberCase(start.goal_id, s.caseId);
    window.location.hash = wizardHref(start.goal_id);
    return;
  }
  s.busy = "continue";
  draw(entry);
  let profile = null;
  try {
    profile = await getProfile(start.upload_id);
  } catch (error) {
    s.error = error;
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
  });
}

async function approve(entry) {
  const sentence = entry.state.screen.view.approval_sentence;
  await act(entry, "approve", () => approveCase(theCase(entry), sentence), { announce: "Approved" });
  const screen = entry.state.screen;
  const view = screen && screen.view;
  // One flow: approved and ready goes straight on, as Guided setup's Approve did - unless a change that needs its
  // own yes is left out: then continuing without it is its own choice ("Continue with this data").
  if (
    view &&
    ["ready", "ready_with_warnings"].includes(view.verdict.key) &&
    view.start &&
    entry.host.approved &&
    !leftOutOf(screen).length
  ) {
    await proceed(entry);
  }
}

async function pickGoal(entry, goalId) {
  const s = entry.state;
  await act(entry, "goal", () => chooseGoal(theCase(entry), goalId), { announce: "Goal chosen" });
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
    if (d.ddsAnswer && d.ddsQ) {
      act(
        entry,
        "answer",
        async () => {
          await answerQuestion(theCase(entry), d.ddsQ, d.ddsAnswer);
          entry.state.changing.delete(d.ddsQ);
        },
        { announce: "Answer saved" },
      );
    } else if (d.ddsChange) toggleChange(entry, d.ddsChange);
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
    else if (target.hasAttribute("data-dds-new")) {
      entry.state = { ...fresh(), loading: false, ai: entry.state.ai };
      draw(entry);
    }
  });
  root.addEventListener("change", (event) => {
    const input = event.target;
    if (!input) return;
    if (input.matches && input.matches("input[data-dds-tick]")) {
      act(entry, "tick", () => decideItem(theCase(entry), input.dataset.ddsTick, input.checked));
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
      upload(entry, input.files[0]);
    }
  });
  entry.aside.addEventListener("input", (event) => {
    if (event.target && event.target.id === "dds-chat-input") entry.state.draft = event.target.value;
  });
  entry.aside.addEventListener("submit", (event) => {
    if (!event.target || event.target.id !== "dds-chat-form") return;
    event.preventDefault();
    const text = entry.state.draft.trim();
    if (!text) return;
    act(entry, "chat", async () => {
      await sendChat(theCase(entry), text);
      entry.state.draft = "";
    });
  });
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
  injectAgentStyles();
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
  // A case continued from the Data page since this place last drew is the one to show.
  const wanted = goalId && remembered.get(goalId);
  const reopen = !isNew && wanted && wanted !== entry.state.caseId;
  if (reopen) entry.state = { ...fresh(), ai: entry.state.ai };
  draw(entry);
  slots.main.replaceChildren(entry.main);
  if (slots.aside) slots.aside.replaceChildren(entry.aside);
  else entry.main.appendChild(entry.aside);
  if (isNew || reopen) open(entry);
  return entry;
}

/** Test seam: forget every mounted screen and remembered case. Not used by any screen. */
export function _resetDataDoctorForTests() {
  entries.clear();
  remembered.clear();
}
