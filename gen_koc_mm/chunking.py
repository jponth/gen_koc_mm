from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .sections import SECTION_DEFS, SECTION_HEADINGS, SectionChunk
from .transcript import Utterance


def _split_utterances_on_cues(utterances: Sequence[Utterance]) -> list[Utterance]:
    """Split very long utterances when they contain mid-utterance section cues.

    Some transcripts compress multiple agenda transitions into one long line
    (e.g., a Grand Knights report that ends with "... we'll move forward with the Treasurer's report.").

    This pre-pass finds cue matches that occur AFTER the start of the utterance
    and splits into multiple utterances so boundaries can land correctly.
    """

    # Flatten all cue patterns across sections.
    all_pats: list[re.Pattern[str]] = []
    for sec in SECTION_DEFS:
        all_pats.extend(_SECTION_CUES.get(sec.key, []))

    out: list[Utterance] = []
    for u in utterances:
        text = u.text
        # Iteratively split if we find a cue match not at the beginning.
        while True:
            best = None
            for p in all_pats:
                m = p.search(text)
                if m and m.start() > 10:  # avoid splitting on the very first words
                    if best is None or m.start() < best.start():
                        best = m
            if best is None:
                break
            pre = text[: best.start()].strip()
            post = text[best.start() :].strip()
            if pre:
                out.append(Utterance(speaker=u.speaker, text=pre))
            text = post
        if text.strip():
            out.append(Utterance(speaker=u.speaker, text=text.strip()))

    return out


def _canon_key(heading: str) -> str:
    # Back-compat: normalize a heading to a key.
    key = heading.rstrip(":").strip().lower()
    return key.replace("’", "").replace("'", "")


from .cues_config import load_section_cues

# Cue patterns, ordered from strongest to weakest, loaded from section_cues.json.
_SECTION_CUES: dict[str, list[re.Pattern[str]]] = load_section_cues().cues




@dataclass(frozen=True)
class DetectedBoundary:
    idx: int
    heading: str
    strength: int


def _detect_boundaries(utterances: Sequence[Utterance]) -> list[DetectedBoundary]:
    """Return boundaries in transcript order.

    We allow sections to appear out-of-order in the raw transcript. We'll later
    reorder output according to the template.
    """

    # Pre-flatten patterns with strength: earlier pattern in a section is stronger.
    flattened: list[tuple[str, re.Pattern[str], int]] = []
    for sec in SECTION_DEFS:
        pats = _SECTION_CUES.get(sec.key, [])
        for j, p in enumerate(pats):
            strength = 100 - j
            flattened.append((sec.heading, p, strength))

    detected: list[DetectedBoundary] = []
    last_idx = -10
    for i, u in enumerate(utterances):
        txt = u.text.strip()
        if not txt:
            continue

        # Basic de-bounce: don't create multiple boundaries within 1 utterance.
        if i - last_idx < 1:
            continue

        best: Optional[DetectedBoundary] = None
        for heading, pat, strength in flattened:
            if pat.search(txt):
                cand = DetectedBoundary(idx=i, heading=heading, strength=strength)
                if best is None or cand.strength > best.strength:
                    best = cand

        if best is not None:
            detected.append(best)
            last_idx = i

    # Sort by idx (already in order), and drop duplicates that point to same heading within a small window.
    cleaned: list[DetectedBoundary] = []
    last_for_heading: dict[str, int] = {}
    for b in detected:
        prev = last_for_heading.get(b.heading)
        if prev is not None and b.idx - prev < 20:
            continue
        cleaned.append(b)
        last_for_heading[b.heading] = b.idx

    return cleaned


def identify_section_boundaries(utterances: Sequence[Utterance]) -> tuple[list[Utterance], list[DetectedBoundary]]:
    """Identify section boundaries using the current safe-regex cue logic.

    Returns:
      (normalized_utterances, detected_boundaries)

    Notes:
    - This includes the pre-pass split that can break long utterances on mid-line cues.
    - DetectedBoundary.heading is the section *heading* (not key).
    """

    utterances2 = _split_utterances_on_cues(utterances)
    boundaries = _detect_boundaries(utterances2)
    return list(utterances2), boundaries


def chunk_utterances(utterances: Sequence[Utterance]) -> list[SectionChunk]:
    utterances, boundaries = identify_section_boundaries(utterances)

    # Build segments between boundaries.
    # Each segment is labeled by the boundary that starts it.
    segs: list[tuple[str, int, int]] = []  # (heading, start_idx, end_idx_exclusive)
    for k, b in enumerate(boundaries):
        start = b.idx
        end = boundaries[k + 1].idx if (k + 1) < len(boundaries) else len(utterances)
        segs.append((b.heading, start, end))

    # Gather text per section heading.
    text_for: dict[str, list[str]] = {s.heading: [] for s in SECTION_DEFS}
    idx_for: dict[str, tuple[int | None, int | None]] = {s.heading: (None, None) for s in SECTION_DEFS}

    for heading, start, end in segs:
        seg = utterances[start:end]
        seg_text = "\n".join([f"{u.speaker}: {u.text}" for u in seg]).strip() + ("\n" if seg else "")
        if seg_text.strip():
            text_for[heading].append(seg_text)
            cur = idx_for.get(heading)
            if cur == (None, None):
                idx_for[heading] = (start, end - 1)

    chunks: list[SectionChunk] = []
    for sec in SECTION_DEFS:
        combined = "\n".join(text_for[sec.heading]).strip()
        if combined:
            combined += "\n"
        start_idx, end_idx = idx_for[sec.heading]
        chunks.append(SectionChunk(heading=sec.heading, text=combined, start_index=start_idx, end_index=end_idx))

    return chunks


def section_is_absent(section_chunk: SectionChunk) -> bool:
    # In phase 2 (generate JSON from a manually reviewed marked transcript),
    # trust the edited boundaries and treat only truly empty chunks as absent.
    return not section_chunk.text.strip()
