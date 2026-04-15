"""Gradio UI for gen_koc_mm.

Local-only, upload-driven 5-step flow:
  1) Audio -> transcript (Whisper CLI via Typer command)
  2) Identify sections -> marked transcript (Typer)
  3) Review/edit boundaries -> save-as marked transcript (UI only)
  4) Generate minutes JSON from marked transcript (Typer)
  5) Merge JSON into Word template (Typer)

Run:
  source .venv/bin/activate
  python app_gradio.py

This UI intentionally keeps gen_koc_mm CLI as the source of truth.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

import gradio as gr


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = REPO_ROOT / "input"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"
DEFAULT_TEMPLATE = REPO_ROOT / "templates" / "KoC-Meeting-Minutes-Template-v02.docx"


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _today_iso() -> str:
    """Return today's date in YYYY-MM-DD (local time)."""
    return date.today().isoformat()


def _ensure_dirs() -> None:
    DEFAULT_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _copy_upload_to(upload_path: str, dest: Path) -> Path:
    src = Path(upload_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


@dataclass(frozen=True)
class CmdResult:
    cmd: str
    returncode: int
    stdout: str
    stderr: str


def _run_cli(*args: str) -> CmdResult:
    # Use the current interpreter so venv deps resolve.
    cmd_list = [sys.executable, "-m", "gen_koc_mm", *args]
    proc = subprocess.run(cmd_list, cwd=str(REPO_ROOT), text=True, capture_output=True)
    return CmdResult(
        cmd=" ".join(cmd_list),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _render_result(res: CmdResult) -> str:
    parts = [f"$ {res.cmd}"]
    if res.stdout.strip():
        parts.append("\n[stdout]\n" + res.stdout.strip())
    if res.stderr.strip():
        parts.append("\n[stderr]\n" + res.stderr.strip())
    parts.append(f"\n(exit code: {res.returncode})")
    return "\n".join(parts).strip() + "\n"


# -------------------------
# Tab 1: Transcribe
# -------------------------

def ui_transcribe(
    audio_file,  # gr.File returns a tempfile with .name
    whisper_model: str,
    language: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (log, transcript_preview, transcript_path)."""
    _ensure_dirs()
    if audio_file is None:
        return "Please upload an audio file.", None, None

    audio_src = Path(audio_file.name)
    out_path = DEFAULT_INPUT_DIR / f"{audio_src.stem}_{_now_slug()}.txt"

    # Copy upload into project input/ for auditability.
    copied_audio = DEFAULT_INPUT_DIR / f"{audio_src.stem}_{_now_slug()}{audio_src.suffix.lower()}"
    _copy_upload_to(audio_src.as_posix(), copied_audio)

    res = _run_cli(
        "transcribe",
        "--input-audio",
        str(copied_audio),
        "--output",
        str(out_path),
        "--whisper-model",
        whisper_model.strip(),
        "--language",
        (language or "").strip(),
        "--format",
        "txt",
    )

    log = _render_result(res)
    if res.returncode != 0:
        return log, None, None

    try:
        preview = out_path.read_text(encoding="utf-8")
    except Exception as e:
        preview = f"(Transcribed OK, but failed to read output: {e})"

    return log, preview, str(out_path)


# -------------------------
# Tab 2: Identify sections
# -------------------------

def ui_identify_sections(transcript_file, transcript_path_text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (log, marked_preview, marked_path)."""
    _ensure_dirs()

    # Source can be from upload OR from a text path produced in Tab 1.
    src_path: Optional[Path] = None
    if transcript_file is not None:
        src_path = Path(transcript_file.name)
        # Copy uploaded transcript into input/.
        dest = DEFAULT_INPUT_DIR / f"{src_path.stem}_{_now_slug()}{src_path.suffix or '.txt'}"
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif transcript_path_text:
        src_path = Path(transcript_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload a transcript file (txt) or provide a valid transcript path.", None, None

    out_path = DEFAULT_OUTPUT_DIR / f"marked_{src_path.stem}_{_now_slug()}.txt"
    res = _run_cli(
        "generate",
        "--identify-sections",
        "--input",
        str(src_path),
        "--output",
        str(out_path),
    )

    log = _render_result(res)
    if res.returncode != 0:
        return log, None, None

    preview = out_path.read_text(encoding="utf-8")
    return log, preview, str(out_path)


# -------------------------
# Tab 3: Edit boundaries (Save As)
# -------------------------


def _extract_boundary_index(marked_text: str) -> str:
    """Show boundary counts for all sections (single list).

    Flags:
    - **MISSING** when count == 0
    - **DUPLICATE** when count > 1

    Boundary markers are standalone lines like:
      ** old_business **
    """

    if not marked_text or not marked_text.strip():
        return "(No transcript loaded.)"

    import re

    # Canonical list of boundary keys from the generator (keeps UI in sync with CLI behavior).
    try:
        from gen_koc_mm.sections import SECTION_DEFS

        known_tags = [s.key for s in SECTION_DEFS]
    except Exception:
        known_tags = []

    pat = re.compile(r"^\s*\*\*\s*(?P<tag>[^*]+?)\s*\*\*\s*$")

    # Map: boundary tag -> 1-indexed line numbers where it appears.
    found_lines: dict[str, list[int]] = {k: [] for k in known_tags}

    for i, ln in enumerate(marked_text.splitlines(), start=1):
        m = pat.match(ln)
        if not m:
            continue
        tag = m.group("tag").strip()
        found_lines.setdefault(tag, []).append(i)

    if not found_lines:
        return "(No known boundary tags available to count.)"

    lines: list[str] = ["**Boundary counts (all sections)**", ""]

    def _fmt_line_numbers(nums: list[int]) -> str:
        if not nums:
            return ""
        if len(nums) <= 12:
            return " (lines: " + ", ".join(map(str, nums)) + ")"
        # Avoid dumping huge lists in the UI.
        head = ", ".join(map(str, nums[:10]))
        return f" (lines: {head}, … +{len(nums) - 10} more)"

    for k in known_tags:
        nums = found_lines.get(k, [])
        n = len(nums)
        suffix = _fmt_line_numbers(nums)
        if n == 0:
            lines.append(f"- `** {k} **`: **0** — **MISSING**")
        elif n > 1:
            lines.append(f"- `** {k} **`: **{n}**{suffix} — **DUPLICATE**")
        else:
            lines.append(f"- `** {k} **`: {n}{suffix}")

    # Also show any unknown tags that appear in the text (typos / non-canonical).
    extras = sorted([k for k in found_lines.keys() if k not in set(known_tags)])
    if extras:
        lines += ["", "**Non-canonical boundary tags found (check for typos)**", ""]
        for k in extras:
            nums = found_lines.get(k, [])
            lines.append(f"- `** {k} **`: **{len(nums)}**{_fmt_line_numbers(nums)}")

    return "\n".join(lines)

def ui_load_marked(marked_file, marked_path_text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Load marked transcript into editor. Returns (status, content, loaded_path)."""
    src_path: Optional[Path] = None
    if marked_file is not None:
        src_path = Path(marked_file.name)
        dest = DEFAULT_OUTPUT_DIR / f"marked_uploaded_{src_path.stem}_{_now_slug()}{src_path.suffix or '.txt'}"
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif marked_path_text:
        src_path = Path(marked_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload or provide the path to a marked transcript.", None, None

    txt = src_path.read_text(encoding="utf-8")
    return f"Loaded: {src_path}", txt, str(src_path)


def ui_save_marked_as(content: str, base_name: str) -> Tuple[str, Optional[str]]:
    """Save edited marked transcript into output/. Returns (status, saved_path)."""
    _ensure_dirs()
    if not content or not content.strip():
        return "Nothing to save (editor is empty).", None

    base = (base_name or "marked_edited").strip()
    base = base.replace(".txt", "")
    out_path = DEFAULT_OUTPUT_DIR / f"{base}_{_now_slug()}.txt"
    out_path.write_text(content, encoding="utf-8")
    return f"Saved: {out_path}", str(out_path)


# -------------------------
# Tab 4: Generate JSON
# -------------------------

def ui_generate_json(
    marked_file,
    marked_path_text: str,
    date_of_meeting,
    provider: str,
    model_override: str,
    debug_chunks: bool,
) -> Tuple[str, Optional[str], Optional[str]]:
    _ensure_dirs()

    src_path: Optional[Path] = None
    if marked_file is not None:
        src_path = Path(marked_file.name)
        dest = DEFAULT_OUTPUT_DIR / f"marked_for_json_{src_path.stem}_{_now_slug()}{src_path.suffix or '.txt'}"
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif marked_path_text:
        src_path = Path(marked_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload/provide a marked transcript to generate JSON.", None, None

    out_path = DEFAULT_OUTPUT_DIR / f"minutes_{src_path.stem}_{_now_slug()}.json"

    args = [
        "generate",
        "--generate-output",
        "--input",
        str(src_path),
        "--output",
        str(out_path),
    ]
    # gr.DateTime can return timestamp (float), datetime, or string depending on `type=`.
    date_str: str = ""
    if date_of_meeting is None:
        date_str = ""
    elif isinstance(date_of_meeting, (datetime, date)):
        date_str = date_of_meeting.date().isoformat() if isinstance(date_of_meeting, datetime) else date_of_meeting.isoformat()
    elif isinstance(date_of_meeting, (int, float)):
        # timestamp seconds
        date_str = datetime.fromtimestamp(date_of_meeting).date().isoformat()
    else:
        date_str = str(date_of_meeting).strip()
        # If something like "YYYY-MM-DD 00:00:00" slips through, keep the date part.
        if len(date_str) >= 10 and date_str[4:5] == "-" and date_str[7:8] == "-":
            date_str = date_str[:10]

    if date_str:
        args += ["--date-of-meeting", date_str]
    prov = (provider or "OpenAI").strip().lower()
    prov = "ollama" if "ollama" in prov else "openai"
    args += ["--provider", prov]

    if model_override and model_override.strip():
        args += ["--model", model_override.strip()]
    if debug_chunks:
        args += ["--debug-chunks"]

    res = _run_cli(*args)
    log = _render_result(res)
    if res.returncode != 0:
        return log, None, None

    preview = out_path.read_text(encoding="utf-8")
    return log, preview, str(out_path)


# -------------------------
# Tab 5: Merge DOCX
# -------------------------

def ui_merge_docx(
    minutes_json_file,
    minutes_json_path_text: str,
    template_docx_file,
    use_default_template: bool,
) -> Tuple[str, Optional[str]]:
    _ensure_dirs()

    json_path: Optional[Path] = None
    if minutes_json_file is not None:
        json_path = Path(minutes_json_file.name)
        dest = DEFAULT_OUTPUT_DIR / f"minutes_uploaded_{json_path.stem}_{_now_slug()}{json_path.suffix or '.json'}"
        _copy_upload_to(json_path.as_posix(), dest)
        json_path = dest
    elif minutes_json_path_text:
        json_path = Path(minutes_json_path_text).expanduser().resolve()

    if json_path is None or not json_path.exists():
        return "Please upload/provide minutes JSON.", None

    template_path: Optional[Path] = None
    if use_default_template:
        template_path = DEFAULT_TEMPLATE
    elif template_docx_file is not None:
        template_path = Path(template_docx_file.name)
        dest = DEFAULT_OUTPUT_DIR / f"template_{template_path.stem}_{_now_slug()}{template_path.suffix or '.docx'}"
        _copy_upload_to(template_path.as_posix(), dest)
        template_path = dest

    if template_path is None or not template_path.exists():
        return "Please select the default template or upload a DOCX template.", None

    out_docx = DEFAULT_OUTPUT_DIR / f"minutes_{json_path.stem}_{_now_slug()}.docx"

    res = _run_cli(
        "merge-docx",
        "--minutes-json",
        str(json_path),
        "--template-docx",
        str(template_path),
        "--output-docx",
        str(out_docx),
    )

    log = _render_result(res)
    if res.returncode != 0:
        return log, None

    return log, str(out_docx)


def build_ui() -> gr.Blocks:
    css = """
    /* Make Gradio file upload dropzones more compact.
       Gradio's internal DOM/classes vary by version/theme, so we target several.
    */
    .compact-upload {
        --upload-h: 36px;
    }

    .compact-upload [data-testid='file-upload'],
    .compact-upload .file-upload,
    .compact-upload .upload-box,
    .compact-upload .upload-container {
        min-height: var(--upload-h) !important;
        height: var(--upload-h) !important;
        max-height: var(--upload-h) !important;
        padding: 2px 10px !important;
        overflow: hidden !important;
    }

    /* Keep the dropzone message on a single line (best-effort) */
    .compact-upload [data-testid='file-upload'] *,
    .compact-upload .file-upload *,
    .compact-upload .upload-box *,
    .compact-upload .upload-container * {
        font-size: 0.92rem;
        line-height: 1.1;
        white-space: nowrap;
    }

    /* Avoid default paragraph margins causing extra height */
    .compact-upload p {
        margin: 0 !important;
    }

    /* Some themes use a .wrap container inside the dropzone */
    .compact-upload .wrap {
        min-height: var(--upload-h) !important;
        height: var(--upload-h) !important;
        max-height: var(--upload-h) !important;
        overflow: hidden !important;
        white-space: nowrap;
    }

    """

    with gr.Blocks(title="KoC Meeting Minutes Generator", css=css) as demo:
        gr.Markdown(
            "# KoC Meeting Minutes Generator\n"
            "Local-only UI for the 5-step workflow. Upload-only (no file picking).\n\n"
            "Tip: keep outputs under versioned filenames; Tab 3 uses Save As for auditability."
        )

        # Shared state outputs
        st_transcript_path = gr.State(value="")
        st_marked_path = gr.State(value="")
        st_edited_marked_path = gr.State(value="")
        st_minutes_json_path = gr.State(value="")

        with gr.Tabs():
            # 1) Transcribe
            with gr.Tab("1) Audio → Transcript"):
                audio = gr.File(label="Upload audio (m4a/mp3/wav)", elem_classes=["compact-upload"])
                with gr.Accordion("Advanced options", open=False):
                    whisper_model = gr.Textbox(value="medium", label="Whisper model")
                    language = gr.Textbox(value="en", label="Language (e.g., en). Leave blank for auto-detect")

                run_btn = gr.Button("Transcribe")
                out_path = gr.Textbox(label="Transcript path (saved)", interactive=False)
                preview = gr.Textbox(label="Transcript preview", lines=18)
                log = gr.Textbox(label="Command log", lines=10)

            # 2) Identify sections
            with gr.Tab("2) Identify Sections"):
                transcript_upload = gr.File(label="Upload transcript (.txt)", elem_classes=["compact-upload"])
                transcript_path_echo = gr.Textbox(
                    label="Or use transcript from Tab 1 (path)",
                    interactive=False,
                )
                run_btn2 = gr.Button("Identify sections")
                marked_path = gr.Textbox(label="Marked transcript path (saved)", interactive=False)
                marked_preview = gr.Textbox(label="Marked transcript preview", lines=18)
                log2 = gr.Textbox(label="Command log", lines=10)
            # 3) Edit boundaries
            with gr.Tab("3) Review/Edit Boundaries"):
                marked_upload = gr.File(label="Upload marked transcript (.txt)", elem_classes=["compact-upload"])
                marked_path_echo = gr.Textbox(label="Or use marked transcript from Tab 2 (path)", interactive=False)
                load_btn = gr.Button("Load into editor")
                status3 = gr.Textbox(label="Status", lines=2)

                with gr.Row():
                    with gr.Column(scale=1, min_width=260):
                        boundary_index = gr.Markdown(value="(Load a marked transcript to see boundaries.)")
                        refresh_bounds_btn = gr.Button("Refresh boundary counts")
                    with gr.Column(scale=3):
                        editor = gr.Code(
                            label="Marked transcript editor",
                            language="markdown",
                            lines=40,
                            show_line_numbers=True,
                            wrap_lines=True,
                        )

                with gr.Row():
                    save_base = gr.Textbox(value="marked_edited", label="Save As base name")
                    save_btn = gr.Button("Save As")

                saved_path = gr.Textbox(label="Saved edited marked transcript path", interactive=False)

            # 4) Generate JSON
            with gr.Tab("4) Generate JSON"):
                marked_upload4 = gr.File(label="Upload edited marked transcript (.txt)", elem_classes=["compact-upload"])
                marked_path_echo4 = gr.Textbox(label="Or use edited marked transcript from Tab 3 (path)", interactive=False)

                # Keep this prominent (not tucked into Advanced options)
                date_of_meeting = gr.DateTime(
                    value=_today_iso(),
                    include_time=False,
                    type="string",
                    label="Date of meeting",
                )

                with gr.Accordion("Advanced options", open=False):
                    provider = gr.Dropdown(
                        choices=["OpenAI", "Ollama Local"],
                        value="OpenAI",
                        label="LLM provider",
                    )
                    model_override = gr.Textbox(value="gpt-5-mini", label="Model")
                    debug_chunks = gr.Checkbox(value=False, label="Write debug chunks")

                run_btn4 = gr.Button("Generate minutes JSON")
                json_path = gr.Textbox(label="Minutes JSON path (saved)", interactive=False)
                json_preview = gr.Textbox(label="Minutes JSON preview", lines=18)
                log4 = gr.Textbox(label="Command log", lines=10)

            # 5) Merge DOCX
            with gr.Tab("5) Merge to Word"):
                minutes_json_upload = gr.File(label="Upload minutes JSON (.json)", elem_classes=["compact-upload"])
                minutes_json_path_echo = gr.Textbox(label="Or use JSON from Tab 4 (path)", interactive=False)

                use_default_template = gr.Checkbox(value=True, label=f"Use default template ({DEFAULT_TEMPLATE.name})")
                template_upload = gr.File(label="Upload a DOCX template (optional if using default)", elem_classes=["compact-upload"])

                run_btn5 = gr.Button("Merge DOCX")
                out_docx_path = gr.Textbox(label="Output DOCX path (saved)", interactive=False)
                log5 = gr.Textbox(label="Command log", lines=10)

            # --- Wiring (cross-tab propagation) ---
            run_btn.click(
                ui_transcribe,
                inputs=[audio, whisper_model, language],
                outputs=[log, preview, out_path],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[out_path],
                outputs=[st_transcript_path, transcript_path_echo],
            )

            run_btn2.click(
                ui_identify_sections,
                inputs=[transcript_upload, transcript_path_echo],
                outputs=[log2, marked_preview, marked_path],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[marked_path],
                outputs=[st_marked_path, marked_path_echo],
            )

            load_btn.click(
                ui_load_marked,
                inputs=[marked_upload, marked_path_echo],
                outputs=[status3, editor, marked_path_echo],
            ).then(
                _extract_boundary_index,
                inputs=[editor],
                outputs=[boundary_index],
            ).then(
                lambda p: p or "",
                inputs=[marked_path_echo],
                outputs=[marked_path_echo4],
            )

            # Keep the boundary index live as the transcript is edited.
            editor.change(
                _extract_boundary_index,
                inputs=[editor],
                outputs=[boundary_index],
            )

            # Manual refresh button (in case the live-update misses an event).
            refresh_bounds_btn.click(
                _extract_boundary_index,
                inputs=[editor],
                outputs=[boundary_index],
            )

            save_btn.click(
                ui_save_marked_as,
                inputs=[editor, save_base],
                outputs=[status3, saved_path],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[saved_path],
                outputs=[st_edited_marked_path, marked_path_echo4],
            )

            # When switching providers, set a sensible default model.
            provider.change(
                lambda p: "gpt-4o-mini" if p == "OpenAI" else "gpt-oss:20b",
                inputs=[provider],
                outputs=[model_override],
            )

            run_btn4.click(
                ui_generate_json,
                inputs=[marked_upload4, marked_path_echo4, date_of_meeting, provider, model_override, debug_chunks],
                outputs=[log4, json_preview, json_path],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[json_path],
                outputs=[st_minutes_json_path, minutes_json_path_echo],
            )

            run_btn5.click(
                ui_merge_docx,
                inputs=[minutes_json_upload, minutes_json_path_echo, template_upload, use_default_template],
                outputs=[log5, out_docx_path],
            )

        gr.Markdown(
            "---\n"
            "Notes:\n"
            "- This UI executes the existing Typer commands via `python -m gen_koc_mm ...`.\n"
            "- All uploads are copied into `input/` or `output/` with timestamped names.\n"
            "- Tab 3 uses Save As to preserve an audit trail."
        )

    return demo


if __name__ == "__main__":
    ui = build_ui()
    ui.launch()
