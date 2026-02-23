from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .sections import SECTION_DEFS, SECTION_HEADINGS, SECTION_KEYS, SectionChunk
from .transcript import Utterance


_MARKER_RE = re.compile(r"^\s*\*\*\s*(.+?)\s*\*\*\s*$")


def _norm_key(s: str) -> str:
    return s.strip().lower().replace("’", "").replace("'", "")


def marker_line_for_key(key: str) -> str:
    """Render a section marker line for the intermediate transcript."""

    return f"** {key} **"


def parse_marked_transcript(text: str) -> list[SectionChunk]:
    """Parse an intermediate transcript file with explicit section markers.

    Marker line format:
        ** <section key> **

    Everything after a marker belongs to that section until the next marker.
    Multiple segments for the same section are concatenated.

    Notes:
    - Keys are normalized to match SECTION_DEFS normalization.
    - Unknown keys raise ValueError (helps catch typos during review).
    """

    key_to_heading = {s.key: s.heading for s in SECTION_DEFS}

    # Accumulate raw lines (we keep speaker prefixes as-is).
    lines_for_heading: dict[str, list[str]] = {h: [] for h in SECTION_HEADINGS}

    current_heading: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            # Keep blank lines only if we're inside a section (helps readability)
            if current_heading is not None:
                lines_for_heading[current_heading].append("")
            continue

        m = _MARKER_RE.match(line)
        if m:
            key = _norm_key(m.group(1))
            if key not in key_to_heading:
                raise ValueError(
                    f"Unknown section key in marker: {m.group(1)!r}. "
                    f"Expected one of: {', '.join(SECTION_KEYS)}"
                )
            current_heading = key_to_heading[key]
            continue

        if current_heading is None:
            # Ignore preamble until the first explicit marker.
            continue

        lines_for_heading[current_heading].append(line)

    chunks: list[SectionChunk] = []
    for sec in SECTION_DEFS:
        combined = "\n".join(lines_for_heading[sec.heading]).strip()
        if combined:
            combined += "\n"
        chunks.append(SectionChunk(heading=sec.heading, text=combined))

    return chunks


@dataclass(frozen=True)
class MarkedBoundary:
    idx: int  # utterance index
    key: str  # normalized section key


def render_marked_transcript(
    *,
    utterances: Sequence[Utterance],
    boundaries: Sequence[MarkedBoundary],
) -> str:
    """Render a clean transcript with section markers inserted.

    Output layout:
      ** <key> **
      Speaker N: ...
      Speaker M: ...

    Boundaries are assumed to be sorted by idx.
    """

    b_by_idx: dict[int, list[str]] = {}
    for b in boundaries:
        b_by_idx.setdefault(b.idx, []).append(b.key)

    out_lines: list[str] = []
    for i, u in enumerate(utterances):
        for key in b_by_idx.get(i, []):
            out_lines.append(marker_line_for_key(key))
        out_lines.append(f"{u.speaker}: {u.text}")

    return "\n".join(out_lines).strip() + "\n"
