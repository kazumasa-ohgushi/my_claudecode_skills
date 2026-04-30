---
disable-model-invocation: true
---
# md-to-gdoc: Convert Markdown to Google Doc

Convert a local Markdown file (with embedded PNG images) to a Google Document. Images are automatically uploaded, embedded, and then deleted from Drive.

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
- `--doc-id` — optional Google Doc ID to overwrite; if the document is not found, a new one is created
- `--folder-id` — optional Google Drive folder ID to move the doc into after creation (find it in the folder's URL: `https://drive.google.com/drive/folders/<FOLDER_ID>`)

## What You Must Do

When this skill is invoked:

1. **Identify the Markdown file path** from the user's argument. Resolve it to an absolute path.

2. **Locate the Python script** — it is bundled alongside this SKILL.md file:
   Use Glob to find `md_to_gdoc.py` by searching `**/.claude/skills/md-to-gdoc/md_to_gdoc.py` in the home directory, or locate it relative to this SKILL.md's own path.

3. **Run the script** using an available Python 3.12+ interpreter.
   Check the user's CLAUDE.md for their configured Python environment. If none is specified, fall back to `python3`:
   ```bash
   python3 /absolute/path/to/md_to_gdoc.py <absolute_path_to_md> [--title "Title"] [--doc-id DOC_ID]
   ```

4. **Report the result** to the user:
   - The Google Doc URL printed by the script
   - Confirmation that temporary Drive images were deleted

## Supported Markdown Elements

| Element | Syntax |
|---------|--------|
| Headings | `# H1`, `## H2`, `### H3` |
| Bold | `**text**` |
| Inline code | `` `code` `` |
| Bold + code | `` **`code`** `` |
| Images | `![alt](relative/path/to/image.png)` |
| Tables | GFM pipe tables |
| Blockquotes | `> text` |
| Bullet lists | `- item` |
| Ordered lists | `1. item` |
| Horizontal rule | `---` |

**Image paths** in the Markdown must be relative to the Markdown file's directory.

## Notes

- The document is created in pageless format automatically.
- Images are uploaded temporarily to Google Drive to embed them in the Doc, then immediately deleted. No Drive clutter.
- The document is created in the authenticated user's Drive root.
