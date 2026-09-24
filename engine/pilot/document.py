"""The one page model every pilot report is drawn from, and its two renderers (Plan E, DEC-903).

A report (the pre-flight check, data readiness, business results, value and ROI) is built once, as
a :class:`ReportDocument` - a title, a few facts about who and when, and a list of blocks - and
drawn twice: :func:`render_html` for the screen and the e-mail, :func:`render_pdf` for the file a
marketing head prints. Drawing both from one model is what keeps them saying the same thing: a
number cannot appear in the PDF and not on the page, because neither renderer computes anything.

Rules the renderers keep:

* **Nothing is computed here.** A block carries text that is already formatted; a renderer only
  lays it out. Every number in a report comes from the module that read the artefact.
* **Everything is escaped.** Column names and file names come from a client's files, so the HTML
  renderer escapes every string it writes and the page carries no script at all.
* **The PDF is Unicode.** The rupee sign, the em dash and the arrows need a Unicode font. The PDF
  uses DejaVu Sans, which matplotlib (pinned in `requirements-freeze.txt`, pulled in by AutoGluon)
  ships; where it is absent the core Helvetica font is used and the few characters it cannot draw
  are spelled out (`₹` becomes `Rs.`), rather than the export failing.
* **Branding is a placeholder.** The Minfy and client logo boxes are named boxes, not images: the
  logos are the client's and Minfy's to supply (plan M61).
"""

from __future__ import annotations

import html
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AnyBlock",
    "Bars",
    "Block",
    "Bullets",
    "Callout",
    "Heading",
    "KeyValues",
    "Paragraph",
    "ReportDocument",
    "Table",
    "Verdict",
    "VerdictState",
    "render_html",
    "render_pdf",
]

VerdictState = Literal["ready", "warnings", "not_ready", "info"]
"""The traffic light: green, amber, red - and a neutral grey for a report that judges nothing."""


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Heading(_Block):
    kind: Literal["heading"] = "heading"
    text: str
    level: Literal[2, 3] = 2


class Paragraph(_Block):
    kind: Literal["paragraph"] = "paragraph"
    text: str
    muted: bool = False


class Verdict(_Block):
    """The traffic-light box at the top of a report: one state, one title, one sentence."""

    kind: Literal["verdict"] = "verdict"
    state: VerdictState
    title: str
    text: str


class KeyValues(_Block):
    kind: Literal["key_values"] = "key_values"
    rows: tuple[tuple[str, str], ...]


class Table(_Block):
    kind: Literal["table"] = "table"
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str = ""
    empty_text: str = "Nothing to show."


class Bullets(_Block):
    kind: Literal["bullets"] = "bullets"
    items: tuple[str, ...]


class Callout(_Block):
    """A boxed note: a problem and its fix, a caveat, what a term means."""

    kind: Literal["callout"] = "callout"
    title: str
    text: str
    tone: Literal["info", "warning", "error", "success"] = "info"


class Bars(_Block):
    """A horizontal bar chart: one labelled bar per item, `share` between 0 and 1 of the widest."""

    kind: Literal["bars"] = "bars"
    title: str
    items: tuple[tuple[str, float, str], ...]
    """(label, share of the longest bar 0..1, the value as text)."""
    reference_label: str = ""
    """Optional sentence under the chart, for example what a random pick would have found."""


AnyBlock = Heading | Paragraph | Verdict | KeyValues | Table | Bullets | Callout | Bars
"""Any one block; what a report builder collects before the document is made."""

Block = Annotated[AnyBlock, Field(discriminator="kind")]


class ReportDocument(BaseModel):
    """One report, laid out but not drawn. Serialised beside the HTML and PDF as `<report>.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    kind: Literal["preflight", "readiness", "results", "roi"]
    title: str
    subtitle: str = ""
    client_name: str = ""
    generated_at: datetime
    facts: tuple[tuple[str, str], ...] = ()
    """Short who/what/when lines under the title (client, use case, data period)."""
    blocks: tuple[Block, ...] = ()
    footer: str = ""


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_STATE_LABEL: Final[dict[str, str]] = {
    "ready": "Ready",
    "warnings": "Ready with warnings",
    "not_ready": "Not ready",
    "info": "For information",
}

_CSS: Final[str] = """
:root { --ink:#1b2430; --muted:#5b6675; --line:#d9dee5; --bg:#ffffff; --panel:#f5f7fa;
  --green:#1f7a4a; --green-bg:#e7f5ec; --amber:#8a5a00; --amber-bg:#fdf3dc; --red:#a3261b;
  --red-bg:#fbe9e7; --blue:#1d4f91; --blue-bg:#e8f0fb; --bar:#2f6fb3; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
.brand { display:flex; justify-content:space-between; gap:12px; margin-bottom:18px; }
.logo { border:1px dashed var(--line); color:var(--muted); font-size:12px; padding:8px 12px;
  border-radius:6px; min-width:120px; text-align:center; }
h1 { font-size:24px; margin:0 0 4px; }
h2 { font-size:18px; margin:28px 0 8px; border-bottom:1px solid var(--line); padding-bottom:4px; }
h3 { font-size:15px; margin:18px 0 6px; }
.subtitle { color:var(--muted); margin:0 0 12px; }
.facts { display:grid; grid-template-columns: max-content 1fr; gap:2px 14px; font-size:13px;
  color:var(--muted); margin: 8px 0 16px; }
.facts dt { font-weight:600; } .facts dd { margin:0; }
.verdict { border-radius:8px; padding:14px 16px; margin:12px 0; border-left:6px solid; }
.verdict .state { font-weight:700; text-transform:uppercase; font-size:12px; letter-spacing:.04em; }
.verdict .title { font-size:17px; font-weight:600; margin:2px 0; }
.v-ready { background:var(--green-bg); border-color:var(--green); }
.v-ready .state { color:var(--green); }
.v-warnings { background:var(--amber-bg); border-color:var(--amber); }
.v-warnings .state { color:var(--amber); }
.v-not_ready { background:var(--red-bg); border-color:var(--red); }
.v-not_ready .state { color:var(--red); }
.v-info { background:var(--panel); border-color:var(--muted); }
p.muted { color:var(--muted); font-size:13px; }
table { width:100%; border-collapse:collapse; margin:8px 0; font-size:13px; }
.table-wrap { overflow-x:auto; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { background:var(--panel); font-weight:600; }
caption { caption-side:bottom; text-align:left; color:var(--muted); font-size:12px; padding-top:4px; }
.kv { display:grid; grid-template-columns: minmax(140px, max-content) 1fr; gap:4px 16px; margin:8px 0; }
.kv dt { color:var(--muted); } .kv dd { margin:0; font-weight:600; }
.callout { border-radius:8px; padding:10px 14px; margin:10px 0; border:1px solid var(--line); }
.callout .ct { font-weight:600; margin-bottom:2px; }
.c-info { background:var(--blue-bg); } .c-warning { background:var(--amber-bg); }
.c-error { background:var(--red-bg); } .c-success { background:var(--green-bg); }
.bars { margin:8px 0; }
.bar-row { display:grid; grid-template-columns: minmax(90px, 180px) 1fr minmax(60px, max-content);
  gap:8px; align-items:center; font-size:13px; margin:3px 0; }
.bar-track { background:var(--panel); height:14px; border-radius:3px; }
.bar-fill { background:var(--bar); height:14px; border-radius:3px; }
footer { margin-top:32px; color:var(--muted); font-size:12px; border-top:1px solid var(--line);
  padding-top:8px; }
@media print { main { padding:0; } .verdict, .callout { break-inside: avoid; } }
"""


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _html_block(block: AnyBlock) -> str:
    if isinstance(block, Heading):
        return f"<h{block.level}>{_e(block.text)}</h{block.level}>"
    if isinstance(block, Paragraph):
        cls = ' class="muted"' if block.muted else ""
        return f"<p{cls}>{_e(block.text)}</p>"
    if isinstance(block, Verdict):
        return (
            f'<div class="verdict v-{block.state}" role="status">'
            f'<div class="state">{_e(_STATE_LABEL[block.state])}</div>'
            f'<div class="title">{_e(block.title)}</div><div>{_e(block.text)}</div></div>'
        )
    if isinstance(block, KeyValues):
        rows = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in block.rows)
        return f'<dl class="kv">{rows}</dl>'
    if isinstance(block, Table):
        if not block.rows:
            return f'<p class="muted">{_e(block.empty_text)}</p>'
        head = "".join(f"<th>{_e(c)}</th>" for c in block.columns)
        body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in block.rows)
        caption = f"<caption>{_e(block.caption)}</caption>" if block.caption else ""
        return (
            f'<div class="table-wrap"><table>{caption}<thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
    if isinstance(block, Bullets):
        return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in block.items) + "</ul>"
    if isinstance(block, Callout):
        return (
            f'<div class="callout c-{block.tone}"><div class="ct">{_e(block.title)}</div>'
            f"<div>{_e(block.text)}</div></div>"
        )
    rows = "".join(
        f'<div class="bar-row"><span>{_e(label)}</span><span class="bar-track">'
        f'<span class="bar-fill" style="display:block;width:{max(0.0, min(1.0, share)) * 100:.1f}%"></span>'
        f"</span><span>{_e(value)}</span></div>"
        for label, share, value in block.items
    )
    note = f'<p class="muted">{_e(block.reference_label)}</p>' if block.reference_label else ""
    return f'<div class="bars"><h3>{_e(block.title)}</h3>{rows}{note}</div>'


def render_html(document: ReportDocument) -> str:
    """The report as one self-contained HTML page: inline style, no script, no external request."""
    facts = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in document.facts)
    blocks = "\n".join(_html_block(block) for block in document.blocks)
    client_logo = f"{document.client_name} logo" if document.client_name else "Client logo"
    subtitle = f'<p class="subtitle">{_e(document.subtitle)}</p>' if document.subtitle else ""
    footer = _e(document.footer) + " " if document.footer else ""
    generated = document.generated_at.strftime("%d %b %Y, %H:%M UTC")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(document.title)}</title><style>{_CSS}</style></head>"
        f'<body><main data-report="{_e(document.kind)}">'
        f'<div class="brand"><div class="logo">Minfy logo</div><div class="logo">{_e(client_logo)}</div></div>'
        f"<h1>{_e(document.title)}</h1>{subtitle}"
        f'<dl class="facts">{facts}</dl>'
        f"{blocks}"
        f"<footer>{footer}Generated {_e(generated)} by Marketing AI from the platform's own records."
        "</footer></main></body></html>\n"
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
_FONT_FAMILY: Final[str] = "DejaVu"
_CORE_FALLBACK: Final[dict[str, str]] = {
    "₹": "Rs.",
    "—": "-",
    "–": "-",
    "→": "->",
    "←": "<-",
    "↑": "up",
    "↓": "down",
    "×": "x",
    "≥": ">=",
    "≤": "<=",
    "…": "...",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "·": "-",
    "•": "-",
    "✓": "OK",
}

_STATE_RGB: Final[dict[str, tuple[int, int, int]]] = {
    "ready": (31, 122, 74),
    "warnings": (138, 90, 0),
    "not_ready": (163, 38, 27),
    "info": (91, 102, 117),
}
_STATE_BG: Final[dict[str, tuple[int, int, int]]] = {
    "ready": (231, 245, 236),
    "warnings": (253, 243, 220),
    "not_ready": (251, 233, 231),
    "info": (245, 247, 250),
}
_TONE_BG: Final[dict[str, tuple[int, int, int]]] = {
    "info": (232, 240, 251),
    "warning": (253, 243, 220),
    "error": (251, 233, 231),
    "success": (231, 245, 236),
}


def _dejavu_dir() -> Path | None:
    """matplotlib's bundled DejaVu fonts, found without importing matplotlib."""
    spec = find_spec("matplotlib")
    if spec is None or not spec.submodule_search_locations:
        return None
    for location in spec.submodule_search_locations:
        candidate = Path(location) / "mpl-data" / "fonts" / "ttf"
        if (candidate / "DejaVuSans.ttf").is_file() and (candidate / "DejaVuSans-Bold.ttf").is_file():
            return candidate
    return None


def render_pdf(document: ReportDocument) -> bytes:
    """The report as an A4 PDF, drawn from the same blocks as :func:`render_html`."""
    from fpdf import FPDF  # type: ignore[import-untyped]  # fpdf2 ships no type information
    from fpdf.enums import XPos, YPos  # type: ignore[import-untyped]

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    fonts = _dejavu_dir()
    if fonts is not None:
        pdf.add_font(_FONT_FAMILY, "", str(fonts / "DejaVuSans.ttf"))
        pdf.add_font(_FONT_FAMILY, "B", str(fonts / "DejaVuSans-Bold.ttf"))
        family = _FONT_FAMILY

        def text(value: str) -> str:
            return value

    else:  # pragma: no cover - matplotlib ships with AutoGluon; this is the documented fallback
        family = "Helvetica"

        def text(value: str) -> str:
            for char, spelled in _CORE_FALLBACK.items():
                value = value.replace(char, spelled)
            return value.encode("latin-1", "replace").decode("latin-1")

    pdf.set_title(text(document.title))
    pdf.set_creator("Marketing AI")
    pdf.set_creation_date(document.generated_at)
    pdf.add_page()
    width = pdf.epw

    def line(
        size: float, value: str, *, bold: bool = False, rgb: tuple[int, int, int] = (27, 36, 48)
    ) -> None:
        pdf.set_font(family, "B" if bold else "", size)
        pdf.set_text_color(*rgb)
        pdf.multi_cell(width, size * 0.5, text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Branding placeholders.
    pdf.set_draw_color(180, 186, 194)
    pdf.set_font(family, "", 8)
    pdf.set_text_color(91, 102, 117)
    client_logo = f"{document.client_name} logo" if document.client_name else "Client logo"
    pdf.cell(45, 9, text("Minfy logo"), border=1, align="C")
    pdf.set_x(pdf.l_margin + width - 45)
    pdf.cell(45, 9, text(client_logo), border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)
    line(18, document.title, bold=True)
    if document.subtitle:
        line(10, document.subtitle, rgb=(91, 102, 117))
    pdf.ln(1)
    for key, value in document.facts:
        pdf.set_font(family, "B", 9)
        pdf.set_text_color(91, 102, 117)
        pdf.cell(38, 5, text(key))
        pdf.set_font(family, "", 9)
        pdf.multi_cell(width - 38, 5, text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    for block in document.blocks:
        if isinstance(block, Heading):
            pdf.ln(3 if block.level == 2 else 1)
            line(13 if block.level == 2 else 11, block.text, bold=True)
            if block.level == 2:
                pdf.set_draw_color(217, 222, 229)
                pdf.line(pdf.l_margin, pdf.get_y() + 0.5, pdf.l_margin + width, pdf.get_y() + 0.5)
                pdf.ln(2)
        elif isinstance(block, Paragraph):
            line(8.5 if block.muted else 10, block.text, rgb=(91, 102, 117) if block.muted else (27, 36, 48))
            pdf.ln(1)
        elif isinstance(block, Verdict | Callout):
            if isinstance(block, Verdict):
                bg, label, title, body = (
                    _STATE_BG[block.state],
                    _STATE_LABEL[block.state].upper(),
                    block.title,
                    block.text,
                )
                accent = _STATE_RGB[block.state]
            else:
                bg, label, title, body = _TONE_BG[block.tone], "", block.title, block.text
                accent = (27, 36, 48)
            pdf.set_fill_color(*bg)
            pdf.set_text_color(*accent)
            if label:
                pdf.set_font(family, "B", 8)
                pdf.multi_cell(width, 5, text(label), fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(family, "B", 10.5)
            pdf.set_text_color(27, 36, 48)
            pdf.multi_cell(width, 5.5, text(title), fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(family, "", 9.5)
            pdf.multi_cell(width, 5, text(body), fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_fill_color(255, 255, 255)
            pdf.ln(2)
        elif isinstance(block, KeyValues):
            for key, value in block.rows:
                pdf.set_font(family, "", 9.5)
                pdf.set_text_color(91, 102, 117)
                pdf.cell(60, 5.5, text(key))
                pdf.set_font(family, "B", 9.5)
                pdf.set_text_color(27, 36, 48)
                pdf.multi_cell(width - 60, 5.5, text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        elif isinstance(block, Table):
            if not block.rows:
                line(9, block.empty_text, rgb=(91, 102, 117))
                continue
            pdf.set_font(family, "", 8.5)
            pdf.set_text_color(27, 36, 48)
            with pdf.table(
                text_align="LEFT",
                line_height=4.6,
                headings_style=_heading_style(family),
                borders_layout="HORIZONTAL_LINES",
            ) as table:
                header = table.row()
                for column in block.columns:
                    header.cell(text(column))
                for row in block.rows:
                    cells = table.row()
                    for value in row:
                        cells.cell(text(value))
            if block.caption:
                line(8, block.caption, rgb=(91, 102, 117))
            pdf.ln(2)
        elif isinstance(block, Bullets):
            pdf.set_font(family, "", 10)
            pdf.set_text_color(27, 36, 48)
            for item in block.items:
                pdf.cell(5, 5.2, text("•") if family == _FONT_FAMILY else "-")
                pdf.multi_cell(width - 5, 5.2, text(item), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        else:  # Bars
            line(10.5, block.title, bold=True)
            label_w, value_w = 42.0, 26.0
            track = width - label_w - value_w - 4
            for label, share, value in block.items:
                y = pdf.get_y()
                pdf.set_font(family, "", 8.5)
                pdf.set_text_color(27, 36, 48)
                pdf.cell(label_w, 5, text(label))
                pdf.set_fill_color(245, 247, 250)
                pdf.rect(pdf.l_margin + label_w, y + 0.8, track, 3.6, style="F")
                pdf.set_fill_color(47, 111, 179)
                filled = max(0.0, min(1.0, share)) * track
                if filled > 0:
                    pdf.rect(pdf.l_margin + label_w, y + 0.8, filled, 3.6, style="F")
                pdf.set_x(pdf.l_margin + label_w + track + 4)
                pdf.cell(value_w, 5, text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if block.reference_label:
                line(8, block.reference_label, rgb=(91, 102, 117))
            pdf.ln(2)

    pdf.ln(4)
    generated = document.generated_at.strftime("%d %b %Y, %H:%M UTC")
    footer = (document.footer + " " if document.footer else "") + (
        f"Generated {generated} by Marketing AI from the platform's own records."
    )
    line(8, footer, rgb=(91, 102, 117))
    return bytes(pdf.output())


def _heading_style(family: str) -> object:
    from fpdf.fonts import FontFace  # type: ignore[import-untyped]

    return FontFace(family=family, emphasis="BOLD", fill_color=(245, 247, 250))
