# Data Doctor AI-first: design for Stages 2, 3 and 4

Grounded in `origin/ai-first` @ `d204ac8a`. Line numbers refer to that commit.

## 0. Where the code is now, and two WIP defects to fix first

- **Stage 1 copied the rules instead of moving them.** `tools_facts.py:315-697` (`FactSuggestion`, `extract_key/goal_build/time/consent_facts`) now sits beside the `visit.py` rule writers, which still run. The two copies already differ:
  - **Build suggestion is not a valid build.** `extract_goal_build_facts` (`tools_facts.py:545-600`) has no `step` (it never goes through `propose_sql_step` / `SqlStepProposal.payload()`), no trial receipt, no audience, and does not run `_say_build_counts`.
  - **Default author is unused and wrong.** `author.run_default_author` (`author.py:150-239`) is never called. It also makes every item PENDING, so key and as-of items, which are "in the plan" today, would drop out of the plan.
- **The WIP `_own` change dropped the person-protection check.** `agent_chat.py:661-669` no longer calls `person_owns`. Today it is only safe because `visit._taken_back` (`visit.py:1525-1544`) still filters by source and `person_owns`. Package 1 below replaces both.

## 1. Stage 2: one author

### 1.1 Plan placement (decision)

Keep `ProposalState` as it is. There are about 120 `ProposalState.ACCEPTED` reads across `engine/doctor`, so changing the enum is too risky. Give the existing meanings explicit names:

| State | Name | Meaning |
|---|---|---|
| `ACCEPTED`, `decided_by=None`, in no approval | "in the plan, undecided" | The author put it in the plan. The approval card shows it as an assumption with yes/change. |
| `PENDING` | "waiting" | Needs the person's tick before it is in the plan. |
| `decided_by=<user>`, or in `case.approvals[*].item_ids` | the person's | Only the person changes it (`agent_apply.person_owns`, `agent_apply.py:155`). |

Rules for how decisions get recorded:

- **The approval decides.** `_approval` (`visit.py:4263-4375`) stamps `decided_by=actor` and `decided_at` on every undecided accepted item it covers. This meets acceptance criterion 1.
- **`decided_by=AGENT` is never written on an acceptance again.** Delete `agent_apply.agent_applies` (`agent_apply.py:280-336`) and its call in `_converse` (`visit.py:1418-1433`).
- **Invariant 1 in the harness is restated.** An accepted item must not have `decided_by ∈ {AGENT, "rules"}`. Once the case is in PRESCRIBE or later, every accepted item must be the person's.

### 1.2 The author action (new `engine/doctor/author.py`, replacing the WIP)

```python
class ItemDraft(StrictBase):          # what a fact tool or an AI tool call produced, receipt included
    kind: str; payload: dict[str, JsonValue]; title: str
    upload_id: str | None; goal_id: str | None
    confidence: AgentConfidence; evidence_ids: tuple[str, ...] = ()

class Create(StrictBase):   op: Literal["create"]; draft: ItemDraft; reason: str
                            replaces: str | None = None; basis: str  # "s:<suggestion id>" | "r<n>" evidence | "msg:<n>"
class Withdraw(StrictBase): op: Literal["withdraw"]; item_id: str; reason: str; consequence: str
class AskPerson(StrictBase):op: Literal["ask"]; ask: Ask                      # protocol.Ask, unchanged
class SetSetting(StrictBase):op: Literal["setting"]; goal_id: str; setting: str; value: str; reason: str
class Doubt(StrictBase):    op: Literal["doubt"]; doubt: CaseDoubt
class Request(StrictBase):  op: Literal["request"]; request: DataRequest
AuthorAction = Annotated[Create | Withdraw | AskPerson | SetSetting | Doubt | Request, Field(discriminator="op")]

@dataclass(frozen=True)
class ApplyContext:
    now: datetime; author: Literal["ai", "default"]; roles: Roles; settings: ProtocolSettings
    goal: GoalSettings | None          # Stage 3
    complete: bool                     # default author: True (retracts what it stops suggesting); AI: False
    requested_by: str | None = None    # the user id whose message caused it

@dataclass
class Applied:
    case: Case; events: list[EventDraft]
    refused: list[tuple[int, Refusal]]       # index of the action, Stage 4 shape
    made: list[str]                          # item/question ids created or changed

def apply_actions(case: Case, actions: Sequence[AuthorAction], ctx: ApplyContext) -> Applied
```

`apply_actions` is pure: it takes a case and returns a case, with no storage and no tool calls. It is the only code that writes items or questions. It runs on top of `reconcile` (`reconcile.py:133`) with a single producer, `"author"`. For each action:

1. **Guard first.** `guards_layer.check_action(case, action, ctx)` (§4) runs before anything is written. A refusal is recorded and the action is skipped; the next action still runs.
2. **`Create`.** Build the `CaseItem` with the current `_item` id rule (`visit.py:2066-2096`, moved here unchanged so ids stay stable):
   - `source=RULES` for the default author, `AI` for the AI. `proposed_by` is derived from that: `rules→default`, `ai→ai`. There is no new stored field.
   - `producer="author"`, `requested_by`, and the basis appended to `evidence_ids`.
   - Placement is decided by code, never by the author: in the plan when `agent_apply.waits_because(draft, roles)` returns None, otherwise PENDING. `waits_because` is generalised: drop its "not the AI's" first branch at `agent_apply.py:216`. `needs_yes` items (the build) are in the plan and confirmed at Approve, as today.
   - A twin of something the person left out (`twin_of`) is refused with `LEFT_OUT_BY_PERSON`.
3. **Replace.** `Create` with `replaces=X`:
   - X is undecided: swap now. The new item goes in the plan, X gets `REJECTED`, `superseded_by`, `withdrawn_because=reason`.
   - X is the person's: the new item goes in PENDING with `replaces=X`. `decide(accept=True)` already leaves X out through `swaps.leave_out_for` (`visit.py:4156`).
4. **`Withdraw`.**
   - X is PENDING and undecided: mark it `REJECTED` with `withdrawn_because`, now.
   - X is in the plan (ACCEPTED) or the person's: create a PENDING item `kind="withdraw"`, `payload={"item_id": X, "consequence": ...}`, `replaces=X`. On the person's yes, the same `leave_out_for` drops X. `plan_check.recipe_steps_of` already skips kinds that are not steps; add a test.
5. **`AskPerson`.** Goes through `protocol.settle`, which keeps the AUTO / assumption / ask choice and the question budget. This is today's `_settle_asks` (`visit.py:3644-3678`), moved here. Next, apply standing answers: `_standing` (`visit.py:3681-3778`) moves to `standing.py` as `apply_standing(store, case, questions)`, because a standing answer is the person's earlier decision. An answered question is never asked again (keep the rule from `_write`, `visit.py:741`).
6. **`SetSetting`.** Value outside `goal.allowed(setting)` is refused with `SETTING_VALUE_NOT_ALLOWED`. Otherwise it becomes a `Create` of `kind="goal_setting"` (§3), with `replaces` set to the current setting item.

What `apply_actions` records:

| Field | Value |
|---|---|
| `proposed_by` | from `source` |
| basis | in `evidence_ids` |
| `decided_by` | never set here |
| `replaces` / `superseded_by` / `withdrawn_because` | as above |
| events | `ITEM_PROPOSED`, `ITEM_DECIDED` (withdrawn), `QUESTION_ASKED` |

### 1.3 Rules become fact tools

Move each rule body out of `visit.py`, unchanged in logic, into `engine/doctor/tools_facts.py` (or `facts_goal.py` if it grows past about 700 lines). Each returns `Facts(facts=..., suggested=tuple[Suggestion, ...], left_out=int)`, where `Suggestion = {id, action: AuthorAction, reason}` and `id` = a stable hash of the action. Nothing writes the case.

| Rule writer (visit.py) | Fact function | Suggests |
|---|---|---|
| `_base_rules` 2165-2273, `_near_key_label` 2276, `_near_keys` 2349, `_row_id_item` 2423, `_event_file` 2406 | `key_facts(loaded, roles)` (replaces WIP `extract_key_facts`) | `Create key` / `AskPerson KEY_AMBIGUOUS, KEY_NOT_UNIQUE` / row-number `Create` |
| `_recommend_customer_column` 2316-2346, `_offered_build_key` 2302 | folded into `key_facts(..., build_key=...)` | the recommended option on `KEY_NOT_UNIQUE` |
| `_approved_build_key` 2286 | `decisions(case).build_key(upload_id)` | (none; it is a decision read) |
| `_generic_rules` 2472-2495 | `goal_fit_facts(ctx)` = paged `what_can_this_power` called directly (no AI budget) + `data_request` | none; writes `diagnosis.use_cases` (a fact, not an item) |
| `_goal_rules` 2523-2683 (outcome part), `_named` 2502, `_built_outcomes`, `_prepared_outcomes`, `_outcome_is_gone` 3006-3131 | `outcome_facts(case, brief, current)` | `Create outcome_column` (exact name) / `AskPerson OUTCOME_COLUMN` for an existing column / doubt and request |
| `_offer_build` 2686-2789, `_say_build_counts` 2812 | `build_facts.suggest_build(run, case, goal, settings)` (calls `propose_sql_step` as today) | `Create recipe_step{buildable, settings_digest}` |
| `_audience_question` 2878-2956, `_LAPSED_WORDS` | **deleted**; replaced by `goal_settings.open_setting_questions` (§3) | `AskPerson GOAL_SETTING` |
| `_time_rule` 3134-3213 | `time_facts(...)` | `Create as_of_column` / `AskPerson AS_OF_COLUMN` / doubt |
| `_consent_rule` 3216-3259 | `consent_facts(...)` | `Create consent_column` (PENDING) / doubt |
| `_detect` 3547-3569 + the seven detector ports (`api/routes/case_visit.py:608-617`) | `detector_facts(case, files, brief)`: `Detected.items→Create`, `asks→AskPerson`, `doubts→Doubt`, `requests→Request` | as listed |
| `_withdraw_role_steps` 1213-1251 | becomes guard `STEP_ON_ROLE_COLUMN` (§4) plus a suggested `Withdraw` | `Withdraw` |
| `_standing` | not a rule: `standing.apply_standing` inside `apply_actions` | (none) |

`gather_facts(run, case, brief) -> tuple[Facts, ...]` lives in `author.py`. It calls these Python functions directly; the AI reaches the same functions through the registry, where they are paged.

### 1.4 The default author

```python
def default_actions(facts: Sequence[Facts], case: Case, goal: GoalSettings | None) -> list[AuthorAction]:
    # every suggested action, in fact order, then one SetSetting per open goal setting whose config default is "safe"
```

Settings with `ask: true` (window, audience) are not set by the default author; they come up as questions. The visit applies the result with `ApplyContext(author="default", complete=True)`.

### 1.5 The AI goes through the same function

The merged AI turn (§2) keeps a working `Case`. For each mutating action it builds an `AuthorAction`. For `propose_*` that means the tool runs exactly as `_step`, `_sql`, `_features` and the others do today (`agent_review.py:794-953`), and the result becomes the `ItemDraft`. The turn then calls `apply_actions(working, [a], ctx_ai)`. Refusals go straight back into `turn.feedback` in the Stage 4 shape, so the AI can fix them within the same turn.

Two new actions:

- `adopt_suggestion{id, reason}` applies a fact suggestion as it stands. This is the cheap path for the build and key items: the AI does not re-derive SQL.
- `propose_setting{setting, value, reason}`.

The turn returns `TurnResult(actions, reply, chat_extras(suggestions, goal), meter, sent, blocked_by, error_codes)`. Inside the `change()` of `_write`, the visit re-applies `actions` to the fresh case. This is deterministic and runs under the case lock.

**Fallback.** If the AI turn could not run (`blocked_by in NO_LAST_WORD`, no consent, out of budget), the visit runs the default author for that event. The product never stalls.

### 1.6 One turn per event, and what is "owed" (`author.py: owed(case, brief) -> tuple[Owed, ...]`)

| Owed | When |
|---|---|
| `message` | last `case.chat` entry is `role="user"` and unanswered |
| `new_file` | `_pending_files` was true and intake has just finished (`visit.taken_in` changed) |
| `goal_chosen` | `active_goal.brief_hash != visit.done["author_goal"]` |
| `setting_changed` | `goal_settings_digest(case) != visit.done["author_settings"]` |
| `build_missing` | `build_facts.build_is_owed(case)` (`build_facts.py:81`) |
| `build_stale` | the plan's build `payload.settings_digest !=` current settings digest |
| `unblocked` | the last open blocking question was answered |

A tick or untick re-runs facts, the default-author refresh and the guards. It calls the AI only when that leaves something owed. This keeps the call count at or below the baseline (acceptance criterion 6).

The single TREAT handler in `visit.py` (replacing `_treat` 1112-1210) does this:

```python
def _author(run, case) -> _Step:
    if _pending_files(case): return _Step(to=INTAKE)
    goal, brief = ...                       # GOAL_BRIEF redirect as today
    facts = gather_facts(run, case, brief)
    owe = owed(case, brief)
    result = run.deps.author(case, files, brief, TurnEvent(owe, facts)) if owe and run.deps.author else None
    actions = result.actions if result and not result.blocked_by else default_actions(facts, case, goal)
    step = _Step(to=PREVIEW, authored=(actions, ctx))   # _write applies them under the lock
    step.visit["done"] = {...author_goal, author_settings...}
    step.to = ASK if blocking_after else PREVIEW
```

`_HANDLERS` (`visit.py:2042`): `TREAT` and `CONVERSE` both map to `_author`. Old cases stored in CONVERSE resume correctly. `chat()` (`visit.py:4721`) moves to TREAT.

**Deleted from `visit.py`** (about 2,000 lines, 4,798 → about 2,700):

- `_treat` 1112-1210
- `_withdraw_role_steps` 1199-1251
- `_mark`, `_reconciled` 1254-1308
- `_twin_of_person`, `_converse` 1311-1506
- `_decided_as`, `_taken_back`, `_proposed_goal`, `_suggested` 1519-1579. The chat-entry building moves to `author.chat_entry(result)`.
- The whole rules section 2059-3260, except `_audience_left_out`, which moves to `buildable.py`.
- `_detect`, `_review`, `_settle_asks`, `_standing` 3543-3778. `_add_request` and `_ground` move to `author.py`.

`VisitDeps.converse` and `VisitDeps.agent` become one `author: AuthorPort | None`.

### 1.7 Stored cases keep working (lazy, no switch)

- **Producers.** `reconcile` treats the old producers `rules` and `detectors` as aliases of `author` with `complete=True`, so the default author retracts stale undecided rule items. `review` and `chat` count as AI (`AI_PRODUCERS`).
- **States.** `VisitState.CONVERSE` resumes into `_author`.
- **Old questions.**
  - Answered `BUILD_AUDIENCE`: read by `decisions(case).setting(goal, "audience")` as a fallback.
  - Open `BUILD_AUDIENCE`: retracted by the next complete default emission, then asked again as `GOAL_SETTING`.
- **Old builds.** A build with no `settings_digest` takes it from its payload (`outcome_window_days`, `audience.audience_id`), so it does not look stale.
- **Other items.** Old `decided_by=AGENT` accepted items keep their meaning. They are undecided until the next approval stamps them. There is no rewrite of stored documents.

## 2. One prompt and one action set

- **One prompt.** New `configs/prompts/data_doctor.v3.md` (`name: data_doctor`). Delete `data_doctor_agent.v2.md` and `data_doctor_chat.v2.md`.
  - Variables: `goal, tools, actions, step_kinds, doctor_limits, guide_index, questions, state, conversation, event, turn_results`.
  - The system section is the chat prompt's tone and rules 1-8, plus the agent prompt's "How you work" 1-6.
  - R1-R29 move to a new guide, `configs/doctor/guides/rules.md`, read with `read_guide rules`. This cuts about 3k tokens per call; keep the cap in `test_guides.py`.
  - The user section is `EVENT` (owed reasons in words, plus the person's message if any), `CONVERSATION`, `STATE`, `TURN RESULTS`.
- **`actions.py`.**
  - Delete `Surface`, `ActionSpec.surfaces`, `surface_models`, `json_schema_extra={"surface": ...}`, and the `surface` parameter of `action_lines`, `step_kind_lines` and `prompt_variables`.
  - One `ACTIONS` tuple: the current union, plus `adopt_suggestion`, `propose_setting`, and `withdraw` (any item; args `item, reason, consequence`).
  - `replaces` goes on every `propose_*`. `reply` ends every turn. `ALIASES = {"ask_question": "ask", "finish": "reply"}`.
  - `step_kind_lines()` uses one set: merge `CHAT_STEP_KINDS` and `DOCTOR_STEP_KINDS` in `engine/agent/loop.py`. Note: `actions.py:498` currently has them swapped.
  - `make generate` and `test_prompt_contract.py` follow.
- **One turn class.** New `engine/doctor/agent_turn.py` with `class DoctorTurn`. It combines `_Review.__init__` and `run` (`agent_review.py:281-703`) with `_Chat.talk`, `_last_word` and `_reply` (`agent_chat.py:385-570`).
  - Dispatch is one `_act`.
  - `_MEANING_TOPICS` stays only as a check on quick questions.
  - `_own` and `_taken_back` are deleted. `withdraw` and `replaces` build `AuthorAction`s.
  - `agent_review.py` and `agent_chat.py` are deleted. Helpers (`_Sql`, `_gate`, `_data_result`, `own_numbers`) move to `agent_turn.py`.
- **Rendered state** (`DoctorTurn.state()`):

```json
{"files":[F1..Fn, T1..], "goal":{"name","a_yes_means",
  "settings":[{"name":"window_days","value":90,"set_by":"default|person|pending","allowed":[30,60,90]},
              {"name":"audience","value":null,"open":true,"options":[{"id":"quiet","label","description","recommended":true}]}],
  "snapshot_rule": "...", "minimums":{"rows":1000,"positives":200}},
 "goals_that_fit":[...]  /* only with no goal; paged, left_out */,
 "plan":[{"id":"c3f9a1","what","state":"in plan|waiting|left out","by":"you|default|person",
          "decided_by":"person|approval|none","receipt":{...,"counted_on":"sample of 1,000 rows"|"whole file"}}],
 "questions":[{"id":"q8b20c","text","options":[{"id","label"}],"answer"}],
 "suggested":[{"id":"s41d0e","what","reason"}] /* top 8, left_out */,
 "stops":[Refusal...] /* deduped by key */, "to_do_now":["propose the build", ...], "doubts":[...]}
```

- **Stable handles.** `c`/`q`/`s` plus 5 hex characters of `sha256(id)`. Raw ids are also accepted. This replaces the positional `c1`/`k1` (`agent_chat.py:205, 266`), which is what made aliases unstable.
- **`to_do_now`.** Generated from `owed()`, replacing `TO_DO_BUILD` and `TO_DO_AUDIENCE` in `build_facts.py:35-56`.

## 3. Stage 3: goals as data

### 3.1 Schema

Extend `OutcomeTemplate` (`outcomes.py:111`) with an optional `settings` block. `days`, `default_days` and `audience` are kept as aliases and read into it, so the other templates need no edit.

```yaml
retail-win-back:
  question: When has a shopper come back?
  sentence: "{An_entity} has come back when there is {an_event} within {days} days after the snapshot."
  label_type: event_presence
  event_kinds: [orders, payments, activity, other_event]
  settings:
    window_days:
      allowed: [30, 60, 90]
      default: 90
      ask: false                      # proposed with its reason; the person may change it in chat
      why: "90 days gives a lapsed shopper a full quarter to come back."
    audience:
      ask: true
      options:
        - id: active
          label: "Every {entities} active on or before the snapshot"
          description: "Includes shoppers still buying; ranks everyone by return chance."
        - id: quiet
          quiet_days: 90
          label: "Only {entities} who have gone quiet (no order in the {quiet_days} days before the snapshot)"
          description: "The shoppers who have stopped buying: who this goal is for."
      recommended: quiet               # from the goal's own meaning, stated here, never a regex
      because: "Retail Win-back is for shoppers who have stopped buying."
      dont_know: quiet                 # the safe choice; falls back to the widest option that reaches the minimums
  snapshot_rule: latest_month_end_with_window_over     # build_facts.SNAPSHOT_RULE becomes this enum's text
  build:
    recipe: rfm                        # compile_build's preset; fallback "events" = the count features today
    outcome_column: reactivated        # window-neutral (decision below)
  # minimums are NOT repeated: read from use_cases/retail_win_back.yaml validation.min_rows / min_positive
```

**Loader.** `engine/doctor/goal_settings.py` (new) provides `load_goal_settings(goal_id, root) -> GoalSettings`, with `allowed(name)`, `default(name)`, `options(name)`, `recommended(name, facts)` and `minimums`.

The recommendation is only offered when the facts support it. For the audience, that means `BuildAudience.enough` for that option. If no option reaches the minimums, there is no recommendation, and `dont_know` falls back to the widest option that is enough.

**Labels.** Labels come from config and are formatted with counts in code: "Only 1,204 shoppers who have gone quiet … (312 a yes)". The recommendation reason is the configured `because`. That removes the label that contradicted the description (symptom 7).

### 3.2 Values on the case

- **Storage.** A setting value is a `CaseItem(kind="goal_setting", payload={"goal_id","setting","value"})`. Its subject is `setting@goal:name`; add it to `subjects.item_subject`.
- **Reading.** `decisions(case).setting(goal, name)` returns the in-plan or person value, then the old `BUILD_AUDIENCE` answer, then the config default.
- **Questions.** `goal_settings.open_setting_questions(goal, case, facts)` makes one `AskPerson` per `ask: true` setting with no value yet:
  - code `GOAL_SETTING`, subject `{goal, setting}`;
  - options = the allowed values, each with effect `goal_setting`, plus "I don't know" mapped to `dont_know`;
  - `recommended` and `why` as in §3.1.

  This replaces `_audience_question` and also the build's outcome-column question. In `_goal_rules`, the planned-build branch (2591-2643) goes away because the build declares its own outcome: `payload.outcome_column`. `decisions` reads an accepted build's declared outcome as `pair("outcome")`. The person's yes to the build covers it, which `needs_yes` already requires.

### 3.3 Changing a setting re-derives the build

- **Parameters.** `buildable_outcome(files, config, root, *, window_days=None, audience_id=None)` (`buildable.py:300`). The defaults keep today's callers working. `compile_build` takes `settings`.
- **Rebuild.** When a person says yes to "60 days", the settings digest changes and `build_stale` is owed. The author's fact `suggest_build(settings)` makes a new build with `Create(replaces=<old build>, reason="window changed from 90 to 60 days")`.
  - The old build is undecided before approval, so it is swapped immediately.
  - After an approval it is the person's, so the new build waits for their yes as a swap.

### 3.4 Outcome column naming (decision: window-neutral)

The built column is `build.outcome_column` (`reactivated`) for every window. The window lives in the outcome sentence and the setting. The column name never changes, so the person's outcome decision never points at a column that no longer exists.

- `config.target.column` (`reactivated_90d`) stays the name looked for in files that already carry an outcome.
- `compile_build` (`buildable.py:440`) and `build_facts.build_facts` (`build_facts.py:124`) use `build.outcome_column`.
- Package 3 must confirm that the prescription maps the decided outcome role to the model target (`prescription.py:~970`). If it does not, rename it once in `materialize.py`. This is the only place the name can drift.

## 4. Stage 4: one guard layer and refusals people can act on

### 4.1 Shape

New `engine/doctor/refusals.py`:

```python
class Refusal(StrictBase):
    code: str
    what_failed: str
    value: str | None      # names, counts only
    how_to_fix: str        # never empty

HOW_TO_FIX: Final[dict[str, str]]   # a default fix sentence for every code

def refusal_of(exc: AgentToolError | CaseError | Finding) -> Refusal
```

- `AgentToolError.__init__(code, message, person="", *, value=None, how_to_fix="")` (`engine/agent/tools.py:129`). An empty `how_to_fix` takes `HOW_TO_FIX[code]`. `DataToolError` and `CaseError` get the same.
- `ledger.Finding` gains `how_to_fix`, derived from `resolutions` plus the table.
- The AI's `turn.feedback`, the state's `stops`, and the screen's `view.ledger` rows all serialise `Refusal`.

### 4.2 One layer (`engine/doctor/guards_layer.py`)

- **`check_action(case, action, ctx) -> Refusal | None`** runs inside `apply_actions`. Its checks:
  - target exists and is in the active section (`ITEM_NOT_FOUND`; `value` = the handle; the fix lists the valid handles);
  - `STEP_ON_ROLE_COLUMN` (from `_withdraw_role_steps`);
  - `FEATURE_IS_ID` ("Include the CustomerID as a feature");
  - `OUTCOME_IS_PERSONS` (the outcome is chosen by answering its question, and a column that is not yes/no cannot be one: "Use Country as the outcome");
  - `SETTING_VALUE_NOT_ALLOWED`;
  - `LEFT_OUT_BY_PERSON`;
  - `APPROVAL_IS_PERSONS` (any attempt to approve, or to set `decided_by`).
- **`check_plan(case, check) -> tuple[Refusal, ...]`** combines `ledger.findings_of` (already merges plan_check, trials and readiness), the leakage probe (`reads_future` only in the build's outcome), consent (`d.consent_missing`), currency (`CURRENCY_MIXED`), and the minimums from `GoalSettings.minimums`.
  - It dedupes by `Finding.key` across sources, extending `ledger._one_per_key` (`ledger.py:207`) to ignore `source`. This fixes the doubled `PLAN_LOSES_TOO_MUCH`.
  - It runs in `_author` after applying, and in `_preview` (`visit.py:1582`) before APPROVE. `_approval` reads it (`visit.py:4290-4300`).

### 4.3 Refusal sites to convert

| Group | Sites | What changes |
|---|---|---|
| Doctor AI turn | `agent_review.py` (17), `agent_chat.py` (19) | Merged into `agent_turn.py`. `AGENT_ITEM_UNKNOWN`/`PERSONS` and `AGENT_SUGGESTION_UNKNOWN` become `ITEM_NOT_FOUND` with the valid handles. |
| Shared loop | `engine/agent/loop.py` (14) | `AGENT_STEP_NO_EFFECT` (`loop.py:1044-1069`): replace the WIP full-recipe rerun with one whole-column matched-row count (DuckDB `COUNT(*) WHERE <predicate>` on the full upload). State the count; refuse only at 0. |
| SQL errors | `engine/agent/sql_step.py:_error_kind` (594) | Binder and conversion errors return `value=<column>` and `how_to_fix="'<col>' is still text: its parse step is not in the plan yet; run preview_working_copy, then cast or propose the parse."` The column comes from DuckDB's `Referenced column "x"` / `column "x"`, matched against the frame's column names, so no cell is leaked. |
| Tools | `tools_wrappers.py` (27), `tools_reshape.py` (18), `registry.py` (11), `combine_run.py` (9), `case_files.py` (9), `agent_unpivot.py` (11), `tools_sql.py` (7), `agent_amount.py` (6), `tools_features.py` (5), `tools_wave_one.py` (4), `tools_outcomes.py` (4), plus the smaller ones | Covered by `HOW_TO_FIX` defaults. Hand-edit the 10 most frequent codes with `value`. |
| Visit | `visit.py`: `APPROVAL_STOP_OPEN`, `APPROVAL_CONFIRMATION_MISSING`, `APPROVAL_SENTENCE_CHANGED`, `_stop`/`VisitStop` (877) | Refusal shape. `_stop` keeps the full message, which is no longer cut to 400 characters. |
| Clipping | `agent_chat._last_word` and `_reply` | Delete the clip. `clean_text` only for model input, never for the AI's reply. |

Also update `configs/doctor/guides/sql_steps.md` (within its cap):

- `$as_of` is a bound parameter and is never quoted.
- Run `preview_working_copy` before date SQL.
- The output keeps the input key column name.

## 5. Work packages

Each package runs only its own targeted unit tests with `-n auto`. The full suite and the scenarios run on CI or locally, as you asked.

**P0: tighten the harness** (small; first; owner of `scripts/doctor_scenarios.py`, `tests/integration/test_doctor_scenarios.py`, fixture `expect.json`)

- Add per-instruction end states to `08_chat_instructions/expect.json`: `window_60`, `uk_only`, `keep_dupes`, `undo_date`, `id_feature`, `country_outcome`, `ignore_safety`, `prepare_training`.
- Make invariants fail the test. Restate Invariant 1 as in §1.1, and require Invariant 4 to check `how_to_fix`.
- Add a `run_scenario(with_ai=False)` parametrisation.
- Mark today's failures with an explicit strict `XFAIL` set in the wrapper, so every later package turns named entries green.

**P1: author core** (owns `author.py` rewrite, `reconcile.py`, `swaps.py`, `agent_apply.py` (generalise `waits_because`, keep `agent_applies` until P6), `subjects.py`, `standing.py` (`apply_standing` moved in))

- Build `AuthorAction`, `apply_actions`, the replace/withdraw rules and producer aliases.
- `check_action` comes in as a hook stub that P4 fills in.
- Land the models and signature as the first commit, so P5 can code against them.
- Tests in `tests/unit/doctor/test_author.py`:
  - replace or withdraw of an undecided, an in-plan and a person item;
  - a twin of a person's no;
  - a stale undecided item is retracted only for `complete`;
  - an old `rules` producer is retracted;
  - `apply_actions` is deterministic (applying twice gives the same case).

**P2: fact tools** (owns `tools_facts.py`, optional new `facts_goal.py`, `catalogue.py`/`policy.yaml` registrations)

- Move the logic of the rule writers in §1.3 into fact functions returning suggestions. Delete the WIP `extract_*` divergences. `suggest_build` goes through `propose_sql_step`.
- Do not edit `visit.py`.
- Tests: golden parity. On the scenario fixture frames, the suggestions' item and question ids and payloads equal what today's `_base_rules`, `_goal_rules` and `_time_rule` produce (call both in the test).

**P3: goals as data** (owns `outcomes.py`, `configs/outcomes.yaml`, new `goal_settings.py`, `buildable.py`, `build_facts.py`, `decisions.py` (`setting()`, build-declared outcome))

- Schema, loader, `open_setting_questions`, the window and audience parameters, window-neutral `build.outcome_column`, the prescription target check.
- Tests:
  - 60 days changes both the SQL interval and the counts;
  - the recommended audience comes from config and is dropped when not enough;
  - `dont_know` fallback;
  - old `BUILD_AUDIENCE` answer is read;
  - every template in `outcomes.yaml` loads.

**P4: guard layer and refusals** (owns `refusals.py`, `guards_layer.py`, `engine/agent/tools.py`, `engine/agent/sql_step.py`, `engine/agent/loop.py` (the no-effect count only), `registry.py`, `ledger.py`, `plan_check.py`, `guides/sql_steps.md`)

- Tests:
  - an AST scan: every `AgentToolError`/`DataToolError`/`CaseError` code in `engine/` has a `HOW_TO_FIX` entry;
  - a binder error names the column;
  - a filter matching only rows past 1,000 is not refused, and its count is stated;
  - one `PLAN_LOSES_TOO_MUCH` per key.
- Turns green: Invariant 4 (`how_to_fix`) and the `id_feature`/`country_outcome` refusal wording, once wired in P6.

**P5: one AI turn and one prompt** (owns new `agent_turn.py`, deletes `agent_review.py` and `agent_chat.py`; owns `actions.py`, `engine/agent/loop.py` (step-kind merge only, coordinate with P4: different hunks), `configs/prompts/*`, `configs/doctor/guides/rules.md`, `test_prompt_contract.py`, generated files)

- Tests: action dispatch through `apply_actions` with a fake model; stable handles across two turns; no clipping; prompt contract.
- Keep `lazy_review` and `lazy_converse` as thin adapters over `DoctorTurn` until P6, so the app runs at every commit.

**P6: visit integration** (owns `visit.py`, `api/routes/case_visit.py`, `conversation.py`, `screen.py`; also re-records `replies.jsonl` and the eval cassette `tests/fixtures/doctor_eval/cassettes/ai_path.json`)

- Build `_author` and `owed`. Delete the code listed in §1.6. Approval stamps `decided_by`. Merge the two ports into `author=`. Delete `agent_applies`. The screen shows `proposed_by` and `Refusal`.
- Turns green: all P0 xfails (`window_60`, `uk_only`, `keep_dupes`, `undo_date`, `prepare_training`, Invariant 1 everywhere, no-AI parity), and acceptance criteria 1, 2, 4, 5 and 7.

**Order.**

- Wave 1, in parallel: P0, P1, P2, P3, P4.
- Wave 2: P5, starting once P1's interface commit lands.
- Wave 3: P6, after P1-P5 have merged.

File overlap is limited to `engine/agent/loop.py` (P4 and P5, different functions) and `agent_apply.py` (P1 edits it; P6 only deletes from it).

## 6. Risks and how to keep the app working

1. **Big-bang integration in P6.**
   - Mitigation: P1-P5 are additive or adapter-backed. P6 is the only commit that changes behaviour.
   - Split P6 into two commits: (a) `_author` running the default author only, with `deps.agent` and `deps.converse` still used through the adapters; (b) AI through `deps.author`, and delete `_converse`.
2. **Item ids change, so stored cases re-propose items.** Keep the `_item` and `_question` id formulas byte for byte, and use P2's parity tests.
3. **"In the plan, undecided" can be misread as accepted.** The approval stamp, the restated Invariant 1, and the screen label "assumed — yes / change" make it explicit.
4. **Fewer AI calls must still cover the work.** `owed()` gates the AI. Watch the COST line per scenario on CI against `DATA_DOCTOR_BASELINE_REPORT.md`.
5. **The target name after the window-neutral change.** P3's prescription check is required before P6.
6. **Prompt change invalidates recordings.** P6 re-records `replies.jsonl` and the eval cassette. The harness reports any scenario whose prompt hash changed (P0).
7. **The whole-column count for a filter step costs time on large files.** Use one DuckDB `COUNT` on the already-loaded full frame, which is the same reader as `full_count`, never the recipe.
8. **Compute.** Each package runs only its own unit tests (`pytest tests/unit/doctor/test_<x>.py -n auto`) plus `ruff`, `black --check` and `mypy` on the files it touched. `make test` and the scenario suite run on CI or locally after each wave.

Files read: `/tmp/claude-0/-home-user-Marketing-ai-/d6d5fd70-42e1-5e46-82cf-6ef971d5beb2/scratchpad/wt-af-read/engine/doctor/{visit,author,agent_chat,agent_review,agent_apply,actions,case,reconcile,build_facts,buildable,tools_facts,outcomes,guards,ledger,plan_check}.py`, `.../configs/outcomes.yaml`, `.../configs/use_cases/retail_win_back.yaml`, `.../scripts/doctor_scenarios.py`