#!/usr/bin/env python3
"""
md_to_gdoc.py — Convert a Markdown file to a Google Doc.

Pipeline:
    md → HTML (markdown-it-py, gfm-like)
       + whitespace between table tags stripped
       + math-bracket sentinels around <code>, <pre>, <blockquote>
       + inline images as base64 data URIs (no separate Drive uploads)
    → Drive files.create OR files.update (mimeType=google-apps.document)
    → Docs API batchUpdate: re-apply styling Drive's HTML importer drops,
      then delete the sentinel chars (reverse-order so deletes don't shift
      indices of later operations)

Compared to the prior batchUpdate-based renderer this:
    - Removes the need to upload images to Drive as world-readable files
    - Lets markdown-it-py replace a hand-rolled markdown parser
    - Cuts ~600 lines of index-tracking DocBuilder code

Usage:
    python3 md_to_gdoc.py <md_file> [--title TITLE] [--doc-id ID] [--folder-id ID]

Requirements:
    pip install google-auth google-auth-httplib2 google-api-python-client \
                Pillow markdown-it-py linkify-it-py

ADC must have Drive + Docs scope:
    gcloud auth application-default login \
        --scopes=https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/drive
"""

import argparse
import base64
import io
import mimetypes
import re
import sys
from pathlib import Path

from PIL import Image
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from markdown_it import MarkdownIt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Math-bracket sentinels. Drive's HTML importer normalises every PUA codepoint
# (U+E000-U+F8FF) to U+E907, so PUA can't be used. These math characters
# survive distinctly. Each is single-codepoint to keep the finder loop simple.
S_INLINE_CODE_OPEN = "⟦"   # MATHEMATICAL LEFT WHITE SQUARE BRACKET
S_INLINE_CODE_CLOSE = "⟧"  # MATHEMATICAL RIGHT WHITE SQUARE BRACKET
S_BLOCKQUOTE_OPEN = "⦃"    # LEFT WHITE CURLY BRACKET
S_BLOCKQUOTE_CLOSE = "⦄"   # RIGHT WHITE CURLY BRACKET
S_CODE_BLOCK_OPEN = "⌈"    # LEFT CEILING
S_CODE_BLOCK_CLOSE = "⌉"   # RIGHT CEILING

# Content width budget (pageless mode). Used for table column sums and the
# image-width cap.
MAX_CONTENT_WIDTH_PT = 665

# Colors (kept in sync with the prior DocBuilder implementation).
INLINE_CODE_FG = {"red": 0.780, "green": 0.145, "blue": 0.306}
INLINE_CODE_BG = {"red": 0.976, "green": 0.949, "blue": 0.957}
CODE_BLOCK_FG = {"red": 0.133, "green": 0.133, "blue": 0.133}
CODE_BLOCK_BG = {"red": 0.949, "green": 0.953, "blue": 0.957}
BQ_BAR = {"red": 0.6, "green": 0.6, "blue": 0.6}

# Text colors, matching the markdown importer: black body, gray H3-H6.
BODY_FG: dict = {}  # empty rgbColor == the Docs default, black
MUTED_FG = {"red": 0.2627451, "green": 0.2627451, "blue": 0.2627451}  # #434343
SUBTLE_FG = {"red": 0.4, "green": 0.4, "blue": 0.4}                   # #666666
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

# Table decoration: navy header band, alternating body rows, hairline navy
# borders on every cell.
ACCENT = {"red": 0.015686275, "green": 0.0, "blue": 0.47058824}            # #040078
TABLE_HEADER_BG = ACCENT
TABLE_HEADER_FG = WHITE
TABLE_STRIPE_BG = {"red": 0.9647059, "green": 0.972549, "blue": 0.9764706}  # #F6F8F9
TABLE_BORDER_FG = ACCENT
TABLE_BORDER_WIDTH_PT = 0.416667

BODY_FONT = "Arial"


def _named_style(
    size: float,
    *,
    bold: bool = False,
    italic: bool = False,
    fg: dict = BODY_FG,
    above: float = 0,
    below: float = 0,
) -> dict:
    """One NAMED_STYLE_PRESET entry. Every field in NAMED_STYLE_FIELDS is
    emitted explicitly: a field named in the mask but missing from the
    payload is reset to the Docs default, so every heading names the body
    font rather than leaving it to inheritance."""
    return {
        "textStyle": {
            "weightedFontFamily": {"fontFamily": BODY_FONT, "weight": 400},
            "fontSize": {"magnitude": size, "unit": "PT"},
            "bold": bold,
            "italic": italic,
            "foregroundColor": {"color": {"rgbColor": fg}},
        },
        "paragraphStyle": {
            "lineSpacing": 115,
            "spaceAbove": {"magnitude": above, "unit": "PT"},
            "spaceBelow": {"magnitude": below, "unit": "PT"},
        },
    }


# Named-style preset captured from a Google-native markdown import: Arial 11
# / 115% line spacing body, 26/20/16/14pt title and headings, gray H3-H6.
# The Title and H1-H3 are bolded on top of it. Applied document-wide after
# import, replacing whatever the HTML importer inferred.
NAMED_STYLE_PRESET: dict[str, dict] = {
    "NORMAL_TEXT": _named_style(11),
    "HEADING_1": _named_style(20, bold=True, above=20, below=6),
    "HEADING_2": _named_style(16, bold=True, above=18, below=6),
    "HEADING_3": _named_style(14, bold=True, fg=MUTED_FG, above=16, below=4),
    "HEADING_4": _named_style(12, fg=SUBTLE_FG, above=14, below=4),
    "HEADING_5": _named_style(11, fg=SUBTLE_FG, above=12, below=4),
    "HEADING_6": _named_style(11, italic=True, fg=SUBTLE_FG, above=12, below=4),
    "TITLE": _named_style(26, bold=True, below=3),
    "SUBTITLE": _named_style(15, fg=SUBTLE_FG, below=16),
}

NAMED_STYLE_FIELDS = (
    "namedStyleType,"
    "textStyle.weightedFontFamily,textStyle.fontSize,textStyle.bold,"
    "textStyle.italic,textStyle.foregroundColor,"
    "paragraphStyle.lineSpacing,paragraphStyle.spaceAbove,paragraphStyle.spaceBelow"
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate():
    scopes = [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
    ]
    creds, _ = google.auth.default(scopes=scopes)
    creds.refresh(google.auth.transport.requests.Request())
    return (
        build("drive", "v3", credentials=creds),
        build("docs", "v1", credentials=creds),
    )


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------

def md_to_html(md_text: str) -> str:
    """Render markdown to HTML, preserving source blank lines as empty
    <p></p> paragraphs (Drive's HTML importer keeps them as empty
    paragraphs — same as Docs' native markdown importer, whose airier
    layout comes largely from these). Gaps directly after a heading are
    swallowed, matching the markdown importer. Blank lines before a block
    also keep <hr> from merging into a following heading on import."""
    md = MarkdownIt("gfm-like")  # tables, strikethrough, linkify
    env: dict = {}
    tokens = md.parse(md_text, env)

    # Split the token stream into top-level blocks.
    chunks: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for i, tok in enumerate(tokens):
        if depth == 0:
            start = i
        depth += tok.nesting
        if depth == 0:
            chunks.append((start, i))

    # Blank-line gap = consecutive blank source lines directly above each
    # block. Counting backward from the block's start line is robust
    # against markdown-it block maps that swallow trailing blanks (lists).
    lines = md_text.split("\n")

    def blank_gap_above(start_line: int) -> int:
        gap = 0
        line = start_line - 1
        while line >= 0 and not lines[line].strip():
            gap += 1
            line -= 1
        return gap

    parts: list[str] = []
    first = True
    prev_was_heading = False
    for s, e in chunks:
        block_map = tokens[s].map
        if block_map and not first and not prev_was_heading:
            parts.append("<p></p>" * blank_gap_above(block_map[0]))
        first = False
        prev_was_heading = tokens[s].type == "heading_open"
        parts.append(md.renderer.render(tokens[s:e + 1], md.options, env))
    return "".join(parts)


def compact_table_markup(html: str) -> str:
    """Strip the whitespace between table tags. Drive's HTML importer turns
    the newline after the last <th> of a header row into a stray ' '
    paragraph in that cell, which inflates the whole header row's height."""
    return re.sub(
        r"<table>.*?</table>",
        lambda m: re.sub(r">\s+<", "><", m.group(0)),
        html,
        flags=re.DOTALL,
    )


def inject_sentinels(html: str) -> str:
    """Wrap target regions with math-bracket sentinels. They become text
    content that Drive's converter preserves; we locate them in the resulting
    Doc and use them to identify ranges that need restyling."""

    # 1. Mask out <pre><code>...</code></pre> blocks before touching <code>,
    #    so we don't accidentally wrap the code-block <code> as inline.
    masked_pres: list[str] = []

    def mask_pre(m: re.Match) -> str:
        masked_pres.append(m.group(0))
        return f"\x00PRE{len(masked_pres) - 1}\x00"

    html = re.sub(r"<pre>.*?</pre>", mask_pre, html, flags=re.DOTALL)

    # 2. inline <code>...</code> — wrap inner text with sentinels.
    html = re.sub(
        r"(<code[^>]*>)(.*?)(</code>)",
        rf"\1{S_INLINE_CODE_OPEN}\2{S_INLINE_CODE_CLOSE}\3",
        html,
        flags=re.DOTALL,
    )

    # 3. <blockquote>...</blockquote> — place sentinels inside the first/last
    #    <p> so they land in text content (not as orphan text nodes which
    #    Drive may drop).
    def wrap_bq(m: re.Match) -> str:
        inner = m.group(1)
        # match styled paragraphs too, or the sentinel pair ends up
        # unbalanced
        inner = re.sub(r"(<p\b[^>]*>)", rf"\1{S_BLOCKQUOTE_OPEN}", inner, count=1)
        idx = inner.rfind("</p>")
        if idx != -1:
            inner = inner[:idx] + S_BLOCKQUOTE_CLOSE + inner[idx:]
        return f"<blockquote>{inner}</blockquote>"

    html = re.sub(r"<blockquote>(.*?)</blockquote>", wrap_bq, html, flags=re.DOTALL)

    # 4. Restore the <pre> blocks with sentinels injected inside.
    for i, pre in enumerate(masked_pres):
        pre = re.sub(
            r"(<pre><code[^>]*>)",
            rf"\1{S_CODE_BLOCK_OPEN}",
            pre,
            count=1,
        )
        pre = pre.replace("</code></pre>", f"{S_CODE_BLOCK_CLOSE}</code></pre>", 1)
        html = html.replace(f"\x00PRE{i}\x00", pre)

    return html


def inline_images_as_data_uri(html: str, base_dir: Path) -> str:
    """Inline local <img src> as base64 data URIs AND cap display width.
    Drive's HTML import treats <img>'s intrinsic pixel size as the embed size,
    which overflows the pageless area for wide PNGs. Setting an HTML width
    attribute (pixels) constrains the imported inline image. PT→PX uses 96
    DPI: MAX_CONTENT_WIDTH_PT × 96/72 ≈ 887 px."""

    max_w_px = int(round(MAX_CONTENT_WIDTH_PT * 96 / 72))

    def repl(match: re.Match) -> str:
        full_tag = match.group(0)
        src = match.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return full_tag
        img_path = (base_dir / src).resolve()
        if not img_path.exists():
            print(f"  [WARN] image not found: {img_path}")
            return full_tag

        mime, _ = mimetypes.guess_type(str(img_path))
        mime = mime or "image/png"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        with Image.open(img_path) as im:
            iw, ih = im.size
        if iw > max_w_px:
            new_w = max_w_px
            new_h = int(round(ih * max_w_px / iw))
        else:
            new_w, new_h = iw, ih

        new_tag = full_tag.replace(f'src="{src}"', f'src="{data_uri}"')
        new_tag = re.sub(r'\s+width="[^"]*"', "", new_tag)
        new_tag = re.sub(r'\s+height="[^"]*"', "", new_tag)
        new_tag = new_tag.replace(
            "<img ", f'<img width="{new_w}" height="{new_h}" ', 1
        )
        print(
            f"  inlined {img_path.name} "
            f"({len(b64) // 1024} KiB base64, {iw}×{ih} → {new_w}×{new_h} px)"
        )
        return new_tag

    return re.sub(r'<img\b[^>]*\bsrc="([^"]+)"[^>]*>', repl, html)


def wrap_html(body: str, title: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        f'<html><head><meta charset="utf-8"><title>{title}</title></head>'
        f"<body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Drive upload
# ---------------------------------------------------------------------------

def get_or_create_doc(
    drive,
    html_bytes: bytes,
    title: str,
    doc_id: str | None,
    folder_id: str | None,
) -> tuple[str, bool]:
    """Returns (doc_id, used_existing). If doc_id is supplied and the update
    succeeds, the existing Doc is replaced in place (URL, comments and
    sharing are preserved). Falls back to creating a new Doc otherwise."""
    if doc_id:
        try:
            drive.files().update(
                fileId=doc_id,
                media_body=MediaIoBaseUpload(
                    io.BytesIO(html_bytes), mimetype="text/html", resumable=False
                ),
            ).execute()
            return doc_id, True
        except Exception as e:
            print(f"  could not update existing doc {doc_id!r}: {e}")
            print("  creating a new doc instead")

    metadata: dict = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        metadata["parents"] = [folder_id]
    result = (
        drive.files()
        .create(
            body=metadata,
            media_body=MediaIoBaseUpload(
                io.BytesIO(html_bytes), mimetype="text/html", resumable=False
            ),
            fields="id",
        )
        .execute()
    )
    return result["id"], False


def move_to_folder(drive, doc_id: str, folder_id: str) -> None:
    """Move a Drive file into the specified folder."""
    file = drive.files().get(fileId=doc_id, fields="parents").execute()
    prev_parents = ",".join(file.get("parents", []))
    drive.files().update(
        fileId=doc_id,
        addParents=folder_id,
        removeParents=prev_parents,
        fields="id,parents",
    ).execute()
    print(f"      Moved to folder: {folder_id}")


# ---------------------------------------------------------------------------
# Docs API helpers
# ---------------------------------------------------------------------------

def set_pageless(docs, doc_id: str) -> None:
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "updateDocumentStyle": {
                        "documentStyle": {
                            "documentFormat": {"documentMode": "PAGELESS"}
                        },
                        "fields": "documentFormat.documentMode",
                    }
                }
            ]
        },
    ).execute()


def _rgb(color: dict) -> dict:
    return {"color": {"rgbColor": color}}


def find_sentinel_ranges(
    doc: dict, open_char: str, close_char: str
) -> list[tuple[int, int]]:
    """Walk text runs (including inside tables); return list of
    (open_index, close_index) pairs."""
    ranges: list[tuple[int, int]] = []
    pending_open: list[int | None] = [None]

    def visit(content_list: list) -> None:
        for elem in content_list:
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    tr = pe.get("textRun")
                    if not tr:
                        continue
                    content = tr.get("content", "")
                    base = pe["startIndex"]
                    for offset, ch in enumerate(content):
                        if ch == open_char:
                            pending_open[0] = base + offset
                        elif ch == close_char and pending_open[0] is not None:
                            ranges.append((pending_open[0], base + offset))
                            pending_open[0] = None
            elif "table" in elem:
                for row in elem["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        visit(cell.get("content", []))

    visit(doc.get("body", {}).get("content", []))
    return ranges


# -- Style-only walkers (these do not shift indices) ------------------------

def _paragraph_spacing_requests(doc: dict) -> list[dict]:
    """Drive's HTML importer leaves NORMAL_TEXT paragraphs with very little
    spaceBelow. Match the prior renderer's 4pt default. List items are left
    alone — Docs collapses spacing between them and the gap looks wrong."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        if para.get("paragraphStyle", {}).get("namedStyleType") != "NORMAL_TEXT":
            continue
        if para.get("bullet"):
            continue
        out.append({
            "updateParagraphStyle": {
                "range": {
                    "startIndex": elem["startIndex"],
                    "endIndex": elem["endIndex"],
                },
                "paragraphStyle": {"spaceBelow": {"magnitude": 4, "unit": "PT"}},
                "fields": "spaceBelow",
            }
        })
    return out


def _heading_space_requests(doc: dict) -> list[dict]:
    """Drive's HTML importer gives HEADING_N paragraphs almost no spaceAbove,
    making sections feel cramped. Set per-level spaceAbove explicitly."""
    above_by_level = {1: 24, 2: 18, 3: 14, 4: 10, 5: 8, 6: 6}
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        nst = para.get("paragraphStyle", {}).get("namedStyleType", "")
        if not nst.startswith("HEADING_"):
            continue
        try:
            level = int(nst.split("_")[1])
        except (IndexError, ValueError):
            continue
        out.append({
            "updateParagraphStyle": {
                "range": {
                    "startIndex": elem["startIndex"],
                    "endIndex": elem["endIndex"],
                },
                "paragraphStyle": {
                    "spaceAbove": {
                        "magnitude": above_by_level.get(level, 6),
                        "unit": "PT",
                    }
                },
                "fields": "spaceAbove",
            }
        })
    return out


def _table_width_requests(doc: dict) -> list[dict]:
    """Equal column widths summing to MAX_CONTENT_WIDTH_PT. Drive's default
    leaves tables narrow with whitespace on the right margin."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        table = elem.get("table")
        if not table:
            continue
        n_cols = table.get("columns", 0)
        if n_cols <= 0:
            continue
        out.append({
            "updateTableColumnProperties": {
                "tableStartLocation": {"index": elem["startIndex"]},
                "columnIndices": list(range(n_cols)),
                "tableColumnProperties": {
                    "widthType": "FIXED_WIDTH",
                    "width": {
                        "magnitude": MAX_CONTENT_WIDTH_PT // n_cols,
                        "unit": "PT",
                    },
                },
                "fields": "widthType,width",
            }
        })
    return out


def _table_cell_padding_requests(doc: dict) -> list[dict]:
    """Drive's default cell padding is tight. Bump to 6/8pt across the table."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        table = elem.get("table")
        if not table:
            continue
        n_rows = table.get("rows", 0)
        n_cols = table.get("columns", 0)
        if n_rows <= 0 or n_cols <= 0:
            continue
        out.append({
            "updateTableCellStyle": {
                "tableRange": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": elem["startIndex"]},
                        "rowIndex": 0,
                        "columnIndex": 0,
                    },
                    "rowSpan": n_rows,
                    "columnSpan": n_cols,
                },
                "tableCellStyle": {
                    "paddingTop": {"magnitude": 6, "unit": "PT"},
                    "paddingBottom": {"magnitude": 6, "unit": "PT"},
                    "paddingLeft": {"magnitude": 8, "unit": "PT"},
                    "paddingRight": {"magnitude": 8, "unit": "PT"},
                },
                "fields": "paddingTop,paddingBottom,paddingLeft,paddingRight",
            }
        })
    return out


def _table_decoration_requests(doc: dict) -> list[dict]:
    """Decorate each table like the reference Doc: hairline accent borders
    on every cell, an accent-filled header row with white text, and body
    rows alternating between white and a light tint. Cell content is
    centered vertically — the HTML importer gives the header row an extra
    line of height, which top-aligned text makes look lopsided."""

    def _border() -> dict:
        return {
            "color": _rgb(TABLE_BORDER_FG),
            "width": {"magnitude": TABLE_BORDER_WIDTH_PT, "unit": "PT"},
            "dashStyle": "SOLID",
        }

    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        table = elem.get("table")
        if not table:
            continue
        n_rows = table.get("rows", 0)
        n_cols = table.get("columns", 0)
        if n_rows <= 0 or n_cols <= 0:
            continue

        def row_range(row_index: int, row_span: int = 1) -> dict:
            return {
                "tableCellLocation": {
                    "tableStartLocation": {"index": elem["startIndex"]},
                    "rowIndex": row_index,
                    "columnIndex": 0,
                },
                "rowSpan": row_span,
                "columnSpan": n_cols,
            }

        out.append({
            "updateTableCellStyle": {
                "tableRange": row_range(0, n_rows),
                "tableCellStyle": {
                    "borderTop": _border(),
                    "borderBottom": _border(),
                    "borderLeft": _border(),
                    "borderRight": _border(),
                    "contentAlignment": "MIDDLE",
                },
                "fields": (
                    "borderTop,borderBottom,borderLeft,borderRight,"
                    "contentAlignment"
                ),
            }
        })

        # Row 0 is the header band; body rows alternate from white.
        for row_index in range(n_rows):
            if row_index == 0:
                fill = TABLE_HEADER_BG
            else:
                fill = WHITE if row_index % 2 == 1 else TABLE_STRIPE_BG
            out.append({
                "updateTableCellStyle": {
                    "tableRange": row_range(row_index),
                    "tableCellStyle": {"backgroundColor": _rgb(fill)},
                    "fields": "backgroundColor",
                }
            })

        # Header text: white, and un-bold whatever the <th> import applied
        # (the accent band already carries the emphasis).
        header_row = (table.get("tableRows") or [{}])[0]
        for cell in header_row.get("tableCells", []):
            for content_elem in cell.get("content", []):
                para = content_elem.get("paragraph")
                if not para:
                    continue
                for pe in para.get("elements", []):
                    tr = pe.get("textRun")
                    if not tr:
                        continue
                    content_len = len(tr.get("content", ""))
                    if content_len <= 0:
                        continue
                    out.append({
                        "updateTextStyle": {
                            "range": {
                                "startIndex": pe["startIndex"],
                                "endIndex": pe["startIndex"] + content_len,
                            },
                            "textStyle": {
                                "bold": False,
                                "foregroundColor": _rgb(TABLE_HEADER_FG),
                            },
                            "fields": "bold,foregroundColor",
                        }
                    })
    return out


def build_post_process_requests(doc: dict) -> list[dict]:
    """Build a single batchUpdate body.

    Order matters because deleteContentRange shifts indices:
      1. Non-shifting style-only requests (paragraph/heading spacing,
         table widths/padding, table decoration)
      2. Sentinel-based requests, processed by start-index DESC so each
         pair's deletes only shift indices higher than later (lower-index)
         pairs — which means those later pairs are unaffected.
    """

    requests: list[dict] = []

    # 0. Apply the house named styles document-wide (fonts, sizes,
    #    weights, colors, line spacing, heading margins).
    for style_type, style in NAMED_STYLE_PRESET.items():
        named_style = {"namedStyleType": style_type, **style}
        requests.append({
            "updateNamedStyle": {
                "namedStyle": named_style,
                "fields": NAMED_STYLE_FIELDS,
            }
        })

    # 1. Style-only requests first.
    requests.extend(_paragraph_spacing_requests(doc))
    requests.extend(_heading_space_requests(doc))
    requests.extend(_table_width_requests(doc))
    requests.extend(_table_cell_padding_requests(doc))
    requests.extend(_table_decoration_requests(doc))

    # 2. Sentinel-based styling + deletes.
    inline = [(s, e, "inline_code") for s, e in find_sentinel_ranges(
        doc, S_INLINE_CODE_OPEN, S_INLINE_CODE_CLOSE
    )]
    quotes = [(s, e, "blockquote") for s, e in find_sentinel_ranges(
        doc, S_BLOCKQUOTE_OPEN, S_BLOCKQUOTE_CLOSE
    )]
    blocks = [(s, e, "code_block") for s, e in find_sentinel_ranges(
        doc, S_CODE_BLOCK_OPEN, S_CODE_BLOCK_CLOSE
    )]

    print(
        f"      sentinels: inline_code={len(inline)}, "
        f"blockquote={len(quotes)}, code_block={len(blocks)}"
    )

    ops = inline + quotes + blocks
    ops.sort(key=lambda x: -x[0])  # descending start index

    for start, end, kind in ops:
        content_start = start + 1
        content_end = end  # exclusive of close sentinel position

        if kind == "inline_code":
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Roboto Mono"},
                        "foregroundColor": _rgb(INLINE_CODE_FG),
                        "backgroundColor": _rgb(INLINE_CODE_BG),
                    },
                    "fields": "weightedFontFamily,foregroundColor,backgroundColor",
                }
            })
        elif kind == "blockquote":
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "paragraphStyle": {
                        "borderLeft": {
                            "color": _rgb(BQ_BAR),
                            "width": {"magnitude": 3, "unit": "PT"},
                            "padding": {"magnitude": 12, "unit": "PT"},
                            "dashStyle": "SOLID",
                        },
                    },
                    "fields": "borderLeft",
                }
            })
        elif kind == "code_block":
            # spaceBelow 0 keeps code lines compact (the 4pt body-paragraph
            # spacing walker would otherwise stretch the block vertically);
            # the last line gets its bottom margin back via the follow-up
            # request on the close-sentinel paragraph.
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "paragraphStyle": {
                        "indentStart": {"magnitude": 18, "unit": "PT"},
                        "indentEnd": {"magnitude": 18, "unit": "PT"},
                        "shading": {"backgroundColor": _rgb(CODE_BLOCK_BG)},
                        "spaceBelow": {"magnitude": 0, "unit": "PT"},
                    },
                    "fields": "indentStart,indentEnd,shading,spaceBelow",
                }
            })
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": end, "endIndex": end + 1},
                    "paragraphStyle": {
                        "spaceBelow": {"magnitude": 8, "unit": "PT"},
                    },
                    "fields": "spaceBelow",
                }
            })
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Roboto Mono"},
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                        "foregroundColor": _rgb(CODE_BLOCK_FG),
                    },
                    "fields": "weightedFontFamily,fontSize,foregroundColor",
                }
            })

        # Delete close (higher index) first, then open. Both are at indices
        # >= start; later (lower-start) ops are unaffected.
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": end, "endIndex": end + 1}
            }
        })
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": start, "endIndex": start + 1}
            }
        })

    return requests


def post_process(docs, doc_id: str) -> None:
    doc = docs.documents().get(documentId=doc_id).execute()
    requests = build_post_process_requests(doc)
    if not requests:
        print("      no sentinels found; nothing to post-process")
        return
    print(f"      sending {len(requests)} batchUpdate requests")
    docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(
    md_path: str | Path,
    title: str | None = None,
    doc_id: str | None = None,
    folder_id: str | None = None,
) -> str:
    md_path = Path(md_path).resolve()
    if not md_path.exists():
        raise FileNotFoundError(md_path)

    print(f"[1/5] Reading {md_path}")
    md_text = md_path.read_text(encoding="utf-8")

    print("[2/5] Markdown → HTML + sentinel injection")
    html_body = md_to_html(md_text)
    html_body = compact_table_markup(html_body)
    html_body = inject_sentinels(html_body)

    print("[3/5] Inlining images as data URIs")
    html_body = inline_images_as_data_uri(html_body, md_path.parent)

    effective_title = title or md_path.stem
    full_html = wrap_html(html_body, effective_title)
    html_bytes = full_html.encode("utf-8")
    print(f"      HTML size: {len(html_bytes) // 1024} KiB")

    drive, docs = authenticate()

    print("[4/5] Uploading to Drive")
    final_doc_id, used_existing = get_or_create_doc(
        drive, html_bytes, effective_title, doc_id, folder_id
    )
    doc_url = f"https://docs.google.com/document/d/{final_doc_id}/edit"
    if used_existing:
        print(f"      Updated existing doc: {doc_url}")
    else:
        print(f"      Created new doc: {doc_url}")

    set_pageless(docs, final_doc_id)

    print("[5/5] Post-processing styles via Docs API")
    post_process(docs, final_doc_id)

    # If we updated an existing doc and a folder is requested, move it.
    # For newly created docs the parents were set on create.
    if used_existing and folder_id:
        move_to_folder(drive, final_doc_id, folder_id)

    print()
    print(f"  Doc URL: {doc_url}")
    return final_doc_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Convert Markdown to a Google Doc.")
    parser.add_argument("md_file", help="Path to the markdown file")
    parser.add_argument("--title", help="Doc title (defaults to the file stem)")
    parser.add_argument(
        "--doc-id",
        default=None,
        help="Existing Google Doc ID to overwrite (creates new if not found)",
    )
    parser.add_argument(
        "--folder-id",
        default=None,
        help="Drive folder ID to place the doc in",
    )
    args = parser.parse_args(argv)

    try:
        convert(
            md_path=args.md_file,
            title=args.title,
            doc_id=args.doc_id,
            folder_id=args.folder_id,
        )
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
