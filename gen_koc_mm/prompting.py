from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import importlib.resources as pkg_resources

from .fewshot_db import count_examples as count_fewshot_db_examples
from .fewshot_db import load_all_examples as load_all_fewshot_db_examples
from .fewshot_db import retrieve_examples as retrieve_fewshot_db_examples
from .sections import SECTION_DEFS, SECTION_HEADINGS


_PROMPTS_CACHE: Optional[dict[str, str]] = None
_PROMPT_SECTION_RE = re.compile(r"^##\s+`([^`]+)`\s*$", flags=re.MULTILINE)


def _read_prompts_text() -> str:
    override = os.environ.get("GEN_KOC_MM_PROMPTS_PATH", "").strip()
    if override:
        return Path(override).read_text(encoding="utf-8")
    return _load_text_from_package("prompts.md")


def _parse_prompt_sections(raw_text: str) -> dict[str, str]:
    matches = list(_PROMPT_SECTION_RE.finditer(raw_text))
    prompts: dict[str, str] = {}

    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_text)
        body = raw_text[start:end].strip()
        lines = body.splitlines()
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            body = "\n".join(lines[1:-1]).strip()
        prompts[name] = body

    return prompts


def _get_prompt_template(name: str) -> str:
    global _PROMPTS_CACHE
    if _PROMPTS_CACHE is None:
        _PROMPTS_CACHE = _parse_prompt_sections(_read_prompts_text())
    prompt = _PROMPTS_CACHE.get(name, "").strip()
    if not prompt:
        raise ValueError(f"Prompt template {name!r} was not found in prompts.md")
    return prompt


def transcript_2_sentence_system_prompt() -> str:
    return _get_prompt_template("transcript_2_sentence_system_prompt")


def minutes_system_prompt() -> str:
    return transcript_2_sentence_system_prompt()


@dataclass(frozen=True)
class FewShotExample:
    title: str
    section_key: str  # canonical key from section_headings.json, or "*" for global
    transcript: str
    cleaned_transcript: str
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


def _fewshot_source_mode() -> str:
    raw = os.environ.get("GEN_KOC_MM_FEWSHOT_SOURCE", "").strip().lower()
    if raw in {"sqlite", "db", "sqlite/fts5"}:
        return "sqlite"
    if raw in {"folder", "file", "files", "fewshot"}:
        return "folder"
    return "auto"


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
    global _FEWSHOT_SOURCE_LABEL

    source_mode = _fewshot_source_mode()

    if source_mode == "sqlite":
        _FEWSHOT_SOURCE_LABEL = "SQLite/FTS5"
        return _load_fewshot_from_db()

    if source_mode == "auto" and count_fewshot_db_examples() > 0:
        _FEWSHOT_SOURCE_LABEL = "SQLite/FTS5"
        return _load_fewshot_from_db()

    _FEWSHOT_SOURCE_LABEL = "packaged JSON"

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

            out.append(
                FewShotExample(
                    title=title,
                    section_key=sk_norm,
                    transcript=transcript,
                    cleaned_transcript="",
                    expected_bullets=expected,
                )
            )
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
        out.append(
            FewShotExample(
                title=title,
                section_key=key,
                transcript=tr,
                cleaned_transcript="",
                expected_bullets=exp,
            )
        )

    return FewShotConfig(examples=out)


# Simple module-level cache so we don't re-read JSON for every section.
_FEWSHOT_CACHE: Optional[FewShotConfig] = None
_FEWSHOT_SOURCE_LABEL = "packaged JSON"


def get_fewshot_source_label() -> str:
    return _FEWSHOT_SOURCE_LABEL


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


def _load_fewshot_from_db() -> FewShotConfig:
    db_examples = load_all_fewshot_db_examples()
    out = [
        FewShotExample(
            title=f"{ex.section_key} ({ex.meeting_date or 'undated'})",
            section_key=ex.section_key,
            transcript=ex.transcript_text.strip(),
            cleaned_transcript=(ex.cleaned_transcript_text or "").strip(),
            expected_bullets=(ex.minutes_text.rstrip() + "\n"),
        )
        for ex in db_examples
        if ex.transcript_text.strip() and ex.minutes_text.strip()
    ]
    return FewShotConfig(examples=out)


def _pick_matching_fewshot_examples(*, examples: list[FewShotExample], target_section_key: str) -> list[FewShotExample]:
    return [ex for ex in examples if ex.section_key == "*" or ex.section_key == target_section_key]


def _render_first_stage_fewshot_block(*, examples: list[FewShotExample], target_section_key: str) -> str:
    if not examples:
        return ""

    picked = [
        ex
        for ex in _pick_matching_fewshot_examples(examples=examples, target_section_key=target_section_key)
        if ex.transcript.strip() and ex.cleaned_transcript.strip()
    ]
    if not picked:
        return ""

    blocks: list[str] = ["Few-shot examples (raw transcript -> cleaned transcript):"]
    for i, ex in enumerate(picked, start=1):
        blocks.append(f"Example {i}: {ex.title}")
        blocks.append(f"Applies to section_key: {ex.section_key}")
        blocks.append("Raw transcript:")
        blocks.append(ex.transcript)
        blocks.append("Cleaned transcript:")
        blocks.append(ex.cleaned_transcript)
        blocks.append("")

    return "\n".join(blocks).rstrip() + "\n\n"


def _render_second_stage_fewshot_block(*, examples: list[FewShotExample], target_section_key: str) -> str:
    if not examples:
        return ""

    picked = [
        ex
        for ex in _pick_matching_fewshot_examples(examples=examples, target_section_key=target_section_key)
        if (ex.cleaned_transcript or ex.transcript).strip() and ex.expected_bullets.strip()
    ]
    if not picked:
        return ""

    blocks: list[str] = ["Few-shot examples (cleaned transcript -> final section minutes):"]
    for i, ex in enumerate(picked, start=1):
        blocks.append(f"Example {i}: {ex.title}")
        blocks.append(f"Applies to section_key: {ex.section_key}")
        blocks.append("Cleaned transcript:")
        blocks.append((ex.cleaned_transcript or ex.transcript).strip())
        blocks.append("Expected output:")
        blocks.append(ex.expected_bullets.rstrip("\n"))
        blocks.append("")

    return "\n".join(blocks).rstrip() + "\n\n"


def _retrieved_db_fewshot_examples(
    *,
    target_section_key: str,
    section_transcript: str,
    current_meeting_date: str = "",
) -> list[FewShotExample]:
    retrieved = retrieve_fewshot_db_examples(
        section_key=target_section_key,
        query_text=section_transcript,
        limit=2,
        exclude_meeting_date=current_meeting_date,
    )
    return [
        FewShotExample(
            title=f"{ex.section_key} ({ex.meeting_date or 'undated'})",
            section_key=ex.section_key,
            transcript=ex.transcript_text.strip(),
            cleaned_transcript=(ex.cleaned_transcript_text or "").strip(),
            expected_bullets=(ex.minutes_text.rstrip() + "\n"),
        )
        for ex in retrieved
        if ex.transcript_text.strip() and ex.minutes_text.strip()
    ]


def _select_fewshot_examples(
    *,
    target_section_key: str,
    section_transcript: str,
    current_meeting_date: str = "",
) -> tuple[list[FewShotExample], str]:
    fewshot_examples: list[FewShotExample] = []
    source_label = "packaged JSON"
    source_mode = _fewshot_source_mode()

    if target_section_key and source_mode == "sqlite":
        fewshot_examples = _retrieved_db_fewshot_examples(
            target_section_key=target_section_key,
            section_transcript=section_transcript,
            current_meeting_date=current_meeting_date,
        )
        source_label = "SQLite/FTS5"
        return fewshot_examples, source_label

    if target_section_key and source_mode == "auto" and count_fewshot_db_examples() > 0:
        fewshot_examples = _retrieved_db_fewshot_examples(
            target_section_key=target_section_key,
            section_transcript=section_transcript,
            current_meeting_date=current_meeting_date,
        )
        source_label = "SQLite/FTS5"
        return fewshot_examples, source_label

    if not fewshot_examples:
        fewshot = _get_fewshot_config()
        fewshot_examples = fewshot.examples
        source_label = get_fewshot_source_label()

    return fewshot_examples, source_label


def minutes_generate_system_prompt() -> str:
    return _get_prompt_template("minutes_generate_system_prompt")


def format_minutes_system_prompt() -> str:
    return minutes_generate_system_prompt()


def minutes_generate_user_prompt(
    *,
    section_heading: str,
    summary_text: str,
    section_transcript: str = "",
    current_meeting_date: str = "",
) -> str:
    summary_text = (summary_text or "").strip()
    heading_to_key = {_norm_key(s.heading): s.key for s in SECTION_DEFS}
    target_key = heading_to_key.get(_norm_key(section_heading), "")

    fewshot_examples, source_label = _select_fewshot_examples(
        target_section_key=target_key,
        section_transcript=section_transcript,
        current_meeting_date=current_meeting_date,
    )
    fewshot_block = _render_second_stage_fewshot_block(
        examples=fewshot_examples,
        target_section_key=target_key,
    )

    examples_block = ""
    if fewshot_block.strip():
        examples_block = f"""
        Here are a few reference examples from the {source_label} few-shot source.

        These examples show how a cleaned transcript for this section is converted into the target final minutes style.
        Use them to match structure, grouping, specificity, and level of detail.
        Do not copy wording from them unless the same facts are explicitly supported by the input cleaned transcript.

        {fewshot_block}
        """

    return _get_prompt_template("minutes_generate_user_prompt").format(
        examples_block=examples_block,
        summary_text=summary_text,
    )


def format_minutes_user_prompt(
    *,
    section_heading: str,
    summary_text: str,
    section_transcript: str = "",
    current_meeting_date: str = "",
) -> str:
    return minutes_generate_user_prompt(
        section_heading=section_heading,
        summary_text=summary_text,
        section_transcript=section_transcript,
        current_meeting_date=current_meeting_date,
    )


def transcript_2_sentence_user_prompt(
    *,
    section_heading: str,
    section_transcript: str,
    current_meeting_date: str = "",
) -> str:
    heading_to_key = {_norm_key(s.heading): s.key for s in SECTION_DEFS}
    target_key = heading_to_key.get(_norm_key(section_heading), "")

    fewshot_examples, source_label = _select_fewshot_examples(
        target_section_key=target_key,
        section_transcript=section_transcript,
        current_meeting_date=current_meeting_date,
    )
    fewshot_block = _render_first_stage_fewshot_block(
        examples=fewshot_examples,
        target_section_key=target_key,
    )

    prompt = _get_prompt_template("transcript_2_sentence_user_prompt").format(
        section_transcript=section_transcript,
    )
    if not fewshot_block.strip():
        return prompt

    return (
        f"{prompt}\n\n"
        f"Here are a few reference examples from the {source_label} few-shot source.\n\n"
        "These examples show how to transform a raw transcript section into a cleaned transcript section "
        "without dropping substantive facts.\n\n"
        f"{fewshot_block}"
    )


def minutes_user_prompt(*, section_heading: str, section_transcript: str, current_meeting_date: str = "") -> str:
    return transcript_2_sentence_user_prompt(
        section_heading=section_heading,
        section_transcript=section_transcript,
        current_meeting_date=current_meeting_date,
    )
