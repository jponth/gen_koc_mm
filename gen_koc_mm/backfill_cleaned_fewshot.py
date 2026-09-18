from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from .fewshot_db import (
    default_db_path,
    list_examples,
    list_examples_needing_cleaned_transcript,
    set_cleaned_transcript_text,
)
from .llm import generate_minutes, load_llm_config
from .prompting import transcript_2_sentence_system_prompt, transcript_2_sentence_user_prompt


LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class BackfillCleanedFewshotResult:
    db_path: Path
    model_name: str
    scanned: int
    updated: int
    skipped: int
    failed: int


def backfill_cleaned_transcripts(
    *,
    provider: str = "openai",
    model: Optional[str] = None,
    path: Path | None = None,
    only_missing: bool = True,
    limit: int | None = None,
    log_callback: Optional[LogCallback] = None,
) -> BackfillCleanedFewshotResult:
    load_dotenv(override=False)

    db_path = (path or default_db_path()).resolve()
    records = (
        list_examples_needing_cleaned_transcript(path=db_path)
        if only_missing
        else list_examples(path=db_path)
    )
    if limit is not None and limit > 0:
        records = records[:limit]

    if not records:
        return BackfillCleanedFewshotResult(
            db_path=db_path,
            model_name=(model or ""),
            scanned=0,
            updated=0,
            skipped=0,
            failed=0,
        )

    llm_cfg = load_llm_config(provider=provider, model=model)
    system_prompt = transcript_2_sentence_system_prompt()

    updated = 0
    skipped = 0
    failed = 0

    for idx, record in enumerate(records, start=1):
        raw_text = (record.transcript_text or "").strip()
        if not raw_text:
            skipped += 1
            if log_callback is not None:
                log_callback(f"[{idx}/{len(records)}] Skipped id={record.id}: transcript_text is empty.")
            continue

        if not only_missing and record.cleaned_transcript_text.strip():
            skipped += 1
            if log_callback is not None:
                log_callback(f"[{idx}/{len(records)}] Skipped id={record.id}: cleaned_transcript_text already present.")
            continue

        if log_callback is not None:
            log_callback(f"[{idx}/{len(records)}] Cleaning transcript for id={record.id} ({record.section_key}).")

        user_prompt = transcript_2_sentence_user_prompt(
            section_heading=record.section_key,
            section_transcript=raw_text,
        )

        try:
            cleaned_text = generate_minutes(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider=provider,
                model=llm_cfg.model,
            ).strip()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if log_callback is not None:
                log_callback(f"[{idx}/{len(records)}] Failed id={record.id}: {exc}")
            continue

        if not cleaned_text:
            failed += 1
            if log_callback is not None:
                log_callback(f"[{idx}/{len(records)}] Failed id={record.id}: cleaned transcript was empty.")
            continue

        set_cleaned_transcript_text(
            example_id=record.id,
            cleaned_transcript_text=cleaned_text,
            path=db_path,
        )
        updated += 1
        if log_callback is not None:
            log_callback(f"[{idx}/{len(records)}] Updated id={record.id}.")

    return BackfillCleanedFewshotResult(
        db_path=db_path,
        model_name=llm_cfg.model,
        scanned=len(records),
        updated=updated,
        skipped=skipped,
        failed=failed,
    )
