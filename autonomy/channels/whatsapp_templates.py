"""WhatsApp message templates: what Meta has approved for the business account, and what we may send.

A business may open a WhatsApp conversation only with a template Meta approved, and only
MARKETING and UTILITY templates are ours to send (AUTHENTICATION is for one-time codes). The
Cloud API lists them at `GET /{waba_id}/message_templates` with name, language, category, status
and components. `parse_templates` reads one page of that list, `TemplateCatalogue` holds the result
and answers "may this template be sent?", and `drafts` turns approved templates into DRAFT content
items for the Critic to review before anything is sent with them (a template Meta approved is not
thereby on-brand or compliant).

A template that Meta later rejects, pauses or disables (webhook `message_template_status_update`)
is updated in the catalogue at once, so a send naming it is refused before dispatch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import Field

from autonomy.contracts import Channel, ContentItem, ContentStatus, Contract, Theme, stable_id

__all__ = [
    "USABLE_CATEGORIES",
    "TemplateCatalogue",
    "TemplateError",
    "WhatsAppTemplate",
    "parse_templates",
]

USABLE_CATEGORIES: frozenset[str] = frozenset({"MARKETING", "UTILITY"})
USABLE_STATUS = "APPROVED"
"""The defaults; `configs/v4/whatsapp.yaml` (`templates`) sets the ones a catalogue uses."""

_PARAM = re.compile(r"\{\{\s*(\d+)\s*\}\}")


class TemplateError(ValueError):
    """A template list response that is not what the Cloud API documents."""


class WhatsAppTemplate(Contract):
    """One message template as Meta lists it."""

    name: str
    language: str
    category: str = Field(description="MARKETING, UTILITY or AUTHENTICATION")
    status: str = Field(description="APPROVED, PENDING, REJECTED, PAUSED, DISABLED, ...")
    components: tuple[dict[str, Any], ...] = ()
    template_id: str | None = None

    @property
    def usable(self) -> bool:
        """Approved, and of a category we may send (the default rules)."""
        return not self.refusal()

    @property
    def body(self) -> str:
        """The BODY component's text, with Meta's `{{1}}` placeholders; empty if it has none."""
        for component in self.components:
            if str(component.get("type", "")).upper() == "BODY":
                return str(component.get("text") or "")
        return ""

    @property
    def parameter_count(self) -> int:
        """How many body parameters the template takes (its highest `{{n}}`)."""
        return max((int(n) for n in _PARAM.findall(self.body)), default=0)

    def refusal(self, categories: frozenset[str] = USABLE_CATEGORIES, status: str = USABLE_STATUS) -> str:
        """Why this template may not be sent, or an empty string when it may."""
        if self.status != status:
            return f"template {self.name!r} ({self.language}) is {self.status}, not {status}"
        if self.category not in categories:
            allowed = " and ".join(sorted(categories)) or "no category"
            return f"template {self.name!r} is {self.category}; only {allowed} may be sent"
        return ""


def parse_templates(doc: Any) -> list[WhatsAppTemplate]:
    """The templates on one page of the `message_templates` response; entries without a name are skipped."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("data"), list):
        raise TemplateError("the template list has no `data` array")
    out: list[WhatsAppTemplate] = []
    for row in doc["data"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            continue
        components = row.get("components")
        out.append(
            WhatsAppTemplate(
                name=row["name"],
                language=str(row.get("language") or "en"),
                category=str(row.get("category") or "").upper(),
                status=str(row.get("status") or "").upper(),
                components=(
                    tuple(c for c in components if isinstance(c, dict))
                    if isinstance(components, list)
                    else ()
                ),
                template_id=str(row["id"]) if row.get("id") is not None else None,
            )
        )
    return out


class TemplateCatalogue:
    """The business account's templates by (name, language).

    A catalogue that was never synced (`synced` is False) knows nothing and refuses nothing: the
    adapter then behaves as before and Meta is the only judge. Once synced, a template that is not
    listed is refused too.
    """

    def __init__(
        self,
        templates: Iterable[WhatsAppTemplate] = (),
        *,
        categories: frozenset[str] = USABLE_CATEGORIES,
        status: str = USABLE_STATUS,
    ) -> None:
        """`categories` and `status` are what may be sent (`WhatsAppLimits.usable_categories` and
        `usable_status` from `configs/v4/whatsapp.yaml`)."""
        self.categories = frozenset(c.upper() for c in categories)
        self.status = status.upper()
        self._templates: dict[tuple[str, str], WhatsAppTemplate] = {}
        self.synced_at: datetime | None = None
        self.replace(templates)

    def replace(self, templates: Iterable[WhatsAppTemplate], at: datetime | None = None) -> None:
        """Make this exactly the listed templates (a template Meta deleted drops out)."""
        self._templates = {(t.name, t.language): t for t in templates}
        if at is not None:
            self.synced_at = at

    @property
    def synced(self) -> bool:
        return self.synced_at is not None or bool(self._templates)

    def get(self, name: str, language: str) -> WhatsAppTemplate | None:
        return self._templates.get((name, language))

    def all(self) -> list[WhatsAppTemplate]:
        return sorted(self._templates.values(), key=lambda t: (t.name, t.language))

    def usable(self) -> list[WhatsAppTemplate]:
        return [t for t in self.all() if not t.refusal(self.categories, self.status)]

    def set_status(self, name: str, language: str | None, status: str) -> int:
        """Apply a `message_template_status_update`; returns how many templates changed.

        `language=None` applies to every language of the name."""
        changed = 0
        for key, template in list(self._templates.items()):
            if key[0] == name and (language is None or key[1] == language) and template.status != status:
                self._templates[key] = template.model_copy(update={"status": status})
                changed += 1
        return changed

    def refusal(self, name: str | None, language: str) -> str:
        """Why a send naming this template must be refused, or an empty string when it may go."""
        if not name:
            return "a WhatsApp message needs an approved template_name"
        if not self.synced:
            return ""
        template = self.get(name, language)
        if template is None:
            return f"template {name!r} ({language}) is not in the business account's approved templates"
        return template.refusal(self.categories, self.status)

    def drafts(
        self,
        now: datetime,
        *,
        goal_id: str | None = None,
        theme: Theme = Theme.REMINDER,
        offer_id: str = "none",
    ) -> list[ContentItem]:
        """A DRAFT content item for every usable template, for the Critic and a person to review.

        The body keeps Meta's `{{1}}` placeholders and no slots are named: someone maps each
        parameter to a customer field before the item can be approved and rendered."""
        return [
            ContentItem(
                content_id=stable_id("content", "whatsapp", t.name, t.language),
                goal_id=goal_id,
                channel=Channel.WHATSAPP,
                theme=theme,
                offer_id=offer_id,
                body=t.body or t.name,
                template_name=t.name,
                language=t.language,
                status=ContentStatus.DRAFT,
                critic_notes=(f"synced from Meta: {t.category}",),
                created_at=now,
            )
            for t in self.usable()
        ]
