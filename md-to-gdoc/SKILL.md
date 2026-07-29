---
name: md-to-gdoc
description: Convert a local Markdown file (with embedded PNG images) to a Google Doc. Fenced code blocks become native Google Docs code blocks (Insert → Building blocks → Code block) with language auto-detection and syntax highlighting. Also supports headings, tables, bullet/ordered lists, blockquotes, horizontal rules, and inline bold/code formatting. Images are inlined as base64 data URIs (no separate Drive uploads). Use this for sharing rendered analysis reports with collaborators who prefer Google Docs over Markdown. Requires gcloud ADC with Drive + Docs scope.
argument-hint: "<path/to/file.md> [--title TITLE] [--doc-id DOC_ID] [--folder-id FOLDER_ID]"
last_verified: 2026-07-29
owner: Kazumasa Ohgushi
---
# md-to-gdoc: Convert Markdown to Google Doc

Convert a local Markdown file (with embedded PNG images) to a Google Document via Drive's native markdown import. Fenced code blocks are rendered as real Google Docs code blocks (the Insert → Building blocks → Code block widget) with language auto-detection and syntax highlighting. Images are inlined as base64 data URIs inside the markdown upload, so no temporary Drive files are created.

## Prerequisites

### 1. ADC with Drive scope (one-time setup per machine)
```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive
```

### 2. Python packages (Python 3.12+)
```bash
pip install google-auth google-auth-httplib2 google-api-python-client Pillow
```

## Usage

```
/md-to-gdoc <path/to/file.md> [--title "Document Title"] [--doc-id DOC_ID] [--folder-id FOLDER_ID]
```

- `<path/to/file.md>` — path to the Markdown file (absolute or relative to CWD)
- `--title` — optional document title (defaults to the filename stem)
- `--doc-id` — optional Google Doc ID to overwrite in place; if the document is not found, a new one is created instead
- `--folder-id` — optional Google Drive folder ID to place the doc in (find it in the folder's URL: `https://drive.google.com/drive/folders/<FOLDER_ID>`)

## What You Must Do

When this skill is invoked:

1. **Identify the Markdown file path** from the user's argument. Resolve it to an absolute path.

2. **Locate the Python script** — it is bundled alongside this SKILL.md file:
   Use Glob to find `md_to_gdoc.py` by searching `**/.claude/skills/md-to-gdoc/md_to_gdoc.py` in the home directory, or locate it relative to this SKILL.md's own path.

3. **If `--doc-id` is provided, confirm the overwrite with the user before running anything.**
   The script replaces all content of the target Google Doc in place — this is destructive and not reversible. You MUST ask the user to confirm via AskUserQuestion, showing them the doc URL (`https://docs.google.com/document/d/<DOC_ID>/edit`) and the Markdown source path. Do not invoke the script until the user explicitly confirms. If they decline, stop and report that nothing was changed.

4. **Run the script** using an available Python 3.12+ interpreter.
   Check the user's CLAUDE.md for their configured Python environment. If none is specified, fall back to `python3`:
   ```bash
   python3 /absolute/path/to/md_to_gdoc.py <absolute_path_to_md> [--title "Title"] [--doc-id DOC_ID]
   ```

5. **Report the result** to the user with the Google Doc URL printed by the script.

## Supported Markdown Elements

| Element | Syntax |
|---------|--------|
| Headings | `# H1` through `###### H6` |
| Bold | `**text**` |
| Inline code | `` `code` `` |
| Bold + code | `` **`code`** `` |
| Code blocks | ` ```lang ` fences → native Docs code block widget |
| Images | `![alt](relative/path/to/image.png)` |
| Tables | GFM pipe tables (header row rendered bold) |
| Blockquotes | `> text` |
| Bullet lists | `- item` |
| Ordered lists | `1. item` |
| Horizontal rule | `---` |
| Strikethrough | `~~text~~` |
| Autolinks | bare URLs |

**Image paths** in the Markdown must be relative to the Markdown file's directory.

## Notes

- The document is created in pageless format automatically.
- Fenced code blocks become native Google Docs code blocks (language chip +
  copy button + syntax highlighting). The language comes from the fence info
  string (` ```python `, ` ```sql `, ` ```bash `, …) via the importer's
  auto-detection; unrecognized languages fall back to an unlabeled block.
- Images are embedded as base64 data URIs inside the markdown upload — no
  temporary Drive files, no public-link window.
- Images wider than the pageless content width (~665pt ≈ 886px) are
  **pixel-downscaled** before upload (the markdown importer offers no
  display-size control), so very high-res screenshots lose some zoom detail.
- New documents land in the authenticated user's Drive root unless `--folder-id` is given.
