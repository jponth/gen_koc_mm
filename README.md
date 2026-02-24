# gen_koc_mm

Generate Knights of Columbus (KoC) council meeting minutes (JSON) from a **transcript text file**.

## What this does
- Takes a transcript text file (timestamps are ok; they will be stripped).
- Keeps speakers as `Speaker 1`, `Speaker 2`, ... (no real-person mapping).
- Splits the transcript into the KoC section template using cue phrases and safe regexes.
- Produces minutes as **JSON** (no timestamps, no header block).

## Output template
The generator outputs these section headings (in order), as bold lines:
- Chaplain’s Report
- Grand Knights Report
- Treasurer’s Report
- Insurance Agent Report
- District Deputy Report
- 4th Degree Report
- Old Business
- New Business
- Birthdays
- Good of the Order
- Closing prayers

If a section has no content (or the transcript indicates the person is not present), it is left empty.

## Install

### Python deps

```bash
cd /Users/jponthempilly/openclaw_projects/gen_koc_mm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Local Whisper (for m4a → transcript)

This project uses the **local** Whisper CLI (no API calls):

```bash
brew install openai-whisper
whisper --help
```

## Configure OpenAI

Option A (recommended): create a `.env` file:

```bash
cat > .env << 'EOF'
OPENAI_API_KEY=YOUR_KEY_HERE
# optional default model:
OPENAI_MODEL=gpt-4o-mini
EOF
```

Option B: export env vars in your shell:

```bash
export OPENAI_API_KEY=YOUR_KEY_HERE
export OPENAI_MODEL=gpt-4o-mini
```

## CLI

This package exposes a Typer CLI:

- `python -m gen_koc_mm transcribe` — transcribe an audio file locally to a transcript file (Whisper CLI)
- `python -m gen_koc_mm generate` — main minutes generation workflow (outputs JSON in phase 2)
- `python -m gen_koc_mm merge-docx` — merge minutes JSON into a Word (.docx) template using placeholders
- `python -m gen_koc_mm suggest-cues` — discover/update section boundary cues

### `transcribe`

Transcribe audio (e.g. `.m4a`) locally into a transcript file.

```bash
python -m gen_koc_mm transcribe \
  --input-audio /path/to/meeting.m4a \
  --output input/meeting.txt \
  --whisper-model medium \
  --language en \
  --format txt
```

Then use the transcript file with `generate`.

### `generate`

This command has **two phases**. You must choose **exactly one** of:

- `--identify-sections`: boundary detection only (writes an intermediate transcript with explicit section markers)
- `--generate-output`: minutes generation JSON from an already-marked transcript (the transcript already contains section markers)

Options:
- `--input PATH` (required) — input transcript file
- `--output PATH` (required) — output file (marked transcript in phase 1; JSON in phase 2)
- `--identify-sections` — phase 1 (creates a marked transcript)
- `--generate-output` — phase 2 (creates minutes JSON)
- `--date-of-meeting TEXT` — set JSON field `date_of_meeting` (best-effort inferred from filename if omitted)
- `--model TEXT` — override model name (otherwise uses `OPENAI_MODEL`, default `gpt-4o-mini`)
- `--debug-chunks` — when generating output, write per-section transcript chunks next to the output for inspection

#### Phase 1 example: identify section boundaries

```bash
python -m gen_koc_mm generate \
  --identify-sections \
  --input /path/to/transcript.txt \
  --output output/marked_transcript.txt
```

This writes a transcript where boundaries are explicitly marked as:

- `** <section key> **`

(You can open and review/edit this before generating minutes.)

#### Phase 2 example: generate minutes JSON from a marked transcript

```bash
python -m gen_koc_mm generate \
  --generate-output \
  --input output/marked_transcript.txt \
  --output output/minutes.json \
  --date-of-meeting 2026-02-23 \
  --debug-chunks
```

The JSON schema (v1.0) looks like:

```json
{
  "schema_version": "1.0",
  "date_of_meeting": "2026-02-23",
  "generator": { "name": "gen_koc_mm", "version": "0.1.0", "model": "gpt-4o-mini" },
  "source": { "input_file": "marked_transcript.txt" },
  "sections": [
    {
      "section_key": "grand_knights_report",
      "section_heading": "Grand Knights Report",
      "section_text": "- ...",
      "format": "markdown",
      "status": "ok"
    }
  ]
}
```

### `merge-docx`

Merge a minutes JSON file into a Word template.

A known-good, compatible template is included:
- `templates/KoC-Meeting-Minutes-Template-compatible.docx`

Your template should contain placeholders like:
- `<<date_of_meeting>>`
- `<<grand_knights_report>>`
- `<<chaplains_report>>`

Section keys are canonical **snake_case** (underscores). Older space-separated keys are still accepted in marked transcripts (e.g. `** grand knights report **`), but the JSON output + Word placeholders use snake_case.

Example:

```bash
python -m gen_koc_mm merge-docx \
  --minutes-json output/minutes.json \
  --template-docx templates/koc_minutes_template.docx \
  --output-docx output/minutes_filled.docx
```

### `suggest-cues`

This command is for improving the section-boundary cue rules.

There are two supported modes:

#### Mode A: discover candidates → JSON

Scan transcript file(s) and produce a discovery JSON file:

```bash
python -m gen_koc_mm suggest-cues \
  --input-path /path/to/transcripts_folder \
  --glob "*.txt" \
  --discover-json output/cue_discovery.json
```

Useful options:
- `--max-candidates-per-file` — cap the amount of discovery output per file

#### Mode B: apply discovery JSON → update `section_cues.json`

```bash
python -m gen_koc_mm suggest-cues \
  --update-cues output/cue_discovery.json \
  --min-confidence 0.7
```

Useful options:
- `--dry-run` — compute the changes but don’t write to `gen_koc_mm/section_cues.json`

## Transcript formats supported
- Plain text where each utterance is either:
  - `Speaker 1` on one line and the text on later lines (as in your samples)
  - Or `Speaker 1: ...`
  - Or `SPEAKER_00: ...` / `SPEAKER 00: ...` (will be normalized)
- If timestamps exist like `[00:01:23]` or `00:01:23` at line start, they are removed.

## Notes
- This version assumes you start from transcript files (not m4a). We can add m4a transcription later.
