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
});

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
    store = { goals: Array.isArray(goals) ? goals : [], details: new Map(), template: detail || null, primary: null, made: 0, batches: null };
    if (detail && detail.goal) {
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

  async function mockRequest(method, path, body) {
    const url = new URL(path, "http://mock.invalid");
    const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    const q = url.searchParams;
    const s = await state();
    const key = `${method} /${parts.map((p, i) => (i > 0 && ["goals", "content", "customers", "batches"].includes(parts[i - 1]) ? "{id}" : p)).join("/")}`;
    const id = parts[1];

    switch (key) {
      case "GET /goals":
        return clone(s.goals);
      case "POST /goals": {
        const text = String((body && body.text) || "").trim();
        const budget = Number(body && body.budget);
        if (!text) throw new ApiError(422, "INVALID", "Say what you want to achieve.", null);
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
          currency: String((body && body.currency) || "USD").toUpperCase(),
          starts_at: at,
          ends_at: null,
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
      case "GET /health":
        return (await loadMock(MOCK_FILES.health)) ?? null;
      case "GET /alerts":
        return (await loadMock(MOCK_FILES.alerts)) || [];
      case "GET /opportunities":
        return (await loadMock(MOCK_FILES.opportunities)) || [];
      default:
        throw new ApiError(404, "NOT_FOUND", `The sample data has no answer for ${method} ${url.pathname}.`, null);
    }
  }

  // ---- the endpoints -------------------------------------------------------------------------------------
  const get = (path) => request("GET", path);
  const post = (path, body = {}) => request("POST", path, body);
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
