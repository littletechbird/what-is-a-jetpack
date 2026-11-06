#!/usr/bin/env python3
"""Remove a document after a replacement is already indexed.

Usage: python tools/remove_docs.py OLD_ID=NEW_ID [...]

Refuses to remove OLD_ID unless NEW_ID already has chunks.
"""
import csv
import datetime
import os
import shutil
import sqlite3
import sys

from home import jetpack_home, safe_doc_id


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("Usage: python tools/remove_docs.py OLD_ID=NEW_ID [...]")
        return 2
    home = jetpack_home()
    db_file = os.path.join(home, "index", "jetpack.db")
    if not os.path.exists(db_file):
        sys.exit(f"No jetpack index at {db_file}. Run tools/ingest.py first.")
    db = sqlite3.connect(db_file)
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
            n_new = db.execute("select count(*) from chunks where doc_id=?", (new,)).fetchone()[0]
            if not n_new:
                print("REFUSED", old, "- newer", new, "not indexed")
                continue
            row = db.execute("select title from docs where doc_id=?", (old,)).fetchone()
            if not row:
                print("absent", old)
                continue
            chunks = db.execute("select id, text from chunks where doc_id=?", (old,)).fetchall()
            for cid, text in chunks:
                db.execute(
                    "insert into chunks_fts(chunks_fts, rowid, text, title) values('delete', ?, ?, ?)",
                    (cid, text, row[0]),
                )
            db.execute("delete from chunks where doc_id=?", (old,))
            db.execute("delete from docs where doc_id=?", (old,))
            try:
                db.execute("delete from page_flags where doc_id=?", (old,))
            except sqlite3.Error:
                pass
            shutil.rmtree(os.path.join(home, "text", old), ignore_errors=True)
            log.append([datetime.datetime.now().isoformat(timespec="seconds"), old, row[0], len(chunks), new])
            print("removed", old, len(chunks), "chunks; superseded by", new)
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
        with open(catalog, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows([row for row in rows if not row or row[0] not in gone])
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
