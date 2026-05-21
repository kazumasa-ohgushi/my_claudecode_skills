# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

A collection of Claude Code skills the user authors and publishes. Each top-level directory is one self-contained skill that users install into `~/.claude/skills/<skill-name>/` (see the skill's README for the curl-based install). This repo is the upstream source — it is not itself loaded as a skill directory.

## Skill layout convention

Each skill directory contains three files:
- `SKILL.md` — front-matter (`name`, `description`, `argument-hint`, `last_verified`, `owner`) plus the prompt Claude reads when the slash command fires. Includes the "What You Must Do" steps so the model knows how to invoke the script.
- `README.md` — user-facing install + usage docs (referenced by the curl URLs end users run).
- `<skill>.py` — the actual implementation script. Skills assume Python 3.12+ and are invoked with `python3 <abs_path_to_script> ...`.

When editing a skill, **keep `SKILL.md`, `README.md`, and the script's `--help`/argparse in sync**. The supported-elements table appears in both `.md` files; updating one without the other causes user-visible drift.

## md-to-gdoc

Converts a local Markdown file (with embedded PNG images) into a Google Doc by uploading HTML to Drive (which converts it to a Doc) and then re-applying styling that Drive's HTML importer drops.

### Running locally during development

```bash
# One-time auth (gives ADC both Drive and Cloud Platform scopes)
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive

pip install google-auth google-auth-httplib2 google-api-python-client \
            Pillow markdown-it-py linkify-it-py

# Run directly against a test .md
python3 md-to-gdoc/md_to_gdoc.py path/to/file.md [--title "..."] [--doc-id ID] [--folder-id ID]
```

There is no test suite or linter configured. Validate changes by running the script end-to-end against a real Markdown file and opening the resulting doc.

### Architecture (md_to_gdoc.py)

The pipeline is five stages, all in one file:

1. **`md_to_html(md)`** — markdown-it-py with the `gfm-like` preset (tables, strikethrough, linkify). `linkify-it-py` is a hard dependency because the preset enables linkify.

2. **`inject_sentinels(html)`** — wraps target HTML regions with single-codepoint math-bracket sentinels:
   - `⟦…⟧` (U+27E6/U+27E7) inside each `<code>`
   - `⦃…⦄` (U+2983/U+2984) inside the first/last `<p>` of each `<blockquote>`
   - `⌈…⌉` (U+2308/U+2309) inside each `<pre><code>`

   **PUA codepoints (U+E000-U+F8FF) cannot be used as sentinels** — Drive's HTML importer collapses every PUA codepoint to U+E907 during import. Math characters survive distinctly. If you add another sentinel pair, pick from outside the PUA block and verify with a probe upload first.

3. **`inline_images_as_data_uri(html, base_dir)`** — replaces every local `<img src="…">` with a base64 data URI AND sets explicit `width`/`height` attributes capped at `MAX_CONTENT_WIDTH_PT × 96/72 ≈ 887 px`. Without the cap, Drive uses the image's intrinsic pixel size and wide PNGs overflow the pageless content area.

4. **`get_or_create_doc(...)`** — if `--doc-id` is given, calls `drive.files().update(fileId=doc_id, media_body=html)` to replace the existing Doc's content **in place** (preserves URL, sharing, comments). Otherwise calls `drive.files().create(...)` with `mimeType=application/vnd.google-apps.document`. Falls back to create-new if the update fails (e.g., doc not found).

5. **`post_process(docs, doc_id)`** — fetches the Doc back via Docs API and builds a single `batchUpdate`. **Ordering is load-bearing**: style-only requests come first (they don't shift indices), then sentinel-based requests sorted by start-index DESC so each pair's deletes only shift indices higher than later (lower-index) pairs.

   The style passes are: paragraph spacing (`spaceBelow=4pt` on NORMAL_TEXT), heading spacing (per-level `spaceAbove`), table column widths (`FIXED_WIDTH = MAX_CONTENT_WIDTH_PT / n_cols`), table cell padding (6/8pt), and table header bold (row 0 of every table). Sentinel-based passes re-apply inline-code (Courier New + red FG + cream BG), blockquote (left border bar), and code block (Courier New 9pt + dark gray FG + cream BG + 18pt indent), then delete the sentinel chars.

### Adding a new sentinel-based style

1. Pick two unused single-codepoint chars outside U+E000-U+F8FF. Add to the `S_*_OPEN`/`S_*_CLOSE` block at the top.
2. Add an `inject_sentinels` branch that wraps the relevant HTML region with the new pair.
3. In `build_post_process_requests`, add a `find_sentinel_ranges(...)` call and an `if kind == "...":` branch that emits the style request(s) for the content range `[start+1, end)`.
4. Probe the upload once: confirm the new sentinel chars survive Drive's HTML conversion distinctly (not collapsed like PUA).

### Image handling

No Drive image files are created. Images are embedded as base64 data URIs in the HTML upload body, so there is no public-link window and no cleanup step. Width is capped at `MAX_CONTENT_WIDTH_PT = 665` (pageless content width); height scales proportionally.
