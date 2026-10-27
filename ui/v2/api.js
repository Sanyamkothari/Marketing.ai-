// Every call the Autonomy screens make, against the v2 API at /v2, with a mock layer behind it.
//
// Bodies are the contracts' `model_dump(mode="json")` (autonomy/contracts/). Nothing here invents a value: a
// field the API did not send stays missing and the screens show an em dash for it.
//
// The mock layer serves the same shapes from JSON files in ./mock/ (one file per endpoint) so the UI works on
// its own: it is used when the page is opened with ?mock=1, or when /v2 cannot be reached (no server, or a
// server without the v2 routes). In mock mode the writes (create, approve, pause, content status) change an
// in-memory copy for this page load only, so a click-through behaves; a reload starts again from the files.
// The mock never computes a result: a goal made in mock mode has no proof, no briefing, no decisions and no
// batches. A customer the sample file does not hold is pieced together from that customer's sample decisions
// (group, channels, contact times); what the decisions cannot tell (purchases, revenue, events) stays missing.

export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

/** Which JSON file under ./mock/ answers which GET endpoint. */
export const MOCK_FILES = Object.freeze({
  goals: "goals.json",
  goalDetail: "goal_detail.json",
  opportunities: "opportunities.json",
  decisions: "decisions.json",
  customer: "customer.json",
  briefing: "briefing.json",
  proof: "proof.json",
  health: "health.json",
  alerts: "alerts.json",
  batches: "batches.json",
  // The promise endpoints (api/v2/insights.py); docs/v2/PROMISE_MAP.md "Data for each screen" says which serves what.
  today: "today.json",
  activity: "activity.json",
  customerInsight: "customer_insight.json",
  plan: "plan.json",
  expected: "expected.json",
  healthLimits: "health_limits.json",
  dataStatus: "data_status.json",
  channels: "channels.json",
  opportunitiesSized: "opportunities_sized.json",
  batchBreakdowns: "batch_breakdowns.json",
  // The SaaS connectors (api/v2/connectors.py): the platform cards, and each sample connection's guided setup.
  connectors: "connectors.json",
  connectorSetup: "connector_setup.json",
  // Zero-touch setup (api/v2/onboarding.py, onboarding_brand.py) and the exceptions-only inbox (api/v2/inbox.py).
  onboardingProposals: "onboarding_proposals.json",
  onboardingLimits: "onboarding_limits.json",
  onboardingMargin: "onboarding_margin.json",
  onboardingBrand: "onboarding_brand.json",
  inbox: "inbox.json",
  autopilot: "autopilot.json",
});

/**
 * `?scenario=` for the sample data: "setup" is a shop that has connected Shopify and Klaviyo (history still
 * loading) and runs no goal yet, so Getting started shows the five-click setup; "new" is the same with nothing
 * connected. Anything else (or none) is the usual sample shop with its running goal.
 */
export function scenarioParam(search) {
  const v = new URLSearchParams(search || "").get("scenario");
  return v === "setup" || v === "new" ? v : null;
}

// The values Goal.kind and Goal.objective may take (autonomy/contracts/goals.py), for the mock's checks.
const GOAL_KINDS = Object.freeze(["win_back", "repeat_purchase", "cart_recovery", "cross_sell", "onboarding", "retention", "custom"]);
const OBJECTIVES = Object.freeze(["conversion", "revenue", "gross_profit", "retention", "lead", "activation"]);

const enc = encodeURIComponent;
const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Did the page ask for mock data? `?mock=1` (or true/yes) forces it; `?mock=0` forces the real API. */
export function mockParam(search) {
  const v = new URLSearchParams(search || "").get("mock");
  if (v === null) return null;
  return ["1", "true", "yes", ""].includes(v.toLowerCase());
}

/** "budget" from a 422 item's `loc` (["body", "budget"]): the field the person can fix, in words. */
function fieldOf(item) {
  const loc = Array.isArray(item && item.loc) ? item.loc.filter((p) => !["body", "query", "path"].includes(p)) : [];
  const last = loc.length ? String(loc[loc.length - 1]) : "";
  return last ? last.charAt(0).toUpperCase() + last.slice(1).replace(/_/g, " ") : "";
}

/**
 * The message a FastAPI error body carries: `{detail: "..."}`, `{detail: {message}}` or a 422 list. A 422 list
 * names the field each problem is in ("Budget: Input should be greater than or equal to 0"), because the
 * validator's own words do not say which box to fix.
 */
export function errorMessage(body, status) {
  const detail = body && typeof body === "object" ? body.detail : null;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.message) return String(detail.message);
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((d) => {
        const msg = d && typeof d === "object" ? d.msg || JSON.stringify(d) : String(d);
        const field = fieldOf(d);
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  return `The Autonomy API answered ${status}.`;
}

/**
 * True when a 404 means "this server has no such route" (FastAPI's own `{"detail": "Not Found"}`), not "no such
 * thing". The batch endpoints arrive in a later API build, so a missing route is "not available yet", while a
 * 404 that names the batch is a real answer.
 */
export function routeMissing(err) {
  if (!err || (err.status !== 404 && err.status !== 405)) return false;
  const detail = err.body && typeof err.body === "object" ? err.body.detail : err.body;
  return err.status === 405 || detail === undefined || detail === null || detail === "Not Found";
}

const MONEY_WORDS = Object.freeze({ $: "USD", "€": "EUR", "£": "GBP", "₹": "INR", rs: "INR", "rs.": "INR", usd: "USD", eur: "EUR", gbp: "GBP", inr: "INR" });
const SCALE_WORDS = Object.freeze({ k: 1e3, thousand: 1e3, lakh: 1e5, lakhs: 1e5, m: 1e6, million: 1e6 });

/**
 * The mock's reading of a goal's words, in the shape of POST /v2/goals/read: the amount after a currency sign
 * ("$5,000", "₹50,000", "5k") and the period ("this month", "this week", "in 30 days"). It only reads what the
 * words say; the server's reader (api/v2/insights.py `read_goal_text`) knows a few more forms.
 */
export function readGoalWords(text, nowDate) {
  const words = String(text || "");
  const out = { text: words, budget: null, currency: null, ends_at: null, period: null, words: "" };
  const m = /(\$|€|£|₹|\brs\.?|\busd|\beur|\bgbp|\binr)\s?(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)\s*(k|thousand|lakhs?|m|million)?\b/i.exec(words);
  if (m) {
    out.currency = MONEY_WORDS[m[1].toLowerCase().trim()] || null;
    out.budget = Number(m[2].replace(/,/g, "")) * (SCALE_WORDS[(m[3] || "").toLowerCase()] || 1);
  }
  const p = /\b(this month|this week|(?:in|over|for|within|next) (\d{1,3}) (days?|weeks?))\b/i.exec(words);
  const now = nowDate instanceof Date ? nowDate : new Date();
  if (p) {
    out.period = p[1];
    const low = p[1].toLowerCase();
    let end;
    if (low === "this month") end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
    else if (low === "this week") end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + (7 - ((now.getUTCDay() + 6) % 7))));
    else end = new Date(now.getTime() + Number(p[2]) * (p[3].toLowerCase().startsWith("week") ? 7 : 1) * 86400000);
    out.ends_at = end.toISOString().replace(".000Z", "Z");
  }
  const parts = [];
  if (out.budget !== null) parts.push(`We read ${out.budget.toLocaleString("en-US")} ${out.currency || "USD"} as your budget.`);
  if (out.period) parts.push(`We read '${out.period}' as the period.`);
  out.words = parts.length ? `${parts.join(" ")} Change it if that's wrong.` : "No budget or period found in the words.";
  return out;
}

function goalOfDecision(d) {
  if (d && d.action && d.action.goal_id) return d.action.goal_id;
  if (d && Array.isArray(d.bids) && d.bids[0] && d.bids[0].goal_id) return d.bids[0].goal_id;
  return null;
}

/**
 * Make an API client. Options (all optional, for tests and odd deployments):
 * `base` (default window.AUTONOMY_API_BASE or "", then "/v2"), `mock` (true/false forces a mode; default reads
 * ?mock= and otherwise probes /v2), `fetch`, `mockBase` (URL of the mock folder) and `now` (a clock for the
 * timestamps mock writes stamp; the real API stamps its own).
 */
export function createApi(options = {}) {
  const g = typeof window !== "undefined" ? window : {};
  const base = `${String(options.base ?? g.AUTONOMY_API_BASE ?? "").replace(/\/+$/, "")}/v2`;
  const fetchImpl = options.fetch || ((...a) => fetch(...a));
  const mockBase = options.mockBase || new URL("./mock/", import.meta.url).href;
  const now = options.now || (() => new Date());
  const forced = options.mock ?? (g.location ? mockParam(g.location.search) : null);
  const scenario = options.scenario ?? (g.location ? scenarioParam(g.location.search) : null);

  let mode = forced === true ? "mock" : "live";
  let reason = forced === true ? "You opened the page with ?mock=1." : "";
  // One probe per client, shared by every request that starts before it finishes, so two first calls made at
  // once cannot both skip it and hit a server that has no /v2 routes.
  let probing = forced !== null && forced !== undefined ? Promise.resolve() : null;
  const listeners = new Set();
  const goalListeners = new Set();
  const batchListeners = new Set();

  /** Tell subscribers a goal changed (paused, resumed, approved), so the shell's bar stays true. */
  function goalChanged(goal) {
    if (!goal || !goal.goal_id) return goal;
    for (const fn of goalListeners) {
      try {
        fn(goal);
      } catch {
        /* a listener's failure is its own */
      }
    }
    return goal;
  }

  /** Tell subscribers a batch was approved or rejected, so the top bar's "to approve" count stays true. */
  function batchChanged(batch) {
    if (!batch || !batch.batch_id) return batch;
    for (const fn of batchListeners) {
      try {
        fn(batch);
      } catch {
        /* a listener's failure is its own */
      }
    }
    return batch;
  }

  function setMock(why) {
    if (mode === "mock") return;
    mode = "mock";
    reason = why;
    for (const fn of listeners) {
      try {
        fn({ mode, reason });
      } catch {
        /* a listener's failure is its own */
      }
    }
  }

  // ---- the real API --------------------------------------------------------------------------------------
  async function real(method, path, body) {
    let response;
    try {
      response = await fetchImpl(base + path, {
        method,
        headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      return null; // unreachable: the caller falls back to mock
    }
    const text = await response.text();
    let parsed = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = text;
      }
    }
    if (response.ok) return { body: parsed };
    const code = (parsed && parsed.detail && parsed.detail.code) || `HTTP_${response.status}`;
    throw new ApiError(response.status, code, errorMessage(parsed, response.status), parsed);
  }

  /** Decide once whether /v2 is there: a network error, a non-JSON answer, a 404 or a 5xx on /health means no. */
  async function probeOnce() {
    let response;
    try {
      response = await fetchImpl(`${base}/health`, { headers: { Accept: "application/json" } });
    } catch {
      setMock(`The Autonomy API at ${base} could not be reached.`);
      return;
    }
    const type = (response.headers && response.headers.get && response.headers.get("content-type")) || "";
    if (response.status === 404 || response.status >= 500 || !type.includes("json")) {
      setMock(`The Autonomy API at ${base} is not available (HTTP ${response.status}).`);
    }
  }

  function probe() {
    if (!probing) probing = probeOnce();
    return probing.then(() => mode);
  }

  async function request(method, path, body) {
    await probe();
    if (mode === "live") {
      const out = await real(method, path, body);
      if (out) return out.body;
      setMock(`The Autonomy API at ${base} stopped answering.`);
    }
    return mockRequest(method, path, body);
  }

  // ---- the mock layer ------------------------------------------------------------------------------------
  const files = new Map();
  async function loadMock(name) {
    if (!files.has(name)) {
      files.set(
        name,
        (async () => {
          try {
            const r = await fetchImpl(new URL(name, mockBase).href, { headers: { Accept: "application/json" } });
            if (!r.ok) return undefined;
            return JSON.parse(await r.text());
          } catch {
            return undefined; // a mock file not written yet: the endpoint answers "nothing"
          }
        })(),
      );
    }
    return clone(await files.get(name));
  }

  let store = null;
  async function state() {
    if (store) return store;
    const [goals, detail] = await Promise.all([loadMock(MOCK_FILES.goals), loadMock(MOCK_FILES.goalDetail)]);
    const fresh = scenario === "setup" || scenario === "new";
    store = {
      goals: fresh || !Array.isArray(goals) ? [] : goals,
      details: new Map(),
      template: detail || null,
      primary: null,
      made: 0,
      batches: null,
      // Zero-touch writes in this page load: signed mandates, the voice approval, inbox items handled.
      mandates: [],
      // The margin question's answer (percent); null until answered, as on a real shop (api/v2/onboarding.py).
      marginPct: null,
      brandApproval: null,
      handled: new Set(),
    };
    if (detail && detail.goal && !fresh) {
      store.primary = detail.goal.goal_id;
      if (!store.goals.some((x) => x.goal_id === detail.goal.goal_id)) store.goals.unshift(detail.goal);
      store.details.set(detail.goal.goal_id, { brief: detail.brief ?? null, content: detail.content ?? [], proof: detail.proof ?? null });
    }
    return store;
  }

  /** A goal's brief and content in mock mode: the sample plan, re-pointed at this goal, not yet approved. */
  function detailFor(s, goal) {
    if (s.details.has(goal.goal_id)) return s.details.get(goal.goal_id);
    const t = s.template;
    let brief = null;
    let content = [];
    if (t && t.brief) {
      brief = clone(t.brief);
      Object.assign(brief, {
        brief_id: `brief_${goal.goal_id}_1`,
        goal_id: goal.goal_id,
        version: 1,
        objective: goal.objective,
        created_at: goal.created_at,
        approved_by: null,
        approved_at: null,
      });
      brief.envelope = { ...brief.envelope, budget: goal.budget };
      content = (t.content || []).map((c) => ({ ...clone(c), content_id: `${c.content_id}_${goal.goal_id}`, goal_id: goal.goal_id, status: "draft", created_at: goal.created_at }));
    }
    const d = { brief, content, proof: null };
    s.details.set(goal.goal_id, d);
    return d;
  }

  function notFound(what) {
    return new ApiError(404, "NOT_FOUND", `${what} is not in the sample data.`, null);
  }

  function findGoal(s, id) {
    const goal = s.goals.find((x) => x.goal_id === id);
    if (!goal) throw notFound(`Goal ${id}`);
    return goal;
  }

  function setGoal(s, goal) {
    s.goals = s.goals.map((x) => (x.goal_id === goal.goal_id ? goal : x));
    return goal;
  }

  /** The sample batches, loaded once and then changed in memory by approve and reject. */
  async function mockBatches(s) {
    if (!s.batches) {
      const rows = await loadMock(MOCK_FILES.batches);
      s.batches = Array.isArray(rows) ? rows : [];
    }
    return s.batches;
  }

  /**
   * A customer the sample file does not hold, from their sample decisions: who they are (id, group) and which
   * channels the agent picked for them (it only picks a channel it may use, so the address and consent are known
   * there). Purchases, revenue, contacts and events are not in a decision (a planned send is not a contact yet),
   * so they stay missing and the screens show an em dash. A customer with no sample decision is a 404, like the
   * real API.
   */
  async function mockCustomer(id) {
    const c = await loadMock(MOCK_FILES.customer);
    if (c && c.state && c.state.customer_id === id) return c;
    const all = (await loadMock(MOCK_FILES.decisions)) || [];
    const mine = (Array.isArray(all) ? all : []).filter((d) => d && d.customer_id === id);
    if (!mine.length) throw notFound(`Customer ${id}`);
    const byTime = [...mine].sort((a, b) => Date.parse(b.made_at) - Date.parse(a.made_at));
    const acts = byTime.filter((d) => d.kind === "act" && d.action);
    const channels = {};
    for (const d of acts) {
      const ch = d.action.channel;
      if (!channels[ch]) channels[ch] = { address_known: true, consent: true, unsubscribed: false };
    }
    const state = {
      customer_id: id,
      as_of: byTime[0].made_at,
      group: byTime[0].group,
      channels,
      top_categories: [],
      preferences: {},
      scores: {},
      open_complaint: false,
    };
    return { state, decisions: byTime, events: [] };
  }

  /**
   * The sample proposals, marked like the server marks them: a proposal whose kind a running (or paused) goal
   * already holds is "already running", and the best one with enough history that is not running is preselected.
   */
  async function mockProposals(s) {
    const scan = await loadMock(MOCK_FILES.onboardingProposals);
    if (!scan) return null;
    const held = new Set(s.goals.filter((g) => g.status === "active" || g.status === "paused").map((g) => g.kind));
    let pick = null;
    for (const p of [...(scan.proposals || [])].sort((a, b) => a.rank - b.rank)) {
      p.already_running = held.has(p.kind);
      if (!pick && p.enough_history && p.monthly_profit && !p.already_running) pick = p.opportunity_id;
    }
    for (const p of scan.proposals || []) p.preselected = p.opportunity_id === pick;
    scan.preselected_id = pick;
    return scan;
  }

  /** POST /onboarding/accept in mock mode: the proposal becomes a goal in Shadow with a signed (sample) mandate. */
  // The margin as the real API keeps it: checked (0 < m < 100, else 422) and stored once answered.
  function mockKeepMargin(s, value) {
    const pct = Number(value);
    if (!Number.isFinite(pct) || pct <= 0 || pct >= 100) throw new ApiError(422, "INVALID", "The margin must be above 0 and below 100 (percent).", null);
    s.marginPct = Math.round(pct * 100) / 100;
  }

  async function mockMargin(s) {
    const status = clone((await loadMock(MOCK_FILES.onboardingMargin)) || null);
    if (!status || s.marginPct === null) return status;
    const pct = s.marginPct;
    const warning = pct < status.warn_below_pct
      ? `${pct}% is very low. If you meant ${pct * 100}%, type it as ${pct * 100}, not as a fraction. Keep it only if only that much is left of each sale.`
      : pct > status.warn_above_pct
        ? `${pct}% is very high. After product cost, shipping and payment fees most shops keep much less; a margin set too high makes the proof show more profit than there is.`
        : null;
    return { ...status, answered: true, margin_pct: pct, margin_rate: Math.round(pct * 100) / 10000, source: "your answer", warning, note: "The proof counts every purchase that carries no margin of its own at this margin. Set AUTONOMY_MARGIN_RATE to keep it after a restart." };
  }

  // The sample shop's orders carry their margins: they still size the goal; the answer only stops the question.
  function mockLimitsAnswered(limits, pct) {
    if (!limits || pct === null) return limits;
    const rows = (limits.limits || []).map((l) => (l.key === "margin" && l.question
      ? { ...l, question: null, found: `${l.found} ${pct}% from your answer counts only the purchases with no margin of their own, in the proof; sizing and the offer floor use your orders' margin.` }
      : l));
    return { ...limits, limits: rows, questions: rows.map((l) => l.question).filter((q) => q) };
  }

  async function mockAccept(s, body) {
    if (body.margin_pct !== undefined && body.margin_pct !== null) mockKeepMargin(s, body.margin_pct);
    if (s.marginPct === null) {
      const status = await mockMargin(s);
      throw new ApiError(409, "CONFLICT", `Answer this first: ${(status && status.question) || "your margin"} (margin_pct, above 0 and below 100). Nothing was written.`, null);
    }
    const scan = await mockProposals(s);
    const id = body.opportunity_id || (scan && scan.preselected_id);
    const proposal = scan && (scan.proposals || []).find((p) => p.opportunity_id === id);
    if (!proposal) throw new ApiError(409, "CONFLICT", (scan && scan.not_enough) || "No proposal is ready yet.", null);
    if (proposal.already_running) throw new ApiError(409, "CONFLICT", "A goal of this kind is already running or paused.", null);
    const limits = ((await loadMock(MOCK_FILES.onboardingLimits)) || {})[id];
    const terms = (limits && limits.terms) || {};
    s.made += 1;
    const at = now().toISOString();
    const until = new Date(now().getTime() + (terms.valid_days || 30) * 86400000).toISOString();
    const goal = {
      goal_id: `goal_mock_${s.made}`,
      business_id: (s.goals[0] && s.goals[0].business_id) || "demo",
      text: proposal.goal_text,
      kind: proposal.kind,
      objective: "gross_profit",
      budget: terms.budget_ceiling ?? proposal.budget,
      currency: proposal.currency || "USD",
      starts_at: at,
      ends_at: null,
      autonomy: "shadow",
      status: "active",
      created_at: at,
    };
    s.goals.push(goal);
    const brief = detailFor(s, goal).brief;
    if (brief) Object.assign(brief, { approved_by: "you", approved_at: at, audience: proposal.audience });
    const mandate = {
      mandate_id: `mandate_mock_${s.made}`,
      version: 1,
      business_id: goal.business_id,
      goal_ids: [goal.goal_id],
      status: "active",
      valid_from: at,
      valid_until: until,
      created_by: body.owner_id || "owner",
    };
    const record = { mandate, goal_id: goal.goal_id, body_hash: "", unmet: [], single_owner: body.single_owner !== false, ledger_head: "" };
    s.mandates.unshift(record);
    return clone({
      goal,
      brief,
      proposal: { ...proposal, already_running: true, preselected: false },
      limits,
      mandate: record,
      words: `${proposal.title} is running: it decides every day and sends nothing. Limits signed by you alone (single-owner path).`,
      notes: ["Sample data: nothing is signed or sent."],
    });
  }

  /** The sample brand page: the voice read from the sample shop, for the goal asked for, approved once approved. */
  async function mockBrand(s, goalId) {
    const page = await loadMock(MOCK_FILES.onboardingBrand);
    if (!page) return null;
    const goal = goalId ? s.goals.find((g) => g.goal_id === goalId) : null;
    if (goalId && !goal) throw notFound(`Goal ${goalId}`);
    if (goal) page.goal = { goal_id: goal.goal_id, text: goal.text, kind: goal.kind };
    if (s.brandApproval) {
      Object.assign(page, { status: "approved", approved_by: s.brandApproval.by, approved_at: s.brandApproval.at, words: `Approved by ${s.brandApproval.by}; every message is checked against it.` });
    }
    return page;
  }

  async function mockRequest(method, path, body) {
    const url = new URL(path, "http://mock.invalid");
    const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    const q = url.searchParams;
    const s = await state();
    const key = `${method} /${parts.map((p, i) => (i > 0 && ["goals", "content", "customers", "batches", "connections", "inbox", "autopilot"].includes(parts[i - 1]) ? "{id}" : p)).join("/")}`;
    const id = parts[1];
    // Connecting, syncing and disconnecting need a real platform: the sample data only shows what it looks like.
    if (parts[0] === "connectors" && method !== "GET") {
      throw new ApiError(409, "SAMPLE_DATA", "This page shows sample data. Connecting needs the live Autonomy API.", null);
    }

    switch (key) {
      case "GET /goals":
        return clone(s.goals);
      case "POST /goals": {
        const text = String((body && body.text) || "").trim();
        if (!text) throw new ApiError(422, "INVALID", "Say what you want to achieve.", null);
        // Like the server: with no budget, read it (and the period) from the words.
        const given = body && body.budget !== undefined && body.budget !== null;
        const reading = given ? null : readGoalWords(text, now());
        if (reading && reading.budget === null) throw new ApiError(400, "INVALID", "Say how much you want to spend, e.g. '$2,000 this month'.", null);
        const budget = given ? Number(body.budget) : reading.budget;
        if (!Number.isFinite(budget) || budget < 0) throw new ApiError(422, "INVALID", "The budget must be zero or more.", null);
        s.made += 1;
        const at = now().toISOString();
        const goal = {
          goal_id: `goal_mock_${s.made}`,
          business_id: (s.goals[0] && s.goals[0].business_id) || "demo",
          text,
          kind: GOAL_KINDS.includes(body && body.kind) ? body.kind : "custom",
          objective: OBJECTIVES.includes(body && body.objective) ? body.objective : "gross_profit",
          budget,
          currency: String((reading && reading.currency) || (body && body.currency) || "USD").toUpperCase(),
          starts_at: at,
          ends_at: (body && body.ends_at) || (reading && reading.ends_at) || null,
          autonomy: "shadow",
          status: "draft",
          created_at: at,
        };
        s.goals.push(goal);
        return { goal: clone(goal), brief: clone(detailFor(s, goal).brief) };
      }
      case "GET /goals/{id}": {
        const goal = findGoal(s, id);
        const d = detailFor(s, goal);
        return clone({ goal, brief: d.brief, content: d.content, proof: d.proof });
      }
      case "POST /goals/{id}": {
        if (id !== "read") break;
        const text = String((body && body.text) || "").trim();
        if (!text) throw new ApiError(422, "INVALID", "Say what you want to achieve.", null);
        return readGoalWords(text, now());
      }
      case "PATCH /goals/{id}": {
        const goal = findGoal(s, id);
        const budget = Number(body && body.budget);
        if (!Number.isFinite(budget) || budget < 0) throw new ApiError(422, "INVALID", "The budget must be zero or more.", null);
        const d = detailFor(s, goal);
        if (d.brief && !d.brief.approved_at) d.brief = { ...d.brief, envelope: { ...d.brief.envelope, budget } };
        else if (d.brief && budget > d.brief.envelope.budget)
          throw new ApiError(409, "CONFLICT", `The approved plan's budget is ${d.brief.envelope.budget}; raising it needs a new plan.`, null);
        const next = setGoal(s, { ...goal, budget });
        return clone({ goal: next, brief: d.brief });
      }
      case "POST /pause": {
        const paused = s.goals.filter((g) => g.status === "active").map((g) => setGoal(s, { ...g, status: "paused" }));
        return clone({ paused });
      }
      case "GET /goals/{id}/plan":
      case "GET /expected":
      case "GET /today": {
        const goalId = key === "GET /goals/{id}/plan" ? id : q.get("goal_id");
        if (goalId) findGoal(s, goalId);
        const file = { "GET /goals/{id}/plan": MOCK_FILES.plan, "GET /expected": MOCK_FILES.expected, "GET /today": MOCK_FILES.today }[key];
        const obj = await loadMock(file);
        // The sample file belongs to the sample goal; a goal made here has nothing counted yet.
        return obj && (!goalId || obj.goal_id === goalId) ? obj : null;
      }
      case "GET /activity": {
        const rows = (await loadMock(MOCK_FILES.activity)) || [];
        const goalId = q.get("goal_id");
        const customerId = q.get("customer_id");
        let out = Array.isArray(rows) ? rows : [];
        if (goalId) out = out.filter((r) => r.goal_id === goalId);
        if (customerId) out = out.filter((r) => r.decision && r.decision.customer_id === customerId);
        const limit = Number(q.get("limit"));
        return Number.isFinite(limit) && limit > 0 ? out.slice(0, limit) : out;
      }
      case "GET /customers/{id}/insight": {
        const obj = await loadMock(MOCK_FILES.customerInsight);
        if (obj && obj.customer_id === id) return obj;
        throw notFound(`Customer ${id}`);
      }
      case "GET /batches/{id}/breakdown": {
        const rows = (await loadMock(MOCK_FILES.batchBreakdowns)) || [];
        const found = (Array.isArray(rows) ? rows : []).find((b) => b.batch_id === id);
        if (!found) throw notFound(`Batch ${id}`);
        return found;
      }
      case "GET /health/limits":
        return (await loadMock(MOCK_FILES.healthLimits)) ?? null;
      case "GET /data/status": {
        const report = await loadMock(MOCK_FILES.dataStatus);
        // A shop that has connected nothing has no data yet.
        if (report && scenario === "new") return { ...report, customers: 0, orders: 0, products: 0, sources: [], fixes: [], problems: [], questions: [] };
        return report ?? null;
      }
      case "GET /channels":
        return (await loadMock(MOCK_FILES.channels)) || [];
      case "GET /opportunities/sized":
        return (await loadMock(MOCK_FILES.opportunitiesSized)) || [];
      case "GET /goals/{id}/content":
        return clone(detailFor(s, findGoal(s, id)).content);
      case "POST /goals/{id}/approve": {
        const goal = findGoal(s, id);
        const d = detailFor(s, goal);
        const autonomy = body && body.autonomy;
        if (!["shadow", "approve", "autopilot"].includes(autonomy)) throw new ApiError(422, "INVALID", "Choose an autonomy level.", null);
        if (d.brief) d.brief = { ...d.brief, approved_by: d.brief.approved_by || "you", approved_at: d.brief.approved_at || now().toISOString() };
        const next = setGoal(s, { ...goal, autonomy, status: goal.status === "draft" ? "active" : goal.status });
        return clone({ goal: next, brief: d.brief });
      }
      case "POST /goals/{id}/pause":
      case "POST /goals/{id}/resume": {
        const goal = findGoal(s, id);
        const pausing = parts[2] === "pause";
        if (!["active", "paused"].includes(goal.status)) throw new ApiError(409, "CONFLICT", "Only a running goal can be paused or resumed. Approve the plan first.", null);
        return clone(setGoal(s, { ...goal, status: pausing ? "paused" : "active" }));
      }
      case "POST /content/{id}/status": {
        const status = body && body.status;
        if (!["draft", "approved", "rejected", "retired"].includes(status)) throw new ApiError(422, "INVALID", "Unknown content status.", null);
        for (const d of s.details.values()) {
          const i = d.content.findIndex((c) => c.content_id === id);
          if (i >= 0) {
            // The server's rule (api/v2/deps.py editor_passed): only a message whose last editor note says "approved" can be approved.
            const notes = d.content[i].critic_notes || [];
            if (status === "approved" && !(notes.length && String(notes[notes.length - 1]).startsWith("approved"))) {
              throw new ApiError(409, "CONFLICT", "The editor has not passed this message; edit it and run it through the editor again.", null);
            }
            d.content[i] = { ...d.content[i], status };
            return clone(d.content[i]);
          }
        }
        throw notFound(`Message ${id}`);
      }
      case "GET /decisions": {
        const all = (await loadMock(MOCK_FILES.decisions)) || [];
        const goalId = q.get("goal_id");
        const customerId = q.get("customer_id");
        let rows = Array.isArray(all) ? all : [];
        if (goalId && !(goalId === s.primary && !rows.some((d) => goalOfDecision(d) === goalId))) rows = rows.filter((d) => goalOfDecision(d) === goalId);
        if (customerId) rows = rows.filter((d) => d.customer_id === customerId);
        const limit = Number(q.get("limit"));
        return Number.isFinite(limit) && limit > 0 ? rows.slice(0, limit) : rows;
      }
      case "GET /customers/{id}":
        return mockCustomer(id);
      case "GET /batches": {
        const goalId = q.get("goal_id");
        const rows = await mockBatches(s);
        // Like the decisions: the sample batches belong to the sample goal, and a goal made here has none.
        return clone(goalId ? rows.filter((b) => b.goal_id === goalId) : rows);
      }
      case "POST /batches/{id}/approve":
      case "POST /batches/{id}/reject": {
        const rows = await mockBatches(s);
        const i = rows.findIndex((b) => b.batch_id === id);
        if (i < 0) throw notFound(`Batch ${id}`);
        if (rows[i].status !== "pending") throw new ApiError(409, "CONFLICT", `This batch was already ${rows[i].status}.`, null);
        // Approving never pretends to send: the batch stays "approved" here, because nothing is sent in mock mode.
        rows[i] = { ...rows[i], status: parts[2] === "approve" ? "approved" : "rejected", decided_by: "you", decided_at: now().toISOString() };
        return clone(rows[i]);
      }
      case "GET /briefing":
      case "GET /proof": {
        const goalId = q.get("goal_id");
        const obj = await loadMock(key === "GET /proof" ? MOCK_FILES.proof : MOCK_FILES.briefing);
        // The sample file belongs to the sample goal; any other goal has none yet (the mock never makes one up).
        if (obj && (!goalId || obj.goal_id === goalId || goalId === s.primary)) return obj;
        if (key === "GET /proof" && goalId && s.details.has(goalId)) return clone(s.details.get(goalId).proof);
        return null;
      }
      case "GET /health": {
        const health = await loadMock(MOCK_FILES.health);
        return health && scenario ? { ...health, alerts: [] } : health ?? null;
      }
      case "GET /connectors": {
        const all = await loadMock(MOCK_FILES.connectors);
        // A shop that has connected nothing yet: the same platforms, with no connection.
        if (all && scenario === "new") for (const p of all.platforms || []) p.connections = [];
        if (all && scenario === "new") Object.assign(all, { can_act: false, on_hold: [] });
        return all ?? null;
      }
      case "GET /onboarding/proposals":
        if (scenario === "new") {
          const scan = await mockProposals(s);
          if (!scan) return null;
          return { ...scan, proposals: [], preselected_id: null, history_days: 0, buyers: 0, orders: 0, enough_history: false, not_enough: "Not enough history yet: no orders have been read." };
        }
        return mockProposals(s);
      case "GET /onboarding/limits": {
        const scan = await mockProposals(s);
        const id = q.get("opportunity_id") || (scan && scan.preselected_id);
        const all = (await loadMock(MOCK_FILES.onboardingLimits)) || {};
        if (!id || !all[id]) throw notFound(`Proposal ${id}`);
        return mockLimitsAnswered(all[id], s.marginPct);
      }
      case "GET /onboarding/mandates":
        return clone(s.mandates);
      case "GET /onboarding/margin":
        return mockMargin(s);
      case "POST /onboarding/margin":
        mockKeepMargin(s, body && body.margin_pct);
        return mockMargin(s);
      case "POST /onboarding/answers":
        // Kept; the sample shop's orders carry their margins, so the proposals are sized as before.
        mockKeepMargin(s, body && body.margin_pct);
        return mockProposals(s);
      case "POST /onboarding/accept":
        return mockAccept(s, body || {});
      case "GET /onboarding/brand":
      case "POST /onboarding/brand/read":
        return mockBrand(s, q.get("goal_id"));
      case "POST /onboarding/brand/approve": {
        const page = await mockBrand(s, null);
        if (!body || body.fingerprint !== page.fingerprint) throw new ApiError(409, "CONFLICT", "The voice changed since it was shown; look again before approving.", null);
        s.brandApproval = { by: body.approved_by || "the owner", at: now().toISOString() };
        return mockBrand(s, null);
      }
      case "GET /inbox": {
        const box = await loadMock(MOCK_FILES.inbox);
        if (!box) return null;
        // The sample items belong to the sample goal: a fresh shop has nothing in its inbox.
        const items = scenario ? [] : (box.items || []).filter((i) => !s.handled.has(i.item_id));
        const n = items.length;
        return { ...box, items, count: n, words: n === 0 ? "Nothing needs you." : n === 1 ? "One thing needs you." : `${n} things need you.` };
      }
      case "POST /inbox/{id}/act": {
        const box = (await loadMock(MOCK_FILES.inbox)) || { items: [] };
        const found = (box.items || []).find((i) => i.item_id === id);
        if (!found || s.handled.has(id)) throw notFound(`Inbox item ${id}`);
        s.handled.add(id);
        const at = now().toISOString();
        return { item: { ...found, resolved_at: at, resolved_by: "person" }, done: true, words: `Done: ${found.button.label.toLowerCase()}. Nothing is sent from the sample data.`, target: found.button.target };
      }
      case "GET /autopilot/{id}": {
        const all = (await loadMock(MOCK_FILES.autopilot)) || {};
        const goal = s.goals.find((x) => x.goal_id === id);
        if (!goal) throw notFound(`Goal ${id}`);
        if (!all[id] || goal.autonomy === "autopilot" || goal.status !== "active") throw new ApiError(404, "NOT_FOUND", "This goal is already on Autopilot, or not running.", null);
        return all[id];
      }
      case "POST /autopilot/{id}/turn-on": {
        const all = (await loadMock(MOCK_FILES.autopilot)) || {};
        const goal = findGoal(s, id);
        if (!all[id] || !all[id].ready || goal.autonomy === "autopilot") throw new ApiError(409, "CONFLICT", "This goal has not earned Autopilot yet.", null);
        const next = setGoal(s, { ...goal, autonomy: "autopilot" });
        return {
          seq: 1, at: now().toISOString(), business_id: next.business_id, goal_id: id, from_level: goal.autonomy, to_level: "autopilot",
          actor: "person:owner", reason: "turned_on", words: "Autopilot is on: it acts on its own inside your signed limits, and drops back to Approve by itself if proof turns negative.",
          evidence: {}, mandate_id: null, mandate_version: null, mandate_hash: null, mandate_ledger_head: null, prev_hash: "", hash: "",
        };
      }
      case "GET /connectors/connections/{id}/setup": {
        const all = (await loadMock(MOCK_FILES.connectorSetup)) || {};
        // The setup scenario is the usual case: Autonomy set up sending in Klaviyo on its own.
        if (scenario && all[parts[2]]) return { ...all[parts[2]], state: "ready", words: "Sending is set up in Klaviyo.", steps: [], channels: (all[parts[2]].channels || []).map((c) => ({ ...c, ready: true, words: "Email: ready", steps: [] })) };
        if (all[parts[2]]) return all[parts[2]];
        throw notFound(`Connection ${parts[2]}`);
      }
      case "GET /alerts":
        // The sample alerts belong to the sample goal: a fresh shop has none.
        return scenario ? [] : (await loadMock(MOCK_FILES.alerts)) || [];
      case "GET /opportunities":
        return (await loadMock(MOCK_FILES.opportunities)) || [];
      default:
        break;
    }
    throw new ApiError(404, "NOT_FOUND", `The sample data has no answer for ${method} ${url.pathname}.`, null);
  }

  // ---- the endpoints -------------------------------------------------------------------------------------
  const get = (path) => request("GET", path);
  const post = (path, body = {}) => request("POST", path, body);
  const patch = (path, body = {}) => request("PATCH", path, body);
  const del = (path) => request("DELETE", path);
  /** A GET of an endpoint an older API build may not have: null when the route is missing, as listBatches does. */
  const optional = (path) =>
    get(path).catch((err) => {
      if (routeMissing(err)) return null;
      throw err;
    });
  const pick = (opts, ...names) => {
    for (const n of names) if (opts && opts[n] !== undefined) return opts[n];
    return undefined;
  };

  const api = {
    get,
    post,
    /** Probe /v2 once; resolves to "live" or "mock". Screens need not call it: every request does. */
    init: probe,
    get mode() {
      return mode;
    },
    get reason() {
      return reason;
    },
    isMock: () => mode === "mock",
    /** Call `fn({mode, reason})` when the client falls back to mock data. Returns an unsubscribe function. */
    onModeChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    /** Call `fn(goal)` after any screen pauses, resumes or approves a goal. Returns an unsubscribe function. */
    onGoalChange(fn) {
      goalListeners.add(fn);
      return () => goalListeners.delete(fn);
    },
    /** Call `fn(batch)` after any screen approves or rejects a batch. Returns an unsubscribe function. */
    onBatchChange(fn) {
      batchListeners.add(fn);
      return () => batchListeners.delete(fn);
    },

    /** GET /v2/goals -> [Goal] */
    listGoals: () => get("/goals"),
    /**
     * POST /v2/goals {text, budget, currency, kind?, objective?} -> {goal, brief}. Takes an object or three
     * arguments. `kind` and `objective` are sent only when given (an Analyst opportunity already knows them), so
     * starting an opportunity keeps what the Analyst found instead of leaving the Strategist to guess it again.
     */
    createGoal: (textOrBody, budget, currency) => {
      const b = typeof textOrBody === "object" && textOrBody !== null ? textOrBody : { text: textOrBody, budget, currency };
      const payload = { text: b.text, budget: Number(b.budget), currency: b.currency || "USD" };
      // No budget given: the server reads the amount and the period from the words ("$5,000 this month").
      if (b.budget === undefined || b.budget === null || b.budget === "") delete payload.budget;
      if (b.ends_at || b.endsAt) payload.ends_at = b.ends_at || b.endsAt;
      if (b.kind) payload.kind = b.kind;
      if (b.objective) payload.objective = b.objective;
      return post("/goals", payload);
    },
    /** GET /v2/goals/{id} -> {goal, brief, content, proof} */
    getGoal: (goalId) => get(`/goals/${enc(goalId)}`),
    /** POST /v2/goals/{id}/approve {autonomy} -> {goal, brief} */
    approveGoal: (goalId, autonomy) =>
      post(`/goals/${enc(goalId)}/approve`, { autonomy }).then((out) => {
        goalChanged(out && out.goal);
        return out;
      }),
    /** POST /v2/goals/{id}/pause -> Goal */
    pauseGoal: (goalId) => post(`/goals/${enc(goalId)}/pause`).then(goalChanged),
    /** POST /v2/goals/{id}/resume -> Goal */
    resumeGoal: (goalId) => post(`/goals/${enc(goalId)}/resume`).then(goalChanged),
    /** GET /v2/goals/{id}/content -> [ContentItem] */
    getContent: (goalId) => get(`/goals/${enc(goalId)}/content`),
    /** POST /v2/content/{id}/status {status} */
    setContentStatus: (contentId, status) => post(`/content/${enc(contentId)}/status`, { status }),
    /** GET /v2/decisions?goal_id=&customer_id=&limit= -> [Decision]. Takes {goalId|goal_id, customerId|customer_id, limit}. */
    listDecisions: (opts = {}) =>
      get(`/decisions${query({ goal_id: pick(opts, "goalId", "goal_id"), customer_id: pick(opts, "customerId", "customer_id"), limit: opts.limit })}`),
    /** GET /v2/customers/{id} -> {state, decisions, events} */
    getCustomer: (customerId) => get(`/customers/${enc(customerId)}`),
    /** GET /v2/briefing?goal_id= -> Briefing | null */
    getBriefing: (goalId) => get(`/briefing${query({ goal_id: goalId })}`),
    /** GET /v2/proof?goal_id= -> ProofSummary | null */
    getProof: (goalId) => get(`/proof${query({ goal_id: goalId })}`),
    /** GET /v2/health -> HealthStatus */
    getHealth: () => get("/health"),
    /** GET /v2/alerts -> [Alert] */
    listAlerts: () => get("/alerts"),
    /** GET /v2/opportunities -> [Goal] */
    listOpportunities: () => get("/opportunities"),
    /**
     * GET /v2/batches?goal_id= -> [Batch], or null when this API build has no batch route yet (it answers
     * FastAPI's own 404), so a screen can say "not available" instead of showing an error or an empty inbox.
     */
    listBatches: (goalId) =>
      get(`/batches${query({ goal_id: goalId })}`).then(
        (rows) => (Array.isArray(rows) ? rows : []),
        (err) => {
          if (routeMissing(err)) return null;
          throw err;
        },
      ),
    /** POST /v2/batches/{id}/approve -> Batch */
    approveBatch: (batchId) => post(`/batches/${enc(batchId)}/approve`).then(batchChanged),
    /** POST /v2/batches/{id}/reject -> Batch */
    rejectBatch: (batchId) => post(`/batches/${enc(batchId)}/reject`).then(batchChanged),

    // ---- the promise endpoints (api/v2/insights.py). Each GET is null when this API build lacks the route. ----
    /**
     * GET /v2/today?goal_id=&day= -> Today: one day's work for a goal, counted over every decision that day
     * (contacted, left_alone, reasons [{code, words, n}], messages_sent, purchases, revenue, spend_to_date,
     * budget_left, last_run_at, next_run_at, learned). Default day: the latest day it decided.
     */
    getToday: (goalId, opts = {}) => optional(`/today${query({ goal_id: goalId, day: opts.day })}`),
    /**
     * GET /v2/activity?goal_id=&customer_id=&limit= -> [ActivityRow]: {decision, goal_id, display_name,
     * contacts_7d, delivery: {state, words, batch_id, at} | null, left_alone: {code, words} | null, item, content}.
     */
    listActivity: (opts = {}) =>
      optional(`/activity${query({ goal_id: pick(opts, "goalId", "goal_id"), customer_id: pick(opts, "customerId", "customer_id"), limit: opts.limit })}`),
    /**
     * GET /v2/customers/{id}/insight -> CustomerInsight: display_name, best_hour(_words), offer_habit(_words),
     * refill {item_id, item_name, days_until_due, window_open, basis}, next_items, items {id: name}, and per
     * decision: deliveries {decision_id: Delivery}, left_alone {decision_id: {code, words}}.
     */
    getCustomerInsight: (customerId) => optional(`/customers/${enc(customerId)}/insight`),
    /**
     * GET /v2/goals/{id}/plan -> PlanFacts: audience_n, agent_n, baseline_n, holdout_n, audience_words,
     * hard_rules [{code, words, value}], content [{content_id, reviewed_by, passed, checks [{check, passed, note}]}],
     * editor_passed, editor_failed, not_reviewed, rejected_by_person, autonomy, autonomy_since.
     */
    getPlanFacts: (goalId) => optional(`/goals/${enc(goalId)}/plan`),
    /** GET /v2/expected?goal_id= -> Expected: the agent's own forecast (label "forecast"), never proof. */
    getExpected: (goalId) => optional(`/expected${query({ goal_id: goalId })}`),
    /** GET /v2/health/limits?goal_id= -> HealthLimits: each stop rule in words, unsubscribe_baseline, judged. */
    getHealthLimits: (goalId) => optional(`/health/limits${query({ goal_id: goalId })}`),
    /**
     * GET /v2/data/status -> DataStatus: customers, orders, products, sources [{name, kind, customers, orders,
     * updated_at}], fixes [{what, rows}], problems, questions [{question_id, text, rows_affected}],
     * doctor_connected. (getDataStatus below is the older v1 /datasets call.)
     */
    getDataReport: () => optional("/data/status"),
    /** GET /v2/channels -> [{channel, mode: direct|handoff, ready, paused, paused_reason, words}] */
    listChannels: () => optional("/channels"),
    /** GET /v2/opportunities/sized -> [{goal, audience_n, reason}] */
    listSizedOpportunities: () => optional("/opportunities/sized"),
    /** GET /v2/batches/{id}/breakdown -> {batch_id, by_channel, by_offer, offer_labels, words} */
    getBatchBreakdown: (batchId) => optional(`/batches/${enc(batchId)}/breakdown`),

    // ---- the SaaS connectors (api/v2/connectors.py; docs/v2/CONNECTORS.md) ----
    /**
     * GET /v2/connectors -> {business_id, as_of, can_act, on_hold, platforms: [{kind, label, tagline, description,
     * oauth_ready, key_allowed, scopes, config_fields, sends, connections: [ConnectionView]}]}; null when this API
     * build has no connector routes. A ConnectionView carries `phase` (healthy, warning, syncing, backfilling,
     * stale, needs_reconnect, paused, disconnected), `words` (the card's one line) and `detail` (its description).
     */
    listConnectors: () => optional("/connectors"),
    /** POST /v2/connectors/{kind}/connect {config} -> {kind, url}: send the browser to `url` (the platform's consent). */
    startConnect: (kind, config = {}) => post(`/connectors/${enc(kind)}/connect`, { config }),
    /** POST /v2/connectors/{kind}/key {api_key, config} -> ConnectionView: connect with a pasted key (pilots). */
    connectWithKey: (kind, apiKey, config = {}) => post(`/connectors/${enc(kind)}/key`, { api_key: apiKey, config }),
    /** GET /v2/connectors/connections/{id} -> ConnectionView */
    getConnection: (connectionId) => get(`/connectors/connections/${enc(connectionId)}`),
    /** POST /v2/connectors/connections/{id}/sync -> {connection_id, streams, run_at, words} */
    syncConnection: (connectionId) => post(`/connectors/connections/${enc(connectionId)}/sync`),
    /** DELETE /v2/connectors/connections/{id} -> ConnectionView (disconnected) */
    disconnectConnection: (connectionId) => del(`/connectors/connections/${enc(connectionId)}`),
    /** GET /v2/connectors/connections/{id}/setup?refresh= -> {state: ready|pending|guided|not_needed|unavailable, words, steps} */
    getConnectorSetup: (connectionId, opts = {}) =>
      get(`/connectors/connections/${enc(connectionId)}/setup${query({ refresh: opts.refresh ? "true" : null })}`),
    // ---- zero-touch setup (api/v2/onboarding.py, api/v2/onboarding_brand.py; docs/v2/ZERO_TOUCH.md) ----
    /**
     * GET /v2/onboarding/proposals?margin_pct= -> OpportunityScan: {as_of, currency, proposals: [Proposal], preselected_id,
     * enough_history, not_enough, margin, skipped, notes}. A Proposal carries monthly_profit {point, low, high} (or null
     * when the history cannot size it, with `not_enough` saying why), audience_now, why, basis and goal_text. Null when
     * this API build has no onboarding routes.
     */
    getProposals: (opts = {}) => optional(`/onboarding/proposals${query({ margin_pct: opts.marginPct })}`),
    /** GET /v2/onboarding/limits?opportunity_id=&offers=&margin_pct= -> LearnedLimits: limits [{key, label, found, use, description, source, from_data, question}], terms. */
    getLimits: (opportunityId, opts = {}) =>
      optional(`/onboarding/limits${query({ opportunity_id: opportunityId, offers: opts.offers ? "true" : null, margin_pct: opts.marginPct })}`),
    /** POST /v2/onboarding/answers {margin_pct} -> OpportunityScan: the margin, asked once and kept. */
    answerMargin: (marginPct) => post("/onboarding/answers", { margin_pct: Number(marginPct) }),
    /** GET /v2/onboarding/margin -> MarginStatus: {question, explanation, answered, margin_pct, warning, note, ...}. */
    getMargin: () => optional("/onboarding/margin"),
    /**
     * POST /v2/onboarding/accept {opportunity_id?, margin_pct?, offers?} -> {goal, brief, proposal, limits, mandate, words,
     * notes}: the proposal becomes a goal, the limits a signed mandate, and the plan runs in Shadow.
     */
    acceptProposal: (body = {}) =>
      post("/onboarding/accept", body).then((out) => {
        goalChanged(out && out.goal);
        return out;
      }),
    /** GET /v2/onboarding/mandates -> [MandateRecord]: the mandates signed in setup, newest first. Null without the route. */
    listMandates: () => optional("/onboarding/mandates"),
    /** GET /v2/onboarding/brand?goal_id= -> BrandPage {status: not_read|waiting|approved, words, found, samples, fingerprint, ...}. */
    getBrand: (goalId) => optional(`/onboarding/brand${query({ goal_id: goalId })}`),
    /** POST /v2/onboarding/brand/read?goal_id= -> BrandPage: read the voice from Klaviyo and Shopify now (409 with nothing connected). */
    readBrand: (goalId) => post(`/onboarding/brand/read${query({ goal_id: goalId })}`),
    /** POST /v2/onboarding/brand/approve {fingerprint, approved_by?} -> BrandPage: the one approval of the voice. */
    approveBrand: (fingerprint, approvedBy) => post("/onboarding/brand/approve", approvedBy ? { fingerprint, approved_by: approvedBy } : { fingerprint }),

    // ---- the exceptions-only inbox and Shadow -> Autopilot (api/v2/inbox.py) ----
    /** GET /v2/inbox?refresh= -> {as_of, items: [InboxItem], count, this_week, quiet, words}; null without the route. */
    listInbox: (opts = {}) => optional(`/inbox${query({ refresh: opts.refresh ? "true" : null })}`),
    /** POST /v2/inbox/{id}/act -> {item, done, words, target}: press the item's one button. */
    actOnInbox: (itemId) => post(`/inbox/${enc(itemId)}/act`),
    /**
     * GET /v2/autopilot/{goal} -> Readiness {ready, sentence, detail, forecast, observed, value_error, checks, ...}: has the
     * goal earned Autopilot. Null when the route is missing, or the goal is already on Autopilot or not running (404).
     */
    getAutopilot: (goalId) =>
      get(`/autopilot/${enc(goalId)}`).catch((err) => {
        if (err && (err.status === 404 || err.status === 405)) return null;
        throw err;
      }),
    /** POST /v2/autopilot/{goal}/turn-on -> AutonomyChange; the goal is re-read so every screen sees the new level. */
    turnOnAutopilot: async (goalId) => {
      const change = await post(`/autopilot/${enc(goalId)}/turn-on`);
      try {
        const detail = await get(`/goals/${enc(goalId)}`);
        goalChanged(detail && detail.goal ? detail.goal : detail);
      } catch {
        /* the change stands; the screens re-read the goal themselves */
      }
      return change;
    },

    /** POST /v2/goals/read {text} -> {text, budget, currency, ends_at, period, words}: what the words say. */
    readGoal: (text) => post("/goals/read", { text }),
    /** PATCH /v2/goals/{id} {budget} -> {goal, brief}: lower a budget ("Approve with a small budget"). 409 above the approved plan. */
    updateGoalBudget: (goalId, budget) =>
      patch(`/goals/${enc(goalId)}`, { budget: Number(budget) }).then((out) => {
        goalChanged(out && out.goal);
        return out;
      }),
    /** POST /v2/pause -> {paused: [Goal]}: the one big Pause button, every active goal at once. */
    pauseAll: () =>
      post("/pause").then((out) => {
        for (const g of (out && out.paused) || []) goalChanged(g);
        return out;
      }),

    /**
     * What data is connected, from the v1 app's GET /datasets (the Data Doctor builds those). Null when it is
     * not available: in mock mode, without sign-in, or with no v1 server. Never throws.
     */
    async getDataStatus() {
      await probe();
      if (mode === "mock") return null;
      try {
        const root = base.replace(/\/v2$/, "");
        const r = await fetchImpl(`${root}/datasets`, { headers: { Accept: "application/json" } });
        if (!r.ok) return null;
        const body = JSON.parse(await r.text());
        return body && Array.isArray(body.datasets) ? body.datasets : null;
      } catch {
        return null;
      }
    },
  };

  // Other names a screen might reach for; the same calls.
  Object.assign(api, {
    goals: api.listGoals,
    getGoals: api.listGoals,
    goal: api.getGoal,
    approve: api.approveGoal,
    pause: api.pauseGoal,
    resume: api.resumeGoal,
    content: api.getContent,
    decisions: api.listDecisions,
    getDecisions: api.listDecisions,
    customer: api.getCustomer,
    briefing: api.getBriefing,
    proof: api.getProof,
    health: api.getHealth,
    alerts: api.listAlerts,
    getAlerts: api.listAlerts,
    opportunities: api.listOpportunities,
    getOpportunities: api.listOpportunities,
    batches: api.listBatches,
  });
  return api;
}
