from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from .chunking import section_is_absent
from .llm import format_minutes_bullets, generate_minutes, load_llm_config
from .marked_transcript import parse_marked_transcript
from .prompting import (
    format_minutes_system_prompt,
    format_minutes_user_prompt,
    get_fewshot_source_label,
    minutes_system_prompt,
    minutes_user_prompt,
    validate_fewshot_config,
)
from .sections import SECTION_DEFS, SECTION_HEADINGS


ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class GenerateOutputResult:
    output_path: Path
    payload: dict
    model_name: str
    fewshot_total: int
    fewshot_global: int
    fewshot_specific: int
    fewshot_source_label: str


def _safe_slug(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "section"


def infer_date_of_meeting(*, input_path: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip()

    match = re.search(r"(\d{4}-\d{2}-\d{2})", input_path.name)
    if match:
        return match.group(1)

    return ""


def generate_minutes_output(
    *,
    input_path: Path,
    output_path: Path,
    date_of_meeting: Optional[str],
    provider: str,
    model: Optional[str],
    debug_chunks: bool,
    minutes_style: str = "bullets",
    fewshot_source: str = "sqlite",
    progress_callback: Optional[ProgressCallback] = None,
    log_callback: Optional[LogCallback] = None,
) -> GenerateOutputResult:
    load_dotenv(override=False)

    fewshot_source_norm = (fewshot_source or "sqlite").strip().lower()
    if fewshot_source_norm not in {"sqlite", "folder"}:
        raise ValueError("--fewshot-source must be one of: sqlite, folder")
    os.environ["GEN_KOC_MM_FEWSHOT_SOURCE"] = fewshot_source_norm

    raw = input_path.read_text(encoding="utf-8")

    cfg = validate_fewshot_config()
    fewshot_total = len(cfg.examples)
    fewshot_global = len([ex for ex in cfg.examples if ex.section_key == "*"])
    fewshot_specific = fewshot_total - fewshot_global
    fewshot_source_label = get_fewshot_source_label()

    if log_callback is not None:
        if fewshot_source_label == "SQLite/FTS5":
            log_callback(
                f"Few-shot examples: {fewshot_total} loaded from SQLite/FTS5 ({fewshot_specific} section-specific)"
            )
        else:
            log_callback(
                f"Few-shot examples: {fewshot_total} loaded from packaged JSON "
                f"({fewshot_global} global, {fewshot_specific} section-specific)"
            )

    chunks = parse_marked_transcript(raw)
    total_bytes = max(sum(len(ch.text.encode("utf-8")) for ch in chunks), 1)
    processed_bytes = 0

    llm_cfg = load_llm_config(provider=provider, model=model)
    model_name = llm_cfg.model

    heading_to_key = {s.heading: s.key for s in SECTION_DEFS}

    minutes_style_norm = (minutes_style or "bullets").strip().lower()
    if minutes_style_norm != "bullets":
        raise ValueError("--minutes-style currently supports only: bullets")

    sys_p = minutes_system_prompt()
    format_sys_p = format_minutes_system_prompt()
    section_to_text: dict[str, str] = {h: "" for h in SECTION_HEADINGS}

    if debug_chunks:
        chunk_dir = output_path.parent / (output_path.stem + "_chunks")
        chunk_dir.mkdir(parents=True, exist_ok=True)
        for ch in chunks:
            safe = ch.heading.lower().replace("’", "'").replace(" ", "_").replace(":", "")
            (chunk_dir / f"{safe}.txt").write_text(ch.text, encoding="utf-8")

    logs_dir = output_path.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    if progress_callback is not None:
        progress_callback(0, total_bytes, "Preparing section prompts")

    for idx, ch in enumerate(chunks, start=1):
        chunk_bytes = len(ch.text.encode("utf-8"))
        if log_callback is not None:
            log_callback(f"[{idx}/{len(chunks)}] Processing section: {ch.heading}")

        if section_is_absent(ch):
            section_to_text[ch.heading] = ""
            processed_bytes += chunk_bytes
            if progress_callback is not None:
                progress_callback(
                    min(processed_bytes, total_bytes),
                    total_bytes,
                    f"{idx}/{len(chunks)} {ch.heading} (absent)",
                )
            continue

        user_p = minutes_user_prompt(section_heading=ch.heading, section_transcript=ch.text)

        slug = _safe_slug(ch.heading)
        base = f"{run_id}_{idx:02d}_{slug}"

        (logs_dir / f"{base}.system.txt").write_text(sys_p + "\n", encoding="utf-8")
        (logs_dir / f"{base}.user.txt").write_text(user_p + "\n", encoding="utf-8")

        bullets_raw = generate_minutes(
            system_prompt=sys_p,
            user_prompt=user_p,
            provider=provider,
            model=model_name,
        )

        (logs_dir / f"{base}.summary.response.txt").write_text(bullets_raw + "\n", encoding="utf-8")
        summary_text = (bullets_raw or "").strip()

        if not summary_text:
            section_to_text[ch.heading] = ""
            processed_bytes += chunk_bytes
            if progress_callback is not None:
                progress_callback(
                    min(processed_bytes, total_bytes),
                    total_bytes,
                    f"{idx}/{len(chunks)} {ch.heading}",
                )
            continue

        format_user_p = format_minutes_user_prompt(section_heading=ch.heading, summary_text=summary_text)

        (logs_dir / f"{base}.formatting.system.txt").write_text(format_sys_p + "\n", encoding="utf-8")
        (logs_dir / f"{base}.formatting.user.txt").write_text(format_user_p + "\n", encoding="utf-8")

        formatted_raw = format_minutes_bullets(
            system_prompt=format_sys_p,
            user_prompt=format_user_p,
            provider=provider,
            model=model_name,
        )

        (logs_dir / f"{base}.formatting.response.txt").write_text(formatted_raw + "\n", encoding="utf-8")
        formatted_text = (formatted_raw or "").strip()

        bullets = "\n".join([ln for ln in formatted_text.splitlines() if ln.strip().startswith("-")]).strip()
        section_to_text[ch.heading] = bullets

        processed_bytes += chunk_bytes
        if progress_callback is not None:
            progress_callback(
                min(processed_bytes, total_bytes),
                total_bytes,
                f"{idx}/{len(chunks)} {ch.heading}",
            )

    date_str = infer_date_of_meeting(input_path=input_path, explicit=date_of_meeting)

    section_status: dict[str, str] = {}
    for ch in chunks:
        if section_is_absent(ch):
            section_status[ch.heading] = "absent"
        else:
            txt = (section_to_text.get(ch.heading) or "").strip()
            section_status[ch.heading] = "ok" if txt else "empty"

    payload = {
        "schema_version": "1.0",
        "date_of_meeting": date_str,
        "generator": {
            "name": "gen_koc_mm",
            "version": "0.1.0",
            "provider": provider,
            "model": model_name,
        },
        "source": {
            "input_file": input_path.name,
        },
        "sections": [
            {
                "section_key": heading_to_key.get(heading, heading.lower()),
                "section_heading": heading,
                "section_text": (section_to_text.get(heading) or "").strip(),
                "format": "markdown",
                "status": section_status.get(heading, "empty"),
            }
            for heading in SECTION_HEADINGS
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if log_callback is not None:
        log_callback(f"Wrote output JSON: {output_path}")
    if progress_callback is not None:
        progress_callback(total_bytes, total_bytes, "Complete")

    return GenerateOutputResult(
        output_path=output_path,
        payload=payload,
        model_name=model_name,
        fewshot_total=fewshot_total,
        fewshot_global=fewshot_global,
        fewshot_specific=fewshot_specific,
        fewshot_source_label=fewshot_source_label,
    )
