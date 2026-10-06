# Validation — md-to-gdoc

Last run: 2026-10-06, against `md_to_gdoc.py` at `a2e7ed0` plus the table-color commit that adds this line.

How to run: see "Validation fixtures" in `md-to-gdoc/CLAUDE.md`.

## Round 2026-10-06

Both fixtures were converted end-to-end (Drive HTML import + Docs API
post-processing). Results were checked by reading the doc back via
`documents.get`. **Not** exported to PDF or inspected as rendered pages this
round, so the visual items from the 2026-08-27 round below were not
re-verified.

### Inputs

| Input | Covers |
|---|---|
| `testdata/style_sample.md` | H1–H6, bold/italic/strikethrough, inline code, links + linkify, Japanese text, tight/loose/nested bullet + ordered lists, 6-row table, blockquote with inline code, Python and SQL code blocks (multi-line `/* */`, consecutive and trailing `--` comments), horizontal rule, narrow (400px) and over-cap (1600px) PNGs |
| `testdata/regression_sample.md` | Inline formatting / adjacent links in table cells; four blockquote shapes; inline code nested in blockquotes; SQL comment-rewrite edge cases; non-SQL fence with `--`; code-block indent |

### Modes

- New document (`files.create`) with `--folder-id` — verified the doc lands in the test folder.
- `--title` — verified (timestamped titles, e.g. `style_sample 2026-10-06 13:41:59`).
- Overwrite in place (`--doc-id`) — not re-verified this round.

### Checked

- **Sentinels**: no sentinel character remains in either doc.
- **Nested sentinels** (fixed in `bcf4f00`): all 7 quotes in `regression_sample` carry the left border, including the three with inline code inside, and inline-code styling covers exactly the code text. The same fixture on the pre-fix code leaves `⟦⟧⦃⦄` in the doc, so the case reproduces the bug.
- **SQL comment rewrite** (`3c43ca8`): every `--` comment in a `sql` fence comes out as `/* ... */`; `--` inside strings, backtick identifiers and existing block comments is unchanged; `*/` inside a comment becomes `* /`; a bare `--` becomes `/* */`. A `text` fence keeps its `--` unchanged.
- **Table colors** (2026 Moloco visual identity): every table has a `#00645D` header row with white text, body rows alternating `#FFFFFF` / `#FAF9F5`, and `#E8E6DB` borders. Candidate header and stripe colors were compared in separate side-by-side docs before choosing.
- **Code-block indent** (`a2e7ed0`): every code-block paragraph has `indentFirstLine` = `indentStart` = 18pt, so no line hangs. Line breaks, leading spaces and the ` * ` lines of block comments are preserved.

### Not checked this round

- Rendered appearance (PDF/screen) of all of the above.
- Whether Docs' own SQL code-block highlighting treats the rewritten comments correctly once the block is switched to SQL.

## Round 2026-08-27

Every case was converted end-to-end, exported back to PDF, and inspected as
rendered pages.

### Inputs

| Input | Covers |
|---|---|
| A synthetic style sample (superseded by `testdata/style_sample.md`) | H1–H6, bold, inline code, links, bullet lists (3 nesting levels, tight + loose items), ordered lists, mixed ordered/bullet nesting, a 6-row table, blockquote, fenced code block, horizontal rule |
| `analysis/projects/apx1/2026-05-19_tagid_grouping/report.md` (real internal report, not in this repo) | Japanese body text, 4 inline PNG figures, 5 tables incl. one spanning a page break, 45 inline-code spans, deep bullet nesting |
| A regression sample (superseded by `testdata/regression_sample.md`) | Table cells containing `**bold** *italic*` and two adjacent links; blockquote holding only a bullet list; blockquote with an intro paragraph followed by a list; plain single-line blockquote |

### Modes

- New document (`files.create`) — verified a doc is created in Drive root and returns a URL.
- Overwrite in place (`--doc-id`) — verified the same doc ID is reused across ~10 iterations, preserving URL and sharing.
- `--title` override — verified.
- `--folder-id` not verified.

### Checked in the rendered output

- **Named styles**: Arial 11 body at 115% line spacing; 26/20/16/14pt Title/H1–H3 bold; H4–H6 regular with H6 italic; gray H3–H6. Confirmed by reading back `namedStyles` via `documents.get` and by inspecting the exported PDF.
- **Tables**: navy `#040078` header band with white text, body rows alternating white / `#F6F8F9` (verified by sampling pixel colors per row), hairline navy borders on all four sides of every cell, cell content vertically centered. Header row repeats correctly across a page break.
- **Header-row height fix**: confirmed the last header cell no longer contains a stray `" "` paragraph (`documents.get` shows one paragraph per header cell), and the navy band is one line tall instead of two.
- **Inline text inside table cells**: `**Revenue** *(USD)*` and two adjacent links render with their separating spaces intact — the table-whitespace compaction only removes whitespace with a table structure tag on both sides.
- **Lists**: bullets are real Docs lists (●/○/■ by nesting level) and ordered lists keep 1./a./i.; loose items keep their continuation paragraph indented under the bullet.
- **Blockquotes**: the left border covers the whole quote for all four shapes tested (plain, multi-paragraph, list-only, intro + list). Sentinel pairs verified balanced (exactly one open/close per quote) for each shape. Inline code inside a quote was not covered (bug found and fixed in the 2026-10-06 round).
- **Code**: fenced blocks render in Roboto Mono 9pt on a light background with indents; inline code keeps its own color/background. All sentinel characters are deleted from the final document. (The hanging indent from line 2 was missed here; fixed in the 2026-10-06 round.)
- **Images**: PNGs embed as base64 data URIs, are capped at the pageless content width, and no temporary Drive files are created.

## Known limitations

- Japanese text falls back to MS PGothic, since Arial (like the markdown
  importer's own default) carries no CJK glyphs.
- A source Markdown file that itself contains one of the six sentinel
  characters (`⟦⟧⦃⦄⌈⌉`) produces broken output.
