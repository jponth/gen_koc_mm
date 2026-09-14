from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable, Optional


@dataclass(frozen=True)
class TranscribeResult:
    output_path: Path
    whisper_stdout: str
    whisper_stderr: str


ProgressCallback = Callable[[int, int, str], None]


def _coerce_bool_arg(value: bool) -> str:
    return "True" if value else "False"


def _maybe_report_progress(
    line: str,
    *,
    total_bytes: int,
    progress_callback: Optional[ProgressCallback],
) -> None:
    if progress_callback is None:
        return

    match = re.search(r"(\d+)/(\d+)", line)
    if not match:
        return

    current_frames = int(match.group(1))
    total_frames = int(match.group(2))
    if total_frames <= 0:
        return

    current_bytes = min(total_bytes, int(total_bytes * (current_frames / total_frames)))
    progress_callback(current_bytes, total_bytes, "Transcribing audio")


def transcribe_with_whisper_cli(
    *,
    input_audio: Path,
    output_path: Path,
    model: str = "medium",
    language: Optional[str] = "en",
    output_format: str = "txt",
    verbose: Optional[bool] = None,
    progress_callback: Optional[ProgressCallback] = None,
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
    if verbose is not None:
        cmd += ["--verbose", _coerce_bool_arg(verbose)]

    total_bytes = max(input_audio.stat().st_size, 1)
    if progress_callback is not None:
        progress_callback(0, total_bytes, "Starting Whisper")

    if progress_callback is None:
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "Whisper transcription failed.\n"
                f"Command: {' '.join(cmd)}\n\n"
                f"stdout:\n{proc.stdout}\n\n"
                f"stderr:\n{proc.stderr}\n"
            )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    else:
        proc = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        combined_chunks: list[str] = []
        current_line = ""
        assert proc.stdout is not None
        while True:
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if not ch:
                continue
            combined_chunks.append(ch)
            current_line += ch
            if ch in {"\r", "\n"}:
                _maybe_report_progress(
                    current_line,
                    total_bytes=total_bytes,
                    progress_callback=progress_callback,
                )
                current_line = ""
        if current_line:
            _maybe_report_progress(
                current_line,
                total_bytes=total_bytes,
                progress_callback=progress_callback,
            )
        returncode = proc.wait()
        stdout_text = "".join(combined_chunks)
        stderr_text = ""
        if returncode != 0:
            raise RuntimeError(
                "Whisper transcription failed.\n"
                f"Command: {' '.join(cmd)}\n\n"
                f"output:\n{stdout_text}\n"
            )

    if progress_callback is not None:
        progress_callback(total_bytes, total_bytes, "Finalizing transcript")

    if progress_callback is None and proc.returncode != 0:
        raise RuntimeError(
            "Whisper transcription failed.\n"
            f"Command: {' '.join(cmd)}\n\n"
            f"stdout:\n{stdout_text}\n\n"
            f"stderr:\n{stderr_text}\n"
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
        whisper_stdout=stdout_text,
        whisper_stderr=stderr_text,
    )
