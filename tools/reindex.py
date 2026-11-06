#!/usr/bin/env python3
"""Rebuild chunks from text/<doc_id>/pNNNN.txt.

Re-chunks every page and replaces chunk rows only for pages whose text
changed; triggers keep the FTS index in step. Chunks for page files that no
longer exist are removed. When a document's text folder is missing, its chunks
leave the search index but its docs row stays. Rebuilds page_flags (toc /
index), which ask.py uses to demote those pages. Then run tools/embed.py if you
want vectors; it embeds new chunks.

  python tools/reindex.py
  python tools/reindex.py --dry-run     reports what would change and commits nothing
"""
import os
import re
import sys

from home import jetpack_home, printable, safe_doc_id
from ingest import PAGE_FILE_RE, chunks, connect_db, page_file_name

LEADER = re.compile(r"(\.\s?){6,}")
IDXLINE = re.compile(r"^.{2,80}?[,:]?\s+(\d{1,4}[a-z]?(\.\d+)*)(\s*[,;-]\s*\d{1,4}[a-z]?(\.\d+)*)*\s*$")
INDEX_HEADINGS = ("index", "subject index")


def page_kind(text):
    if text.startswith("EDITION NOTE") and "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    head = " ".join(lines[:4]).lower()
    leaders = sum(1 for line in lines if LEADER.search(line))
    if re.search(r"\b(contents|list of tables|list of figures)\b", head) and (leaders >= 1 or "table of contents" in head):
        return "toc"
    if leaders >= 4 and leaders / len(lines) >= 0.3:
        return "toc"
    if any(line.lower().rstrip(" :") in INDEX_HEADINGS for line in lines[:4]):
        return "index"
    if re.match(r"(index|subject index)\b", head):
        if sum(1 for line in lines if IDXLINE.match(line)) / len(lines) > 0.4:
            return "index"
    return ""


def page_files(folder):
    """{page number: file name} for pNNNN.txt files, including numbers past 9999."""
    found = {}
    for name in os.listdir(folder):
        match = PAGE_FILE_RE.fullmatch(name)
        if match and name == page_file_name(int(match.group(1))):
            found[int(match.group(1))] = name
    return found


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    dry = "--dry-run" in argv
    home = jetpack_home()
    db_file = os.path.join(home, "index", "jetpack.db")
    if not os.path.exists(db_file):
        sys.exit(f"No jetpack index at {db_file}. Run tools/ingest.py first.")
    try:
        db = connect_db(db_file)
    except RuntimeError as exc:
        sys.exit(str(exc))
    changed = added = removed = 0
    kinds = {}
    try:
        docs = db.execute("select doc_id, title from docs order by doc_id").fetchall()
        db.execute("delete from page_flags")
        for doc, title in docs:
            try:
                safe_doc_id(doc)
            except ValueError:
                print(f"refusing doc id {doc!r} from the index: not a safe folder name", file=sys.stderr)
                continue
            title = printable(title or "")
            db.execute("update chunks set title=? where doc_id=? and title is not ?", (title, doc, title))
            folder = os.path.join(home, "text", doc)
            if os.path.islink(folder) or not os.path.isdir(folder):
                gone = db.execute("delete from chunks where doc_id=?", (doc,)).rowcount
                if gone:
                    removed += gone
                    print(f"text/{doc} is missing: {gone} chunks left the search index; the docs row stays", file=sys.stderr)
                continue
            files = page_files(folder)
            stored = {page for (page,) in db.execute("select distinct page from chunks where doc_id=?", (doc,))}
            for page in sorted(stored - set(files)):
                removed += db.execute("delete from chunks where doc_id=? and page=?", (doc, page)).rowcount
                changed += 1
                if dry:
                    print("would remove", doc, page)
            found = {}
            for page, name in sorted(files.items()):
                with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                kind = page_kind(text)
                if kind:
                    found[page] = kind
                new = chunks(text)
                old = [body for (body,) in db.execute(
                    "select text from chunks where doc_id=? and page=? order by seq", (doc, page)
                )]
                if old == new:
                    continue
                changed += 1
                if dry:
                    print("would update", doc, page)
                removed += db.execute("delete from chunks where doc_id=? and page=?", (doc, page)).rowcount
                for seq, piece in enumerate(new):
                    db.execute(
                        "insert into chunks(doc_id,page,seq,text,title) values(?,?,?,?,?)",
                        (doc, page, seq, piece, title),
                    )
                    added += 1
            last = max(files) if files else 0
            indexed = [page for page, kind in found.items() if kind == "index"]
            if indexed and min(indexed) >= 0.85 * last:
                for page in range(min(indexed), last + 1):
                    if page in files:
                        found[page] = "index"
            for page, kind in found.items():
                db.execute("insert or replace into page_flags values(?,?,?)", (doc, page, kind))
                kinds[kind] = kinds.get(kind, 0) + 1
        total = db.execute("select count(*) from chunks").fetchone()[0]
        if dry:
            db.rollback()
        else:
            db.commit()
            db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
            db.commit()
    finally:
        db.close()
    prefix = "dry run, nothing written: " if dry else ""
    print(f"{prefix}pages_changed={changed} chunks_removed={removed} chunks_added={added} page_flags={kinds} total_chunks={total}")


if __name__ == "__main__":
    main()
