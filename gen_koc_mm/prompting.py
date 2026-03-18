from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import importlib.resources as pkg_resources

from .sections import SECTION_DEFS, SECTION_HEADINGS


def minutes_system_prompt() -> str:
    system_prompt = """
            You are an AI Assistant that can generate minutes of a meeting, in Markdown format.

            Here are the constraints:
            1. If you find sub-sections in the transcript, you should generate indented bullet points.
               Bullet points should be only 2 level deep. After two levels, it should be paragraph text.
            2. If there is no substantive content in the transcript, output nothing (empty string).
            3. If there is no substantive content for this section, output nothing (empty string).
            4. Speakers must remain generic: Speaker 1, Speaker 2, ... (do NOT map to real people).
            5. Do NOT include any timestamps anywhere.
            6. Do NOT add any header block.
            7. Output MUST be valid Markdown.
            8. Output MUST be ONLY bullet points (each line starts with '- ').
            9. If there is no substantive content in the transcript, output nothing (empty string). 
            10. If there is no substantive content for this section, output nothing (empty string).
        """
    #print(f"system_prompt: {system_prompt}")

    return system_prompt


@dataclass(frozen=True)
class FewShotExample:
    title: str
    section_key: str  # canonical key from section_headings.json, or "*" for global
    transcript: str
    expected_bullets: str


@dataclass(frozen=True)
class FewShotConfig:
    examples: list[FewShotExample]


def _load_text_from_package(filename: str) -> str:
    return pkg_resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")


def _norm_key(s: str) -> str:
    return s.strip().lower().replace("’", "").replace("'", "")


def _canonical_section_keys() -> set[str]:
    return {s.key for s in SECTION_DEFS}


def _read_json_text() -> tuple[str, Optional[Path]]:
    """Return (raw_json, override_path_if_any).

    If GEN_KOC_MM_FEWSHOT_PATH is set, we read that file from disk.
    Otherwise we read the packaged minutes_fewshot.json.
    """

    override = os.environ.get("GEN_KOC_MM_FEWSHOT_PATH", "").strip()
    if override:
        p = Path(override)
        return p.read_text(encoding="utf-8"), p

    raw = _load_text_from_package("minutes_fewshot.json")
    return raw, None


def load_fewshot_config() -> FewShotConfig:
    """Load few-shot examples for minutes generation.

    Supports two formats:

    v1 (inline text in JSON):
    {
      "version": 1,
      "examples": [
        {
          "title": "...",
          "section_heading": "Grand Knights Report:",
          "transcript": "Speaker 1: ...",
          "expected_bullets": "- ...\n- ...\n"
        }
      ]
    }

    v2 (recommended: index + external files):
    {
      "version": 2,
      "base_dir": "fewshot_examples",
      "examples": [
        {
          "id": "gk_01",
          "title": "...",
          "section_key": "grand knights report",
          "transcript_path": "grand_knights_report/gk_01.transcript.txt",
          "expected_path": "grand_knights_report/gk_01.expected.md"
        }
      ]
    }

    Notes:
    - section_key must match a canonical key from section_headings.json, or "*" for global examples.
    - Incomplete examples are skipped to allow placeholders.
    """

    raw, override_path = _read_json_text()

    data = json.loads(raw)
    if not isinstance(data, dict):
        return FewShotConfig(examples=[])

    version = int(data.get("version") or 1)
    exs = data.get("examples")
    if not exs:
        return FewShotConfig(examples=[])
    if not isinstance(exs, list):
        raise ValueError("minutes_fewshot.json: 'examples' must be a list")

    canonical = _canonical_section_keys()

    out: list[FewShotExample] = []

    # v2: resolve base_dir relative to the JSON file location (override),
    # or relative to the package (local/editable install).
    base_dir = str(data.get("base_dir") or "").strip() if version >= 2 else ""
    base_dir_path: Optional[Path] = None
    base_dir_pkg = None
    if version >= 2:
        if override_path is not None:
            base_dir_path = (override_path.parent / base_dir).resolve() if base_dir else override_path.parent
        else:
            # Local run: treat base_dir as a folder inside the package.
            # importlib.resources gives us a Traversable we can read from.
            base_dir_pkg = pkg_resources.files(__package__).joinpath(base_dir) if base_dir else pkg_resources.files(__package__)

    for e in exs:
        if not isinstance(e, dict):
            continue

        title = str(e.get("title") or "").strip() or "Example"

        if version >= 2:
            sk = str(e.get("section_key") or "").strip()
            sk_norm = "*" if sk.strip() == "*" else _norm_key(sk)
            if sk_norm != "*" and sk_norm not in canonical:
                raise ValueError(
                    f"Few-shot example has unknown section_key={sk!r}. Expected one of: {', '.join(sorted(canonical))} or '*'."
                )

            # Skip placeholders
            tp = str(e.get("transcript_path") or "").strip()
            ep = str(e.get("expected_path") or "").strip()
            if not (tp and ep):
                continue

            try:
                if base_dir_path is not None:
                    transcript = (base_dir_path / tp).read_text(encoding="utf-8").strip()
                    expected = (base_dir_path / ep).read_text(encoding="utf-8").strip()
                else:
                    # Packaged/local mode: read from importlib.resources Traversable
                    if base_dir_pkg is None:
                        raise ValueError("internal error: base_dir_pkg is not set for v2")
                    transcript = base_dir_pkg.joinpath(tp).read_text(encoding="utf-8").strip()
                    expected = base_dir_pkg.joinpath(ep).read_text(encoding="utf-8").strip()
            except FileNotFoundError as fe:
                ex_id = str(e.get("id") or "").strip() or "(missing id)"
                raise ValueError(
                    f"Few-shot example id={ex_id!r} references a missing file: {fe.filename}"
                ) from fe

            if not (transcript and expected):
                continue
            if not expected.endswith("\n"):
                expected += "\n"

            out.append(FewShotExample(title=title, section_key=sk_norm, transcript=transcript, expected_bullets=expected))
            continue

        # v1 (legacy): map section_heading -> section_key by matching heading text.
        sh = str(e.get("section_heading") or "").strip()
        tr = str(e.get("transcript") or "").strip()
        exp = str(e.get("expected_bullets") or "").strip()
        if not (sh and tr and exp):
            continue

        heading_to_key = {_norm_key(s.heading): s.key for s in SECTION_DEFS}
        key = heading_to_key.get(_norm_key(sh))
        if not key:
            # Skip unknown headings in v1 (lets older placeholders exist)
            continue

        if not exp.endswith("\n"):
            exp += "\n"
        out.append(FewShotExample(title=title, section_key=key, transcript=tr, expected_bullets=exp))

    return FewShotConfig(examples=out)


# Simple module-level cache so we don't re-read JSON for every section.
_FEWSHOT_CACHE: Optional[FewShotConfig] = None


def validate_fewshot_config() -> FewShotConfig:
    """Sanity-check few-shot configuration.

    This forces a fresh load (ignores cache) and raises with a readable error if:
    - JSON is malformed
    - section_key is unknown
    - referenced files are missing (v2)

    Returns the loaded config (may have 0 examples).
    """

    global _FEWSHOT_CACHE
    _FEWSHOT_CACHE = None
    cfg = load_fewshot_config()
    # Cache the validated config for subsequent prompt calls.
    _FEWSHOT_CACHE = cfg
    return cfg


def _get_fewshot_config() -> FewShotConfig:
    global _FEWSHOT_CACHE
    if _FEWSHOT_CACHE is None:
        _FEWSHOT_CACHE = load_fewshot_config()
    return _FEWSHOT_CACHE


def _render_fewshot_block(*, examples: list[FewShotExample], target_section_key: str) -> str:
    if not examples:
        return ""

    # Include global (*) examples and ones matching target key.
    picked = [ex for ex in examples if ex.section_key == "*" or ex.section_key == target_section_key]
    if not picked:
        return ""

    blocks: list[str] = ["Few-shot examples (raw transcript -> expected bullet output):"]
    for i, ex in enumerate(picked, start=1):
        blocks.append(f"Example {i}: {ex.title}")
        blocks.append(f"Applies to section_key: {ex.section_key}")
        blocks.append("Transcript:")
        blocks.append(ex.transcript)
        blocks.append("Expected output (ONLY bullets):")
        blocks.append(ex.expected_bullets.rstrip("\n"))
        blocks.append("")

    return "\n".join(blocks).rstrip() + "\n\n"


def minutes_user_prompt(*, section_heading: str, section_transcript: str) -> str:
    """Prompt for a single section.

    Prompt includes optional few-shot examples loaded from minutes_fewshot.json.

    Few-shot selection rule:
      - include examples where section_key == "*" (global)
      - plus examples where section_key matches the target section key
    """

    headings = "\n".join(SECTION_HEADINGS)

    heading_to_key = {_norm_key(s.heading): s.key for s in SECTION_DEFS}
    target_key = heading_to_key.get(_norm_key(section_heading), "")

    fewshot = _get_fewshot_config()
    fewshot_block = _render_fewshot_block(examples=fewshot.examples, target_section_key=target_key)

    user_prompt = f"""
        Generate meeting minutes of the transcription of a meeting, in Markdown format.

        Constraints:
        - Speakers must remain generic: Speaker 1, Speaker 2, ... (do NOT map to real people).
        - Do NOT include any timestamps anywhere.
        - Do NOT add any header block.
        - Output MUST be valid Markdown.
        - Output MUST be ONLY bullet points (each line starts with '- ').
        - If you find sub-sections in the transcript, you should generate indented bullet points.
        - If there is no substantive content in the transcript, output nothing (empty string).

        Here are a few examples of transcripts and the corresponding expected output (bullet points):

        {fewshot_block}

        Now, using the style and length demonstrated above, generate extractive summarization of the following transcript: 
        {section_transcript}
        """.strip()

    #print(f"user_prompt: {user_prompt}")

    return user_prompt
