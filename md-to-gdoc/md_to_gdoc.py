#!/usr/bin/env python3
"""
md_to_gdoc.py — Convert a Markdown file to a Google Doc.

Pipeline:
    md → preprocess markdown text
       + inline local images as base64 data URIs (no separate Drive uploads),
         physically downscaled to the pageless content width when wider
    → Drive files.create OR files.update (media mimeType=text/markdown,
      target mimeType=google-apps.document) — Drive's native markdown
      importer renders fenced code blocks as REAL Google Docs code blocks
      (Insert → Building blocks → Code block) with language auto-detection
      and syntax highlighting
    → Docs API batchUpdate: spacing/table polish + inline-code and
      blockquote styling the importer leaves flat

Compared to the prior HTML-upload pipeline this:
    - Produces native Docs code blocks (language chip, copy button,
      syntax highlighting) instead of hand-styled shaded paragraphs
    - Removes all sentinel-character machinery (inject/find/style/delete)
    - Lets Google's own markdown importer handle tables (bold headers),
      lists, blockquotes, links, strikethrough
    - Drops the markdown-it-py / linkify-it-py dependencies

Key facts (verified empirically, 2026-07):
    - The Docs API batchUpdate has NO request type for code blocks; the
      ONLY programmatic way to create them is Drive markdown import.
    - In documents.get JSON, a native code block appears as NORMAL_TEXT
      paragraphs bracketed by U+E907 placeholder characters, with
      Roboto Mono runs and materialized syntax-highlight colors.
      The U+E907 markers are part of the widget — never delete them.
    - The importer ignores PNG DPI metadata and HTML width attributes;
      display size is always intrinsic pixels at 96 DPI. Wide images are
      NOT capped, so pixels must be downscaled before upload.

Usage:
    python3 md_to_gdoc.py <md_file> [--title TITLE] [--doc-id ID] [--folder-id ID]

Requirements:
    pip install google-auth google-auth-httplib2 google-api-python-client Pillow

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Placeholder character the Docs API uses for elements it cannot express
# (here: the code-block widget boundaries produced by the markdown importer).
CODE_BLOCK_MARKER = "\ue907"

# Content width budget (pageless mode).
MAX_CONTENT_WIDTH_PT = 665
# The markdown importer places images at intrinsic pixel size / 96 DPI.
MAX_IMG_WIDTH_PX = int(round(MAX_CONTENT_WIDTH_PT * 96 / 72))  # ≈ 886 px

# Inline-code colors (kept from the prior renderer; the importer itself
# only switches the font to Roboto Mono).
INLINE_CODE_FG = {"red": 0.780, "green": 0.145, "blue": 0.306}
INLINE_CODE_BG = {"red": 0.976, "green": 0.949, "blue": 0.957}
BQ_BAR = {"red": 0.6, "green": 0.6, "blue": 0.6}

# Paragraph indent (PT) the markdown importer assigns to blockquotes.
BLOCKQUOTE_INDENT_PT = 30


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
# Markdown preprocessing
# ---------------------------------------------------------------------------

def _encode_image(img_path: Path) -> tuple[str, int, int]:
    """Return (data_uri, width_px, height_px), downscaling to
    MAX_IMG_WIDTH_PX if wider. The importer has no display-size control,
    so pixel size IS display size (at 96 DPI)."""
    mime, _ = mimetypes.guess_type(str(img_path))
    mime = mime or "image/png"

    with Image.open(img_path) as im:
        iw, ih = im.size
        if iw > MAX_IMG_WIDTH_PX:
            new_w = MAX_IMG_WIDTH_PX
            new_h = max(1, int(round(ih * MAX_IMG_WIDTH_PX / iw)))
            im = im.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            if mime == "image/jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=90)
            else:
                im.save(buf, format="PNG")
                mime = "image/png"
            data = buf.getvalue()
            iw, ih = new_w, new_h
        else:
            data = img_path.read_bytes()

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", iw, ih


def _mask_code_regions(md_text: str):
    """Mask fenced code blocks and inline code spans so image rewriting
    never touches example markdown inside them. Returns (masked, restore)."""
    stash: list[str] = []

    def mask(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00CODE{len(stash) - 1}\x00"

    masked = re.sub(r"^(```|~~~).*?^\1\s*$", mask, md_text,
                    flags=re.DOTALL | re.MULTILINE)
    masked = re.sub(r"`[^`\n]+`", mask, masked)

    def restore(text: str) -> str:
        return re.sub(r"\x00CODE(\d+)\x00",
                      lambda m: stash[int(m.group(1))], text)

    return masked, restore


def inline_local_images(md_text: str, base_dir: Path) -> str:
    """Replace local image references (md syntax and raw <img> tags) with
    base64 data URIs, downscaled to the content width."""

    md_text, restore = _mask_code_regions(md_text)

    def is_local(src: str) -> bool:
        return not src.startswith(("http://", "https://", "data:"))

    def encode_or_none(src: str):
        img_path = (base_dir / src).resolve()
        if not img_path.exists():
            print(f"  [WARN] image not found: {img_path}")
            return None
        data_uri, w, h = _encode_image(img_path)
        print(f"  inlined {img_path.name} ({len(data_uri) // 1024} KiB, {w}×{h} px)")
        return data_uri

    def repl_md(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        if not is_local(src):
            return m.group(0)
        data_uri = encode_or_none(src)
        return m.group(0) if data_uri is None else f"![{alt}]({data_uri})"

    md_text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", repl_md, md_text)

    def repl_html(m: re.Match) -> str:
        src = m.group(1)
        if not is_local(src):
            return m.group(0)
        data_uri = encode_or_none(src)
        return m.group(0) if data_uri is None else m.group(0).replace(src, data_uri)

    md_text = re.sub(r'<img\b[^>]*\bsrc="([^"]+)"[^>]*>', repl_html, md_text)

    return restore(md_text)


# ---------------------------------------------------------------------------
# Drive upload
# ---------------------------------------------------------------------------

def get_or_create_doc(
    drive,
    md_bytes: bytes,
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
                    io.BytesIO(md_bytes), mimetype="text/markdown",
                    resumable=False,
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
                io.BytesIO(md_bytes), mimetype="text/markdown", resumable=False
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


def find_code_block_ranges(doc: dict) -> list[tuple[int, int]]:
    """Locate native code blocks: their content is bracketed by U+E907
    placeholder characters (the API's stand-in for the widget boundary).
    Returns [(open_index, close_index), ...] pairing markers in order."""
    marker_indices: list[int] = []

    def visit(content_list: list) -> None:
        for elem in content_list:
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    tr = pe.get("textRun")
                    if not tr:
                        continue
                    base = pe["startIndex"]
                    for offset, ch in enumerate(tr.get("content", "")):
                        if ch == CODE_BLOCK_MARKER:
                            marker_indices.append(base + offset)
            elif "table" in elem:
                for row in elem["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        visit(cell.get("content", []))

    visit(doc.get("body", {}).get("content", []))

    if len(marker_indices) % 2 != 0:
        print(
            f"  [WARN] odd number of code-block markers "
            f"({len(marker_indices)}); skipping the last one"
        )
        marker_indices = marker_indices[:-1]
    return [
        (marker_indices[i], marker_indices[i + 1])
        for i in range(0, len(marker_indices), 2)
    ]


def _in_code_block(start: int, end: int, code_ranges: list[tuple[int, int]]) -> bool:
    """True if [start, end) overlaps any code block (with 1 char of margin
    for the shaded frame paragraphs adjacent to the markers)."""
    return any(start <= ce + 1 and end >= cs - 1 for cs, ce in code_ranges)


# -- Style-only walkers (these do not shift indices) ------------------------

def _paragraph_spacing_requests(doc: dict, code_ranges) -> list[dict]:
    """The markdown importer leaves NORMAL_TEXT paragraphs with very little
    spaceBelow. Match the prior renderer's 4pt default. Code-block content
    is excluded to preserve the native widget's compact line spacing."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        if para.get("paragraphStyle", {}).get("namedStyleType") != "NORMAL_TEXT":
            continue
        if _in_code_block(elem["startIndex"], elem["endIndex"], code_ranges):
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
    """Per-level spaceAbove so sections don't feel cramped."""
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
    """Equal column widths summing to MAX_CONTENT_WIDTH_PT."""
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
    """Bump the importer's tight cell padding to 6/8pt."""
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


def _inline_code_requests(doc: dict, code_ranges) -> list[dict]:
    """The importer renders inline code as bare Roboto Mono. Re-add the
    fg/bg accent colors. Monospace runs inside native code blocks are the
    blocks' own content — leave those untouched."""
    out: list[dict] = []

    def visit(content_list: list) -> None:
        for elem in content_list:
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    tr = pe.get("textRun")
                    if not tr:
                        continue
                    font = (
                        tr.get("textStyle", {})
                        .get("weightedFontFamily", {})
                        .get("fontFamily", "")
                    )
                    if "Mono" not in font:
                        continue
                    start = pe["startIndex"]
                    end = start + len(tr.get("content", "").rstrip("\n"))
                    if end <= start:
                        continue
                    if _in_code_block(start, end, code_ranges):
                        continue
                    out.append({
                        "updateTextStyle": {
                            "range": {"startIndex": start, "endIndex": end},
                            "textStyle": {
                                "foregroundColor": _rgb(INLINE_CODE_FG),
                                "backgroundColor": _rgb(INLINE_CODE_BG),
                            },
                            "fields": "foregroundColor,backgroundColor",
                        }
                    })
            elif "table" in elem:
                for row in elem["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        visit(cell.get("content", []))

    visit(doc.get("body", {}).get("content", []))
    return out


def _blockquote_requests(doc: dict, code_ranges) -> list[dict]:
    """The importer renders blockquotes as plain indented paragraphs
    (indentStart == indentFirstLine == 30pt, no bullet). Add the left bar."""
    out: list[dict] = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para or para.get("bullet"):
            continue
        style = para.get("paragraphStyle", {})
        if style.get("namedStyleType") != "NORMAL_TEXT":
            continue
        indent = style.get("indentStart", {}).get("magnitude")
        first = style.get("indentFirstLine", {}).get("magnitude")
        if indent != BLOCKQUOTE_INDENT_PT or first != BLOCKQUOTE_INDENT_PT:
            continue
        if _in_code_block(elem["startIndex"], elem["endIndex"], code_ranges):
            continue
        out.append({
            "updateParagraphStyle": {
                "range": {
                    "startIndex": elem["startIndex"],
                    "endIndex": elem["endIndex"],
                },
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
    return out


def build_post_process_requests(doc: dict) -> list[dict]:
    """All requests are style-only (no index shifting), so relative order
    doesn't matter."""
    code_ranges = find_code_block_ranges(doc)
    print(f"      native code blocks detected: {len(code_ranges)}")

    requests: list[dict] = []
    requests.extend(_paragraph_spacing_requests(doc, code_ranges))
    requests.extend(_heading_space_requests(doc))
    requests.extend(_table_width_requests(doc))
    requests.extend(_table_cell_padding_requests(doc))
    requests.extend(_inline_code_requests(doc, code_ranges))
    requests.extend(_blockquote_requests(doc, code_ranges))
    return requests


def post_process(docs, doc_id: str) -> None:
    doc = docs.documents().get(documentId=doc_id).execute()
    requests = build_post_process_requests(doc)
    if not requests:
        print("      nothing to post-process")
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

    print("[2/5] Inlining images as data URIs")
    md_text = inline_local_images(md_text, md_path.parent)
    md_bytes = md_text.encode("utf-8")
    print(f"      markdown size: {len(md_bytes) // 1024} KiB")

    effective_title = title or md_path.stem
    drive, docs = authenticate()

    print("[3/5] Uploading to Drive (native markdown import)")
    final_doc_id, used_existing = get_or_create_doc(
        drive, md_bytes, effective_title, doc_id, folder_id
    )
    doc_url = f"https://docs.google.com/document/d/{final_doc_id}/edit"
    if used_existing:
        print(f"      Updated existing doc: {doc_url}")
    else:
        print(f"      Created new doc: {doc_url}")

    print("[4/5] Setting pageless mode")
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
