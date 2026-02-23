from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional


_TS_PREFIX_RE = re.compile(
    r"^\s*(?:\[\s*)?(\d{1,2}:\d{2}(?::\d{2})?)(?:\s*\])?\s*[-–—]?\s*"
)

# Matches inline speaker formats:
#   Speaker 1: text
#   speaker 2 - text
#   SPEAKER_00: text
#   SPEAKER 00: text
_SPK_INLINE_RE = re.compile(
    r"^\s*(?:(speaker)\s*_?\s*(\d+)|SPEAKER\s*_?\s*(\d+))\s*[:\-–—]\s*(.*)$",
    re.IGNORECASE,
)

# Matches speaker label alone on a line:
#   Speaker 1
#   SPEAKER_00
#   SPEAKER 00
_SPK_ALONE_RE = re.compile(
    r"^\s*(?:speaker\s*_?\s*(\d+)|SPEAKER\s*_?\s*(\d+))\s*$",
    re.IGNORECASE,
)


@dataclass
class Utterance:
    speaker: str  # e.g. "Speaker 1" or "Unknown"
    text: str


def _normalize_speaker(raw_num: int) -> str:
    # If diarization starts at 0, make it 1-based for readability.
    n = raw_num + 1 if raw_num == 0 else raw_num
    return f"Speaker {n}"


def _is_timestamp_line(line: str) -> bool:
    # Whole-line timestamps like 00:00:10 or 1:02:03
    return bool(re.match(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$", line))


def parse_transcript(text: str) -> List[Utterance]:
    """Parse a transcript into utterances.

    Supports two common transcript layouts:

    (A) Inline speaker lines:
        Speaker 1: hello

    (B) Three-line blocks (common in your samples):
        00:00:10
        Speaker 1
        hello

    Also:
    - Strips leading timestamps like [00:01:23] or 00:01:23
    - Normalizes speaker labels to Speaker N
    """

    utterances: List[Utterance] = []
    current_speaker: str = "Unknown"

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Skip file-name-like lines (e.g., "KoC Council Meeting Jan 2026.m4a")
        if line.lower().endswith(".m4a"):
            continue

        # Skip pure timestamps / durations
        if _is_timestamp_line(line):
            continue

        # Strip timestamp prefix (if timestamp and text share a line)
        line = _TS_PREFIX_RE.sub("", line).strip()
        if not line:
            continue

        # Inline speaker format
        m = _SPK_INLINE_RE.match(line)
        if m:
            num1 = m.group(2)
            num2 = m.group(3)
            rest = (m.group(4) or "").strip()
            raw = int(num1 or num2)
            speaker = _normalize_speaker(raw)
            current_speaker = speaker
            if rest:
                utterances.append(Utterance(speaker=speaker, text=rest))
            continue

        # Speaker-alone line; next content belongs to this speaker
        m2 = _SPK_ALONE_RE.match(line)
        if m2:
            raw = int(m2.group(1) or m2.group(2))
            current_speaker = _normalize_speaker(raw)
            continue

        # Regular content line, attach to current speaker
        utterances.append(Utterance(speaker=current_speaker, text=line))

    # Merge consecutive utterances by same speaker
    merged: List[Utterance] = []
    for u in utterances:
        if merged and merged[-1].speaker == u.speaker:
            merged[-1].text = (merged[-1].text + " " + u.text).strip()
        else:
            merged.append(Utterance(speaker=u.speaker, text=u.text))

    return merged


def render_clean_transcript(utterances: Iterable[Utterance]) -> str:
    parts: List[str] = []
    for u in utterances:
        parts.append(f"{u.speaker}: {u.text}")
    return "\n".join(parts).strip() + "\n"
