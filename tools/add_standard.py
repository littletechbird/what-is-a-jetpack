#!/usr/bin/env python3
"""Add one document to an existing jetpack.

Writes text/<doc_id>/pNNNN.txt and a docs row. Contact details are scrubbed
with tools/redact.py. Chunks are left to tools/reindex.py.

  python tools/add_standard.py <file> <doc_id> "Title" [flags] [topic]
"""
import argparse
import os
import sys

from home import safe_doc_id
from ingest import (
    catalog_note,
    connect_db,
    extract,
    prepare_pages,
    sha16,
    stored_source,
    upsert_catalog_row,
    write_page_files,
)
from redact import redact

EDITION_NOTE = (
    "EDITION NOTE: older edition kept for reference; verify against the current edition.\n\n"
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add one pdf, docx, html, or txt file to the jetpack.")
    parser.add_argument("file")
    parser.add_argument("doc_id")
    parser.add_argument("title")
    parser.add_argument("flags", nargs="?", default="")
    parser.add_argument("topic", nargs="?", default="standards")
    args = parser.parse_args(argv)
    try:
        doc_id = safe_doc_id(args.doc_id)
    except ValueError as exc:
        sys.exit(str(exc))
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit(f"file not found: {os.path.basename(path)}")
    try:
        pages, kind, label = extract(path)
    except Exception as exc:
        sys.exit(f"could not read file: {exc}")
    pages = prepare_pages(pages, scrub=False)
    pages = [redact(page) for page in pages]
    if "superseded" in (args.flags or ""):
        pages = [EDITION_NOTE + page for page in pages]
    write_page_files(doc_id, pages)
    words = low = 0
    for text in pages:
        count = len(text.split())
        words += count
        if count < 30:
            low += 1
    digest = sha16(path)
    flags = args.flags or ""
    db = connect_db()
    try:
        db.execute(
            "insert or replace into docs values(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, args.title, args.topic, flags, stored_source(path), kind, len(pages), words, digest, label),
        )
        db.commit()
    finally:
        db.close()
    upsert_catalog_row([
        doc_id, args.title, args.topic, flags, kind, len(pages), words, low, label,
        os.path.basename(path), digest, catalog_note(flags),
    ])
    print(doc_id, label + "s", len(pages), "words", words, "low", low)
    print("next: python tools/reindex.py")
    print("optional: python tools/embed.py")
    return doc_id


if __name__ == "__main__":
    main()
