"""Standing answers (Plan O M246, DEC-1816; the design §7.2, decision O12): an answer remembered per client.

Data Doctor asks one clear question, remembers the answer **for this client** and applies it next time, visibly
and revocably ([REQ] 8, 11):

* When a person answers a question of a kind that may stand (:data:`STANDING_CODES`: the detectors of the design
  §7.1 and the time settings of §3.5) with an option that is not APPROVE-class, the answer is written down here:
  the detector (the question's code), the column's fingerprint, the values it is about (`value_signature`) and
  the option chosen, with who answered and when.
* Next time the same detector fires on the same fingerprint and the same values - in this case or another case
  of the same client - the visit applies it as a **pre-ticked item** that says so ("Using your answer from
  12 Oct: ..."), shown in the checklist, the preview and the receipt like any other. Nothing is applied silently.
* **Only for the same meaning.** The answer keeps the meaning of what was chosen (:func:`meaning_of`: the
  option's change - its kind and parameters - without the ids of the file it was made on). It is applied only
  when the option with the same label still does exactly that; when the same button now carries another change
  (another spelling, unit factor or format), the question is asked again and says the earlier answer no longer
  fits.
* :meth:`StandingAnswerStore.revoke` revokes it (Settings lists every answer per client with Revoke); a revoked
  answer is asked again next time, and revoking it inside a case asks the question there again.
* **Never for APPROVE-class items** (O12): an outcome definition, consent, keeping personal data, a threshold,
  rows left out - those are asked again whenever the evidence changes.

Stored at `cases/<client_id>/standing_answers.json`, beside the client's cases: the `cases` store of
`engine.privacy.layout`, so an erasure request finds a person's id in it like in any JSON artefact (and
`engine.doctor.case.remove_principal` already knows the list's shape). What is stored: ids, the detector's code,
a column name, the values the question was about (spellings such as `haan` / `nahi`, never a row) and the option.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.doctor.case import CASE_PREFIX, CLIENT_ID_PATTERN, CaseQuestion, payload_digest
from engine.storage import Storage, StorageError

__all__ = [
    "PER_FILE_KEY",
    "STANDING_CODES",
    "STANDING_FILENAME",
    "StandingAnswer",
    "StandingAnswerStore",
    "StandingAnswers",
    "answer_id_for",
    "describe",
    "fingerprint_of",
    "may_stand",
    "meaning_of",
    "signature_of",
    "standing_key",
]

STANDING_FILENAME: Final[str] = "standing_answers.json"

STANDING_CODES: Final[frozenset[str]] = frozenset(
    {
        # The design §7.1's detectors (M245 raises them through the visit's detector port).
        "UNREADABLE_VALUES",
        "UNIT_MISMATCH",
        "CURRENCY_FOREIGN",
        "MEANING_CHANGED",
        "VALUE_DRIFT",
        "NEW_CATEGORY",
        "NEW_COLUMN",
        "MISSING_COLUMN",
        "UNKNOWN_HEADER_WORD",
        "UNKNOWN_BOOLEAN",
        "KEY_FORMAT_CHANGED",
        "DATE_ORDER_FLIPPED",
        "DATE_ORDER",
        # The design §3.5's time settings that stand per client (M235 asks AS_OF_COLUMN; M244 the others).
        "AS_OF_COLUMN",
        "TIME_CUTS",
        "LABEL_MATURITY",
        "SNAPSHOT_CADENCE",
    }
)
"""Questions whose answer may stand for the client. Never: `CURRENCY_MIXED` (it stops), `UNKNOWN_CONSENT_WORD`
(consent vocabulary is APPROVE-class), the outcome and its window (part of the outcome definition), a key or a
role chosen for one file (`KEY_AMBIGUOUS`), and every APPROVE-class item (`engine.doctor.protocol`)."""

_LOCK: Final = threading.Lock()


def standing_key(client_id: str) -> str:
    return f"{CASE_PREFIX}{client_id}/{STANDING_FILENAME}"


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def signature_of(question: CaseQuestion) -> tuple[str, ...]:
    """The values a question is about, normalised and sorted; its options' labels when it names no values."""
    values = question.value_signature or tuple(option.label for option in question.options if not option.safe)
    return tuple(sorted({str(value).strip().casefold() for value in values}))


def fingerprint_of(question: CaseQuestion) -> str:
    """The column's fingerprint: the detector, the column's name as written (case and punctuation aside), the
    goal. Two files of the same client with the same column get the same fingerprint."""
    return payload_digest(
        {"code": question.code, "column": _norm(question.column), "goal": question.goal_id}
    )[:32]


_FILE_IDS: Final[frozenset[str]] = frozenset(
    {"upload_id", "upload_ids", "evidence_id", "evidence_ids", "item_id", "question_id", "case_id"}
)
"""Keys naming the file, case or evidence a change was made on: not part of what the change means."""


PER_FILE_KEY: Final[str] = "per_file"
"""An effect payload may name, under this key, its own keys whose values are worked out again on each file (Plan O
M244: the cut-off date of "leave the newest month out", and the recipe step carrying it). They are not part of what
the choice means, so next month's file - a later cut-off - applies the same answer instead of asking again."""


def _without_ids(value: object) -> object:
    if isinstance(value, Mapping):
        own = value.get(PER_FILE_KEY)
        recomputed = {str(k) for k in own} if isinstance(own, list | tuple) else set()
        if recomputed:
            recomputed.add(PER_FILE_KEY)
        return {
            str(k): _without_ids(v)
            for k, v in value.items()
            if str(k) not in _FILE_IDS and str(k) not in recomputed
        }
    if isinstance(value, list | tuple):
        return [_without_ids(v) for v in value]
    return value


def meaning_of(effect_kind: str | None, effect_payload: Mapping[str, object]) -> str:
    """What choosing an option does, as a digest: the change's kind and payload without file-specific ids, so
    the same choice on next month's file has the same meaning and a different change behind the same button
    does not."""
    return payload_digest({"kind": effect_kind, "payload": _without_ids(effect_payload)})


def answer_id_for(client_id: str, question: CaseQuestion) -> str:
    """The standing answer's id for this question of this client: stable (the detector, fingerprint, values)."""
    return (
        "sa_"
        + payload_digest(
            {
                "client": client_id,
                "code": question.code,
                "fp": fingerprint_of(question),
                "sig": list(signature_of(question)),
            }
        )[:24]
    )


def may_stand(question: CaseQuestion, effect_kind: str | None, effect_payload: Mapping[str, object]) -> bool:
    """Whether answering `question` with an option adding `effect_kind` may become a standing answer."""
    from engine.doctor.protocol import approve_class_of

    if question.code not in STANDING_CODES:
        return False
    return effect_kind is None or approve_class_of(effect_kind, effect_payload) is None


class StandingAnswer(StrictBase):
    """One remembered answer (the design §7.2)."""

    answer_id: str = Field(
        pattern=r"^sa_[a-f0-9]{24}$", description="Stable: the detector, fingerprint and values."
    )
    client_id: str
    detector: str = Field(description="The question's code.")
    column: str | None = Field(default=None, description="The column it is about, as written then.")
    fingerprint: str = Field(description="`fingerprint_of` the question.")
    value_signature: tuple[str, ...] = Field(default=(), description="The values it covers.")
    option_label: str = Field(description="The answer chosen, as the person read it.")
    option_id: str
    effect_digest: str | None = Field(default=None, description="The proposal it applied then; null = as is.")
    meaning: str | None = Field(
        default=None,
        description="`meaning_of` the option chosen: applied again only to an option that still means this.",
    )
    title: str = Field(default="", description="What the answer does, in one plain sentence.")
    answered_by: str = Field(description="Who answered (user id).")
    answered_at: AwareDatetime
    used_in: tuple[str, ...] = Field(default=(), description="Cases it was applied in.")
    used_count: int = Field(default=0, ge=0)
    revoked_at: AwareDatetime | None = None
    revoked_by: str | None = None


class StandingAnswers(StrictBase):
    """`cases/<client_id>/standing_answers.json`."""

    client_id: str
    standing_answers: tuple[StandingAnswer, ...] = ()


class StandingAnswerStore:
    """The client's standing answers. One writer at a time within a process; each write is whole-file."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def _read(self, client_id: str) -> StandingAnswers:
        key = standing_key(client_id)
        try:
            if not self._storage.exists(key):
                return StandingAnswers(client_id=client_id)
            return self._storage.read_model(key, StandingAnswers)
        except (StorageError, ValueError):
            return StandingAnswers(client_id=client_id)

    def _write(self, document: StandingAnswers) -> None:
        self._storage.write_model(standing_key(document.client_id), document)

    def list(self, client_id: str, *, include_revoked: bool = True) -> tuple[StandingAnswer, ...]:
        """Every answer of the client, oldest first (Settings shows them, with Revoke)."""
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            return ()
        answers = self._read(client_id).standing_answers
        return answers if include_revoked else tuple(a for a in answers if a.revoked_at is None)

    def every_client(self) -> tuple[StandingAnswer, ...]:
        """Plan O M245 (DEC-1815): every client's answers, by client then oldest first (Settings' one list)."""
        clients = sorted(
            parts[1]
            for key in self._storage.list_keys(CASE_PREFIX)
            if len(parts := key.split("/")) == 3 and parts[2] == STANDING_FILENAME
        )
        return tuple(answer for client in clients for answer in self.list(client))

    def find(self, client_id: str, question: CaseQuestion) -> StandingAnswer | None:
        """The live answer for this question's detector, column and values, if the client has one."""
        fingerprint, signature = fingerprint_of(question), signature_of(question)
        for answer in self.list(client_id, include_revoked=False):
            if (
                answer.detector == question.code
                and answer.fingerprint == fingerprint
                and answer.value_signature == signature
            ):
                return answer
        return None

    def remember(
        self,
        client_id: str,
        question: CaseQuestion,
        *,
        option_id: str,
        effect_digest: str | None,
        meaning: str,
        title: str,
        actor: str,
        at: datetime,
    ) -> StandingAnswer:
        """Write the answer down (replacing a live one for the same detector, column and values)."""
        label = next((o.label for o in question.options if o.option_id == option_id), option_id)
        fingerprint, signature = fingerprint_of(question), signature_of(question)
        answer = StandingAnswer(
            answer_id=answer_id_for(client_id, question),
            client_id=client_id,
            detector=question.code,
            column=question.column,
            fingerprint=fingerprint,
            value_signature=signature,
            option_label=label,
            option_id=option_id,
            effect_digest=effect_digest,
            meaning=meaning,
            title=title,
            answered_by=actor,
            answered_at=at,
        )
        with _LOCK:
            document = self._read(client_id)
            kept = tuple(a for a in document.standing_answers if a.answer_id != answer.answer_id)
            self._write(document.model_copy(update={"standing_answers": (*kept, answer)}))
        return answer

    def used(self, client_id: str, answer_id: str, case_id: str) -> None:
        """Count a use in one case, once per case (so a retried write counts it once)."""
        with _LOCK:
            document = self._read(client_id)
            answers = list(document.standing_answers)
            for index, answer in enumerate(answers):
                if answer.answer_id == answer_id and case_id not in answer.used_in:
                    used_in = (*answer.used_in, case_id)
                    answers[index] = answer.model_copy(
                        update={"used_in": used_in, "used_count": len(used_in)}
                    )
                    self._write(document.model_copy(update={"standing_answers": tuple(answers)}))
                    return

    def revoke(self, client_id: str, answer_id: str, *, actor: str, at: datetime) -> StandingAnswer:
        """Revoke an answer: it is asked again next time. `STANDING_ANSWER_NOT_FOUND` when there is none."""
        from engine.doctor.protocol import ProtocolError

        with _LOCK:
            document = self._read(client_id)
            answers = list(document.standing_answers)
            for index, answer in enumerate(answers):
                if answer.answer_id == answer_id:
                    if answer.revoked_at is not None:
                        return answer
                    answers[index] = answer.model_copy(update={"revoked_at": at, "revoked_by": actor})
                    self._write(document.model_copy(update={"standing_answers": tuple(answers)}))
                    return answers[index]
        raise ProtocolError(
            "STANDING_ANSWER_NOT_FOUND", "There is no such remembered answer for this client.", status=404
        )

    def get(self, client_id: str, answer_id: str) -> StandingAnswer | None:
        return next((a for a in self.list(client_id) if a.answer_id == answer_id), None)


def describe(answer: StandingAnswer, *, at: datetime | None = None) -> str:
    """ "Using your answer from 12 Oct: 'nahi' means no." """
    when = (at or answer.answered_at).strftime("%d %b").lstrip("0")
    what = answer.title or answer.option_label
    return f"Using your answer from {when}: {what}"
