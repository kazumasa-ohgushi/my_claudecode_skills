# Validation — md-to-gdoc

Last run: 2026-08-27, against `md_to_gdoc.py` at the head of this branch.

Every case below was converted end-to-end (Drive HTML import + Docs API
post-processing), exported back to PDF, and inspected as rendered pages — not
just at the intermediate HTML layer.

## Inputs

| Input | Covers |
|---|---|
| A synthetic style sample | H1–H6, bold, inline code, links, bullet lists (3 nesting levels, tight + loose items), ordered lists, mixed ordered/bullet nesting, a 6-row table, blockquote, fenced code block, horizontal rule |
| `analysis/projects/apx1/2026-05-19_tagid_grouping/report.md` (real internal report, not in this repo) | Japanese body text, 4 inline PNG figures, 5 tables incl. one spanning a page break, 45 inline-code spans, deep bullet nesting |
| A regression sample for the review findings | Table cells containing `**bold** *italic*` and two adjacent links; blockquote holding only a bullet list; blockquote with an intro paragraph followed by a list; plain single-line blockquote |

## Modes

- New document (`files.create`) — verified a doc is created in Drive root and returns a URL.
- Overwrite in place (`--doc-id`) — verified the same doc ID is reused across ~10 iterations, preserving URL and sharing.
- `--title` override — verified.
- `--folder-id` not re-verified this round (untouched by these changes).

## Checked in the rendered output

- **Named styles**: Arial 11 body at 115% line spacing; 26/20/16/14pt Title/H1–H3 bold; H4–H6 regular with H6 italic; gray H3–H6. Confirmed by reading back `namedStyles` via `documents.get` and by inspecting the exported PDF.
- **Tables**: navy `#040078` header band with white text, body rows alternating white / `#F6F8F9` (verified by sampling pixel colors per row), hairline navy borders on all four sides of every cell, cell content vertically centered. Header row repeats correctly across a page break.
- **Header-row height fix**: confirmed the last header cell no longer contains a stray `" "` paragraph (`documents.get` shows one paragraph per header cell), and the navy band is one line tall instead of two.
- **Inline text inside table cells**: `**Revenue** *(USD)*` and two adjacent links render with their separating spaces intact — the table-whitespace compaction only removes whitespace with a table structure tag on both sides.
- **Lists**: bullets are real Docs lists (●/○/■ by nesting level) and ordered lists keep 1./a./i.; loose items keep their continuation paragraph indented under the bullet.
- **Blockquotes**: the left border covers the whole quote for all four shapes tested (plain, multi-paragraph, list-only, intro + list). Sentinel pairs verified balanced (exactly one open/close per quote) for each shape.
- **Code**: fenced blocks render in Roboto Mono 9pt on a light background with indents; inline code keeps its own color/background. All sentinel characters are deleted from the final document.
- **Images**: PNGs embed as base64 data URIs, are capped at the pageless content width, and no temporary Drive files are created.

## Known limitations

- Japanese text falls back to MS PGothic, since Arial (like the markdown
  importer's own default) carries no CJK glyphs. Pre-existing behavior, not
  introduced by this revision.
- `--folder-id` was not exercised in this round.
