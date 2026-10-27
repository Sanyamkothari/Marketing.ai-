"""WhatsApp number health: quality rating, messaging-limit tier, and what Meta's error codes mean.

Three things a WhatsApp sender must respect that email does not have (docs/v4/WHATSAPP.md):

- **Quality rating** of the phone number (GREEN, YELLOW, RED), from `GET /{phone_number_id}` and the
  `phone_number_quality_update` webhook. YELLOW halves the weekly WhatsApp budget (messages per
  customer per week); RED pauses WhatsApp for the shop and raises an alert. A rating we never heard
  about is GREEN: nothing is cut on a guess.
- **Messaging-limit tier** (TIER_250, TIER_1K, ...): kept so the dashboard and the sender can see it;
  Meta enforces the tier itself.
- **Error codes** that mean "this person, not our number": 131049 (Meta withheld a marketing
  message to keep the person's inbox healthy) and 131026 (the person cannot receive WhatsApp). They
  are recorded as "not delivered", the customer is not retried on WhatsApp for a while, and they do
  not count toward the circuit breakers' delivery rate (`breaker_ignore`). 131047 (a free-form
  message outside the 24-hour window) is our own mistake and does count.

The numbers and the code lists are in `configs/v4/whatsapp.yaml`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import yaml

from autonomy.contracts import Alert, stable_id
from autonomy.contracts.events import AttrValue

DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "v4" / "whatsapp.yaml"

GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"
RATINGS: tuple[str, ...] = (GREEN, YELLOW, RED)
QUALITY_ALERT_CODE = "whatsapp.quality_red"

BREAKER_IGNORE_ATTR = "breaker_ignore"
"""Set (True) on a FAIL event that must not count toward a circuit breaker's delivery rate."""
HOLD_UNTIL_ATTR = "hold_until"
"""ISO time on a FAIL event until which the customer is not retried on WhatsApp."""
NOT_DELIVERED_ATTR = "not_delivered"
"""The plain-English label of a failure Meta's error code explains, e.g. "not delivered: Meta limit"."""

_CODE_IN_TEXT = re.compile(r"\(#(\d+)\)")


@dataclass(frozen=True)
class ErrorRule:
    """What one family of Meta error codes means for us."""

    name: str
    codes: frozenset[int]
    label: str
    counts_as_failure: bool
    hold: timedelta


@dataclass(frozen=True)
class WhatsAppLimits:
    """The settings of `configs/v4/whatsapp.yaml`."""

    yellow_budget_factor: float = 0.5
    red_pauses: bool = True
    errors: tuple[ErrorRule, ...] = ()
    usable_categories: frozenset[str] = frozenset({"MARKETING", "UTILITY"})
    usable_status: str = "APPROVED"

    def rule_for(self, code: int) -> ErrorRule | None:
        return next((r for r in self.errors if code in r.codes), None)


def parse_limits(raw: Mapping[str, object]) -> WhatsAppLimits:
    """`WhatsAppLimits` from the parsed YAML mapping."""
    quality = raw.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    errors_raw = raw.get("errors")
    errors: list[ErrorRule] = []
    if isinstance(errors_raw, Mapping):
        for name, spec in errors_raw.items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"whatsapp.yaml: errors.{name} must be a mapping")
            errors.append(
                ErrorRule(
                    name=str(name),
                    codes=frozenset(int(c) for c in spec.get("codes", [])),
                    label=str(spec.get("label") or name),
                    counts_as_failure=bool(spec.get("counts_as_failure", True)),
                    hold=timedelta(hours=float(spec.get("hold_hours", 0))),
                )
            )
    templates = raw.get("templates")
    templates = templates if isinstance(templates, Mapping) else {}
    factor = float(quality.get("yellow_budget_factor", 0.5))
    if not 0.0 < factor <= 1.0:
        raise ValueError("whatsapp.yaml: quality.yellow_budget_factor must be in (0, 1]")
    return WhatsAppLimits(
        yellow_budget_factor=factor,
        red_pauses=bool(quality.get("red_pauses", True)),
        errors=tuple(errors),
        usable_categories=frozenset(
            str(c).upper() for c in templates.get("usable_categories", ("MARKETING", "UTILITY"))
        ),
        usable_status=str(templates.get("usable_status", "APPROVED")).upper(),
    )


def load_limits(path: Path | None = None) -> WhatsAppLimits:
    """The limits in `path` (default: the shipped `configs/v4/whatsapp.yaml`)."""
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("whatsapp.yaml must hold a mapping")
    return parse_limits(raw)


def error_code_in(text: str) -> int | None:
    """The Meta error code in an adapter error string such as "(#131049) message not delivered"."""
    found = _CODE_IN_TEXT.search(text)
    return int(found.group(1)) if found else None


def failure_attrs(limits: WhatsAppLimits, code: int | None, at: datetime) -> dict[str, AttrValue]:
    """The attrs a FAIL event gets for a Meta error code: its meaning, the hold, the breaker flag.

    Empty for a code we have no rule for (an ordinary failure)."""
    rule = limits.rule_for(code) if code is not None else None
    if rule is None:
        return {}
    attrs: dict[str, AttrValue] = {NOT_DELIVERED_ATTR: rule.label}
    if not rule.counts_as_failure:
        attrs[BREAKER_IGNORE_ATTR] = True
    if rule.hold > timedelta(0):
        attrs[HOLD_UNTIL_ATTR] = (at + rule.hold).isoformat()
    return attrs


@dataclass
class WhatsAppHealth:
    """The number's current quality rating and tier, and the budget and pause they imply.

    Thread-safe: webhooks arrive on request threads while the dispatcher reads it.
    """

    limits: WhatsAppLimits = field(default_factory=load_limits)
    rating: str = GREEN
    tier: str | None = None
    updated_at: datetime | None = None
    _alerts: list[Alert] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, rating: str | None, tier: str | None, at: datetime) -> Alert | None:
        """Record a report from Meta; returns the alert it raised, if the number just turned RED.

        An older report than the one we hold is ignored. A report with no rating keeps the rating."""
        with self._lock:
            if self.updated_at is not None and at < self.updated_at:
                return None
            if tier:
                self.tier = tier
            self.updated_at = at
            new = rating.upper() if rating else None
            if new not in RATINGS:
                return None
            before, self.rating = self.rating, new
            if new == RED and before != RED:
                alert = Alert(
                    alert_id=stable_id("alert", QUALITY_ALERT_CODE, at.isoformat()),
                    at=at,
                    severity="critical",
                    code=QUALITY_ALERT_CODE,
                    message=(
                        "WhatsApp rated our number's quality RED"
                        + (" and the number is paused for sending." if self.limits.red_pauses else ".")
                        + " Fix the cause (blocks and reports) and resume it by hand once it recovers."
                    ),
                    auto_paused=self.limits.red_pauses,
                )
                self._alerts.append(alert)
                return alert
            return None

    def drain_alerts(self) -> list[Alert]:
        """The alerts raised since the last call (for the runtime's tick to hand to the inbox)."""
        with self._lock:
            out, self._alerts = self._alerts, []
            return out

    @property
    def paused(self) -> bool:
        """True while the number is RED and the limits say RED pauses WhatsApp."""
        return self.rating == RED and self.limits.red_pauses

    def weekly_cap(self, base: int) -> int:
        """The weekly WhatsApp messages per customer allowed, given the shop's cap `base`: halved on
        YELLOW (never below one), unchanged on GREEN, zero while paused."""
        if self.paused:
            return 0
        if self.rating == YELLOW:
            return max(1, int(base * self.limits.yellow_budget_factor))
        return base

    def reset(self) -> None:
        """A person resumed WhatsApp after fixing the cause: forget the RED rating."""
        with self._lock:
            self.rating = GREEN

    def describe(self) -> Mapping[str, str | None]:
        return {"rating": self.rating, "tier": self.tier}
