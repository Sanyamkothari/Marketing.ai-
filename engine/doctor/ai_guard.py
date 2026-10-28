"""What stands between the Doctor and its AI: a spending limit per case, the client's one-time consent, and a
masked look at every file of the case.

* **Spending limit.** :class:`AiGuard` wraps the meter every Doctor AI call goes through. A case may use
  `data_doctor.ai_limits.max_tokens_per_case` tokens and `max_calls_per_case` calls in all, counted over the
  whole case (the counters on the case, not one request). Before each call the guard looks; when either is used
  up it refuses the call, the Doctor says so plainly and carries on with its rules alone. A call already under
  way is not cut short, so a case can end a little over its limit.
* **Consent.** The AI reads masked summaries of a client's data only after someone with the right role has said
  so once for that client. The yes is kept at `cases/<client_id>/ai_consent.json` with who gave it and when
  (`engine.ai_consent`, which also gates every other AI call). Without it the Doctor makes no AI call at all.
* **Every file masked.** :func:`file_overviews` gives the AI an overview of each of the case's other files, each
  one through that file's own egress gate (`engine.agent.egress`), like any tool result the chat reads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from engine.ai_consent import AiConsent, AiConsentStore, consent_key
from engine.config import BudgetConfig, LlmConfig
from engine.doctor.case import Case
from engine.generative.budget import Meter
from engine.generative.errors import BUDGET_EXCEEDED, generative_error

if TYPE_CHECKING:
    from engine.agent.contracts import SentItem
    from engine.agent.tools import AgentContext
    from engine.doctor.config import AiLimitsConfig

__all__ = [
    "AI_LIMIT_REPLY",
    "AI_NO_CONSENT_REPLY",
    "AiConsent",
    "AiConsentStore",
    "AiGuard",
    "consent_key",
    "file_overviews",
]

AI_LIMIT_REPLY: Final[str] = (
    "The AI has used up what this case may spend on it, so it will not be asked again here. "
    "Everything else keeps working without it: the questions, the suggestions, the preview and the approval."
)
AI_NO_CONSENT_REPLY: Final[str] = (
    "The Doctor's AI has not been allowed to read this client's data yet, so I am working with my rules alone. "
    "Allow it once for this client and I can use it."
)
MAX_OVERVIEW_CHARS: Final[int] = 20_000
"""The most the other files' overviews take of a chat prompt; files past it are left out, so a case with many wide
files still leaves room for the chat (the prompt's own limit is `engine.agent.loop.MAX_PROMPT_CHARS`)."""
_OVERVIEW_COLUMN_KEYS: Final[tuple[str, ...]] = (
    "name",
    "type",
    "null_rate",
    "looks_like_id",
    "personal_data",
)


class AiGuard(Meter):
    """The meter of the Doctor's AI calls, with the case's spending limit in front of every call.

    It is a `Meter` (the chat and its guardrails use it as one) whose budget check also looks at what the case
    has used: the tokens and calls already on the case plus those of this meter. :meth:`ask` says whether the AI
    may be asked now, and starts the count from the case.
    """

    def __init__(
        self,
        client: Any,
        *,
        job_id: str,
        llm: LlmConfig,
        budget: BudgetConfig,
        limits: AiLimitsConfig,
        consented: Callable[[str], bool],
    ) -> None:
        super().__init__(client, job_id=job_id, llm=llm, budget=budget)
        self._limits = limits
        self._consented = consented
        self._on_case = (0, 0)

    @property
    def chat_steps(self) -> int:
        """The most actions the chat takes to answer one message (`data_doctor.ai_limits.max_chat_steps`)."""
        return self._limits.max_chat_steps

    def _own_tokens(self) -> int:
        return sum(t.input_tokens + t.output_tokens for t in self._by_model.values())

    def tokens_used(self) -> int:
        return self._on_case[0] + self._own_tokens()

    def calls_used(self) -> int:
        return self._on_case[1] + self.calls

    def spent(self) -> bool:
        return (
            self.tokens_used() >= self._limits.max_tokens_per_case
            or self.calls_used() >= self._limits.max_calls_per_case
        )

    def ask(self, case: Case) -> Literal["ok", "no_consent", "spent"]:
        """Whether the AI may be asked now on this case; counts from what the case has used so far."""
        if not self._consented(case.client_id):
            return "no_consent"
        self._on_case = (
            case.counters.tokens - self._own_tokens(),
            case.counters.model_calls - self.calls,
        )
        return "spent" if self.spent() else "ok"

    def _check_budget(self) -> None:
        if self.spent():
            raise generative_error(BUDGET_EXCEEDED, limit="case", calls=self.calls_used())
        super()._check_budget()


def file_overviews(
    contexts: Sequence[AgentContext], labels: Sequence[str] | None = None
) -> list[tuple[dict[str, Any], SentItem]]:
    """An overview of each file, made safe for a prompt by that file's own gate, with the note of what it sent.

    One gate per file: its own columns, its own personal-data columns, the use case's settings. The overview is
    a short form of the chat's `get_profile` result, so it holds counts, types and roles, and no cell. Files past
    :data:`MAX_OVERVIEW_CHARS` are left out. `labels` name the tables (`F1`.. for files, `T1`.. for the tables a query
    made); without them each is its number.
    """
    from engine.agent.egress import Egress, sent_item
    from engine.agent.tools import call_tool
    from engine.agent.untrusted import for_prompt

    found: list[tuple[dict[str, Any], SentItem]] = []
    room = MAX_OVERVIEW_CHARS
    for number, ctx in enumerate(contexts, start=1):
        agent = ctx.config.agent
        columns = tuple(str(c) for c in ctx.frame.columns)
        gate = Egress.build(
            columns,
            personal_columns=[column.name for column in ctx.profile.columns if column.pii_kinds],
            always_hide=agent.always_hide_columns,
            shape_only=ctx.long_text,
            mode=agent.ai_data_access,
        )
        label = labels[number - 1] if labels is not None else number
        profile: dict[str, Any] = dict(
            call_tool(
                ctx, "get_profile", {}, evidence_id=f"f{number}" if labels is None else str(label).lower()
            ).result
        )
        short = {
            **{key: profile[key] for key in ("file_name", "rows", "columns_total", "primary_key_candidates")},
            "columns": [{key: c[key] for key in _OVERVIEW_COLUMN_KEYS} for c in profile["columns"]],
        }
        names = {column: gate.label(column) for column in columns}
        overview = for_prompt(gate.prepare(short), names)
        text = json.dumps(overview, ensure_ascii=False)
        room -= len(text)
        if room < 0:
            break
        found.append(
            (
                {"file": number if str(label).startswith("F") else label, **overview},
                sent_item(gate, "get_profile", {}, text),
            )
        )
    return found
