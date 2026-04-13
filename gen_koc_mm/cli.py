from __future__ import annotations

from pathlib import Path
from typing import Optional

import json
from datetime import datetime
import re

import typer
from dotenv import load_dotenv
from rich.console import Console

from .chunking import chunk_utterances, identify_section_boundaries, section_is_absent
from .llm import format_minutes_bullets, generate_minutes, load_llm_config
from .marked_transcript import MarkedBoundary, parse_marked_transcript, render_marked_transcript
from .prompting import (
    format_minutes_system_prompt,
    format_minutes_user_prompt,
    minutes_system_prompt,
    minutes_user_prompt,
    validate_fewshot_config,
)
from .sections import SECTION_DEFS, SECTION_HEADINGS
from .transcript import parse_transcript
from .docx_merge import merge_minutes_into_docx
from .transcribe import transcribe_with_whisper_cli

app = typer.Typer(add_completion=False, help="Generate KoC meeting minutes from a transcript")
console = Console()


@app.callback()
def _main():
    """KoC minutes generator."""
    # Having a callback forces Typer to keep subcommands (so `generate` is a subcommand).
    return


def _safe_slug(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "section"


def _infer_date_of_meeting(*, input_path: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip()

    # Best-effort inference from filename: look for YYYY-MM-DD.
    m = re.search(r"(\d{4}-\d{2}-\d{2})", input_path.name)
    if m:
        return m.group(1)

    return ""


@app.command()
def generate(
    input: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
    identify_sections: bool = typer.Option(
        False,
        "--identify-sections",
        help="Part 1: write an intermediate transcript with explicit ** <section key> ** markers inserted at detected boundaries.",
    ),
    generate_output: bool = typer.Option(
        False,
        "--generate-output",
        help="Part 2: generate minutes JSON using an intermediate transcript that already contains ** <section key> ** markers.",
    ),
    date_of_meeting: Optional[str] = typer.Option(
        None,
        "--date-of-meeting",
        help="Meeting date to embed in the JSON output. If omitted, we try to infer from the input filename (YYYY-MM-DD).",
    ),
    provider: str = typer.Option("openai", "--provider", help="LLM provider (openai|ollama)"),
    model: Optional[str] = typer.Option(None, "--model", help="Model name (provider-specific)"),
    debug_chunks: bool = typer.Option(False, "--debug-chunks", help="Write section chunks next to output for inspection"),
    minutes_style: str = typer.Option(
        "bullets",
        "--minutes-style",
        help="Final minutes style. Currently only 'bullets' is supported.",
    ),
):
    """Generate KoC meeting minutes.

    This command has two modes:

    - `--identify-sections`: boundary detection only (safe-regex cues), writes a marked transcript for review.
    - `--generate-output`: minutes generation from a marked transcript (explicit section boundaries).
    """

    load_dotenv(override=False)

    if identify_sections == generate_output:
        raise typer.BadParameter("Please specify exactly one of --identify-sections or --generate-output")

    raw = input.read_text(encoding="utf-8")

    # Part 1: Identify boundaries and write intermediate marked transcript.
    if identify_sections:
        utterances = parse_transcript(raw)
        utterances2, detected = identify_section_boundaries(utterances)

        heading_to_key = {s.heading: s.key for s in SECTION_DEFS}
        boundaries: list[MarkedBoundary] = [
            MarkedBoundary(idx=b.idx, key=heading_to_key[b.heading]) for b in detected if b.heading in heading_to_key
        ]

        marked = render_marked_transcript(utterances=utterances2, boundaries=boundaries)

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(marked, encoding="utf-8")
        console.print(f"Wrote intermediate marked transcript: [bold]{output}[/bold]")
        return

    # Part 2: Generate minutes from an already-marked transcript.
    # Sanity-check few-shot examples before we start making LLM calls.
    try:
        cfg = validate_fewshot_config()
    except Exception as e:
        raise typer.BadParameter(f"Few-shot examples sanity-check failed: {e}")

    # One-liner summary
    n_total = len(cfg.examples)
    n_global = len([ex for ex in cfg.examples if ex.section_key == "*"])
    n_specific = n_total - n_global
    console.print(f"Few-shot examples: {n_total} loaded ({n_global} global, {n_specific} section-specific)")

    chunks = parse_marked_transcript(raw)

    llm_cfg = load_llm_config(provider=provider, model=model)
    model_name = llm_cfg.model

    heading_to_key = {s.heading: s.key for s in SECTION_DEFS}

    minutes_style_norm = (minutes_style or "bullets").strip().lower()
    if minutes_style_norm != "bullets":
        raise typer.BadParameter("--minutes-style currently supports only: bullets")

    sys_p = minutes_system_prompt()
    format_sys_p = format_minutes_system_prompt()

    section_to_text: dict[str, str] = {h: "" for h in SECTION_HEADINGS}

    # Optional: write chunks for inspection
    if debug_chunks:
        chunk_dir = output.parent / (output.stem + "_chunks")
        chunk_dir.mkdir(parents=True, exist_ok=True)
        for ch in chunks:
            safe = (
                ch.heading.lower()
                .replace("’", "'")
                .replace(" ", "_")
                .replace(":", "")
            )
            (chunk_dir / f"{safe}.txt").write_text(ch.text, encoding="utf-8")

    # Logs folder for prompt/response traces
    logs_dir = output.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    for idx, ch in enumerate(chunks, start=1):
        # If section is empty, or the transcript explicitly says the person isn't present, leave it empty.
        if section_is_absent(ch):
            section_to_text[ch.heading] = ""
            continue

        user_p = minutes_user_prompt(section_heading=ch.heading, section_transcript=ch.text)

        slug = _safe_slug(ch.heading)
        base = f"{run_id}_{idx:02d}_{slug}"

        # Log prompts before LLM call (helps debug crashes/timeouts).
        (logs_dir / f"{base}.system.txt").write_text(sys_p + "\n", encoding="utf-8")
        (logs_dir / f"{base}.user.txt").write_text(user_p + "\n", encoding="utf-8")

        bullets_raw = generate_minutes(
            system_prompt=sys_p,
            user_prompt=user_p,
            provider=provider,
            model=model_name,
        )

        # Log raw extractive summary response.
        (logs_dir / f"{base}.summary.response.txt").write_text(bullets_raw + "\n", encoding="utf-8")

        summary_text = (bullets_raw or "").strip()

        if not summary_text:
            section_to_text[ch.heading] = ""
            continue

        format_user_p = format_minutes_user_prompt(section_heading=ch.heading, summary_text=summary_text)

        # Log final formatting prompts.
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

    # Build JSON payload (schema v1.0)
    date_str = _infer_date_of_meeting(input_path=input, explicit=date_of_meeting)

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
            "input_file": input.name,
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

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    console.print(f"Wrote: [bold]{output}[/bold]")


@app.command(name="transcribe")
def transcribe(
    input_audio: Path = typer.Option(..., "--input-audio", exists=True, dir_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
    whisper_model: str = typer.Option("medium", "--whisper-model", help="Whisper model name (tiny|base|small|medium|large)") ,
    language: Optional[str] = typer.Option(
        "en",
        "--language",
        help="Audio language (e.g. en). If omitted, whisper will auto-detect.",
    ),
    format: str = typer.Option("txt", "--format", help="Whisper output format (txt|vtt|srt|tsv|json)"),
):
    """Transcribe an audio file locally using the Whisper CLI.

    This uses the local `whisper` binary (no API calls).

    Example:
      python -m gen_koc_mm transcribe --input-audio meeting.m4a --output input/meeting.txt
    """

    res = transcribe_with_whisper_cli(
        input_audio=input_audio,
        output_path=output,
        model=whisper_model,
        language=language,
        output_format=format,
    )

    console.print(f"Wrote transcript: [bold]{res.output_path}[/bold]")


@app.command(name="merge-docx")
def merge_docx(
    minutes_json: Path = typer.Option(..., "--minutes-json", exists=True, dir_okay=False),
    template_docx: Path = typer.Option(..., "--template-docx", exists=True, dir_okay=False),
    output_docx: Path = typer.Option(..., "--output-docx", dir_okay=False),
):
    """Merge minutes JSON into a Word (.docx) template.

    The template should contain placeholders like:
      - <<date_of_meeting>>
      - <<grand_knights_report>>
      - <<chaplains_report>>

    Placeholders can appear in normal paragraphs or table cells.
    """

    summary = merge_minutes_into_docx(
        minutes_json_path=minutes_json,
        template_docx_path=template_docx,
        output_docx_path=output_docx,
    )
    console.print_json(data=summary)


@app.command(name="suggest-cues")
def suggest_cues(
    input_path: Optional[Path] = typer.Option(
        None, "--input-path", exists=True, help="A transcript file or a folder containing transcript files"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="Write markdown report to this file (defaults to stdout)"
    ),
    glob: str = typer.Option("*.txt", "--glob", help="When --input-path is a folder, which files to include"),
    max_per_pattern: int = typer.Option(20, "--max-per-pattern", help="Max lines per (section, pattern, file)"),
    discover_json: Optional[Path] = typer.Option(
        None,
        "--discover-json",
        help="Write discovery candidates as JSON to this file (no markdown).",
    ),
    max_candidates_per_file: int = typer.Option(500, "--max-candidates-per-file", help="Discovery JSON: limit candidates per file"),
    update_cues: Optional[Path] = typer.Option(
        None,
        "--update-cues",
        help="Path to a discovery JSON file. Updates gen_koc_mm/section_cues.json by appending suggested safe_regex patterns.",
    ),
    min_confidence: float = typer.Option(0.7, "--min-confidence", help="--update-cues: minimum suggested_confidence to apply"),
    dry_run: bool = typer.Option(False, "--dry-run", help="--update-cues: compute changes but don't write section_cues.json"),
):
    """Scan transcript files for section-cue lines, or discover new cues, or update cues from discovery JSON."""

    from .cue_suggest import build_discover_json, update_section_cues_json

    # Mode 1: Update cues from a discovery JSON file
    if update_cues is not None:
        summary = update_section_cues_json(
            discover_json_path=update_cues,
            min_confidence=min_confidence,
            dry_run=dry_run,
        )
        console.print_json(data=summary)
        return

    if input_path is None:
        raise typer.BadParameter("--input-path is required unless you use --update-cues")

    # Mode 2: Discover candidates and output JSON
    if discover_json is not None:
        payload = build_discover_json(
            input_path=input_path,
            glob=glob,
            max_candidates_per_file=max_candidates_per_file,
        )
        discover_json.parent.mkdir(parents=True, exist_ok=True)
        discover_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        console.print(f"Wrote: [bold]{discover_json}[/bold]")
        return

    # Mode 3: If you really want the old markdown report, we can re-add it.
    raise typer.BadParameter(
        "Please specify either --discover-json (to generate discovery JSON) or --update-cues (to apply a discovery JSON)."
    )


if __name__ == "__main__":
    app()
