# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

A collection of Claude Code skills the user authors and publishes. Each top-level directory is one self-contained skill that users install into `~/.claude/skills/<skill-name>/` (see the skill's README for the curl-based install). This repo is the upstream source — it is not itself loaded as a skill directory.

## Skill layout convention

Each skill directory contains three files:
- `SKILL.md` — front-matter (`disable-model-invocation: true`) plus the prompt Claude reads when the slash command fires. Includes the "What You Must Do" steps so the model knows how to invoke the script.
- `README.md` — user-facing install + usage docs (referenced by the curl URLs end users run).
- `<skill>.py` — the actual implementation script. Skills assume Python 3.12+ and are invoked with `python3 <abs_path_to_script> ...`.

When editing a skill, **keep `SKILL.md`, `README.md`, and the script's `--help`/argparse in sync**. The supported-elements table appears in both `.md` files; updating one without the other causes user-visible drift.

## md-to-gdoc

Converts a local Markdown file (with embedded PNG images) into a Google Doc via the Drive + Docs APIs.

### Running locally during development

```bash
# One-time auth (gives ADC both Drive and Cloud Platform scopes)
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive

pip install google-auth google-auth-httplib2 google-api-python-client Pillow

# Run directly against a test .md
python3 md-to-gdoc/md_to_gdoc.py path/to/file.md [--title "..."] [--doc-id ID] [--folder-id ID]
```

There is no test suite or linter configured. Validate changes by running the script end-to-end against a real Markdown file and opening the resulting doc.

### Architecture (md_to_gdoc.py)

The pipeline is three stages, all in one file:

1. **`parse_md(path) -> list[block]`** — line-oriented scanner. Each branch in the main `while` loop consumes one block type (heading, hr, image, quote, table, list, ordered_list, code_block, paragraph) and advances `i`. The `if i == prev_i` guard at the bottom is a safety net: any line that no branch consumed gets logged and skipped so the loop cannot livelock. **Preserve this guard** when adding new block types — the previous bug was an infinite loop on unrecognized lines (see commit 4b09c26).

2. **`DocBuilder`** — accumulates Google Docs `batchUpdate` requests in `self.requests` and tracks the insertion index `self.idx` locally. Each block writer (`heading`, `paragraph`, `quote`, `list_item`, `code_block`, `image`, `table`) appends `insertText` + style requests and updates `self.idx` to match. `flush()` ships the batch.

   - **Tables are special**: they call `flush()` then `_sync_idx()` (re-reads the doc to get the true end index), insert the empty table with its own `batchUpdate`, re-read the doc to find each cell's `startIndex`, then fill cells in **reverse index order** so earlier inserts don't shift later cell positions. If you add another block type that mutates structure via its own `batchUpdate` (rather than just appending text), follow the same flush→sync→mutate→sync pattern.
   - Inline runs (`parse_inline`) return `(text, bold, code)` tuples. The `_insert_inline_runs` / `_apply_inline_styles` pair is shared across paragraph/quote/list_item — reuse them rather than re-implementing styling per block.

3. **`convert()`** — orchestrates auth → parse → (open existing doc OR create new) → set `PAGELESS` mode → walk blocks → flush → delete temporary Drive images → optionally move to folder. The `--doc-id` path clears existing content before re-rendering; if the doc isn't found, it falls back to creating a new one.

### Image handling

Images are uploaded to Drive with public-reader permission so `insertInlineImage` can fetch them by URL, then **deleted after the doc batch flushes** (`delete_drive_files`). Errors during deletion are caught per-file so one missing file doesn't abort the rest of the cleanup. The width is capped at `MAX_IMG_WIDTH_PT = 665` (≈ pageless content width); height scales proportionally.
