#!/usr/bin/env python3
"""
md_to_gdoc_html_spike_v2.py — Spike 2: HTML conversion + Docs API
post-process to restore styling Drive's HTML→Doc converter drops.

Pipeline:
    md → HTML (markdown-it-py, gfm-like)
        + inject math-bracket sentinels around <code>, <pre>, <blockquote>
    → inline images as data URIs
    → Drive files.create (mimeType=application/vnd.google-apps.document)
    → Docs API: fetch doc, locate sentinels, batchUpdate to apply styles
      and delete sentinels (reverse-order, single batch)

Styles re-applied:
    - inline code: Courier New + light gray background
    - code block: paragraph shading (gray)
    - blockquote: paragraph borderLeft (gray bar) + indent

Usage:
    python3 md_to_gdoc_html_spike_v2.py <md_file> [--title TITLE] [--folder-id ID]
"""

import argparse
import base64
import io
import mimetypes
import re
import sys
from pathlib import Path

import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from markdown_it import MarkdownIt


# Sentinels: math-bracket Unicode chars. PUA (U+E000-U+F8FF) gets collapsed
# to U+E907 by Drive's HTML converter; these math chars survive distinctly.
# Each is single-codepoint to keep the finder logic simple.
S_INLINE_CODE_OPEN = "⟦"   # MATHEMATICAL LEFT WHITE SQUARE BRACKET
S_INLINE_CODE_CLOSE = "⟧"  # MATHEMATICAL RIGHT WHITE SQUARE BRACKET
S_BLOCKQUOTE_OPEN = "⦃"    # LEFT WHITE CURLY BRACKET
S_BLOCKQUOTE_CLOSE = "⦄"   # RIGHT WHITE CURLY BRACKET
S_CODE_BLOCK_OPEN = "⌈"    # LEFT CEILING
S_CODE_BLOCK_CLOSE = "⌉"   # RIGHT CEILING


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


def ensure_test_image(md_dir: Path) -> None:
    img_path = md_dir / "spike_test.png"
    if img_path.exists():
        return
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (480, 240), (245, 248, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 460, 220], outline=(60, 90, 160), width=3)
    for i, bar_h in enumerate([60, 120, 90, 180, 130]):
        x = 60 + i * 80
        draw.rectangle([x, 200 - bar_h, x + 50, 200], fill=(80, 130, 220))
    draw.text((30, 30), "Spike sample chart", fill=(20, 30, 80))
    img.save(img_path, "PNG")
    print(f"  generated {img_path.name}")


def md_to_html(md_text: str) -> str:
    md = MarkdownIt("gfm-like")
    return md.render(md_text)


def inject_sentinels(html: str) -> str:
    """Wrap target HTML regions with PUA sentinels. The sentinels become
    text content; Drive's converter preserves text. We locate them in the
    resulting Doc and use them to identify ranges to re-style."""

    # 1. <pre><code>...</code></pre> — code blocks. Do this BEFORE inline
    #    code so we can mask <pre> blocks out.
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

    # 3. <blockquote> ... </blockquote> — place sentinels inside the
    #    first/last <p> so they land in text content (not as orphan text
    #    nodes which Drive may drop).
    def wrap_bq(m: re.Match) -> str:
        inner = m.group(1)
        inner = re.sub(r"(<p>)", rf"\1{S_BLOCKQUOTE_OPEN}", inner, count=1)
        # Replace the LAST </p>
        idx = inner.rfind("</p>")
        if idx != -1:
            inner = inner[:idx] + S_BLOCKQUOTE_CLOSE + inner[idx:]
        return f"<blockquote>{inner}</blockquote>"

    html = re.sub(r"<blockquote>(.*?)</blockquote>", wrap_bq, html, flags=re.DOTALL)

    # 4. Restore <pre> blocks with sentinels injected inside.
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
    """Inline local <img src> as base64 data URIs AND cap display width
    at TABLE_WIDTH_PT-equivalent pixels. Drive's HTML import uses the
    image's intrinsic pixel size if no width/height is specified, which
    can overflow the pageless content area for wide PNGs. Setting the
    HTML width attribute (treated as pixels) constrains the imported
    inline image's size."""

    # 1pt ≈ 1.333 px at 96 DPI (CSS reference); cap so display width
    # matches production's MAX_IMG_WIDTH_PT (665pt).
    max_w_px = int(round(TABLE_WIDTH_PT * 96 / 72))  # 887

    from PIL import Image

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

        # Replace src, then ensure width/height attributes are set
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


def upload_as_doc(drive, html_bytes: bytes, title: str, folder_id: str | None) -> str:
    metadata: dict = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
    }
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaIoBaseUpload(
        io.BytesIO(html_bytes), mimetype="text/html", resumable=False
    )
    result = (
        drive.files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()
    )
    return result["id"]


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


def find_sentinel_ranges(
    doc: dict, open_char: str, close_char: str
) -> list[tuple[int, int]]:
    """Walk text runs (including inside tables), return list of
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


# Production colors (match md_to_gdoc.py)
INLINE_CODE_FG = {"red": 0.780, "green": 0.145, "blue": 0.306}
INLINE_CODE_BG = {"red": 0.976, "green": 0.949, "blue": 0.957}
CODE_BLOCK_FG = {"red": 0.133, "green": 0.133, "blue": 0.133}
CODE_BLOCK_BG = {"red": 0.949, "green": 0.953, "blue": 0.957}
BQ_BAR = {"red": 0.6, "green": 0.6, "blue": 0.6}

# Page width budget for FIXED_WIDTH tables (matches production MAX_IMG_WIDTH_PT)
TABLE_WIDTH_PT = 665


def _rgb(color: dict) -> dict:
    return {"color": {"rgbColor": color}}


def _collect_paragraph_spacing_requests(doc: dict) -> list[dict]:
    """Tighten the Drive default by setting spaceBelow=4pt on every
    NORMAL_TEXT paragraph (matches production, which always sets this)."""
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
                "paragraphStyle": {
                    "spaceBelow": {"magnitude": 4, "unit": "PT"}
                },
                "fields": "spaceBelow",
            }
        })
    return out


def _collect_heading_space_requests(doc: dict) -> list[dict]:
    """Drive's HTML import gives HEADING_N paragraphs almost no spaceAbove,
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


def _collect_table_cell_padding_requests(doc: dict) -> list[dict]:
    """Drive's default cell padding feels tight. Bump to ~6pt vertical,
    8pt horizontal across every cell in every table."""
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


def _collect_table_header_bold_requests(doc: dict) -> list[dict]:
    """Apply bold to every text run in row 0 of each table (matches
    production's markdown-table convention of bold headers)."""
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
                    # Skip the trailing newline character so we don't bold
                    # an empty range.
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


def _collect_table_width_requests(doc: dict) -> list[dict]:
    """Force equal column widths summing to TABLE_WIDTH_PT (matches
    production). Drive's default leaves tables narrow with whitespace
    on the right margin."""
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
                        "magnitude": TABLE_WIDTH_PT // n_cols,
                        "unit": "PT",
                    },
                },
                "fields": "widthType,width",
            }
        })
    return out


def build_post_process_requests(doc: dict) -> list[dict]:
    """Build a single batchUpdate body.

    Order matters because deleteContentRange shifts indices:
      1. Non-shifting style-only requests (paragraph spacing, table widths)
      2. Sentinel-based requests, processed by start-index DESC so each
         pair's deletes only shift indices higher than later (lower-index)
         pairs — which means those later pairs are unaffected.
    """

    requests: list[dict] = []

    # 1. Stage non-shifting style requests first (no index shifts here).
    requests.extend(_collect_paragraph_spacing_requests(doc))
    requests.extend(_collect_heading_space_requests(doc))
    requests.extend(_collect_table_width_requests(doc))
    requests.extend(_collect_table_cell_padding_requests(doc))
    requests.extend(_collect_table_header_bold_requests(doc))

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
        f"  found sentinels: inline_code={len(inline)}, "
        f"blockquote={len(quotes)}, code_block={len(blocks)}"
    )

    ops = inline + quotes + blocks
    ops.sort(key=lambda x: -x[0])  # descending start index

    for start, end, kind in ops:
        content_start = start + 1  # first char after open sentinel
        content_end = end  # exclusive; = position of close sentinel

        if kind == "inline_code":
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Courier New"},
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
            # Paragraph-level: shading + indent
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "paragraphStyle": {
                        "indentStart": {"magnitude": 18, "unit": "PT"},
                        "indentEnd": {"magnitude": 18, "unit": "PT"},
                        "shading": {"backgroundColor": _rgb(CODE_BLOCK_BG)},
                    },
                    "fields": "indentStart,indentEnd,shading",
                }
            })
            # Text-level: smaller font + lighter foreground (match production)
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": content_start, "endIndex": content_end},
                    "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Courier New"},
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                        "foregroundColor": _rgb(CODE_BLOCK_FG),
                    },
                    "fields": "weightedFontFamily,fontSize,foregroundColor",
                }
            })

        # Delete close sentinel first (higher index), then open. Both deletes
        # are at indices >= start, and the next op processed has a smaller
        # start, so its indices are unaffected.
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
    print("[6/6] Post-processing styles via Docs API")
    doc = docs.documents().get(documentId=doc_id).execute()
    requests = build_post_process_requests(doc)
    if not requests:
        print("  no sentinels found; nothing to post-process")
        return
    print(f"  sending {len(requests)} requests")
    docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()


def convert(md_path: Path, title: str | None, folder_id: str | None) -> str:
    print(f"[1/6] Reading {md_path}")
    md_text = md_path.read_text(encoding="utf-8")
    # Only generate test_image when the canned test_input.md references it
    if "spike_test.png" in md_text:
        ensure_test_image(md_path.parent)

    print("[2/6] Markdown → HTML + sentinel injection")
    html_body = md_to_html(md_text)
    html_body = inject_sentinels(html_body)

    print("[3/6] Inlining images as data URIs")
    html_body = inline_images_as_data_uri(html_body, md_path.parent)

    full_html = wrap_html(html_body, title or md_path.stem)
    html_bytes = full_html.encode("utf-8")
    print(f"       HTML size: {len(html_bytes) // 1024} KiB")

    debug_path = md_path.with_suffix(".v2.html")
    debug_path.write_text(full_html, encoding="utf-8")
    print(f"       saved local copy: {debug_path}")

    print("[4/6] Uploading to Drive as Google Doc")
    drive, docs = authenticate()
    doc_id = upload_as_doc(drive, html_bytes, title or md_path.stem, folder_id)

    print("[5/6] Setting PAGELESS layout")
    try:
        set_pageless(docs, doc_id)
    except Exception as e:
        print(f"  [WARN] could not set PAGELESS: {e}")

    post_process(docs, doc_id)
    return doc_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("md_file")
    parser.add_argument("--title")
    parser.add_argument("--folder-id")
    args = parser.parse_args(argv)

    md_path = Path(args.md_file).resolve()
    if not md_path.exists():
        print(f"ERROR: file not found: {md_path}", file=sys.stderr)
        return 1

    doc_id = convert(md_path, args.title, args.folder_id)
    print()
    print(f"  Doc ID:  {doc_id}")
    print(f"  Doc URL: https://docs.google.com/document/d/{doc_id}/edit")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
