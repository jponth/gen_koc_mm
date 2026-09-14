from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

from .sections import SECTION_DEFS


@dataclass(frozen=True)
class ParsedMinutesSection:
    source_id: str
    detected_key: str
    heading: str
    minutes_text: str


@dataclass(frozen=True)
class ParsedMinutesDoc:
    meeting_date: str
    sections: list[ParsedMinutesSection]
    ignored_headings: list[str]


_HEADING_ALIASES = {
    "social action committee report": "social_action_report",
}

_TOP_LEVEL_SKIP_HEADINGS = {
    "call to order",
    "wardens report",
    "opening prayer",
    "opening prayer for vocations",
    "pledge of allegiance",
    "reading and approval of minutes",
    "50/50 raffle",
    "next meeting",
    "meeting adjourned at",
}

_BULLET_PREFIXES = ("•", "-", "*")


def _clean_text(text: str) -> str:
    return text.replace("\xa0", " ").strip()


def _norm_heading(text: str) -> str:
    text = _clean_text(text).lower()
    text = text.replace("’", "'")
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" :\t")
    text = text.replace("'", "")
    return text


def _strip_presenter_suffix(text: str) -> str:
    parts = re.split(r"\s+[–-]\s+", _clean_text(text), maxsplit=1)
    return parts[0].strip()


def _heading_map() -> dict[str, str]:
    out = {_norm_heading(sec.heading): sec.key for sec in SECTION_DEFS}
    out.update(_HEADING_ALIASES)
    return out


def _extract_heading_match(text: str) -> tuple[str, str, str] | None:
    raw = _clean_text(text)
    if ":" not in raw:
        return None

    left, _, right = raw.partition(":")
    candidate = _strip_presenter_suffix(left)
    key = _heading_map().get(_norm_heading(candidate))
    if not key:
        return None
    return key, left.strip() + ":", right.strip()


def _extract_top_level_heading(text: str) -> str | None:
    raw = _clean_text(text)
    if ":" not in raw:
        return None
    left, _, _ = raw.partition(":")
    return _norm_heading(_strip_presenter_suffix(left))


def _paragraph_level(paragraph: Paragraph) -> int:
    indent = paragraph.paragraph_format.left_indent
    if indent is not None and indent.pt >= 18:
        return 2
    return 1


def _append_markdown_paragraph(lines: list[str], paragraph: Paragraph, *, text_override: str | None = None) -> None:
    text = _clean_text(text_override if text_override is not None else paragraph.text)
    if not text:
        if lines and lines[-1] != "":
            lines.append("")
        return

    style_name = (paragraph.style.name or "").lower()
    is_list_style = "list" in style_name
    stripped = text.lstrip()
    is_bullet = stripped.startswith(_BULLET_PREFIXES)

    if is_bullet:
        bullet_text = stripped[1:].strip()
        prefix = "  - " if _paragraph_level(paragraph) >= 2 else "- "
        lines.append(prefix + bullet_text)
        return

    if is_list_style and lines and lines[-1].lstrip().startswith("- "):
        lines[-1] = lines[-1] + " " + stripped
        return

    lines.append(stripped)


def _finalize_section(
    sections: list[ParsedMinutesSection],
    counts: dict[str, int],
    *,
    current_key: str | None,
    current_heading: str,
    current_lines: list[str],
) -> None:
    if not current_key:
        return

    body = "\n".join(current_lines).strip()
    if not body:
        return

    counts[current_key] = counts.get(current_key, 0) + 1
    source_id = f"{current_key}#{counts[current_key]}"
    sections.append(
        ParsedMinutesSection(
            source_id=source_id,
            detected_key=current_key,
            heading=current_heading,
            minutes_text=body,
        )
    )


def _extract_meeting_date(lines: list[str]) -> str:
    for line in lines[:12]:
        raw = _clean_text(line)
        if not raw:
            continue
        normalized = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(normalized, fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def parse_minutes_docx(path: Path) -> ParsedMinutesDoc:
    doc = Document(str(path))
    paragraphs = [p for p in doc.paragraphs if _clean_text(p.text)]

    sections: list[ParsedMinutesSection] = []
    ignored_headings: list[str] = []
    counts: dict[str, int] = {}
    current_key: str | None = None
    current_heading = ""
    current_lines: list[str] = []

    for paragraph in paragraphs:
        raw = _clean_text(paragraph.text)
        matched = _extract_heading_match(raw)
        if matched:
            if current_key is not None and matched[0] == current_key:
                _append_markdown_paragraph(current_lines, paragraph)
                continue
            _finalize_section(
                sections,
                counts,
                current_key=current_key,
                current_heading=current_heading,
                current_lines=current_lines,
            )
            current_key, current_heading, remainder = matched
            current_lines = []
            if remainder:
                _append_markdown_paragraph(current_lines, paragraph, text_override=remainder)
            continue

        top_level = _extract_top_level_heading(raw)
        if top_level and top_level in _TOP_LEVEL_SKIP_HEADINGS:
            if current_key is not None:
                _finalize_section(
                    sections,
                    counts,
                    current_key=current_key,
                    current_heading=current_heading,
                    current_lines=current_lines,
                )
                current_key = None
                current_heading = ""
                current_lines = []
            ignored_headings.append(raw)
            continue

        if current_key is None:
            if top_level:
                ignored_headings.append(raw)
            continue

        _append_markdown_paragraph(current_lines, paragraph)

    _finalize_section(
        sections,
        counts,
        current_key=current_key,
        current_heading=current_heading,
        current_lines=current_lines,
    )

    meeting_date = _extract_meeting_date([p.text for p in paragraphs])
    return ParsedMinutesDoc(meeting_date=meeting_date, sections=sections, ignored_headings=ignored_headings)
