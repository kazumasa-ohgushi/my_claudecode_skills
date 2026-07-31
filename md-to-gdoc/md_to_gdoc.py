#!/usr/bin/env python3
"""
md_to_gdoc.py — Convert a Markdown file to a Google Doc.

Pipeline:
    md → HTML (markdown-it-py, gfm-like)
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
    md = MarkdownIt("gfm-like")  # tables, strikethrough, linkify
    return md.render(md_text)


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
        inner = re.sub(r"(<p>)", rf"\1{S_BLOCKQUOTE_OPEN}", inner, count=1)
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
    spaceBelow. Match the prior renderer's 4pt default."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        if para.get("paragraphStyle", {}).get("namedStyleType") != "NORMAL_TEXT":
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


def _table_header_bold_requests(doc: dict) -> list[dict]:
    """Bold every text run in row 0 of each table (markdown convention)."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        table = elem.get("table")
        if not table:
            continue
        rows = table.get("tableRows", [])
        if not rows:
            continue
        for cell in rows[0].get("tableCells", []):
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
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    })
    return out


def build_post_process_requests(doc: dict) -> list[dict]:
    """Build a single batchUpdate body.

    Order matters because deleteContentRange shifts indices:
      1. Non-shifting style-only requests (paragraph/heading spacing,
         table widths/padding, table header bold)
      2. Sentinel-based requests, processed by start-index DESC so each
         pair's deletes only shift indices higher than later (lower-index)
         pairs — which means those later pairs are unaffected.
    """

    requests: list[dict] = []

    # 0. Document-wide 115% line spacing on body text. The HTML importer
    #    leaves the NORMAL_TEXT named style at 100, which reads cramped;
    #    115 matches what Docs' own markdown importer produces.
    requests.append({
        "updateNamedStyle": {
            "namedStyle": {
                "namedStyleType": "NORMAL_TEXT",
                "paragraphStyle": {"lineSpacing": 115},
            },
            "fields": "namedStyleType,paragraphStyle.lineSpacing",
        }
    })

    # 1. Style-only requests first.
    requests.extend(_paragraph_spacing_requests(doc))
    requests.extend(_heading_space_requests(doc))
    requests.extend(_table_width_requests(doc))
    requests.extend(_table_cell_padding_requests(doc))
    requests.extend(_table_header_bold_requests(doc))

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
