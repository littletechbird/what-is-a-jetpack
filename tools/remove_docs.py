#!/usr/bin/env python3
"""Remove a document after a replacement is already indexed.

Usage: python tools/remove_docs.py OLD_ID=NEW_ID [...]

Refuses to remove OLD_ID unless NEW_ID is a different document that already
has chunks.
"""
import csv
import datetime
import os
import sys

from home import jetpack_home, safe_doc_id
from ingest import IndexUnavailable, delete_document, open_index, remove_path, text_root, write_catalog


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("Usage: python tools/remove_docs.py OLD_ID=NEW_ID [...]")
        return 2
    home = jetpack_home()
    try:
        db = open_index()
    except IndexUnavailable as exc:
        sys.exit(str(exc))
    log = []
    try:
        for arg in argv:
            if "=" not in arg:
                print("REFUSED", arg, "- want OLD_ID=NEW_ID")
                continue
            old, new = arg.split("=", 1)
            try:
                old = safe_doc_id(old)
                new = safe_doc_id(new)
            except ValueError as exc:
                print("REFUSED", exc)
                continue
            if old.lower() == new.lower():
                print("REFUSED", old, "- a document cannot replace itself")
                continue
            n_new = db.execute("select count(*) from chunks where doc_id=?", (new,)).fetchone()[0]
            if not n_new:
                print("REFUSED", old, "- newer", new, "not indexed")
                continue
            row = db.execute("select title from docs where doc_id=?", (old,)).fetchone()
            if not row:
                print("absent", old)
                continue
            n_old = db.execute("select count(*) from chunks where doc_id=?", (old,)).fetchone()[0]
            delete_document(db, old)
            remove_path(os.path.join(text_root(), old))
            log.append([datetime.datetime.now().isoformat(timespec="seconds"), old, row[0], n_old, new])
            print("removed", old, n_old, "chunks; superseded by", new)
        db.commit()
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
        db.commit()
    finally:
        db.close()
    catalog = os.path.join(home, "index", "catalog.csv")
    gone = {item[1] for item in log}
    if gone and os.path.exists(catalog):
        with open(catalog, encoding="utf-8", errors="replace", newline="") as handle:
            rows = list(csv.reader(handle))
        if rows and rows[0] and rows[0][0] == "doc_id":
            rows = rows[1:]
        write_catalog([row for row in rows if row and row[0] not in gone])
    if log:
        log_path = os.path.join(home, "index", "removed_superseded.csv")
        new_file = not os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["when", "doc_id", "title", "chunks", "superseded_by"])
            writer.writerows(log)
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
