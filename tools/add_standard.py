#!/usr/bin/env python3
"""Add one document to an existing jetpack.

Writes text/<doc_id>/pNNNN.txt and a docs row, and takes the document's old
chunks out of search right away. New chunks are left to tools/reindex.py.
Documents added this way survive a later tools/ingest.py run.

  python tools/add_standard.py <file> <doc_id> "Title" [flags] [topic] [--redact]

Doc ids are stored lowercase. --redact scrubs the text, title, and topic the
same way ingest --redact does, stores the id as d- plus a hash of the id you
gave, and keeps the file name out of the index and catalog.
"""
import argparse
import os
import sys

from home import printable, safe_doc_id
from ingest import (
    catalog_note,
    connect_db,
    extract,
    page_stats,
    prepare_pages,
    put_doc,
    redact_label,
    redacted_doc_id,
    sha16,
    slug,
    stored_source,
    upsert_catalog_row,
    write_page_files,
)
from redact import TermsError, load_local_terms, scrubber

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
    parser.add_argument(
        "--redact", action="store_true",
        help="Scrub emails, URLs, phone numbers, street addresses, and local extra terms, as ingest --redact does.",
    )
    args = parser.parse_args(argv)
    try:
        requested = safe_doc_id(args.doc_id.lower())
    except ValueError as exc:
        sys.exit(str(exc))
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit(f"file not found: {os.path.basename(path)}")
    scrub = None
    if args.redact:
        try:
            scrub = scrubber(load_local_terms())
        except TermsError as exc:
            sys.exit(f"add_standard: {exc}")
    try:
        pages, kind, label = extract(path)
    except Exception as exc:
        sys.exit(f"could not read file: {exc}")
    pages = prepare_pages(pages, scrub)
    flags = args.flags or ""
    title = " ".join(printable(args.title).split())
    if scrub:
        doc_id = redacted_doc_id(requested)
        title = redact_label(title, scrub)
        topic = slug(redact_label(args.topic, scrub), 40) or "general"
        flags = " ".join(part for part in (flags, "redacted") if part)
        source = source_file = "redacted"
    else:
        doc_id = requested
        topic = slug(args.topic, 40) or "general"
        source = stored_source(path)
        source_file = os.path.basename(path)
    title = title[:240] or "Untitled"
    if "superseded" in flags:
        pages = [EDITION_NOTE + page for page in pages]
    words, low = page_stats(pages)
    digest = sha16(path)
    try:
        db = connect_db()
    except RuntimeError as exc:
        sys.exit(str(exc))
    try:
        write_page_files(doc_id, pages)
        put_doc(db, doc_id, title, topic, flags, source, kind, len(pages), words, digest, label, "added")
        db.commit()
    finally:
        db.close()
    upsert_catalog_row([
        doc_id, title, topic, flags, kind, len(pages), words, low, label,
        source_file, digest, catalog_note(flags),
    ])
    print(doc_id, label + "s", len(pages), "words", words, "low", low)
    print("next: python tools/reindex.py")
    print("optional: python tools/embed.py")
    return doc_id


if __name__ == "__main__":
    main()
