"""What a person may set for a goal, read from `configs/outcomes.yaml`, and the questions that ask for it.

A goal's outcome is built from events, and two things about that build are the person's to say: the **window** (the days
after the snapshot that the outcome looks at) and, for a goal about a narrower audience, the **audience** (every
customer active by the snapshot, or only the ones who have gone quiet). Both are data, in the template's `settings`
(`engine.doctor.outcomes.TemplateSettings`): the values allowed, the default, whether the person is asked, which
audience the goal is for and why. Nothing here reads the goal's description for a hint.

* :func:`load_goal_settings` gives a goal's :class:`GoalSettings`: `allowed`, `default`, `options`, `recommended`,
  `dont_know`, and `value`, the setting as the case decided it (`engine.doctor.decisions`) or its default.
* :func:`open_setting_questions` makes one question for each setting that asks, has no value yet and can be put to the
  person with the facts at hand. It replaces the audience question the visit used to write.

A recommendation is only made when the facts support it: an audience is recommended when its table would reach the
goal's minimums (`validation.min_rows`, `validation.min_positive`). "I don't know" takes the configured safe reading
when that one is large enough, else the widest reading that is; with none large enough it decides nothing.

Only counts, labels from the configuration and the entity's noun are in a question: no cell text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import config_root, load_use_case
from engine.doctor.decisions import decisions
from engine.doctor.outcomes import AudienceOption, OutcomeTemplate, load_outcome_catalogue
from engine.doctor.protocol import DONT_KNOW, Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from pathlib import Path

    from engine.doctor.buildable import BuildableOutcome, BuildAudience
    from engine.doctor.case import Case
    from engine.doctor.decisions import Decisions

__all__ = [
    "GOAL_SETTING",
    "GoalSettings",
    "SettingValue",
    "load_goal_settings",
    "open_setting_questions",
]

GOAL_SETTING: Final[str] = "GOAL_SETTING"
"""The code of the question that asks for a goal's setting; its answer is a `goal_setting` item."""

SettingValue = int | str
"""A setting's value: the days of a window, or the id of an audience."""

_WINDOW: Final[str] = "window_days"
_AUDIENCE: Final[str] = "audience"
_LEAD: Final[dict[str, str]] = {"active": "All", "quiet": "Only"}
"""What starts a counted audience: everyone known by the snapshot, or only those who have gone quiet."""


def _plural(noun: str) -> str:
    return noun if noun.endswith("s") else f"{noun}s"


@dataclass(frozen=True)
class SettingOption:
    """One value a setting may take, as the person reads it."""

    value: SettingValue
    label: str
    description: str = ""


@dataclass(frozen=True)
class GoalSettings:
    """A goal's settings: what is allowed, the defaults, what is recommended and what the minimums are."""

    goal_id: str
    template: OutcomeTemplate | None
    entity: str
    minimums: tuple[int, int]
    """`(min_rows, min_positive)`: read from the use case, never repeated in the outcomes file."""

    @property
    def names(self) -> tuple[str, ...]:
        """The settings this goal has: none for a goal with no outcome sentence."""
        if self.template is None:
            return ()
        return (_WINDOW, *((_AUDIENCE,) if self.template.audience is not None else ()))

    def _template(self, name: str) -> OutcomeTemplate:
        if self.template is None or name not in self.names:
            raise KeyError(f"{self.goal_id} has no setting {name!r}")
        return self.template

    def asks(self, name: str) -> bool:
        """The person is asked for this setting before the build is proposed."""
        template = self._template(name)
        if name == _WINDOW:
            return template.settings.window_days.ask
        assert template.audience is not None
        return template.audience.ask

    def allowed(self, name: str) -> tuple[SettingValue, ...]:
        template = self._template(name)
        if name == _WINDOW:
            return template.settings.window_days.allowed
        assert template.audience is not None
        return tuple(option.id for option in template.audience.options)

    def default(self, name: str) -> SettingValue:
        """The value used when nobody decided one: the window's default, or the audience the goal says "I don't know"
        takes (the first reading when it says none)."""
        template = self._template(name)
        if name == _WINDOW:
            return template.settings.window_days.default
        assert template.audience is not None
        return template.audience.dont_know or template.audience.options[0].id

    def options(self, name: str) -> tuple[SettingOption, ...]:
        template = self._template(name)
        if name == _WINDOW:
            return tuple(
                SettingOption(days, f"{days} days") for days in template.settings.window_days.allowed
            )
        assert template.audience is not None
        return tuple(
            SettingOption(o.id, o.phrase(_plural(self.entity)), o.description)
            for o in template.audience.options
        )

    def recommended(self, name: str, facts: BuildableOutcome | None) -> SettingValue | None:
        """The value the goal recommends, only when the facts support it: the default window, or the audience the
        configuration names when its table reaches the minimums. None otherwise."""
        template = self._template(name)
        if name == _WINDOW:
            return template.settings.window_days.default
        assert template.audience is not None
        wanted = template.audience.recommended
        counted = _counted(facts, wanted)
        return wanted if counted is not None and counted.enough else None

    def dont_know(self, name: str, facts: BuildableOutcome | None) -> SettingValue | None:
        """What "I don't know" takes: the window's default, or the audience the goal says is the safe choice when it
        is large enough, else the widest reading that is. None when no reading reaches the minimums."""
        template = self._template(name)
        if name == _WINDOW:
            return template.settings.window_days.default
        assert template.audience is not None
        safe = _counted(facts, template.audience.dont_know)
        if safe is not None and safe.enough:
            return safe.audience_id
        enough = [a for a in (facts.audiences if facts is not None else ()) if a.enough]
        return max(enough, key=lambda a: a.customers).audience_id if enough else None

    def value(self, name: str, decided: Decisions) -> SettingValue | None:
        """The setting as the case has it: what the plan or the person decided (when it is still allowed), else the
        default. None for a setting that asks and has no allowed decision yet: the person has not said."""
        found = decided.setting(self.goal_id, name)
        if found is not None and found.value in self.allowed(name):
            return found.value
        return None if self.asks(name) else self.default(name)

    def values(self, decided: Decisions) -> dict[str, SettingValue | None]:
        return {name: self.value(name, decided) for name in self.names}

    def digest(self, decided: Decisions) -> str:
        """What the settings are now, for a build to remember: a changed window or audience changes it, and the
        build it made is then out of date."""
        return sha256_hex(canonical_bytes(sorted((k, str(v)) for k, v in self.values(decided).items())))[:16]


def _counted(facts: BuildableOutcome | None, audience_id: str | None) -> BuildAudience | None:
    if facts is None or audience_id is None:
        return None
    return next((a for a in facts.audiences if a.audience_id == audience_id), None)


def load_goal_settings(goal_id: str, root: Path | None) -> GoalSettings:
    """The settings of `goal_id` from `<root>/outcomes.yaml` and its use case. A goal with no outcome sentence has
    none. An unknown goal is the use-case loader's error."""
    config = load_use_case(goal_id, root)
    template = load_outcome_catalogue(config_root(root)).templates.get(goal_id)
    return GoalSettings(
        goal_id=goal_id,
        template=template,
        entity=config.entity or "customer",
        minimums=(config.validation.min_rows, config.validation.min_positive),
    )


def _effect(goal: GoalSettings, name: str, value: SettingValue, option_id: str, title: str) -> OptionEffect:
    return OptionEffect(
        option_id=option_id,
        kind="goal_setting",
        title=title,
        payload={"goal_id": goal.goal_id, "setting": name, "value": value},
    )


def _window_question(goal: GoalSettings) -> Ask:
    assert goal.template is not None
    window = goal.template.settings.window_days
    options = tuple(
        AskOption(
            label=f"{days} days",
            effect=_effect(
                goal, _WINDOW, days, str(days), f"The outcome looks {days} days after the snapshot"
            ),
            says=f"The outcome is read from the {days} days after the snapshot.",
        )
        for days in window.allowed
    )
    safe = AskOption(
        label=DONT_KNOW,
        effect=_effect(
            goal,
            _WINDOW,
            window.default,
            "dont_know",
            f"The outcome looks {window.default} days after the snapshot",
        ),
        safe=True,
        says=f"Takes {window.default} days.",
    )
    return Ask(
        code=GOAL_SETTING,
        subject={"goal": goal.goal_id, "setting": _WINDOW},
        text="How many days after the snapshot should the outcome look?",
        why=window.why,
        options=(*options, safe),
        rows=0,
        goal_id=goal.goal_id,
        blocking=True,
        recommended=str(window.default),
    )


def _audience_label(option: AudienceOption, counted: BuildAudience, noun: str) -> str:
    who = option.phrase(f"{counted.customers:,} {_plural(noun)}")
    short = "" if counted.enough else ": too few to learn from"
    return f"{_LEAD[option.id]} {who} ({counted.yes:,} of them a yes){short}"


def _audience_question(goal: GoalSettings, facts: BuildableOutcome) -> Ask | None:
    """Which customers the built table is about, with both readings' counts. None while the facts do not count both."""
    assert goal.template is not None and goal.template.audience is not None
    audience = goal.template.audience
    counted = {a.audience_id: a for a in facts.audiences}
    if set(counted) != {o.id for o in audience.options}:
        return None
    options = tuple(
        AskOption(
            label=_audience_label(option, counted[option.id], goal.entity),
            effect=_effect(goal, _AUDIENCE, option.id, option.id, option.phrase(_plural(goal.entity))),
            says=option.description,
        )
        for option in audience.options
    )
    recommended = goal.recommended(_AUDIENCE, facts)
    fallback = goal.dont_know(_AUDIENCE, facts)
    chosen = next((o for o in audience.options if o.id == fallback), None)
    safe_why = (
        f"Not sure? Data Doctor takes the {chosen.phrase(_plural(goal.entity))}"
        + ("." if fallback == recommended else ": it is the widest reading that is large enough.")
        if chosen is not None
        else ""
    )
    safe = AskOption(
        label=DONT_KNOW,
        effect=(
            _effect(goal, _AUDIENCE, fallback, "dont_know", chosen.phrase(_plural(goal.entity)))
            if fallback is not None and chosen is not None
            else OptionEffect(option_id="dont_know")
        ),
        safe=True,
        says=f"Takes {fallback}." if fallback is not None else "",
    )
    why = " ".join(part for part in (audience.because if recommended is not None else "", safe_why) if part)
    return Ask(
        code=GOAL_SETTING,
        subject={"goal": goal.goal_id, "setting": _AUDIENCE},
        text=f"Which {_plural(goal.entity)} should the training table be built from?",
        why=why,
        options=(*options, safe),
        rows=0,
        upload_id=facts.upload_id,
        goal_id=goal.goal_id,
        blocking=True,
        recommended=str(recommended) if recommended is not None else None,
    )


def open_setting_questions(goal: GoalSettings, case: Case, facts: BuildableOutcome | None) -> list[Ask]:
    """One question for each setting of `goal` that asks and has no value yet: its allowed values as options (each
    adds a `goal_setting` item when the person answers), "I don't know" taking `dont_know`'s value, and the
    recommendation with its reason when the facts support one. The audience is asked only once `facts` count both
    readings. A setting the case already has a value for (the person's, one in the plan, or an older audience answer)
    is not asked again."""
    decided = decisions(case)
    asked: list[Ask] = []
    for name in goal.names:
        if not goal.asks(name) or goal.value(name, decided) is not None:
            continue
        question = (
            _window_question(goal)
            if name == _WINDOW
            else (_audience_question(goal, facts) if facts is not None else None)
        )
        if question is not None:
            asked.append(question)
    return asked
