from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TranscribeResult:
    output_path: Path
    whisper_stdout: str
    whisper_stderr: str


def transcribe_with_whisper_cli(
    *,
    input_audio: Path,
    output_path: Path,
    model: str = "medium",
    language: Optional[str] = "en",
    output_format: str = "txt",
) -> TranscribeResult:
    """Transcribe audio locally using the Whisper CLI binary (`whisper`).

    This is intentionally local-only (no API calls).

    Notes:
    - Whisper will create the output filename itself based on the audio basename.
      We run it in `output_path.parent` and then move/rename to `output_path`.
    """

    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        raise RuntimeError(
            "Whisper CLI not found (missing `whisper` on PATH). "
            "Install it locally, e.g. `brew install openai-whisper`, then retry."
        )

    if not input_audio.exists() or not input_audio.is_file():
        raise FileNotFoundError(f"Input audio not found: {input_audio}")

    output_format = output_format.strip().lower()
    if output_format not in {"txt", "vtt", "srt", "tsv", "json"}:
        raise ValueError("--format must be one of: txt, vtt, srt, tsv, json")

    out_dir = output_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Whisper writes: <audio_stem>.<output_format> into --output_dir
    expected = out_dir / f"{input_audio.stem}.{output_format}"

    cmd = [
        whisper_bin,
        str(input_audio),
        "--model",
        model,
        "--output_format",
        output_format,
        "--output_dir",
        str(out_dir),
    ]
    if language:
        cmd += ["--language", language]

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Whisper transcription failed.\n"
            f"Command: {' '.join(cmd)}\n\n"
            f"stdout:\n{proc.stdout}\n\n"
            f"stderr:\n{proc.stderr}\n"
        )

    if not expected.exists():
        raise RuntimeError(
            "Whisper finished without producing the expected output file.\n"
            f"Expected: {expected}\n"
            f"(audio stem: {input_audio.stem}, format: {output_format})\n"
        )

    # Move into the requested output_path if needed.
    if expected.resolve() != output_path.resolve():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        expected.replace(output_path)

    return TranscribeResult(
        output_path=output_path,
        whisper_stdout=proc.stdout,
        whisper_stderr=proc.stderr,
    )
