from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FewshotExampleRow:
    source_transcript_path: str
    source_docx_path: str
    section_key: str
    transcript_text: str
    minutes_text: str
    meeting_date: str = ""


@dataclass(frozen=True)
class RetrievedFewshotExample:
    section_key: str
    transcript_text: str
    minutes_text: str
    source_transcript_path: str
    source_docx_path: str
    meeting_date: str
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    score: float | None = None


@dataclass(frozen=True)
class FewshotExampleRecord:
    id: int
    source_transcript_path: str
    source_docx_path: str
    section_key: str
    transcript_text: str
    minutes_text: str
    meeting_date: str
    created_at: str
    updated_at: str


_STOP_TOKENS = {
    "speaker",
    "unknown",
    "okay",
    "yeah",
    "just",
    "this",
    "that",
    "with",
    "from",
    "into",
    "there",
    "have",
    "will",
    "would",
    "about",
    "they",
    "them",
    "their",
    "your",
    "ours",
    "ourselves",
}


def default_db_path() -> Path:
    override = os.environ.get("GEN_KOC_MM_FEWSHOT_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "fewshot_examples.sqlite"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_db(path: Path | None = None) -> sqlite3.Connection:
    db_path = (path or default_db_path()).resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db(path: Path | None = None) -> Path:
    db_path = (path or default_db_path()).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fewshot_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_transcript_path TEXT NOT NULL,
                source_docx_path TEXT NOT NULL,
                section_key TEXT NOT NULL,
                transcript_text TEXT NOT NULL,
                minutes_text TEXT NOT NULL,
                meeting_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_transcript_path, section_key)
            )
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS fewshot_examples_fts
                USING fts5(
                    section_key UNINDEXED,
                    transcript_text,
                    minutes_text,
                    content='fewshot_examples',
                    content_rowid='id'
                )
                """
            )
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "SQLite FTS5 is not available in this Python/SQLite build."
            ) from exc

        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS fewshot_examples_ai AFTER INSERT ON fewshot_examples BEGIN
                INSERT INTO fewshot_examples_fts(rowid, section_key, transcript_text, minutes_text)
                VALUES (new.id, new.section_key, new.transcript_text, new.minutes_text);
            END;

            CREATE TRIGGER IF NOT EXISTS fewshot_examples_ad AFTER DELETE ON fewshot_examples BEGIN
                INSERT INTO fewshot_examples_fts(fewshot_examples_fts, rowid, section_key, transcript_text, minutes_text)
                VALUES('delete', old.id, old.section_key, old.transcript_text, old.minutes_text);
            END;

            CREATE TRIGGER IF NOT EXISTS fewshot_examples_au AFTER UPDATE ON fewshot_examples BEGIN
                INSERT INTO fewshot_examples_fts(fewshot_examples_fts, rowid, section_key, transcript_text, minutes_text)
                VALUES('delete', old.id, old.section_key, old.transcript_text, old.minutes_text);
                INSERT INTO fewshot_examples_fts(rowid, section_key, transcript_text, minutes_text)
                VALUES (new.id, new.section_key, new.transcript_text, new.minutes_text);
            END;
            """
        )
    return db_path


def count_examples(path: Path | None = None) -> int:
    db_path = (path or default_db_path()).resolve()
    if not db_path.exists():
        return 0

    ensure_db(db_path)
    with connect_db(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM fewshot_examples").fetchone()
        return int(row["n"]) if row else 0


def load_all_examples(path: Path | None = None) -> list[RetrievedFewshotExample]:
    db_path = (path or default_db_path()).resolve()
    if not db_path.exists():
        return []

    ensure_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                section_key,
                transcript_text,
                minutes_text,
                source_transcript_path,
                source_docx_path,
                meeting_date,
                created_at,
                updated_at
            FROM fewshot_examples
            ORDER BY section_key, updated_at DESC, id DESC
            """
        ).fetchall()
        return [
            RetrievedFewshotExample(
                id=row["id"],
                section_key=row["section_key"],
                transcript_text=row["transcript_text"],
                minutes_text=row["minutes_text"],
                source_transcript_path=row["source_transcript_path"],
                source_docx_path=row["source_docx_path"],
                meeting_date=row["meeting_date"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]


def list_examples(path: Path | None = None) -> list[FewshotExampleRecord]:
    db_path = (path or default_db_path()).resolve()
    if not db_path.exists():
        return []

    ensure_db(db_path)
    with connect_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                source_transcript_path,
                source_docx_path,
                section_key,
                transcript_text,
                minutes_text,
                meeting_date,
                created_at,
                updated_at
            FROM fewshot_examples
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        return [FewshotExampleRecord(**dict(row)) for row in rows]


def get_example(example_id: int, path: Path | None = None) -> FewshotExampleRecord | None:
    ensure_db(path)
    with connect_db(path) as conn:
        row = conn.execute(
            """
            SELECT
                id,
                source_transcript_path,
                source_docx_path,
                section_key,
                transcript_text,
                minutes_text,
                meeting_date,
                created_at,
                updated_at
            FROM fewshot_examples
            WHERE id = ?
            """,
            (example_id,),
        ).fetchone()
        return FewshotExampleRecord(**dict(row)) if row else None


def update_example(
    *,
    example_id: int,
    source_transcript_path: str,
    source_docx_path: str,
    section_key: str,
    transcript_text: str,
    minutes_text: str,
    meeting_date: str = "",
    path: Path | None = None,
) -> FewshotExampleRecord:
    ensure_db(path)

    source_transcript_path = (source_transcript_path or "").strip()
    source_docx_path = (source_docx_path or "").strip()
    section_key = (section_key or "").strip()
    transcript_text = (transcript_text or "").strip()
    minutes_text = (minutes_text or "").strip()
    meeting_date = (meeting_date or "").strip()

    if not source_transcript_path:
        raise ValueError("source_transcript_path cannot be empty")
    if not source_docx_path:
        raise ValueError("source_docx_path cannot be empty")
    if not section_key:
        raise ValueError("section_key cannot be empty")
    if not transcript_text:
        raise ValueError("transcript_text cannot be empty")
    if not minutes_text:
        raise ValueError("minutes_text cannot be empty")

    now = _utc_now()
    with connect_db(path) as conn:
        try:
            cur = conn.execute(
                """
                UPDATE fewshot_examples
                SET source_transcript_path = ?,
                    source_docx_path = ?,
                    section_key = ?,
                    transcript_text = ?,
                    minutes_text = ?,
                    meeting_date = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    source_transcript_path,
                    source_docx_path,
                    section_key,
                    transcript_text,
                    minutes_text,
                    meeting_date,
                    now,
                    example_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Updating this row would violate the unique (source_transcript_path, section_key) constraint."
            ) from exc

        if cur.rowcount == 0:
            raise ValueError(f"Example id={example_id} was not found.")

    record = get_example(example_id, path=path)
    if record is None:
        raise ValueError(f"Example id={example_id} was not found after update.")
    return record


def delete_example(example_id: int, path: Path | None = None) -> bool:
    ensure_db(path)
    with connect_db(path) as conn:
        cur = conn.execute("DELETE FROM fewshot_examples WHERE id = ?", (example_id,))
        return cur.rowcount > 0


def record_to_dict(record: FewshotExampleRecord) -> dict[str, str | int]:
    return asdict(record)


def _query_tokens(text: str, *, limit: int = 24) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []

    for raw in re.findall(r"[A-Za-z0-9]+", (text or "").lower()):
        if raw in seen:
            continue
        if raw in _STOP_TOKENS:
            continue
        if len(raw) < 3 and not raw.isdigit():
            continue
        seen.add(raw)
        tokens.append(raw)
        if len(tokens) >= limit:
            break

    return tokens


def _build_match_query(text: str) -> str:
    tokens = _query_tokens(text)
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens)


def retrieve_examples(
    *,
    section_key: str,
    query_text: str,
    limit: int = 2,
    path: Path | None = None,
) -> list[RetrievedFewshotExample]:
    db_path = (path or default_db_path()).resolve()
    if not db_path.exists():
        return []

    ensure_db(db_path)
    match_query = _build_match_query(query_text)

    with connect_db(db_path) as conn:
        rows = []
        if match_query:
            rows = conn.execute(
                """
                SELECT
                    e.section_key,
                    e.transcript_text,
                    e.minutes_text,
                    e.source_transcript_path,
                    e.source_docx_path,
                    e.meeting_date,
                    bm25(fewshot_examples_fts) AS score
                FROM fewshot_examples_fts
                JOIN fewshot_examples AS e
                  ON e.id = fewshot_examples_fts.rowid
                WHERE e.section_key = ?
                  AND fewshot_examples_fts MATCH ?
                ORDER BY score ASC, e.updated_at DESC, e.id DESC
                LIMIT ?
                """,
                (section_key, match_query, limit),
            ).fetchall()

        if not rows:
            rows = conn.execute(
                """
                SELECT
                    section_key,
                    transcript_text,
                    minutes_text,
                    source_transcript_path,
                    source_docx_path,
                    meeting_date,
                    NULL AS score
                FROM fewshot_examples
                WHERE section_key = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (section_key, limit),
            ).fetchall()

    return [
        RetrievedFewshotExample(
            section_key=row["section_key"],
            transcript_text=row["transcript_text"],
            minutes_text=row["minutes_text"],
            source_transcript_path=row["source_transcript_path"],
            source_docx_path=row["source_docx_path"],
            meeting_date=row["meeting_date"],
            score=float(row["score"]) if row["score"] is not None else None,
        )
        for row in rows
    ]


def find_conflicts(
    rows: Iterable[FewshotExampleRow],
    *,
    path: Path | None = None,
) -> list[dict[str, str]]:
    ensure_db(path)
    rows_list = list(rows)
    if not rows_list:
        return []

    wanted = [(row.source_transcript_path, row.section_key) for row in rows_list]
    placeholders = ", ".join(["(?, ?)"] * len(wanted))
    args: list[str] = []
    for source_path, section_key in wanted:
        args.extend([source_path, section_key])

    with connect_db(path) as conn:
        cur = conn.execute(
            f"""
            SELECT source_transcript_path, section_key, source_docx_path, updated_at
            FROM fewshot_examples
            WHERE (source_transcript_path, section_key) IN ({placeholders})
            ORDER BY section_key
            """,
            args,
        )
        return [dict(row) for row in cur.fetchall()]


def save_examples(
    rows: Iterable[FewshotExampleRow],
    *,
    overwrite: bool = False,
    path: Path | None = None,
) -> dict[str, int]:
    db_path = ensure_db(path)
    rows_list = list(rows)
    if not rows_list:
        return {"inserted": 0, "updated": 0}

    now = _utc_now()
    inserted = 0
    updated = 0

    with connect_db(db_path) as conn:
        for row in rows_list:
            existing = conn.execute(
                """
                SELECT id
                FROM fewshot_examples
                WHERE source_transcript_path = ? AND section_key = ?
                """,
                (row.source_transcript_path, row.section_key),
            ).fetchone()

            if existing:
                if not overwrite:
                    raise ValueError(
                        f"Duplicate row exists for source_transcript_path={row.source_transcript_path!r}, "
                        f"section_key={row.section_key!r}."
                    )

                conn.execute(
                    """
                    UPDATE fewshot_examples
                    SET source_docx_path = ?,
                        transcript_text = ?,
                        minutes_text = ?,
                        meeting_date = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        row.source_docx_path,
                        row.transcript_text,
                        row.minutes_text,
                        row.meeting_date,
                        now,
                        existing["id"],
                    ),
                )
                updated += 1
                continue

            conn.execute(
                """
                INSERT INTO fewshot_examples (
                    source_transcript_path,
                    source_docx_path,
                    section_key,
                    transcript_text,
                    minutes_text,
                    meeting_date,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.source_transcript_path,
                    row.source_docx_path,
                    row.section_key,
                    row.transcript_text,
                    row.minutes_text,
                    row.meeting_date,
                    now,
                    now,
                ),
            )
            inserted += 1

    return {"inserted": inserted, "updated": updated}
