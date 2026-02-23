from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document


@dataclass(frozen=True)
class MinutesJSON:
    date_of_meeting: str
    sections: dict[str, str]  # section_key -> section_text (markdown)


def load_minutes_json(path: Path) -> MinutesJSON:
    payload = json.loads(path.read_text(encoding="utf-8"))

    date = (payload.get("date_of_meeting") or "").strip()

    sections_map: dict[str, str] = {}
    for s in payload.get("sections") or []:
        key = (s.get("section_key") or "").strip()
        txt = (s.get("section_text") or "").rstrip()
        if key:
            sections_map[key] = txt

    return MinutesJSON(date_of_meeting=date, sections=sections_map)


def _replace_placeholder_in_paragraph(paragraph, placeholder: str, replacement: str) -> bool:
    """Replace placeholder in a paragraph, even when Word has split it across runs.

    Returns True if a replacement was applied.
    """

    if placeholder not in paragraph.text:
        return False

    # Collapse runs to a single string, do replace, then rebuild paragraph.
    new_text = paragraph.text.replace(placeholder, replacement)

    # Clear existing runs
    for r in paragraph.runs[::-1]:
        r._element.getparent().remove(r._element)

    # Rebuild with line breaks
    lines = new_text.splitlines() or [""]
    run = paragraph.add_run(lines[0])
    for ln in lines[1:]:
        run.add_break()
        paragraph.add_run(ln)

    return True


def _iter_all_paragraphs(doc: Document):
    # Body paragraphs
    for p in doc.paragraphs:
        yield p

    # Table cells paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p


def merge_minutes_into_docx(*, minutes_json_path: Path, template_docx_path: Path, output_docx_path: Path) -> dict[str, Any]:
    minutes = load_minutes_json(minutes_json_path)

    doc = Document(str(template_docx_path))

    replaced: list[str] = []

    # Special top-level placeholder (optional)
    date_placeholder = "<<date_of_meeting>>"
    for p in _iter_all_paragraphs(doc):
        if _replace_placeholder_in_paragraph(p, date_placeholder, minutes.date_of_meeting):
            replaced.append(date_placeholder)

    for key, txt in minutes.sections.items():
        ph = f"<<{key}>>"
        for p in _iter_all_paragraphs(doc):
            if _replace_placeholder_in_paragraph(p, ph, txt):
                replaced.append(ph)

    output_docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_docx_path))

    return {
        "output": str(output_docx_path),
        "replaced_placeholders": sorted(set(replaced)),
    }
