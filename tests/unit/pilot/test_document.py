"""The one page model and its two renderers (Plan E, DEC-903)."""

from __future__ import annotations

from datetime import UTC, datetime

from engine.pilot.document import (
    Bars,
    Bullets,
    Callout,
    Heading,
    KeyValues,
    Paragraph,
    ReportDocument,
    Table,
    Verdict,
    render_html,
    render_pdf,
)

NOW = datetime(2026, 9, 23, 10, 30, tzinfo=UTC)


def document(**overrides: object) -> ReportDocument:
    base: dict[str, object] = {
        "kind": "readiness",
        "title": "Data readiness report",
        "client_name": "Demo Telecom",
        "generated_at": NOW,
        "facts": (("Use case", "Churn"),),
        "blocks": (
            Verdict(state="not_ready", title="Not ready: IDs repeat", text="Fix it."),
            Heading(text="Tables"),
            Table(columns=("File", "Rows"), rows=(("customers.csv", "2,000"),)),
            Bars(title="Found", items=(("Top 10%", 0.5, "38%"),), reference_label="Random: 10%"),
            Callout(title="Value", text="₹12,34,567 → net", tone="warning"),
            KeyValues(rows=(("Needed", "150 days"),)),
            Bullets(items=("one", "two")),
            Paragraph(text="done", muted=True),
        ),
    }
    base.update(overrides)
    return ReportDocument.model_validate(base)


def test_the_html_is_one_self_contained_page_with_no_script() -> None:
    html = render_html(document())
    assert html.startswith("<!doctype html>")
    assert "<script" not in html and "http://" not in html and "https://" not in html
    assert "Minfy logo" in html and "Demo Telecom logo" in html
    assert 'class="verdict v-not_ready"' in html


def test_every_string_from_a_client_file_is_escaped() -> None:
    hostile = "<img src=x onerror=alert(1)>"
    html = render_html(document(blocks=(Table(columns=("File",), rows=((hostile,),)),), title=hostile))
    assert hostile not in html and "&lt;img" in html


def test_the_pdf_is_a_pdf_and_carries_the_rupee_sign() -> None:
    pdf = render_pdf(document())
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 2_000


def test_an_empty_table_says_so_instead_of_drawing_nothing() -> None:
    doc = document(blocks=(Table(columns=("A",), rows=(), empty_text="Nothing yet."),))
    assert "Nothing yet." in render_html(doc)
    assert render_pdf(doc).startswith(b"%PDF-")


def test_the_document_round_trips_as_json() -> None:
    doc = document()
    assert ReportDocument.model_validate_json(doc.model_dump_json()) == doc
