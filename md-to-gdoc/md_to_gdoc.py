#!/usr/bin/env python3
"""
md_to_gdoc.py - Convert a Markdown file to a Google Doc.

Usage:
    python tools/md_to_gdoc.py <md_file> [--title TITLE]

Requirements (already in venv):
    google-api-python-client, google-auth-httplib2, Pillow

ADC must have Drive + Docs scope:
    gcloud auth application-default login \
        --scopes=https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/drive
"""

import re
import sys
import argparse
from pathlib import Path

from PIL import Image
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


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
    return build("drive", "v3", credentials=creds), build("docs", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------

_image_cache: dict[str, tuple[str, str, int, int]] = {}  # path -> (url, file_id, w_pt, h_pt)

MAX_IMG_WIDTH_PT = 665  # 9.24 inches × 72 pt/in — fills pageless content width


def upload_image(drive, path: Path) -> tuple[str, str, int, int]:
    """Upload PNG to Drive (public), return (url, file_id, width_pt, height_pt).
    The file should be deleted from Drive after the image is embedded in the doc."""
    key = str(path.resolve())
    if key in _image_cache:
        return _image_cache[key]

    with Image.open(path) as img:
        px_w, px_h = img.size
        dpi = img.info.get("dpi", (96, 96))[0]

    w_pt = min(px_w * 72.0 / dpi, MAX_IMG_WIDTH_PT)
    h_pt = w_pt * px_h / px_w

    f = drive.files().create(
        body={"name": path.name},
        media_body=MediaFileUpload(str(path), mimetype="image/png"),
        fields="id",
    ).execute()
    drive.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}
    ).execute()

    url = f"https://drive.google.com/uc?export=download&id={f['id']}"
    result = (url, f["id"], int(w_pt), int(h_pt))
    _image_cache[key] = result
    print(f"    Uploaded: {path.name} ({int(w_pt)}×{int(h_pt)} pt)")
    return result


def delete_drive_files(drive, file_ids: list[str]) -> None:
    """Delete Drive files by ID (call after images are embedded in the doc)."""
    for fid in file_ids:
        drive.files().delete(fileId=fid).execute()
    if file_ids:
        print(f"    Deleted {len(file_ids)} temporary Drive file(s)")


# ---------------------------------------------------------------------------
# Markdown parser → list of blocks
# ---------------------------------------------------------------------------

def parse_inline(text: str) -> list[tuple[str, bool, bool]]:
    """Return (text, bold, code) runs. Handles **bold**, `code`, **`nested`**."""
    runs: list[tuple[str, bool, bool]] = []

    def split_codes(s: str, bold: bool) -> None:
        last = 0
        for m in re.finditer(r"`(.+?)`", s):
            if m.start() > last:
                runs.append((s[last:m.start()], bold, False))
            runs.append((m.group(1), bold, True))
            last = m.end()
        if last < len(s):
            runs.append((s[last:], bold, False))

    last = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        if m.start() > last:
            split_codes(text[last:m.start()], bold=False)
        split_codes(m.group(1), bold=True)
        last = m.end()
    if last < len(text):
        split_codes(text[last:], bold=False)

    return runs or [(text, False, False)]


def _is_special(line: str) -> bool:
    return bool(
        line.startswith(("#", "|", "!", "> "))
        or re.match(r"^(---+|[-*]\s|\d+\.\s)", line)
    )


def _parse_table_lines(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    return rows


def parse_md(md_path: Path) -> list[dict]:
    """Parse markdown file into a flat list of block dicts."""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    blocks: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if m := re.match(r"^(#{1,3})\s+(.+)", line):
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1

        elif re.match(r"^---+$", line):
            blocks.append({"type": "hr"})
            i += 1

        elif m := re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", line):
            blocks.append({"type": "image", "alt": m.group(1), "path": (md_path.parent / m.group(2)).resolve()})
            i += 1

        elif line.startswith("> "):
            blocks.append({"type": "quote", "text": line[2:]})
            i += 1

        elif line.startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            if rows := _parse_table_lines(tbl):
                blocks.append({"type": "table", "rows": rows})

        elif re.match(r"^[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                items.append(re.sub(r"^[-*]\s+", "", lines[i]))
                i += 1
            blocks.append({"type": "list", "items": items})

        elif re.match(r"^\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i]))
                i += 1
            blocks.append({"type": "ordered_list", "items": items})

        elif line.strip() == "":
            i += 1

        else:
            para = []
            while i < len(lines) and lines[i].strip() and not _is_special(lines[i]):
                para.append(lines[i])
                i += 1
            if para:
                blocks.append({"type": "paragraph", "text": " ".join(para)})

    return blocks


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------

class DocBuilder:
    """Accumulates Google Docs API batchUpdate requests and flushes them."""

    def __init__(self, docs, doc_id: str):
        self.docs = docs
        self.doc_id = doc_id
        self.requests: list[dict] = []
        self.idx = 1

    # -- primitives --

    def _ins(self, text: str) -> None:
        if text:
            self.requests.append({"insertText": {"location": {"index": self.idx}, "text": text}})
            self.idx += len(text)

    def _para_style(self, start: int, end: int, named: str, space_below_pt: int = 0) -> None:
        style: dict = {"namedStyleType": named}
        fields = "namedStyleType"
        if space_below_pt:
            style["spaceBelow"] = {"magnitude": space_below_pt, "unit": "PT"}
            fields += ",spaceBelow"
        self.requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": style,
                "fields": fields,
            }
        })

    @staticmethod
    def _text_style_req(start: int, end: int, bold: bool, code: bool) -> dict | None:
        """Build an updateTextStyle request dict, or None if no styles apply."""
        style: dict = {}
        fields: list[str] = []
        if bold:
            style["bold"] = True
            fields.append("bold")
        if code:
            style["weightedFontFamily"] = {"fontFamily": "Courier New"}
            style["foregroundColor"] = {"color": {"rgbColor": {"red": 0.780, "green": 0.145, "blue": 0.306}}}
            style["backgroundColor"] = {"color": {"rgbColor": {"red": 0.976, "green": 0.949, "blue": 0.957}}}
            fields += ["weightedFontFamily", "foregroundColor", "backgroundColor"]
        if not fields:
            return None
        return {
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": style,
                "fields": ",".join(fields),
            }
        }

    def _text_style(self, start: int, end: int, bold: bool, code: bool) -> None:
        if req := self._text_style_req(start, end, bold, code):
            self.requests.append(req)

    def flush(self) -> None:
        if self.requests:
            self.docs.documents().batchUpdate(
                documentId=self.doc_id, body={"requests": self.requests}
            ).execute()
            self.requests = []

    def _sync_idx(self) -> None:
        """Re-read doc to get the current end index (needed after table ops)."""
        content = self.docs.documents().get(documentId=self.doc_id).execute().get("body", {}).get("content", [])
        self.idx = content[-1]["endIndex"] - 1 if content else 1

    # -- inline helpers (shared by paragraph / quote / list_item) --

    def _insert_inline_runs(self, runs: list[tuple[str, bool, bool]]) -> tuple[int, list[tuple[int, int, bool, bool]]]:
        """Insert runs at current position. Returns (para_start, spans)."""
        s = self.idx
        spans: list[tuple[int, int, bool, bool]] = []
        for text, bold, code in runs:
            rs = self.idx
            self._ins(text)
            spans.append((rs, self.idx, bold, code))
        return s, spans

    def _apply_inline_styles(self, spans: list[tuple[int, int, bool, bool]]) -> None:
        for rs, re_, bold, code in spans:
            if bold or code:
                self._text_style(rs, re_, bold, code)

    # -- block writers --

    def heading(self, text: str, level: int) -> None:
        s = self.idx
        self._ins(text + "\n")
        self._para_style(s, self.idx, f"HEADING_{level}")

    def paragraph(self, text: str) -> None:
        runs = parse_inline(text)
        # Entirely-bold paragraphs (e.g. "**主な発見:**") act as section labels:
        # omit spaceBelow so they sit flush against the content that follows.
        all_bold = all(bold for rt, bold, _ in runs if rt.strip())
        s, spans = self._insert_inline_runs(runs)
        self._ins("\n")
        self._para_style(s, self.idx, "NORMAL_TEXT", space_below_pt=0 if all_bold else 8)
        self._apply_inline_styles(spans)

    def quote(self, text: str) -> None:
        runs = parse_inline(text)
        s, spans = self._insert_inline_runs(runs)
        self._ins("\n")
        self.requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": s, "endIndex": self.idx},
                "paragraphStyle": {
                    "namedStyleType": "NORMAL_TEXT",
                    "spaceBelow": {"magnitude": 8, "unit": "PT"},
                    "indentStart": {"magnitude": 36, "unit": "PT"},
                    "indentFirstLine": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "namedStyleType,spaceBelow,indentStart,indentFirstLine",
            }
        })
        self._apply_inline_styles(spans)

    def list_item(self, text: str, ordered: bool = False) -> None:
        runs = parse_inline(text)
        s, spans = self._insert_inline_runs(runs)
        self._ins("\n")
        self.requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": s, "endIndex": self.idx},
                "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN" if ordered else "BULLET_DISC_CIRCLE_SQUARE",
            }
        })
        self._para_style(s, self.idx, "NORMAL_TEXT", space_below_pt=4)
        self._apply_inline_styles(spans)

    def image(self, url: str, w_pt: int, h_pt: int) -> None:
        """Insert an inline image in its own centered paragraph."""
        s = self.idx
        self.requests.append({
            "insertInlineImage": {
                "location": {"index": self.idx},
                "uri": url,
                "objectSize": {
                    "height": {"magnitude": h_pt, "unit": "PT"},
                    "width": {"magnitude": w_pt, "unit": "PT"},
                },
            }
        })
        self.idx += 1  # inline image counts as 1 character
        self._ins("\n")
        self.requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": s, "endIndex": self.idx},
                "paragraphStyle": {"alignment": "CENTER"},
                "fields": "alignment",
            }
        })

    def table(self, rows: list[list[str]]) -> None:
        """Insert a table. Flushes pending requests, inserts structure, fills cells."""
        if not rows:
            return
        n_rows, n_cols = len(rows), max(len(r) for r in rows)

        self.flush()
        self._sync_idx()

        # Insert empty table structure
        self.docs.documents().batchUpdate(
            documentId=self.doc_id,
            body={"requests": [{"insertTable": {"rows": n_rows, "columns": n_cols, "location": {"index": self.idx}}}]},
        ).execute()

        # Re-read to find cell indices and table start (single pass)
        content = self.docs.documents().get(documentId=self.doc_id).execute().get("body", {}).get("content", [])
        table_el = table_start = None
        for el in reversed(content):
            if "table" in el:
                table_el, table_start = el["table"], el["startIndex"]
                break
        if not table_el:
            self._sync_idx()
            return

        # Build cell fill requests (collected then executed in reverse index order)
        cell_groups: list[tuple[int, list[dict]]] = []
        for r_i, (row_el, row_data) in enumerate(zip(table_el["tableRows"], rows)):
            for cell_el, raw_cell in zip(row_el["tableCells"], row_data):
                raw_text = raw_cell.strip()
                if not raw_text or not cell_el.get("content"):
                    continue
                para_start = cell_el["content"][0]["startIndex"]
                runs = parse_inline(raw_text)
                plain = "".join(t for t, _, _ in runs)

                reqs: list[dict] = [
                    {"insertText": {"location": {"index": para_start}, "text": plain}}
                ]
                if r_i == 0:  # bold header row
                    reqs.append({
                        "updateTextStyle": {
                            "range": {"startIndex": para_start, "endIndex": para_start + len(plain)},
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    })
                offset = 0
                for run_text, bold, code in runs:
                    if (bold or code) and (req := self._text_style_req(
                        para_start + offset, para_start + offset + len(run_text), bold, code
                    )):
                        reqs.append(req)
                    offset += len(run_text)

                cell_groups.append((para_start, reqs))

        if cell_groups:
            cell_groups.sort(key=lambda x: x[0], reverse=True)
            self.docs.documents().batchUpdate(
                documentId=self.doc_id,
                body={"requests": [req for _, reqs in cell_groups for req in reqs]},
            ).execute()

        # Set equal column widths so table spans full content width
        self.docs.documents().batchUpdate(
            documentId=self.doc_id,
            body={"requests": [{
                "updateTableColumnProperties": {
                    "tableStartLocation": {"index": table_start},
                    "columnIndices": list(range(n_cols)),
                    "tableColumnProperties": {
                        "widthType": "FIXED_WIDTH",
                        "width": {"magnitude": int(MAX_IMG_WIDTH_PT / n_cols), "unit": "PT"},
                    },
                    "fields": "widthType,width",
                }
            }]},
        ).execute()

        # Add a spacing paragraph after the table
        self._sync_idx()
        s = self.idx
        self._ins("\n")
        self._para_style(s, self.idx, "NORMAL_TEXT", space_below_pt=4)
        self.flush()


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------

def convert(md_path: str | Path, title: str | None = None) -> str:
    md_path = Path(md_path)
    if not md_path.exists():
        sys.exit(f"File not found: {md_path}")
    if title is None:
        title = md_path.stem

    print("[1/5] Authenticating...")
    drive, docs = authenticate()

    print(f"[2/5] Parsing {md_path.name}...")
    blocks = parse_md(md_path)
    print(f"      {len(blocks)} blocks found")

    print(f"[3/5] Creating Google Doc: {title!r}")
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    print(f"      {doc_url}")
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"updateDocumentStyle": {
            "documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
            "fields": "documentFormat.documentMode",
        }}]},
    ).execute()

    print("[4/5] Building document...")
    builder = DocBuilder(docs, doc_id)
    uploaded_file_ids: list[str] = []

    for block in blocks:
        btype = block["type"]
        if btype == "heading":
            builder.heading(block["text"], block["level"])
        elif btype == "paragraph":
            builder.paragraph(block["text"])
        elif btype == "quote":
            builder.quote(block["text"])
        elif btype == "list":
            for item in block["items"]:
                builder.list_item(item)
        elif btype == "ordered_list":
            for item in block["items"]:
                builder.list_item(item, ordered=True)
        elif btype == "image":
            img_path: Path = block["path"]
            if img_path.exists():
                print(f"  [img] {img_path.name}")
                url, file_id, w, h = upload_image(drive, img_path)
                uploaded_file_ids.append(file_id)
                builder.image(url, w, h)
            else:
                print(f"  [WARN] Image not found: {img_path}")
        elif btype == "table":
            print(f"  [tbl] {len(block['rows'])} rows × {max(len(r) for r in block['rows'])} cols")
            builder.table(block["rows"])
        # hr: intentionally skipped

    print("[5/5] Flushing final requests...")
    builder.flush()

    # Images are now embedded in the doc — delete the temporary Drive files
    delete_drive_files(drive, uploaded_file_ids)

    print(f"\nDone! {doc_url}")
    return doc_url


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert a Markdown file to a Google Doc")
    parser.add_argument("md_file", help="Path to the .md file")
    parser.add_argument("--title", default=None, help="Google Doc title (default: filename stem)")
    args = parser.parse_args()
    convert(args.md_file, args.title)
