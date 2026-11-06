#!/usr/bin/env python3
"""Rebuild chunks from text/<doc_id>/pNNNN.txt.

Re-chunks every page and replaces chunk and FTS rows only for pages whose
text changed. Rebuilds page_flags (toc / index), which ask.py uses to demote
those pages. Then run tools/embed.py if you want vectors; it embeds new chunks.

  python tools/reindex.py
  python tools/reindex.py --dry-run
"""
import os
import re
import sqlite3
import sys

from home import jetpack_home
from ingest import chunks

LEADER = re.compile(r"(\.\s?){6,}")
IDXLINE = re.compile(r"^.{2,80}?[,:]?\s+(\d{1,4}[a-z]?(\.\d+)*)(\s*[,;-]\s*\d{1,4}[a-z]?(\.\d+)*)*\s*$")


def page_kind(text):
    if text.startswith("EDITION NOTE") and "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    if any(line.lower() in ("index", "subject index") for line in lines[:4]):
        return "index"
    head = " ".join(lines[:4]).lower()
    leaders = sum(1 for line in lines if LEADER.search(line))
    if re.search(r"\b(contents|list of tables|list of figures)\b", head) and (leaders >= 1 or "table of contents" in head):
        return "toc"
    if leaders >= 4 and leaders / len(lines) >= 0.3:
        return "toc"
    if re.search(r"^\s*(index|subject index)\b", head) or (re.search(r"\bindex\b", head) and len(lines) > 20):
        if sum(1 for line in lines if IDXLINE.match(line)) / len(lines) > 0.4:
            return "index"
    return ""


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    dry = "--dry-run" in argv
    home = jetpack_home()
    db_file = os.path.join(home, "index", "jetpack.db")
    if not os.path.exists(db_file):
        sys.exit(f"No jetpack index at {db_file}. Run tools/ingest.py first.")
    db = sqlite3.connect(db_file)
    titles = dict(db.execute("select doc_id, title from docs"))
    db.execute("create table if not exists page_flags(doc_id text, page int, flag text, primary key(doc_id, page))")
    db.execute("delete from page_flags")
    changed = added = removed = 0
    kinds = {}
    for doc in sorted(titles):
        folder = os.path.join(home, "text", doc)
        if not os.path.isdir(folder):
            continue
        found = {}
        files = sorted(name for name in os.listdir(folder) if re.fullmatch(r"p\d{4}\.txt", name))
        for name in files:
            match = re.fullmatch(r"p(\d{4})\.txt", name)
            if not match:
                continue
            page = int(match.group(1))
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                text = handle.read()
            kind = page_kind(text)
            if kind:
                found[page] = kind
            new = chunks(text)
            old = db.execute(
                "select id, text from chunks where doc_id=? and page=? order by seq",
                (doc, page),
            ).fetchall()
            if [item for _, item in old] == new:
                continue
            changed += 1
            if dry:
                print("would update", doc, page)
                continue
            for cid, body in old:
                db.execute(
                    "insert into chunks_fts(chunks_fts, rowid, text, title) values('delete', ?, ?, ?)",
                    (cid, body, titles[doc]),
                )
                db.execute("delete from chunks where id=?", (cid,))
                removed += 1
            for seq, piece in enumerate(new):
                cur = db.execute(
                    "insert into chunks(doc_id,page,seq,text) values(?,?,?,?)",
                    (doc, page, seq, piece),
                )
                db.execute(
                    "insert into chunks_fts(rowid,text,title) values(?,?,?)",
                    (cur.lastrowid, piece, titles[doc]),
                )
                added += 1
        indexed = [page for page, kind in found.items() if kind == "index"]
        if indexed and min(indexed) >= 0.85 * len(files):
            for page in range(min(indexed), len(files) + 1):
                found[page] = "index"
        for page, kind in found.items():
            db.execute("insert or replace into page_flags values(?,?,?)", (doc, page, kind))
            kinds[kind] = kinds.get(kind, 0) + 1
    if not dry:
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
    db.commit()
    total = db.execute("select count(*) from chunks").fetchone()[0]
    db.close()
    print(f"pages_changed={changed} chunks_removed={removed} chunks_added={added} page_flags={kinds} total_chunks={total}")


if __name__ == "__main__":
    main()
