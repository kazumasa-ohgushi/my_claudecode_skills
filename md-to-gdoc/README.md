# md-to-gdoc

A Claude Code skill that converts a local Markdown file (with embedded PNG images) to a Google Document.

Images are inlined as base64 data URIs inside the HTML upload, so there are no temporary Drive files and no public-link window.

## Installation

**1. Place the skill files in your global Claude Code skills directory:**

```bash
mkdir -p ~/.claude/skills/md-to-gdoc
cd ~/.claude/skills/md-to-gdoc
curl -O https://raw.githubusercontent.com/kazumasa-ohgushi/my_claudecode_skills/main/md-to-gdoc/SKILL.md
curl -O https://raw.githubusercontent.com/kazumasa-ohgushi/my_claudecode_skills/main/md-to-gdoc/md_to_gdoc.py
```

Once placed in `~/.claude/skills/`, the skill is available as `/md-to-gdoc` across all your projects.

**2. Install Python dependencies (Python 3.12+):**

```bash
pip install google-auth google-auth-httplib2 google-api-python-client \
            Pillow markdown-it-py linkify-it-py
```

**3. Authenticate with Google (one-time setup, adds Drive scope to ADC):**

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive
```

## Usage

In a Claude Code session:

```
/md-to-gdoc path/to/report.md
/md-to-gdoc path/to/report.md --title "My Report Title"
/md-to-gdoc path/to/report.md --doc-id EXISTING_DOC_ID
/md-to-gdoc path/to/report.md --folder-id DRIVE_FOLDER_ID
```

- The Markdown file path can be absolute or relative to the current working directory.
- `--title` is optional — defaults to the filename stem.
- `--doc-id` updates an existing Google Doc in place (preserves URL, sharing, and comments). Falls back to creating a new doc if the ID is not found.
- `--folder-id` places the Doc in the specified Drive folder.
- Image paths in the Markdown must be relative to the Markdown file's directory.

## Supported Markdown Elements

| Element | Syntax |
|---------|--------|
| Headings | `# H1` through `###### H6` |
| Bold | `**text**` |
| Inline code | `` `code` `` |
| Bold + code | `` **`code`** `` |
| Images | `![alt](relative/path/to/image.png)` |
| Tables | GFM pipe tables (deep-teal header band, alternating row fill) |
| Blockquotes | `> text` |
| Code blocks | ` ```lang ` fenced blocks (` ```sql ` etc.) |
| Bullet lists | `- item` |
| Ordered lists | `1. item` |
| Horizontal rule | `---` |
| Strikethrough | `~~text~~` |
| Autolinks | bare URLs |

## Notes

- The document is created in pageless format automatically.
- Typography mirrors Google's own markdown importer: its named-style set (Arial 11 body at 115% line spacing, 26/20/16/14pt title and headings, gray H3-H6), with the Title and H1-H3 bolded, blank lines preserved as empty paragraphs, and Roboto Mono code.
- In SQL code blocks (` ```sql `, `bigquery`, `bq`, `postgresql`, `postgres`, `mysql`), every `-- comment` is rewritten as `/* comment */`. Once the block is switched to a Docs SQL code block, a `--` comment would otherwise be highlighted as a comment through to the end of the block. Strings, backtick identifiers and existing `/* */` comments are left untouched.
- Tables follow the 2026 Moloco visual identity: a deep-teal (#00645D) header band with centered white text, body rows alternating white / Parchment (#FAF9F5), and hairline Vellum (#E8E6DB) borders on every cell. Cell content is vertically centered.
- Bullet and ordered lists are real Docs lists (●/○/■ and 1./a./i.), so Docs auto-continues them when the doc is edited.
- Wide images are capped at the pageless content width (~665pt) display-wise; full pixel resolution is preserved.
- New documents land in the authenticated user's Drive root unless `--folder-id` is given.
- To update the skill, re-run the `curl` commands in step 1.
