# md-to-gdoc

A Claude Code skill that converts a local Markdown file (with embedded PNG images) to a Google Document.

Images are automatically uploaded to Google Drive for embedding, then immediately deleted — no Drive clutter, no public image exposure.

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
pip install google-auth google-auth-httplib2 google-api-python-client Pillow
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
```

- The Markdown file path can be absolute or relative to the current working directory.
- `--title` is optional — defaults to the filename stem.
- Image paths in the Markdown must be relative to the Markdown file's directory.

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

## Notes

- The created document opens in paged view by default. To switch to pageless: **View → Pageless** in the Google Doc.
- The document is created in the authenticated user's Drive root.
- To update the skill, re-run the `curl` commands in step 1.
