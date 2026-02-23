from __future__ import annotations

from dataclasses import dataclass

from .sections_config import SectionDef, load_sections_config


# Canonical section order for KoC council meeting minutes.
# Headings are for output; keys are for cue lookup.
SECTION_DEFS: list[SectionDef] = load_sections_config().sections
SECTION_HEADINGS: list[str] = [s.heading for s in SECTION_DEFS]
SECTION_KEYS: list[str] = [s.key for s in SECTION_DEFS]


@dataclass(frozen=True)
class SectionChunk:
    heading: str
    text: str  # normalized transcript text belonging to this section (may be empty)
    start_index: int | None = None  # utterance index (debug)
    end_index: int | None = None
