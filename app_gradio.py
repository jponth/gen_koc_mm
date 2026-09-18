"""Gradio UI for gen_koc_mm.

Local-only, upload-driven workflow:
  1) Audio -> transcript (Whisper CLI with live progress)
  2) Identify sections -> marked transcript (Typer)
  3) Review/edit boundaries -> save-as marked transcript (UI only)
  4) Generate minutes JSON from marked transcript (shared generator with live progress)
  5) Merge JSON into Word template (Typer)

Run:
  source .venv/bin/activate
  python app_gradio.py

This UI intentionally keeps shared gen_koc_mm logic as the source of truth.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
import re
from typing import Any, Optional, Tuple

import gradio as gr

from gen_koc_mm.fewshot_db import (
    FewshotExampleRow,
    default_db_path,
    delete_example,
    find_conflicts,
    get_example,
    get_last_meeting_date,
    list_examples,
    save_examples,
    set_last_meeting_date,
    update_example,
)
from gen_koc_mm.fewshot_docx import parse_minutes_docx
from gen_koc_mm.generation import generate_minutes_output
from gen_koc_mm.marked_transcript import parse_marked_transcript
from gen_koc_mm.sections import SECTION_DEFS
from gen_koc_mm.transcribe import transcribe_with_whisper_cli


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = REPO_ROOT / "input"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"
DEFAULT_TEMPLATE = REPO_ROOT / "templates" / "KoC-Meeting-Minutes-Template-v02.docx"
DEFAULT_FEWSHOT_DB = default_db_path()


def _next_available_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate

    n = 2
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _meeting_root_stem(path: Path) -> str:
    stem = path.stem.strip()
    patterns = [
        r"^(?P<root>.+)_transcript_marked_edited(?:_\d+)?$",
        r"^(?P<root>.+)_transcript_marked(?:_\d+)?$",
        r"^(?P<root>.+)_transcript(?:_\d+)?$",
        r"^(?P<root>.+)_output(?:_\d+)?$",
        r"^(?P<root>.+)_minutes(?:_\d+)?$",
        r"^(?P<root>.+)_\d{8}_\d{6}$",
        r"^marked_(?P<root>.+)_\d{8}_\d{6}$",
        r"^minutes_(?P<root>.+)_\d{8}_\d{6}$",
    ]
    for pat in patterns:
        m = re.match(pat, stem)
        if m:
            return m.group("root")
    return stem


def _derived_audio_copy_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_INPUT_DIR, _meeting_root_stem(src), src.suffix.lower())


def _derived_transcript_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_INPUT_DIR, f"{_meeting_root_stem(src)}_transcript", ".txt")


def _derived_marked_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_OUTPUT_DIR, f"{_meeting_root_stem(src)}_transcript_marked", ".txt")


def _derived_edited_marked_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_OUTPUT_DIR, f"{_meeting_root_stem(src)}_transcript_marked_edited", ".txt")


def _derived_output_json_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_OUTPUT_DIR, f"{_meeting_root_stem(src)}_output", ".json")


def _derived_minutes_docx_path(src: Path) -> Path:
    return _next_available_path(DEFAULT_OUTPUT_DIR, f"{_meeting_root_stem(src)}_minutes", ".docx")


def _today_iso() -> str:
    """Return today's date in YYYY-MM-DD (local time)."""
    return date.today().isoformat()


def _normalize_meeting_date_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).date().isoformat()

    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        text = text[:10]

    try:
        return date.fromisoformat(text).isoformat()
    except Exception:
        return text


def _default_meeting_date() -> str:
    saved = (get_last_meeting_date(path=DEFAULT_FEWSHOT_DB) or "").strip()
    return saved or _today_iso()


def ui_sync_meeting_date(meeting_date_value: Any):
    normalized = _normalize_meeting_date_value(meeting_date_value) or _default_meeting_date()
    set_last_meeting_date(normalized, path=DEFAULT_FEWSHOT_DB)
    return (
        normalized,
        gr.update(value=normalized),
        gr.update(value=normalized),
        gr.update(value=normalized),
        gr.update(value=normalized),
        gr.update(value=normalized),
    )


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


def _uploaded_items(upload_value) -> list[Any]:
    if upload_value is None:
        return []
    if isinstance(upload_value, list):
        return [item for item in upload_value if item is not None]
    return [upload_value]


def _progress_percent(current_bytes: int, total_bytes: int) -> int:
    total = max(int(total_bytes or 0), 1)
    current = max(0, min(int(current_bytes or 0), total))
    return max(0, min(100, math.floor((current * 100) / total)))


def _update_progress(progress: gr.Progress, *, current_bytes: int, total_bytes: int, label: str) -> None:
    percent = _progress_percent(current_bytes, total_bytes)
    progress(percent / 100, desc=f"{label}: {percent}/100")


def _single_path_for_next_tab(path_text: str) -> tuple[str, str]:
    paths = [line.strip() for line in (path_text or "").splitlines() if line.strip()]
    if len(paths) == 1:
        return paths[0], paths[0]
    return "", ""


def _default_review_state() -> dict[str, Any]:
    return {
        "source_transcript_path": "",
        "source_docx_path": "",
        "meeting_date": "",
        "transcript_sections": {},
        "word_sections": [],
        "assignments": {},
        "ignored_headings": [],
    }


def _default_pending_overwrite() -> dict[str, Any]:
    return {"rows": [], "conflicts": []}


def _default_browser_state() -> dict[str, Any]:
    return {
        "records": [],
        "selected_id": 0,
        "mode": "view",
        "pending_delete_id": 0,
    }


def _summarize_text(text: str, *, limit: int = 96) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _short_path(path_text: str) -> str:
    try:
        return Path(path_text).name or path_text
    except Exception:
        return path_text


def _browser_table_rows(records: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record["section_key"],
                record.get("meeting_date", ""),
                _short_path(record["source_transcript_path"]),
                record.get("updated_at", ""),
            ]
        )
    return rows


def _browser_record_dicts() -> list[dict[str, Any]]:
    return [asdict(record) for record in list_examples(path=DEFAULT_FEWSHOT_DB)]


def _browser_selected_record(browser_state: dict[str, Any]) -> Optional[dict[str, Any]]:
    selected_id = int((browser_state or {}).get("selected_id") or 0)
    if selected_id <= 0:
        return None
    record = get_example(selected_id, path=DEFAULT_FEWSHOT_DB)
    return asdict(record) if record else None


def _browser_detail_summary(record: Optional[dict[str, Any]], *, editable: bool):
    if not record:
        return gr.update(value="Select a row on the left to view or edit a few-shot example.")

    mode = "Edit" if editable else "View"
    return gr.update(
        value=(
            f"**{mode} mode**  \n"
            f"Section key: `{record['section_key']}`  \n"
            f"Transcript file: `{_short_path(record['source_transcript_path'])}`  \n"
            f"Word file: `{_short_path(record['source_docx_path'])}`"
        )
    )


def _browser_form_updates(record: Optional[dict[str, Any]], *, editable: bool):
    if not record:
        blank = gr.update(value="", interactive=editable)
        return (
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value="", interactive=editable, choices=[sec.key for sec in SECTION_DEFS]),
            blank,
            blank,
            blank,
            gr.update(value="", interactive=False),
            gr.update(value="", interactive=False),
            gr.update(value="", interactive=editable),
            gr.update(value="", interactive=editable),
            gr.update(value="", interactive=editable),
        )

    return (
        gr.update(value=str(record["id"])),
        gr.update(value="Edit" if editable else "View"),
        gr.update(value=record["section_key"], choices=[sec.key for sec in SECTION_DEFS], interactive=editable),
        gr.update(value=record.get("meeting_date", ""), interactive=editable),
        gr.update(value=record["source_transcript_path"], interactive=editable),
        gr.update(value=record["source_docx_path"], interactive=editable),
        gr.update(value=record.get("created_at", ""), interactive=False),
        gr.update(value=record.get("updated_at", ""), interactive=False),
        gr.update(value=record["transcript_text"], interactive=editable),
        gr.update(value=record.get("cleaned_transcript_text", ""), interactive=editable),
        gr.update(value=record["minutes_text"], interactive=editable),
    )


def _browser_action_updates(record: Optional[dict[str, Any]], *, editable: bool):
    selected = bool(record and record.get("id"))
    return (
        gr.update(interactive=selected),
        gr.update(interactive=selected),
        gr.update(interactive=selected),
        gr.update(interactive=selected and editable),
    )


def _browser_render(
    status: str,
    browser_state: Optional[dict[str, Any]],
    record: Optional[dict[str, Any]],
    *,
    editable: bool,
    delete_visible: bool = False,
    delete_message: str = "",
):
    state = dict(browser_state or _default_browser_state())
    state["records"] = state.get("records") or []
    return (
        status,
        state,
        _browser_table_rows(state["records"]),
        _browser_detail_summary(record, editable=editable),
        *_browser_form_updates(record, editable=editable),
        *_browser_action_updates(record, editable=editable),
        *_browser_delete_controls(visible=delete_visible, message=delete_message),
    )


def _browser_delete_controls(*, visible: bool, message: str):
    return (
        gr.update(value=message, visible=visible),
        gr.update(visible=visible, interactive=visible),
        gr.update(visible=visible, interactive=visible),
    )


def ui_load_browser_examples():
    records = _browser_record_dicts()
    state = _default_browser_state()
    state["records"] = records
    return _browser_render(
        f"Loaded {len(records)} few-shot example row(s) from `{DEFAULT_FEWSHOT_DB}`.",
        state,
        None,
        editable=False,
    )


def ui_browser_refresh():
    return ui_load_browser_examples()


def ui_browser_select_row(browser_state: dict[str, Any], evt: gr.SelectData):
    records = (browser_state or {}).get("records") or []
    index = evt.index
    if isinstance(index, (tuple, list)):
        row_idx = int(index[0]) if len(index) >= 1 else 0
    else:
        row_idx = int(index)

    if row_idx < 0 or row_idx >= len(records):
        return _browser_render(
            "Selected row is out of range.",
            browser_state or _default_browser_state(),
            None,
            editable=False,
        )

    record = records[row_idx]
    record_id = int(record["id"])
    live_record = get_example(record_id, path=DEFAULT_FEWSHOT_DB)
    if live_record is None:
        refreshed = _default_browser_state()
        refreshed["records"] = _browser_record_dicts()
        return _browser_render(
            f"Example id={record_id} no longer exists. Table refreshed.",
            refreshed,
            None,
            editable=False,
        )

    record_dict = asdict(live_record)
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["selected_id"] = record_id
    updated_state["records"] = _browser_record_dicts()
    updated_state["mode"] = "view"
    updated_state["pending_delete_id"] = 0
    return _browser_render(
        f"Viewing example id={record_id}.",
        updated_state,
        record_dict,
        editable=False,
    )


def ui_browser_view_selected(browser_state: dict[str, Any]):
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = _browser_record_dicts()
    updated_state["pending_delete_id"] = 0
    record = _browser_selected_record(updated_state)
    if not record:
        updated_state["selected_id"] = 0
        updated_state["mode"] = "view"
        return _browser_render("Select a row first.", updated_state, None, editable=False)

    updated_state["mode"] = "view"
    return _browser_render(
        f"Viewing example id={record['id']}.",
        updated_state,
        record,
        editable=False,
    )


def ui_browser_edit_selected(browser_state: dict[str, Any]):
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = _browser_record_dicts()
    updated_state["pending_delete_id"] = 0
    record = _browser_selected_record(updated_state)
    if not record:
        updated_state["selected_id"] = 0
        updated_state["mode"] = "view"
        return _browser_render("Select a row first.", updated_state, None, editable=False)

    updated_state["mode"] = "edit"
    return _browser_render(
        f"Editing example id={record['id']}.",
        updated_state,
        record,
        editable=True,
    )


def ui_browser_save(
    browser_state: dict[str, Any],
    example_id: str,
    section_key: str,
    meeting_date: str,
    source_transcript_path: str,
    source_docx_path: str,
    transcript_text: str,
    cleaned_transcript_text: str,
    minutes_text: str,
):
    if not example_id.strip():
        return _browser_render(
            "No example is selected for editing.",
            browser_state or _default_browser_state(),
            None,
            editable=False,
        )

    try:
        updated = update_example(
            example_id=int(example_id),
            section_key=section_key,
            meeting_date=meeting_date,
            source_transcript_path=source_transcript_path,
            source_docx_path=source_docx_path,
            transcript_text=transcript_text,
            cleaned_transcript_text=cleaned_transcript_text,
            minutes_text=minutes_text,
            path=DEFAULT_FEWSHOT_DB,
        )
    except Exception as exc:
        return _browser_render(
            f"Save failed: {exc}",
            browser_state or _default_browser_state(),
            {
                "id": int(example_id) if example_id.strip().isdigit() else "",
                "section_key": section_key,
                "meeting_date": meeting_date,
                "source_transcript_path": source_transcript_path,
                "source_docx_path": source_docx_path,
                "created_at": "",
                "updated_at": "",
                "transcript_text": transcript_text,
                "cleaned_transcript_text": cleaned_transcript_text,
                "minutes_text": minutes_text,
            },
            editable=True,
        )

    records = _browser_record_dicts()
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = records
    updated_state["selected_id"] = updated.id
    updated_state["mode"] = "edit"
    updated_state["pending_delete_id"] = 0

    return _browser_render(
        f"Saved example id={updated.id}.",
        updated_state,
        asdict(updated),
        editable=True,
    )


def ui_browser_delete_request(browser_state: dict[str, Any]):
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = _browser_record_dicts()
    record = _browser_selected_record(updated_state)
    if not record:
        updated_state["selected_id"] = 0
        updated_state["mode"] = "view"
        return _browser_render("Select a row first.", updated_state, None, editable=False)

    updated_state["pending_delete_id"] = int(record["id"])
    editable = updated_state.get("mode") == "edit"
    return _browser_render(
        f"Confirm deletion of example id={record['id']} ({record['section_key']}).",
        updated_state,
        record,
        editable=editable,
        delete_visible=True,
        delete_message=f"Delete example id `{record['id']}` for section `{record['section_key']}`?",
    )


def ui_browser_delete_cancel(browser_state: dict[str, Any]):
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = _browser_record_dicts()
    updated_state["pending_delete_id"] = 0
    record = _browser_selected_record(updated_state)
    editable = updated_state.get("mode") == "edit"
    return _browser_render(
        "Delete cancelled.",
        updated_state,
        record,
        editable=editable and bool(record),
    )


def ui_browser_delete_confirm(browser_state: dict[str, Any], example_id: str):
    pending_id = int((browser_state or {}).get("pending_delete_id") or 0)
    if pending_id <= 0 and (example_id or "").strip().isdigit():
        pending_id = int(example_id)
    if pending_id <= 0:
        return _browser_render(
            "There is no pending delete request.",
            browser_state or _default_browser_state(),
            None,
            editable=False,
        )

    deleted = delete_example(pending_id, path=DEFAULT_FEWSHOT_DB)
    records = _browser_record_dicts()
    updated_state = dict(browser_state or _default_browser_state())
    updated_state["records"] = records
    updated_state["selected_id"] = 0
    updated_state["mode"] = "view"
    updated_state["pending_delete_id"] = 0

    message = f"Deleted example id={pending_id}." if deleted else f"Example id={pending_id} was already gone."
    return _browser_render(message, updated_state, None, editable=False)


def _choice_label(word_section: dict[str, str]) -> str:
    return f"{word_section['source_id']} | {word_section['heading']}"


def _word_sections_by_id(review_state: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {row["source_id"]: row for row in review_state.get("word_sections") or []}


def _word_section_choices(review_state: dict[str, Any]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = [("(Unmatched)", "")]
    for row in review_state.get("word_sections") or []:
        choices.append((_choice_label(row), row["source_id"]))
    return choices


def _review_rows(review_state: dict[str, Any]) -> list[list[str]]:
    assignments = review_state.get("assignments") or {}
    transcript_sections = review_state.get("transcript_sections") or {}
    word_sections = _word_sections_by_id(review_state)
    rows: list[list[str]] = []

    for sec in SECTION_DEFS:
        transcript_text = (transcript_sections.get(sec.key) or "").strip()
        source_id = str(assignments.get(sec.key) or "")
        word_section = word_sections.get(source_id)
        minutes_text = (word_section or {}).get("minutes_text", "").strip()
        heading = (word_section or {}).get("heading", "")
        ready = "Yes" if transcript_text and minutes_text else "No"

        rows.append(
            [
                sec.key,
                heading,
                "Yes" if transcript_text else "No",
                str(len(transcript_text)),
                "No",
                "0",
                "Yes" if minutes_text else "No",
                str(len(minutes_text)),
                ready,
                _summarize_text(transcript_text),
                "",
                _summarize_text(minutes_text),
            ]
        )

    return rows


def _review_summary(review_state: dict[str, Any]) -> str:
    transcript_sections = review_state.get("transcript_sections") or {}
    word_sections = review_state.get("word_sections") or []
    assignments = review_state.get("assignments") or {}
    word_by_id = _word_sections_by_id(review_state)

    transcript_count = sum(1 for sec in SECTION_DEFS if (transcript_sections.get(sec.key) or "").strip())
    ready_count = 0
    for sec in SECTION_DEFS:
        source_id = str(assignments.get(sec.key) or "")
        minutes_text = (word_by_id.get(source_id) or {}).get("minutes_text", "").strip()
        transcript_text = (transcript_sections.get(sec.key) or "").strip()
        if transcript_text and minutes_text:
            ready_count += 1

    ignored = review_state.get("ignored_headings") or []
    lines = [
        f"- Transcript sections with content: {transcript_count}",
        f"- Word sections parsed: {len(word_sections)}",
        f"- Complete matched rows ready to save: {ready_count}",
        f"- SQLite path: `{DEFAULT_FEWSHOT_DB}`",
    ]
    if ignored:
        lines.append("- Ignored top-level Word headings: " + ", ".join(ignored[:6]))
    return "\n".join(lines)


def _select_default_section(review_state: dict[str, Any]) -> str:
    transcript_sections = review_state.get("transcript_sections") or {}
    assignments = review_state.get("assignments") or {}
    for sec in SECTION_DEFS:
        if (transcript_sections.get(sec.key) or "").strip() or assignments.get(sec.key):
            return sec.key
    return SECTION_DEFS[0].key


def _row_preview_bundle(review_state: dict[str, Any], section_key: str) -> tuple[Any, str, str, str, str]:
    choices = _word_section_choices(review_state)
    assignments = review_state.get("assignments") or {}
    transcript_sections = review_state.get("transcript_sections") or {}
    source_id = str(assignments.get(section_key) or "")
    word_section = _word_sections_by_id(review_state).get(source_id)
    heading = (word_section or {}).get("heading", "")
    minutes_text = (word_section or {}).get("minutes_text", "")
    transcript_text = transcript_sections.get(section_key, "")
    return (
        gr.update(choices=choices, value=source_id),
        heading,
        transcript_text,
        "",
        minutes_text,
    )


def _collect_complete_rows(review_state: dict[str, Any]) -> list[FewshotExampleRow]:
    rows: list[FewshotExampleRow] = []
    transcript_sections = review_state.get("transcript_sections") or {}
    word_sections = _word_sections_by_id(review_state)
    assignments = review_state.get("assignments") or {}
    source_transcript_path = str(review_state.get("source_transcript_path") or "")
    source_docx_path = str(review_state.get("source_docx_path") or "")
    meeting_date = str(review_state.get("meeting_date") or "")

    for sec in SECTION_DEFS:
        transcript_text = (transcript_sections.get(sec.key) or "").strip()
        source_id = str(assignments.get(sec.key) or "")
        minutes_text = ((word_sections.get(source_id) or {}).get("minutes_text") or "").strip()
        if not (transcript_text and minutes_text):
            continue
        rows.append(
            FewshotExampleRow(
                source_transcript_path=source_transcript_path,
                source_docx_path=source_docx_path,
                section_key=sec.key,
                transcript_text=transcript_text,
                minutes_text=minutes_text,
                meeting_date=meeting_date,
            )
        )
    return rows


def _serialize_pending_rows(rows: list[FewshotExampleRow]) -> list[dict[str, str]]:
    return [
        {
            "source_transcript_path": row.source_transcript_path,
            "source_docx_path": row.source_docx_path,
            "section_key": row.section_key,
            "transcript_text": row.transcript_text,
            "minutes_text": row.minutes_text,
            "meeting_date": row.meeting_date,
        }
        for row in rows
    ]


def _deserialize_pending_rows(rows: list[dict[str, str]]) -> list[FewshotExampleRow]:
    return [FewshotExampleRow(**row) for row in rows]


def ui_toggle_parse_match(marked_file, minutes_docx_file):
    enabled = marked_file is not None and minutes_docx_file is not None
    return gr.update(interactive=enabled)


def ui_parse_fewshot_examples(marked_file, minutes_docx_file):
    _ensure_dirs()
    if marked_file is None or minutes_docx_file is None:
        return (
            "Please upload both a marked transcript and a Word minutes file.",
            "(Nothing parsed yet.)",
            [],
            _default_review_state(),
            gr.update(value=SECTION_DEFS[0].key),
            gr.update(choices=[("(Unmatched)", "")], value=""),
            "",
            "",
            "",
            "",
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            [],
            "",
            _default_pending_overwrite(),
        )

    marked_src = Path(marked_file.name)
    docx_src = Path(minutes_docx_file.name)
    marked_dest = _next_available_path(
        DEFAULT_INPUT_DIR,
        f"{_meeting_root_stem(marked_src)}_transcript_marked",
        marked_src.suffix or ".txt",
    )
    docx_dest = _next_available_path(DEFAULT_INPUT_DIR, docx_src.stem, docx_src.suffix or ".docx")
    _copy_upload_to(marked_src.as_posix(), marked_dest)
    _copy_upload_to(docx_src.as_posix(), docx_dest)

    try:
        marked_text = marked_dest.read_text(encoding="utf-8")
        chunks = parse_marked_transcript(marked_text)
        transcript_sections = {
            sec.key: chunk.text.strip()
            for sec, chunk in zip(SECTION_DEFS, chunks, strict=False)
        }
        parsed_doc = parse_minutes_docx(docx_dest)
    except Exception as exc:
        return (
            f"Parse failed: {exc}",
            "(Nothing parsed yet.)",
            [],
            _default_review_state(),
            gr.update(value=SECTION_DEFS[0].key),
            gr.update(choices=[("(Unmatched)", "")], value=""),
            "",
            "",
            "",
            "",
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            [],
            "",
            _default_pending_overwrite(),
        )

    word_sections = [
        {
            "source_id": sec.source_id,
            "detected_key": sec.detected_key,
            "heading": sec.heading,
            "minutes_text": sec.minutes_text,
        }
        for sec in parsed_doc.sections
    ]

    assignments: dict[str, str] = {sec.key: "" for sec in SECTION_DEFS}
    for row in word_sections:
        target_key = row["detected_key"]
        if target_key in assignments and not assignments[target_key]:
            assignments[target_key] = row["source_id"]

    review_state = {
        "source_transcript_path": str(marked_dest),
        "source_docx_path": str(docx_dest),
        "meeting_date": parsed_doc.meeting_date,
        "transcript_sections": transcript_sections,
        "word_sections": word_sections,
        "assignments": assignments,
        "ignored_headings": parsed_doc.ignored_headings,
    }

    selected_key = _select_default_section(review_state)
    assigned_dropdown, heading_text, transcript_preview_text, cleaned_preview_text, minutes_preview_text = _row_preview_bundle(
        review_state, selected_key
    )

    return (
        f"Parsed transcript and Word minutes successfully.\nTranscript: {marked_dest}\nWord: {docx_dest}",
        _review_summary(review_state),
        _review_rows(review_state),
        review_state,
        gr.update(value=selected_key),
        assigned_dropdown,
        heading_text,
        transcript_preview_text,
        cleaned_preview_text,
        minutes_preview_text,
        gr.update(interactive=True),
        gr.update(interactive=True),
        gr.update(interactive=True),
        [],
        "",
        _default_pending_overwrite(),
    )


def ui_refresh_fewshot_review(review_state: dict[str, Any], selected_key: str):
    if not review_state or not review_state.get("source_transcript_path"):
        empty_state = _default_review_state()
        return (
            "Nothing loaded yet.",
            "(Nothing parsed yet.)",
            [],
            empty_state,
            gr.update(value=SECTION_DEFS[0].key),
            gr.update(choices=[("(Unmatched)", "")], value=""),
            "",
            "",
            "",
            "",
        )

    selected = selected_key if selected_key in {sec.key for sec in SECTION_DEFS} else _select_default_section(review_state)
    assigned_dropdown, heading_text, transcript_preview_text, cleaned_preview_text, minutes_preview_text = _row_preview_bundle(
        review_state, selected
    )
    return (
        "Review table refreshed.",
        _review_summary(review_state),
        _review_rows(review_state),
        review_state,
        gr.update(value=selected),
        assigned_dropdown,
        heading_text,
        transcript_preview_text,
        cleaned_preview_text,
        minutes_preview_text,
    )


def ui_apply_word_assignment(review_state: dict[str, Any], target_key: str, source_id: str):
    if not review_state or not review_state.get("source_transcript_path"):
        return ui_refresh_fewshot_review(_default_review_state(), target_key)

    assignments = dict(review_state.get("assignments") or {})
    old_target: str | None = None
    for key, current_source in assignments.items():
        if source_id and current_source == source_id:
            old_target = key
            break

    if old_target and old_target != target_key:
        assignments[old_target] = ""

    assignments[target_key] = source_id or ""
    updated_state = dict(review_state)
    updated_state["assignments"] = assignments

    assigned_dropdown, heading_text, transcript_preview_text, cleaned_preview_text, minutes_preview_text = _row_preview_bundle(
        updated_state, target_key
    )
    moved_msg = f" Moved it from `{old_target}`." if old_target and old_target != target_key else ""
    status = f"Assigned `{source_id or '(unmatched)'}` to `{target_key}`.{moved_msg}"

    return (
        status,
        _review_summary(updated_state),
        _review_rows(updated_state),
        updated_state,
        gr.update(value=target_key),
        assigned_dropdown,
        heading_text,
        transcript_preview_text,
        cleaned_preview_text,
        minutes_preview_text,
    )


def ui_preview_fewshot_row(review_state: dict[str, Any], selected_key: str):
    if not review_state or not review_state.get("source_transcript_path"):
        return gr.update(choices=[("(Unmatched)", "")], value=""), "", "", "", ""
    return _row_preview_bundle(review_state, selected_key)


def ui_save_fewshot_examples(review_state: dict[str, Any]):
    if not review_state or not review_state.get("source_transcript_path"):
        return (
            "Nothing loaded yet.",
            [],
            _default_pending_overwrite(),
            gr.update(interactive=False),
        )

    rows = _collect_complete_rows(review_state)
    if not rows:
        return (
            "Nothing to save yet. Only rows with both transcript text and minutes text are written to the DB.",
            [],
            _default_pending_overwrite(),
            gr.update(interactive=False),
        )

    conflicts = find_conflicts(rows, path=DEFAULT_FEWSHOT_DB)
    if conflicts:
        conflict_rows = [
            [row["section_key"], row["source_transcript_path"], row["source_docx_path"], row["updated_at"]]
            for row in conflicts
        ]
        pending = {
            "rows": _serialize_pending_rows(rows),
            "conflicts": conflicts,
        }
        return (
            "Duplicate rows found. Review the conflicts below, then click Confirm Overwrite to replace them.",
            conflict_rows,
            pending,
            gr.update(interactive=True),
        )

    result = save_examples(rows, overwrite=False, path=DEFAULT_FEWSHOT_DB)
    return (
        f"Saved {result['inserted']} row(s) to `{DEFAULT_FEWSHOT_DB}`.",
        [],
        _default_pending_overwrite(),
        gr.update(interactive=False),
    )


def ui_confirm_overwrite(pending_overwrite: dict[str, Any]):
    rows_raw = (pending_overwrite or {}).get("rows") or []
    if not rows_raw:
        return (
            "There is no pending overwrite request.",
            [],
            _default_pending_overwrite(),
            gr.update(interactive=False),
        )

    rows = _deserialize_pending_rows(rows_raw)
    result = save_examples(rows, overwrite=True, path=DEFAULT_FEWSHOT_DB)
    return (
        f"Overwrote {result['updated']} row(s) and inserted {result['inserted']} new row(s) in `{DEFAULT_FEWSHOT_DB}`.",
        [],
        _default_pending_overwrite(),
        gr.update(interactive=False),
    )


# -------------------------
# Tab 1: Transcribe
# -------------------------

def ui_transcribe(
    audio_file,  # gr.File returns a tempfile with .name
    whisper_model: str,
    language: str,
    progress=gr.Progress(),
) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (log, transcript_preview, transcript_path)."""
    _ensure_dirs()
    uploads = _uploaded_items(audio_file)
    if not uploads:
        return "Please upload one or more audio files.", None, None

    jobs: list[tuple[Path, Path, Path]] = []
    total_bytes = 0
    for upload in uploads:
        audio_src = Path(upload.name)
        copied_audio = _derived_audio_copy_path(audio_src)
        _copy_upload_to(audio_src.as_posix(), copied_audio)
        out_path = _derived_transcript_path(audio_src)
        jobs.append((audio_src, copied_audio, out_path))
        total_bytes += max(copied_audio.stat().st_size, 1)

    total_bytes = max(total_bytes, 1)
    _update_progress(progress, current_bytes=0, total_bytes=total_bytes, label="Audio -> Transcript")

    completed_bytes = 0
    output_paths: list[str] = []
    preview_texts: list[str] = []
    log_blocks: list[str] = []

    for idx, (audio_src, copied_audio, out_path) in enumerate(jobs, start=1):
        file_size = max(copied_audio.stat().st_size, 1)
        completed_before = completed_bytes
        try:
            res = transcribe_with_whisper_cli(
                input_audio=copied_audio,
                output_path=out_path,
                model=whisper_model.strip(),
                language=(language or "").strip(),
                output_format="txt",
                verbose=False,
                progress_callback=lambda current, total, current_idx=idx, base_bytes=completed_before: _update_progress(
                    progress,
                    current_bytes=base_bytes + current,
                    total_bytes=total_bytes,
                    label=f"Audio -> Transcript ({current_idx}/{len(jobs)})",
                ),
            )
        except Exception as exc:
            partial_log = "\n\n".join(log_blocks).strip()
            message = (
                f"Transcription failed on file {idx}/{len(jobs)} `{audio_src.name}`: {exc}"
                if not partial_log
                else partial_log + f"\n\nTranscription failed on file {idx}/{len(jobs)} `{audio_src.name}`: {exc}"
            )
            return message + "\n", None, None

        completed_bytes += file_size
        _update_progress(
            progress,
            current_bytes=completed_bytes,
            total_bytes=total_bytes,
            label=f"Audio -> Transcript ({idx}/{len(jobs)})",
        )

        log_parts = [
            f"## File {idx}/{len(jobs)}: {audio_src.name}",
            "$ whisper "
            f"{copied_audio} --model {whisper_model.strip()} --output_format txt --output_dir {out_path.parent} "
            f"--language {(language or '').strip()} --verbose False",
        ]
        if res.whisper_stdout.strip():
            log_parts.append("\n[stdout/stderr]\n" + res.whisper_stdout.strip())
        if res.whisper_stderr.strip():
            log_parts.append("\n[stderr]\n" + res.whisper_stderr.strip())
        log_parts.append("\n(exit code: 0)")
        log_blocks.append("\n".join(log_parts).strip())
        output_paths.append(str(out_path))

        try:
            preview_texts.append(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            preview_texts.append(f"(Transcribed OK, but failed to read output: {exc})")

    if len(preview_texts) == 1:
        preview = preview_texts[0]
    else:
        lines = [
            f"Created {len(output_paths)} transcript files.",
            "",
            "Saved paths:",
            *output_paths,
            "",
            f"Preview of first transcript: {Path(output_paths[0]).name}",
            "",
            preview_texts[0],
        ]
        preview = "\n".join(lines)

    return "\n\n".join(log_blocks).strip() + "\n", preview, "\n".join(output_paths)


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
        dest = _derived_transcript_path(src_path)
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif transcript_path_text:
        src_path = Path(transcript_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload a transcript file (txt) or provide a valid transcript path.", None, None

    out_path = _derived_marked_path(src_path)
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


def _editor_lines_for_text(marked_text: str) -> int:
    if not marked_text:
        return 14

    wrap_width = 110
    visible_lines = 0
    for line in marked_text.splitlines() or [""]:
        line_len = max(len(line), 1)
        visible_lines += max(1, math.ceil(line_len / wrap_width))

    return max(14, min(42, visible_lines + 2))


def _editor_update(marked_text: str):
    lines = _editor_lines_for_text(marked_text)
    return gr.update(value=marked_text, lines=lines, max_lines=lines)


def ui_resize_editor(marked_text: str):
    lines = _editor_lines_for_text(marked_text)
    return gr.update(lines=lines, max_lines=lines)


def ui_load_marked(marked_file, marked_path_text: str) -> Tuple[str, Any, Optional[str]]:
    """Load marked transcript into editor. Returns (status, editor_update, loaded_path)."""
    src_path: Optional[Path] = None
    if marked_file is not None:
        src_path = Path(marked_file.name)
        dest = _derived_marked_path(src_path)
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif marked_path_text:
        src_path = Path(marked_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload or provide the path to a marked transcript.", _editor_update(""), None

    txt = src_path.read_text(encoding="utf-8")
    return f"Loaded: {src_path}", _editor_update(txt), str(src_path)


def ui_save_marked_as(content: str, loaded_marked_path: str) -> Tuple[str, Optional[str]]:
    """Save edited marked transcript into output/. Returns (status, saved_path)."""
    _ensure_dirs()
    if not content or not content.strip():
        return "Nothing to save (editor is empty).", None

    src = Path(loaded_marked_path).expanduser().resolve() if loaded_marked_path else None
    if src is None:
        return "No marked transcript is loaded.", None
    out_path = _derived_edited_marked_path(src)
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
    transcript_model_override: str,
    minutes_model_override: str,
    debug_chunks: bool,
    fewshot_source: str,
    progress=gr.Progress(),
) -> Tuple[str, Optional[str], Optional[str]]:
    _ensure_dirs()

    src_path: Optional[Path] = None
    if marked_file is not None:
        src_path = Path(marked_file.name)
        dest = _derived_edited_marked_path(src_path)
        _copy_upload_to(src_path.as_posix(), dest)
        src_path = dest
    elif marked_path_text:
        src_path = Path(marked_path_text).expanduser().resolve()

    if src_path is None or not src_path.exists():
        return "Please upload/provide a marked transcript to generate JSON.", None, None

    out_path = _derived_output_json_path(src_path)

    args = [
        "generate",
        "--generate-output",
        "--input",
        str(src_path),
        "--output",
        str(out_path),
    ]
    date_str = _normalize_meeting_date_value(date_of_meeting)

    if date_str:
        args += ["--date-of-meeting", date_str]
    prov = (provider or "OpenAI").strip().lower()
    prov = "ollama" if "ollama" in prov else "openai"
    args += ["--provider", prov]

    if transcript_model_override and transcript_model_override.strip():
        args += ["--transcript-model", transcript_model_override.strip()]
    if minutes_model_override and minutes_model_override.strip():
        args += ["--minutes-model", minutes_model_override.strip()]
    if debug_chunks:
        args += ["--debug-chunks"]
    fewshot_source_norm = (fewshot_source or "sqlite").strip().lower()
    if fewshot_source_norm in {"sqlite/fts5 examples", "sqlite"}:
        fewshot_source_norm = "sqlite"
    elif fewshot_source_norm in {"fewshot examples", "folder"}:
        fewshot_source_norm = "folder"
    else:
        fewshot_source_norm = "sqlite"
    args += ["--fewshot-source", fewshot_source_norm]

    log_lines = [f"$ {sys.executable} -m gen_koc_mm " + " ".join(args)]
    total_bytes = max(src_path.stat().st_size, 1)
    _update_progress(progress, current_bytes=0, total_bytes=total_bytes, label="Generate JSON")

    def _log(message: str) -> None:
        log_lines.append(message)

    try:
        generate_minutes_output(
            input_path=src_path,
            output_path=out_path,
            date_of_meeting=date_str,
            provider=prov,
            transcript_model=transcript_model_override.strip() or None,
            minutes_model=minutes_model_override.strip() or None,
            debug_chunks=debug_chunks,
            fewshot_source=fewshot_source_norm,
            progress_callback=lambda current, total, message: _update_progress(
                progress,
                current_bytes=current,
                total_bytes=total,
                label=f"Generate JSON ({message})",
            ),
            log_callback=_log,
        )
    except Exception as exc:
        return "\n".join(log_lines + [f"Generation failed: {exc}"]).strip() + "\n", None, None

    _update_progress(progress, current_bytes=total_bytes, total_bytes=total_bytes, label="Generate JSON")
    preview = out_path.read_text(encoding="utf-8")
    return "\n".join(log_lines).strip() + "\n", preview, str(out_path)


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
        dest = _derived_output_json_path(json_path)
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
        dest = _next_available_path(DEFAULT_OUTPUT_DIR, template_path.stem, template_path.suffix or ".docx")
        _copy_upload_to(template_path.as_posix(), dest)
        template_path = dest

    if template_path is None or not template_path.exists():
        return "Please select the default template or upload a DOCX template.", None

    out_docx = _derived_minutes_docx_path(json_path)

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
    initial_meeting_date = _default_meeting_date()
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
            "Local-only UI for the workflow, including few-shot example ingestion. Upload-only (no file picking).\n\n"
            "Tip: derived files keep the original meeting filename first; Tab 3 uses Save As for auditability."
        )

        # Shared state outputs
        st_browser = gr.State(value=_default_browser_state())
        st_fewshot_review = gr.State(value=_default_review_state())
        st_pending_overwrite = gr.State(value=_default_pending_overwrite())
        st_transcript_path = gr.State(value="")
        st_marked_path = gr.State(value="")
        st_edited_marked_path = gr.State(value="")
        st_minutes_json_path = gr.State(value="")
        st_meeting_date = gr.State(value=initial_meeting_date)

        with gr.Tabs():
            with gr.Tab("View Fewshot Examples"):
                browser_status = gr.Textbox(label="Status", lines=2)
                with gr.Row():
                    with gr.Column(scale=2, min_width=420):
                        gr.Markdown("Select a row to load its details.")
                        refresh_browser_btn = gr.Button("Refresh Table")
                        browser_table = gr.Dataframe(
                            headers=[
                                "Section Key",
                                "Meeting Date",
                                "Transcript File",
                                "Updated At",
                            ],
                            datatype=["str"] * 4,
                            interactive=False,
                            wrap=True,
                            value=[],
                            label="Few-shot examples",
                        )

                    with gr.Column(scale=3, min_width=520):
                        browser_detail_title = gr.Markdown("Select a row on the left to view or edit a few-shot example.")

                        with gr.Row():
                            browser_view_btn = gr.Button("View", interactive=False)
                            browser_edit_btn = gr.Button("Edit", interactive=False)
                            browser_delete_btn = gr.Button("Delete", variant="stop", interactive=False)
                            browser_save_btn = gr.Button("Save", interactive=False)

                        with gr.Row():
                            browser_delete_text = gr.Markdown("", visible=False)
                            browser_delete_confirm_btn = gr.Button(
                                "Confirm Delete",
                                variant="stop",
                                visible=False,
                                interactive=False,
                            )
                            browser_delete_cancel_btn = gr.Button("Cancel", visible=False, interactive=False)

                        with gr.Row():
                            browser_id = gr.Textbox(label="ID", interactive=False, scale=1)
                            browser_mode = gr.Textbox(label="Mode", interactive=False, scale=1)
                            browser_section_key = gr.Dropdown(
                                choices=[sec.key for sec in SECTION_DEFS],
                                label="Section key",
                                interactive=False,
                                scale=2,
                            )
                            browser_meeting_date = gr.Textbox(label="Meeting date", interactive=False, scale=1)

                        with gr.Row():
                            browser_source_transcript_path = gr.Textbox(label="Source transcript path", interactive=False)
                            browser_source_docx_path = gr.Textbox(label="Source Word path", interactive=False)

                        with gr.Row():
                            browser_created_at = gr.Textbox(label="Created at", interactive=False)
                            browser_updated_at = gr.Textbox(label="Updated at", interactive=False)

                        browser_transcript_text = gr.Textbox(label="Transcript text", lines=12, interactive=False)
                        browser_cleaned_transcript_text = gr.Textbox(
                            label="Cleaned transcript text",
                            lines=12,
                            interactive=False,
                        )
                        browser_minutes_text = gr.Textbox(label="Minutes text", lines=12, interactive=False)

            with gr.Tab("Populate Fewshot Examples"):
                with gr.Row():
                    marked_fewshot_upload = gr.File(
                        label="Upload marked transcript (.txt)",
                        elem_classes=["compact-upload"],
                    )
                    minutes_docx_upload = gr.File(
                        label="Upload final minutes (.docx)",
                        elem_classes=["compact-upload"],
                    )

                parse_match_btn = gr.Button("Parse & Match", interactive=False)
                fewshot_status = gr.Textbox(label="Status", lines=3)
                fewshot_summary = gr.Markdown(value="(Nothing parsed yet.)")
                fewshot_table = gr.Dataframe(
                    headers=[
                        "Section Key",
                        "Assigned Word Heading",
                        "Transcript?",
                        "Transcript Chars",
                        "Cleaned?",
                        "Cleaned Chars",
                        "Minutes?",
                        "Minutes Chars",
                        "Ready",
                        "Transcript Preview",
                        "Cleaned Preview",
                        "Minutes Preview",
                    ],
                    datatype=["str"] * 12,
                    interactive=False,
                    wrap=True,
                    row_count=(len(SECTION_DEFS), "fixed"),
                    column_count=(12, "fixed"),
                    value=[],
                    label="Review table",
                )

                with gr.Row():
                    review_section_key = gr.Dropdown(
                        choices=[sec.key for sec in SECTION_DEFS],
                        value=SECTION_DEFS[0].key,
                        label="Canonical section key",
                    )
                    assigned_word_section = gr.Dropdown(
                        choices=[("(Unmatched)", "")],
                        value="",
                        label="Assigned Word section",
                    )

                with gr.Row():
                    apply_assignment_btn = gr.Button("Apply Assignment", interactive=False)
                    refresh_fewshot_btn = gr.Button("Refresh Table", interactive=False)

                assigned_word_heading = gr.Textbox(label="Assigned Word heading", interactive=False)
                with gr.Row():
                    transcript_preview = gr.Textbox(label="Transcript text", lines=14)
                    cleaned_transcript_preview = gr.Textbox(label="Cleaned transcript text", lines=14, interactive=False)
                    minutes_preview = gr.Textbox(label="Minutes text", lines=14)

                with gr.Row():
                    save_fewshot_btn = gr.Button("Save to DB", interactive=False)
                    confirm_overwrite_btn = gr.Button("Confirm Overwrite", interactive=False)

                fewshot_save_status = gr.Textbox(label="Save status", lines=4)
                fewshot_conflicts = gr.Dataframe(
                    headers=["Section Key", "Transcript Path", "Existing DOCX Path", "Existing Updated At"],
                    datatype=["str"] * 4,
                    interactive=False,
                    wrap=True,
                    value=[],
                    label="Duplicate conflicts",
                )

            # 1) Transcribe
            with gr.Tab("1) Audio → Transcript"):
                meeting_date_tab1 = gr.Textbox(
                    value=initial_meeting_date,
                    label="Meeting date (YYYY-MM-DD)",
                )
                audio = gr.File(label="Upload audio file(s) (m4a/mp3/wav/mp4)", file_count="multiple", elem_classes=["compact-upload"])
                with gr.Accordion("Advanced options", open=True):
                    whisper_model = gr.Textbox(value="medium", label="Whisper model")
                    language = gr.Textbox(value="en", label="Language (e.g., en). Leave blank for auto-detect")

                run_btn = gr.Button("Transcribe")
                out_path = gr.Textbox(label="Transcript path(s) (saved)", interactive=False)
                preview = gr.Textbox(label="Transcript preview", lines=18)
                log = gr.Textbox(label="Command log", lines=10)

            # 2) Identify sections
            with gr.Tab("2) Identify Sections"):
                meeting_date_tab2 = gr.Textbox(
                    value=initial_meeting_date,
                    label="Meeting date (YYYY-MM-DD)",
                )
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
                meeting_date_tab3 = gr.Textbox(
                    value=initial_meeting_date,
                    label="Meeting date (YYYY-MM-DD)",
                )
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
                            lines=14,
                            max_lines=42,
                            show_line_numbers=True,
                            wrap_lines=True,
                        )

                with gr.Row():
                    save_base = gr.Textbox(
                        value="Derived automatically from the loaded transcript",
                        label="Edited filename",
                        interactive=False,
                    )
                    save_btn = gr.Button("Save As")

                saved_path = gr.Textbox(label="Saved edited marked transcript path", interactive=False)

            # 4) Generate JSON
            with gr.Tab("4) Generate JSON"):
                meeting_date_tab4 = gr.Textbox(
                    value=initial_meeting_date,
                    label="Meeting date (YYYY-MM-DD)",
                )
                marked_upload4 = gr.File(label="Upload edited marked transcript (.txt)", elem_classes=["compact-upload"])
                marked_path_echo4 = gr.Textbox(label="Or use edited marked transcript from Tab 3 (path)", interactive=False)

                with gr.Accordion("Advanced options", open=True):
                    provider = gr.Dropdown(
                        choices=["OpenAI", "Ollama Local"],
                        value="OpenAI",
                        label="LLM provider",
                    )
                    fewshot_source = gr.Dropdown(
                        choices=["sqlite/fts5 Examples", "fewshot Examples"],
                        value="sqlite/fts5 Examples",
                        label="Few-shot source",
                    )
                    transcript_model_override = gr.Textbox(
                        value="gpt-5.4-nano",
                        label="Model - Transcript to sentence",
                    )
                    minutes_model_override = gr.Textbox(
                        value="gpt-5.4-mini",
                        label="Model - Generate minutes",
                    )
                    debug_chunks = gr.Checkbox(value=True, label="Write debug chunks")

                run_btn4 = gr.Button("Generate minutes JSON")
                json_path = gr.Textbox(label="Minutes JSON path (saved)", interactive=False)
                json_preview = gr.Textbox(label="Minutes JSON preview", lines=18)
                log4 = gr.Textbox(label="Command log", lines=10)

            # 5) Merge DOCX
            with gr.Tab("5) Merge to Word"):
                meeting_date_tab5 = gr.Textbox(
                    value=initial_meeting_date,
                    label="Meeting date (YYYY-MM-DD)",
                )
                minutes_json_upload = gr.File(label="Upload minutes JSON (.json)", elem_classes=["compact-upload"])
                minutes_json_path_echo = gr.Textbox(label="Or use JSON from Tab 4 (path)", interactive=False)

                use_default_template = gr.Checkbox(value=True, label=f"Use default template ({DEFAULT_TEMPLATE.name})")
                template_upload = gr.File(label="Upload a DOCX template (optional if using default)", elem_classes=["compact-upload"])

                run_btn5 = gr.Button("Merge DOCX")
                out_docx_path = gr.Textbox(label="Output DOCX path (saved)", interactive=False)
                log5 = gr.Textbox(label="Command log", lines=10)

            # --- Wiring (cross-tab propagation) ---
            demo.load(
                ui_load_browser_examples,
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            refresh_browser_btn.click(
                ui_browser_refresh,
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_table.select(
                ui_browser_select_row,
                inputs=[st_browser],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_view_btn.click(
                ui_browser_view_selected,
                inputs=[st_browser],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_edit_btn.click(
                ui_browser_edit_selected,
                inputs=[st_browser],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_delete_btn.click(
                ui_browser_delete_request,
                inputs=[st_browser],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_save_btn.click(
                ui_browser_save,
                inputs=[
                    st_browser,
                    browser_id,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                ],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_delete_confirm_btn.click(
                ui_browser_delete_confirm,
                inputs=[st_browser, browser_id],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            browser_delete_cancel_btn.click(
                ui_browser_delete_cancel,
                inputs=[st_browser],
                outputs=[
                    browser_status,
                    st_browser,
                    browser_table,
                    browser_detail_title,
                    browser_id,
                    browser_mode,
                    browser_section_key,
                    browser_meeting_date,
                    browser_source_transcript_path,
                    browser_source_docx_path,
                    browser_created_at,
                    browser_updated_at,
                    browser_transcript_text,
                    browser_cleaned_transcript_text,
                    browser_minutes_text,
                    browser_view_btn,
                    browser_edit_btn,
                    browser_delete_btn,
                    browser_save_btn,
                    browser_delete_text,
                    browser_delete_confirm_btn,
                    browser_delete_cancel_btn,
                ],
            )

            marked_fewshot_upload.change(
                ui_toggle_parse_match,
                inputs=[marked_fewshot_upload, minutes_docx_upload],
                outputs=[parse_match_btn],
            )
            minutes_docx_upload.change(
                ui_toggle_parse_match,
                inputs=[marked_fewshot_upload, minutes_docx_upload],
                outputs=[parse_match_btn],
            )

            parse_match_btn.click(
                ui_parse_fewshot_examples,
                inputs=[marked_fewshot_upload, minutes_docx_upload],
                outputs=[
                    fewshot_status,
                    fewshot_summary,
                    fewshot_table,
                    st_fewshot_review,
                    review_section_key,
                    assigned_word_section,
                    assigned_word_heading,
                    transcript_preview,
                    cleaned_transcript_preview,
                    minutes_preview,
                    apply_assignment_btn,
                    refresh_fewshot_btn,
                    save_fewshot_btn,
                    fewshot_conflicts,
                    fewshot_save_status,
                    st_pending_overwrite,
                ],
            ).then(
                lambda: gr.update(interactive=False),
                outputs=[confirm_overwrite_btn],
            )

            review_section_key.change(
                ui_preview_fewshot_row,
                inputs=[st_fewshot_review, review_section_key],
                outputs=[assigned_word_section, assigned_word_heading, transcript_preview, cleaned_transcript_preview, minutes_preview],
            )

            apply_assignment_btn.click(
                ui_apply_word_assignment,
                inputs=[st_fewshot_review, review_section_key, assigned_word_section],
                outputs=[
                    fewshot_status,
                    fewshot_summary,
                    fewshot_table,
                    st_fewshot_review,
                    review_section_key,
                    assigned_word_section,
                    assigned_word_heading,
                    transcript_preview,
                    cleaned_transcript_preview,
                    minutes_preview,
                ],
            )

            refresh_fewshot_btn.click(
                ui_refresh_fewshot_review,
                inputs=[st_fewshot_review, review_section_key],
                outputs=[
                    fewshot_status,
                    fewshot_summary,
                    fewshot_table,
                    st_fewshot_review,
                    review_section_key,
                    assigned_word_section,
                    assigned_word_heading,
                    transcript_preview,
                    cleaned_transcript_preview,
                    minutes_preview,
                ],
            )

            save_fewshot_btn.click(
                ui_save_fewshot_examples,
                inputs=[st_fewshot_review],
                outputs=[
                    fewshot_save_status,
                    fewshot_conflicts,
                    st_pending_overwrite,
                    confirm_overwrite_btn,
                ],
            )

            confirm_overwrite_btn.click(
                ui_confirm_overwrite,
                inputs=[st_pending_overwrite],
                outputs=[
                    fewshot_save_status,
                    fewshot_conflicts,
                    st_pending_overwrite,
                    confirm_overwrite_btn,
                ],
            )

            for meeting_input in [
                meeting_date_tab1,
                meeting_date_tab2,
                meeting_date_tab3,
                meeting_date_tab4,
                meeting_date_tab5,
            ]:
                meeting_input.change(
                    ui_sync_meeting_date,
                    inputs=[meeting_input],
                    outputs=[
                        st_meeting_date,
                        meeting_date_tab1,
                        meeting_date_tab2,
                        meeting_date_tab3,
                        meeting_date_tab4,
                        meeting_date_tab5,
                    ],
                )

            run_btn.click(
                ui_transcribe,
                inputs=[audio, whisper_model, language],
                outputs=[log, preview, out_path],
            ).then(
                ui_sync_meeting_date,
                inputs=[meeting_date_tab1],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
            ).then(
                _single_path_for_next_tab,
                inputs=[out_path],
                outputs=[st_transcript_path, transcript_path_echo],
            )

            run_btn2.click(
                ui_identify_sections,
                inputs=[transcript_upload, transcript_path_echo],
                outputs=[log2, marked_preview, marked_path],
            ).then(
                ui_sync_meeting_date,
                inputs=[meeting_date_tab2],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
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
                ui_sync_meeting_date,
                inputs=[meeting_date_tab3],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
            ).then(
                lambda p: str(_derived_edited_marked_path(Path(p).expanduser().resolve()).name) if p else "",
                inputs=[marked_path_echo],
                outputs=[save_base],
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

            editor.change(
                ui_resize_editor,
                inputs=[editor],
                outputs=[editor],
            )

            # Manual refresh button (in case the live-update misses an event).
            refresh_bounds_btn.click(
                _extract_boundary_index,
                inputs=[editor],
                outputs=[boundary_index],
            )

            save_btn.click(
                ui_save_marked_as,
                inputs=[editor, marked_path_echo],
                outputs=[status3, saved_path],
            ).then(
                ui_sync_meeting_date,
                inputs=[meeting_date_tab3],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[saved_path],
                outputs=[st_edited_marked_path, marked_path_echo4],
            )

            # When switching providers, set a sensible default model.
            provider.change(
                lambda p: (
                    ("gpt-5.4-nano", "gpt-5.4-mini")
                    if p == "OpenAI"
                    else ("gpt-oss:20b", "gpt-oss:20b")
                ),
                inputs=[provider],
                outputs=[transcript_model_override, minutes_model_override],
            )

            run_btn4.click(
                ui_generate_json,
                inputs=[
                    marked_upload4,
                    marked_path_echo4,
                    meeting_date_tab4,
                    provider,
                    transcript_model_override,
                    minutes_model_override,
                    debug_chunks,
                    fewshot_source,
                ],
                outputs=[log4, json_preview, json_path],
            ).then(
                ui_sync_meeting_date,
                inputs=[meeting_date_tab4],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
            ).then(
                lambda p: (p or "", p or ""),
                inputs=[json_path],
                outputs=[st_minutes_json_path, minutes_json_path_echo],
            )

            run_btn5.click(
                ui_merge_docx,
                inputs=[minutes_json_upload, minutes_json_path_echo, template_upload, use_default_template],
                outputs=[log5, out_docx_path],
            ).then(
                ui_sync_meeting_date,
                inputs=[meeting_date_tab5],
                outputs=[
                    st_meeting_date,
                    meeting_date_tab1,
                    meeting_date_tab2,
                    meeting_date_tab3,
                    meeting_date_tab4,
                    meeting_date_tab5,
                ],
            )

        gr.Markdown(
            "---\n"
            "Notes:\n"
            "- This UI reuses the existing `gen_koc_mm` logic; long-running tabs show live progress in Gradio.\n"
            "- Derived files keep the original meeting filename first and add `_2`, `_3`, etc only when needed.\n"
            "- Tab 3 uses Save As to preserve an audit trail.\n"
            f"- Few-shot examples are stored locally in `{DEFAULT_FEWSHOT_DB.name}`."
        )

    return demo


if __name__ == "__main__":
    ui = build_ui()
    ui.launch()
