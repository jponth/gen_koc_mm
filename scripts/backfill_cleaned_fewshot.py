from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gen_koc_mm.backfill_cleaned_fewshot import backfill_cleaned_transcripts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill cleaned transcript text for SQLite few-shot examples.")
    parser.add_argument("--provider", default="openai", help="LLM provider (openai|ollama)")
    parser.add_argument("--model", default=None, help="Model name (provider-specific)")
    parser.add_argument("--db-path", type=Path, default=None, help="Few-shot SQLite path")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many rows")
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Rebuild cleaned transcript text even for rows that already have it.",
    )
    args = parser.parse_args()

    result = backfill_cleaned_transcripts(
        provider=args.provider,
        model=args.model,
        path=args.db_path,
        only_missing=not args.overwrite_existing,
        limit=args.limit,
        log_callback=print,
    )
    print(
        f"Backfill complete for {result.db_path} using model {result.model_name}. "
        f"Scanned={result.scanned} Updated={result.updated} Skipped={result.skipped} Failed={result.failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
