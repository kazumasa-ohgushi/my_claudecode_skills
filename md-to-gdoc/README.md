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
| Tables | GFM pipe tables (header row rendered bold) |
| Blockquotes | `> text` |
| Bullet lists | `- item` |
| Ordered lists | `1. item` |
| Horizontal rule | `---` |
| Strikethrough | `~~text~~` |
| Autolinks | bare URLs |

## Notes

- The document is created in pageless format automatically.
- Wide images are capped at the pageless content width (~665pt); height scales proportionally.
- New documents land in the authenticated user's Drive root unless `--folder-id` is given.
- To update the skill, re-run the `curl` commands in step 1.
