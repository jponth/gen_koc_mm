from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console

from .backfill_cleaned_fewshot import backfill_cleaned_transcripts
from .chunking import identify_section_boundaries
from .generation import generate_minutes_output
from .marked_transcript import MarkedBoundary, parse_marked_transcript, render_marked_transcript
from .sections import SECTION_DEFS
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
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Legacy shared model name for both stages (provider-specific).",
    ),
    transcript_model: Optional[str] = typer.Option(
        None,
        "--transcript-model",
        help="Model name for the transcript-to-sentence stage (provider-specific).",
    ),
    minutes_model: Optional[str] = typer.Option(
        None,
        "--minutes-model",
        help="Model name for the minutes-generation stage (provider-specific).",
    ),
    debug_chunks: bool = typer.Option(False, "--debug-chunks", help="Write section chunks next to output for inspection"),
    minutes_style: str = typer.Option(
        "bullets",
        "--minutes-style",
        help="Final minutes style. Currently only 'bullets' is supported.",
    ),
    fewshot_source: str = typer.Option(
        "sqlite",
        "--fewshot-source",
        help="Few-shot example source: sqlite or folder.",
    ),
):
    """Generate KoC meeting minutes.

    This command has two modes:

    - `--identify-sections`: boundary detection only (safe-regex cues), writes a marked transcript for review.
    - `--generate-output`: minutes generation from a marked transcript (explicit section boundaries).
    """

    load_dotenv(override=False)
    fewshot_source_norm = (fewshot_source or "sqlite").strip().lower()
    if fewshot_source_norm not in {"sqlite", "folder"}:
        raise typer.BadParameter("--fewshot-source must be one of: sqlite, folder")

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

    try:
        result = generate_minutes_output(
            input_path=input,
            output_path=output,
            date_of_meeting=date_of_meeting,
            provider=provider,
            transcript_model=transcript_model or model,
            minutes_model=minutes_model or model,
            debug_chunks=debug_chunks,
            minutes_style=minutes_style,
            fewshot_source=fewshot_source_norm,
        )
    except Exception as e:
        raise typer.BadParameter(f"Minutes generation failed: {e}")

    if result.fewshot_source_label == "SQLite/FTS5":
        console.print(
            f"Few-shot examples: {result.fewshot_total} loaded from SQLite/FTS5 "
            f"({result.fewshot_specific} section-specific)"
        )
    else:
        console.print(
            f"Few-shot examples: {result.fewshot_total} loaded from packaged JSON "
            f"({result.fewshot_global} global, {result.fewshot_specific} section-specific)"
        )

    console.print(f"Wrote: [bold]{result.output_path}[/bold]")


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
      - (** grand_knights_report **)
      - (** chaplains_report **)

    Placeholders can appear in normal paragraphs or table cells.
    """

    summary = merge_minutes_into_docx(
        minutes_json_path=minutes_json,
        template_docx_path=template_docx,
        output_docx_path=output_docx,
    )
    console.print_json(data=summary)


@app.command(name="backfill-cleaned-fewshot")
def backfill_cleaned_fewshot(
    provider: str = typer.Option("openai", "--provider", help="LLM provider (openai|ollama)"),
    model: Optional[str] = typer.Option(None, "--model", help="Model name (provider-specific)"),
    db_path: Optional[Path] = typer.Option(None, "--db-path", dir_okay=False, help="Few-shot SQLite path"),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Process at most this many rows"),
    overwrite_existing: bool = typer.Option(
        False,
        "--overwrite-existing",
        help="Rebuild cleaned transcript text even for rows that already have it.",
    ),
):
    """Backfill cleaned transcript text for SQLite few-shot examples."""

    result = backfill_cleaned_transcripts(
        provider=provider,
        model=model,
        path=db_path,
        only_missing=not overwrite_existing,
        limit=limit,
        log_callback=console.print,
    )

    console.print(
        f"Backfill complete for [bold]{result.db_path}[/bold] using model [bold]{result.model_name}[/bold]."
    )
    console.print(
        f"Scanned={result.scanned} Updated={result.updated} Skipped={result.skipped} Failed={result.failed}"
    )


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
