#!/usr/bin/env python3
"""Add one document to an existing jetpack.

Writes text/<doc_id>/pNNNN.txt, a docs row, and the document's chunks, so it
is searchable right away. Adding a doc id that already exists replaces that
document, title and pages included. A later tools/ingest.py keeps it.
Contact details are scrubbed only with --redact, the same switch ingest uses.

  python tools/add_standard.py <file> <doc_id> "Title" [flags] [topic] [--redact]
"""
import argparse
import os
import shutil
import sys

from extract import regular_file
from home import safe_doc_id
from ingest import (
    IndexUnavailable,
    add_flag,
    catalog_note,
    clean_name,
    extract,
    insert_document,
    make_redactor,
    new_staging,
    open_index,
    prepare_pages,
    sha16,
    stage_pages,
    stored_source,
    swap_in,
    upsert_catalog_row,
)
from redact import TermsError

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
        help="Scrub emails, URLs, phone numbers, street addresses, and local extra terms from the text, title, and source.",
    )
    parser.add_argument("--terms", help="Terms file for --redact. Default: redact_terms.local.json in the jetpack home.")
    args = parser.parse_args(argv)
    try:
        doc_id = safe_doc_id(args.doc_id)
    except ValueError as exc:
        sys.exit(str(exc))
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit(f"file not found: {os.path.basename(path)}")
    try:
        regular_file(path)
    except (OSError, ValueError) as exc:
        sys.exit(f"could not read file: {exc}")
    redactor = None
    if args.redact:
        try:
            redactor = make_redactor(args.terms)
        except TermsError as exc:
            sys.exit(f"{exc}. Nothing was added.")
    title = clean_name(args.title) or "Untitled"
    topic = clean_name(args.topic) or "standards"
    flags = clean_name(args.flags)
    source = stored_source(path)
    if redactor:
        if redactor(doc_id) != doc_id or redactor(topic) != topic:
            sys.exit("the doc id or topic contains text that --redact would remove; choose another")
        title, source = redactor(title), redactor(source)
        flags = add_flag(flags, "redacted")
    try:
        db = open_index(create=True)
    except IndexUnavailable as exc:
        sys.exit(str(exc))
    try:
        clash = db.execute(
            "select doc_id from docs where lower(doc_id)=lower(?) and doc_id<>?", (doc_id, doc_id)
        ).fetchone()
        if clash:
            sys.exit(f"doc id {doc_id} differs from {clash[0]} only by case; use {clash[0]} or another id")
        try:
            pages, kind, label = extract(path)
        except Exception as exc:
            sys.exit(f"could not read file: {exc}")
        pages = prepare_pages(pages, redactor)
        if "superseded" in flags:
            pages = [EDITION_NOTE + page for page in pages]
        digest = sha16(path)
        staging = new_staging()
        try:
            stage_pages(staging, doc_id, pages)
            words, low = insert_document(
                db, doc_id, title, topic, flags, source, kind, label, digest, pages, origin="added"
            )
            swap_in(staging, doc_id)
            db.commit()
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
        db.commit()
    finally:
        db.close()
    upsert_catalog_row([
        doc_id, title, topic, flags, kind, len(pages), words, low, label,
        os.path.basename(source), digest, catalog_note(flags),
    ])
    print(doc_id, label + "s", len(pages), "words", words, "low", low)
    print("optional: python tools/embed.py")
    return doc_id


if __name__ == "__main__":
    main()
