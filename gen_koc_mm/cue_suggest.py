from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import json
import re
from datetime import datetime

from .sections import SECTION_DEFS
from .transcript import Utterance, parse_transcript
from .cues_config import load_section_cues

_SECTION_CUES = load_section_cues().cues


@dataclass(frozen=True)
class CueHit:
    file: str
    section_heading: str
    pattern: str
    utterance_index: int
    speaker: str
    text: str


def iter_transcript_files(input_path: Path, glob: str = "*.txt") -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.glob(glob))


def _canon_key(heading: str) -> str:
    key = heading.rstrip(":").strip().lower()
    return key.replace("’", "").replace("'", "")


def _flatten_existing_patterns() -> list[re.Pattern[str]]:
    pats: list[re.Pattern[str]] = []
    for _, ps in _SECTION_CUES.items():
        pats.extend(ps)
    return pats


def _transition_like_patterns() -> list[re.Pattern[str]]:
    # Strict-ish triggers: either an agenda keyword or an explicit transition verb.
    return [
        re.compile(
            r"\b(?:chaplain'?s\s+report|grand\s+knight(?:s)?\s+report|treasurer'?s\s+report|treasury\s+report|"
            r"insurance\s+agent(?:\s+report)?|district\s+deputy(?:\s+report)?|(?:4th|fourth)\s+degree(?:\s+report)?|"
            r"old\s+business|new\s+business|birthdays|good\s+of\s+the\s+order|closing\s+prayers?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:move\s+on\s+to|moving\s+on\s+to|we'?ll\s+move\s+on\s+to|we'?ll\s+move\s+forward\s+with|"
            r"next\b|start\s+with|start\s+by|with\s+that)\b",
            re.IGNORECASE,
        ),
    ]


def _matches_any(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def _suggest_section_key_and_regex(text: str) -> tuple[Optional[str], Optional[str], str, float]:
    """Best-effort mapping from a transition-like line to a section key + safe regex.

    Returns: (section_key, safe_regex, reason, confidence)
    """
    t = text.lower()

    # Template-based suggestions
    templates: list[tuple[str, list[str], str, float]] = [
        ("chaplains report", ["chaplain"], r"\bchaplain'?s\s+report\b", 0.7),
        ("grand knights report", ["grand knights report"], r"\bgrand\s+knights\s+report\b", 0.85),
        ("grand knights report", ["grand knight report"], r"\bgrand\s+knight\s+report\b", 0.85),
        ("treasurers report", ["treasurer's report"], r"\btreasurer'?s\s+report\b", 0.9),
        ("treasurers report", ["treasury report"], r"\btreasury\s+report\b", 0.9),
        ("insurance agent report", ["insurance agent report"], r"\binsurance\s+agent\s+report\b", 0.85),
        ("insurance agent report", ["insurance agent"], r"\binsurance\s+agent\b", 0.75),
        ("district deputy report", ["district deputy report"], r"\bdistrict\s+deputy\s+report\b", 0.85),
        ("district deputy report", ["district deputy"], r"\bdistrict\s+deputy\b", 0.7),
        ("4th degree report", ["fourth degree report"], r"\b(?:4th|fourth)\s+degree\s+report\b", 0.85),
        ("4th degree report", ["4th degree report"], r"\b(?:4th|fourth)\s+degree\s+report\b", 0.85),
        ("old business", ["old business"], r"\bold\s+business\b", 0.9),
        ("new business", ["new business"], r"\bnew\s+business\b", 0.9),
        ("birthdays", ["birthdays"], r"\bbirthdays\b", 0.9),
        ("good of the order", ["good of the order"], r"\bgood\s+of\s+the\s+order\b", 0.9),
        ("closing prayers", ["closing prayer"], r"\bclosing\s+prayers?\b", 0.9),
        ("closing prayers", ["closing prayers"], r"\bclosing\s+prayers?\b", 0.9),
    ]

    for key, needles, regex, conf in templates:
        if any(n in t for n in needles):
            return key, regex, f"contains {needles[0]!r}", conf

    # Escaped-literal fallback (low confidence)
    escaped = re.escape(text.strip())
    # Loosen whitespace
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    # Make apostrophes flexible
    escaped = escaped.replace("\\'", "'?" )
    escaped = escaped.replace("’", "'?" )
    safe = escaped
    return None, safe, "escaped-literal fallback", 0.3


def build_discover_json(
    *,
    input_path: Path,
    glob: str = "*.txt",
    max_text_chars: int = 500,
    max_candidates_per_file: int = 500,
) -> dict:
    files = iter_transcript_files(input_path, glob=glob)
    if not files:
        raise RuntimeError(f"No files found at {input_path} with glob {glob!r}")

    existing = _flatten_existing_patterns()
    transition_like = _transition_like_patterns()

    # Drop common noisy non-agenda phrases
    noise_blacklist = [
        re.compile(r"\b(opening\s+prayer|pledge\s+of\s+allegiance|prayer\s+for\s+vocations)\b", re.IGNORECASE),
        re.compile(r"\b(reading\s+and\s+approval\s+of\s+last\s+month's\s+minutes|approve\s+the\s+minutes|motion\s+to\s+approve)\b", re.IGNORECASE),
    ]

    # Require at least one explicit agenda keyword OR a transition verb + the word 'report'
    agenda_keyword = re.compile(
        r"\b(?:chaplain|grand\s+knight|treasurer|treasury|insurance\s+agent|district\s+deputy|(?:4th|fourth)\s+degree|"
        r"old\s+business|new\s+business|birthdays|good\s+of\s+the\s+order|closing\s+prayers?)\b",
        re.IGNORECASE,
    )
    transition_verb = re.compile(
        r"\b(?:move\s+on\s+to|moving\s+on\s+to|we'?ll\s+move\s+on\s+to|we'?ll\s+move\s+forward\s+with|next\b|start\s+with|start\s+by|with\s+that)\b",
        re.IGNORECASE,
    )
    report_word = re.compile(r"\breport\b", re.IGNORECASE)

    candidates: list[dict] = []
    for f in files:
        utts = parse_transcript(f.read_text(encoding="utf-8", errors="ignore"))
        added = 0
        for i, u in enumerate(utts):
            txt = u.text.strip()
            if not txt:
                continue
            if not _matches_any(txt, transition_like):
                continue
            if _matches_any(txt, existing):
                continue
            if any(p.search(txt) for p in noise_blacklist):
                continue

            # Tighten: only keep if it strongly looks like an agenda boundary.
            if not (agenda_keyword.search(txt) or (transition_verb.search(txt) and report_word.search(txt))):
                continue

            section_key, safe_regex, reason, conf = _suggest_section_key_and_regex(txt)
            if section_key is None:
                continue

            snippet = txt.replace("\n", " ")
            if len(snippet) > max_text_chars:
                snippet = snippet[: max_text_chars - 1] + "…"

            compiles = True
            try:
                re.compile(safe_regex or "")
            except re.error:
                compiles = False

            candidates.append(
                {
                    "file": f.name,
                    "utterance_index": i,
                    "speaker": u.speaker,
                    "text": snippet,
                    "matched_existing_cue": False,
                    "suggested_section_key": section_key,
                    "suggested_reason": reason,
                    "suggested_confidence": conf,
                    "safe_regex": safe_regex,
                    "safe_regex_strategy": "template" if conf >= 0.7 and section_key else "escaped-literal",
                    "safe_regex_compiles": compiles,
                }
            )
            added += 1
            if added >= max_candidates_per_file:
                break

    return {
        "version": 1,
        "input": {"input_path": str(input_path), "glob": glob},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidates": candidates,
    }


def update_section_cues_json(
    *,
    discover_json_path: Path,
    section_cues_path: Optional[Path] = None,
    min_confidence: float = 0.7,
    dry_run: bool = False,
) -> dict:
    """Update section_cues.json by appending safe_regexes from a discover JSON file.

    Returns a summary dict.
    """

    payload = json.loads(discover_json_path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("discover JSON must contain a 'candidates' array")

    if section_cues_path is None:
        section_cues_path = Path(__file__).with_name("section_cues.json")

    current = json.loads(section_cues_path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError("section_cues.json must be a JSON object")

    added = 0
    skipped = 0
    for c in candidates:
        if not isinstance(c, dict):
            skipped += 1
            continue
        key = c.get("suggested_section_key")
        conf = float(c.get("suggested_confidence") or 0.0)
        safe = c.get("safe_regex")
        if not key or not isinstance(key, str):
            skipped += 1
            continue
        if conf < min_confidence:
            skipped += 1
            continue
        if not safe or not isinstance(safe, str):
            skipped += 1
            continue

        arr = current.get(key)
        if arr is None:
            current[key] = [safe]
            added += 1
            continue
        if not isinstance(arr, list):
            skipped += 1
            continue
        if safe in arr:
            skipped += 1
            continue

        arr.append(safe)
        current[key] = arr
        added += 1

    summary = {
        "section_cues_path": str(section_cues_path),
        "discover_json_path": str(discover_json_path),
        "added": added,
        "skipped": skipped,
        "min_confidence": min_confidence,
        "dry_run": dry_run,
    }

    if not dry_run:
        section_cues_path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return summary
