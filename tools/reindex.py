#!/usr/bin/env python3
"""Rebuild chunks from text/<doc_id>/pNNNN.txt.

Re-chunks every page and replaces chunk rows only for pages whose text or
document title changed. Chunks for page files that were deleted are
removed, and so is a document whose text folder is gone. Rebuilds
page_flags (toc / index), which ask.py uses to demote those pages. Then run
tools/embed.py if you want vectors; it embeds new chunks.

  python tools/reindex.py
  python tools/reindex.py --dry-run
"""
import argparse
import sys

from ingest import (
    IndexUnavailable,
    chunk_fp,
    chunks,
    delete_document,
    doc_folder,
    open_index,
    page_flags,
    read_page_files,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rebuild chunks and page flags from the stored page text.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change and write nothing.")
    dry = parser.parse_args(argv).dry_run
    try:
        db = open_index()
    except IndexUnavailable as exc:
        sys.exit(str(exc))
    verb = "would " if dry else ""
    changed = added = removed = pages_gone = docs_gone = 0
    kinds = {}
    try:
        docs = dict(db.execute("select doc_id, title from docs"))
        for doc in sorted(docs):
            title = docs[doc]
            folder = doc_folder(doc)
            if folder is None:
                count = db.execute("select count(*) from chunks where doc_id=?", (doc,)).fetchone()[0]
                print(f"{verb}remove {doc}: its text folder is gone")
                docs_gone += 1
                removed += count
                if not dry:
                    delete_document(db, doc)
                continue
            pages = read_page_files(folder)
            have = {page_no for page_no, _ in pages}
            old_rows = {}
            for cid, page, piece, old_title in db.execute(
                "select id, page, text, title from chunks where doc_id=? order by page, seq", (doc,)
            ):
                old_rows.setdefault(page, []).append((cid, piece, old_title))
            for page in sorted(set(old_rows) - have):
                pages_gone += 1
                removed += len(old_rows[page])
                if dry:
                    print(f"would drop {doc} page {page}: its page file is gone")
                else:
                    db.execute("delete from chunks where doc_id=? and page=?", (doc, page))
            words = 0
            for page_no, text in pages:
                words += len(text.split())
                new = chunks(text)
                old = old_rows.get(page_no, [])
                if [piece for _, piece, _ in old] == new and all(item == title for _, _, item in old):
                    continue
                changed += 1
                removed += len(old)
                added += len(new)
                if dry:
                    print("would update", doc, page_no)
                    continue
                db.execute("delete from chunks where doc_id=? and page=?", (doc, page_no))
                for seq, piece in enumerate(new):
                    db.execute(
                        "insert into chunks(doc_id,page,seq,title,text,fp) values(?,?,?,?,?,?)",
                        (doc, page_no, seq, title, piece, chunk_fp(title, piece)),
                    )
            flags = page_flags(pages)
            for kind in flags.values():
                kinds[kind] = kinds.get(kind, 0) + 1
            if not dry:
                db.execute("delete from page_flags where doc_id=?", (doc,))
                db.executemany(
                    "insert into page_flags values(?,?,?)",
                    [(doc, page_no, kind) for page_no, kind in flags.items()],
                )
                db.execute("update docs set pages=?, words=? where doc_id=?", (len(pages), words, doc))
        if not dry:
            db.commit()
            db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
            db.commit()
        total = db.execute("select count(*) from chunks").fetchone()[0]
    finally:
        db.close()
    print(
        f"pages_changed={changed} pages_removed={pages_gone} docs_removed={docs_gone} chunks_removed={removed} "
        f"chunks_added={added} page_flags={kinds} total_chunks={total}" + (" (dry run, nothing written)" if dry else "")
    )


if __name__ == "__main__":
    main()
