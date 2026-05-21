#!/usr/bin/env python3
"""
md_to_gdoc_html_spike.py — Spike: Markdown → HTML → Google Doc (via Drive's
built-in HTML converter). Images are inlined as base64 data URIs so they
never become standalone Drive files.

Goal: judge whether Drive's HTML→Doc conversion is faithful enough to
replace the current Docs-API `batchUpdate` rendering layer.

Usage:
    python3 md_to_gdoc_html_spike.py <md_file> [--title TITLE] [--folder-id ID]

Requires ADC with Drive scope (same as md_to_gdoc.py):
    gcloud auth application-default login \\
        --scopes=https://www.googleapis.com/auth/cloud-platform,\\
https://www.googleapis.com/auth/drive
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
    """Generate spike_test.png if missing — keeps no binaries in git."""
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
    md = MarkdownIt("gfm-like")  # tables, strikethrough, linkify
    return md.render(md_text)


def inline_images_as_data_uri(html: str, base_dir: Path) -> str:
    """Replace <img src="relative.png"> with data: URIs.
    Skips srcs that already start with http/https/data:."""

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
        new_tag = full_tag.replace(f'src="{src}"', f'src="{data_uri}"')
        print(f"  inlined {img_path.name} ({len(b64) // 1024} KiB base64)")
        return new_tag

    return re.sub(r'<img\b[^>]*\bsrc="([^"]+)"[^>]*>', repl, html)


def wrap_html(body: str, title: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        f"<html><head><meta charset=\"utf-8\"><title>{title}</title></head>"
        f"<body>{body}</body></html>"
    )


def upload_as_doc(drive, html_bytes: bytes, title: str, folder_id: str | None) -> str:
    metadata = {
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
    """Match the production script's PAGELESS layout."""
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


def convert(md_path: Path, title: str | None, folder_id: str | None) -> str:
    print(f"[1/5] Reading {md_path}")
    md_text = md_path.read_text(encoding="utf-8")
    ensure_test_image(md_path.parent)

    print("[2/5] Markdown → HTML (markdown-it-py, gfm-like)")
    html_body = md_to_html(md_text)

    print("[3/5] Inlining images as data URIs")
    html_body = inline_images_as_data_uri(html_body, md_path.parent)

    full_html = wrap_html(html_body, title or md_path.stem)
    html_bytes = full_html.encode("utf-8")
    print(f"       HTML size: {len(html_bytes) // 1024} KiB")

    # Stash a copy next to the .md for offline inspection
    debug_path = md_path.with_suffix(".html")
    debug_path.write_text(full_html, encoding="utf-8")
    print(f"       saved local copy: {debug_path}")

    print("[4/5] Uploading to Drive as Google Doc")
    drive, docs = authenticate()
    doc_id = upload_as_doc(drive, html_bytes, title or md_path.stem, folder_id)

    print("[5/5] Setting PAGELESS layout")
    try:
        set_pageless(docs, doc_id)
    except Exception as e:
        print(f"  [WARN] could not set PAGELESS: {e}")

    return doc_id


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("md_file", help="Path to markdown file")
    parser.add_argument("--title", help="Doc title (defaults to file stem)")
    parser.add_argument("--folder-id", help="Drive folder ID to place doc in")
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
