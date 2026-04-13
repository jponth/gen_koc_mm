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
            You are an AI Assistant that performs extractive summarization for a single meeting section.

            Requirements:
            1. Perform extractive summarization only. Keep the output tightly grounded in the transcript.
            2. Do not add facts, decisions, names, dates, amounts, or actions that are not supported by the transcript.
            3. Do not format the output as Markdown, bullets, headings, or sections.
            4. Output plain text only.
            5. Preserve important details, terminology, numbers, and action items that are explicitly supported by the transcript.
            6. Speakers must remain generic: Speaker 1, Speaker 2, ... (do NOT map to real people).
            7. Do NOT include any timestamps anywhere.
            8. If there is no substantive content for this section, output nothing (empty string).
        """

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

    blocks: list[str] = ["Few-shot examples (raw transcript -> extractive plain-text summary):"]
    for i, ex in enumerate(picked, start=1):
        blocks.append(f"Example {i}: {ex.title}")
        blocks.append(f"Applies to section_key: {ex.section_key}")
        blocks.append("Transcript:")
        blocks.append(ex.transcript)
        blocks.append("Expected output:")
        blocks.append(ex.expected_bullets.rstrip("\n"))
        blocks.append("")

    return "\n".join(blocks).rstrip() + "\n\n"


def format_minutes_system_prompt() -> str:
    system_prompt = """
            You are an AI Assistant that formats an extractive section summary into final meeting minutes in Markdown.

            Requirements:
            1. Preserve all information from the input summary. Do not add new facts and do not omit supported details.
            2. Format the output as Markdown.
            3. When the context changes to a new sub-topic, group related items into nested bullet lists.
            4. Each top-level bullet line must start with '- '. Nested bullets may be used for grouped sub-sections.
            5. Keep bullet nesting to at most 2 levels deep.
            6. Maintain the original meaning, order, specificity, terminology, and numbers from the input summary.
            7. Speakers must remain generic: Speaker 1, Speaker 2, ... (do NOT map to real people).
            8. Do NOT include any timestamps anywhere.
            9. Do NOT add any header block.
            10. Output MUST be only Markdown bullet lists, with nested bullets when helpful.
            11. If the input is empty or has no substantive content, output nothing (empty string).
        """
    return system_prompt


def format_minutes_user_prompt(*, section_heading: str, summary_text: str) -> str:
    summary_text = (summary_text or "").strip()

    user_prompt = f"""
        You are formatting the final minutes for the section '{section_heading}'.

        Task:
        1. Convert the input summary into final Markdown bullet-list minutes.
        2. Group related items into nested bullet sub-sections when the context clearly changes.

        Constraints:
        - Preserve all supported information from the input.
        - Do not add new facts, decisions, names, dates, amounts, or action items.
        - Output only Markdown bullet lists.
        - Use nested bullets only when they help group related content under a clear context change.
        - If the input is empty, output an empty string.

        Input summary:
        {summary_text}
        """.strip()

    return user_prompt


def minutes_user_prompt(*, section_heading: str, section_transcript: str) -> str:
    """Prompt for a single section.

    Prompt includes optional few-shot examples loaded from minutes_fewshot.json.

    Few-shot selection rule:
      - include examples where section_key == "*" (global)
      - plus examples where section_key matches the target section key
    """

    heading_to_key = {_norm_key(s.heading): s.key for s in SECTION_DEFS}
    target_key = heading_to_key.get(_norm_key(section_heading), "")

    fewshot = _get_fewshot_config()
    fewshot_block = _render_fewshot_block(examples=fewshot.examples, target_section_key=target_key)

    examples_block = ""
    if fewshot_block.strip():
        examples_block = f"""
        Here are a few examples of transcripts and the corresponding expected extractive summaries:

        {fewshot_block}
        """

    user_prompt = f"""
        You are generating an extractive summary for the meeting section: '{section_heading}'.

        Task:
        1. Perform extractive summarization of this section only.
        2. Capture the substantive information in plain text without applying final formatting.

        Constraints:
        - Keep the output tightly grounded in the transcript.
        - Do not add new facts, decisions, names, dates, amounts, or action items that are not supported by the transcript.
        - Do not format the output as Markdown, bullets, headings, or sections.
        - Output plain text only.
        - Preserve important details, terminology, numbers, and action items that are explicitly supported by the transcript.
        - Speakers must remain generic: Speaker 1, Speaker 2, ... (do NOT map to real people).
        - Do NOT include any timestamps anywhere.
        - If there is no substantive content in this section, output an empty string.

        {examples_block}
        Transcript for this section:
        {section_transcript}
        """.strip()

    return user_prompt
