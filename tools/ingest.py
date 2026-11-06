#!/usr/bin/env python3
"""Index a folder of your own documents into a local jetpack.

Scans a library directory (default: $JETPACK_HOME/library) for pdf, docx,
html, and txt files. Each page is written to text/<doc_id>/pNNNN.txt and
chunked at about 1000 characters with about 150 characters of overlap.
Chunks do not cross pages. The search index is index/jetpack.db (SQLite FTS5).

  python tools/ingest.py
  python tools/ingest.py /path/to/your/files
  python tools/ingest.py --redact
"""
import argparse
import csv
import hashlib
import html
import os
import re
import shutil
import sqlite3
import sys

from home import jetpack_home, safe_doc_id

EXTS = {".pdf", ".docx", ".html", ".htm", ".txt"}
CATALOG_HEADER = [
    "doc_id", "title", "topic", "flags", "kind", "pages", "words",
    "low_text_pages", "page_label", "source_file", "sha256_16", "note",
]
SCHEMA = """
create table if not exists docs(
    doc_id text primary key,
    title text,
    topic text,
    flags text,
    source text,
    kind text,
    pages int,
    words int,
    sha256 text,
    page_label text
);
create table if not exists chunks(
    id integer primary key,
    doc_id text,
    page int,
    seq int,
    text text
);
create index if not exists ix_chunks_doc_page on chunks(doc_id, page);
create table if not exists page_flags(
    doc_id text,
    page int,
    flag text,
    primary key(doc_id, page)
);
create virtual table if not exists chunks_fts using fts5(
    text, title, content='', tokenize='porter unicode61'
);
"""


def chunks(text, size=1000, overlap=150):
    """Split one page. Callers pass a single page so pieces never cross pages."""
    lines, out, cur = text.split("\n"), [], ""
    for ln in lines:
        while len(ln) > size:
            out.append((cur + ln[:size]).strip())
            cur = ""
            ln = ln[size - overlap:]
        if len(cur) + len(ln) + 1 > size and cur.strip():
            out.append(cur.strip())
            cur = cur[-overlap:]
            cur = cur[cur.find(" ") + 1:] if " " in cur else cur
        cur += ln + "\n"
    if cur.strip() and (not out or len(cur.strip()) > 40):
        out.append(cur.strip())
    return [c for c in out if len(c.split()) >= 4]


def clean(text):
    text = text.replace("\x00", "")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\ufb00", "ff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]{3,}", "   ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def pack_parts(paras, size=3000):
    parts, cur = [], ""
    for para in paras:
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    if cur:
        parts.append(cur)
    return parts or [""]


def pdf_pages(path):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    doc = pymupdf.open(path)
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()
    return pages or [""], "pdf", "page"


def docx_pages(path):
    from docx import Document
    document = Document(path)
    paras = []
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if text:
            paras.append(text)
    for table in document.tables:
        for row in table.rows:
            seen = []
            for cell in row.cells:
                text = (cell.text or "").strip()
                if text and text not in seen:
                    seen.append(text)
            if seen:
                paras.append(" | ".join(seen))
    return pack_parts(paras), "docx", "part"


def html_pages(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        raw = handle.read()
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr|table)>", "\n", raw)
    paras = []
    for piece in raw.split("\n"):
        text = html.unescape(re.sub(r"<[^>]+>", "", piece))
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            paras.append(text)
    return pack_parts(paras), "html", "part"


def txt_pages(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        raw = handle.read()
    pages = raw.split("\f")
    if len(pages) > 1 and pages[-1].strip() == "":
        pages = pages[:-1]
    return pages or [""], "txt", "page"


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return pdf_pages(path)
    if ext == ".docx":
        return docx_pages(path)
    if ext in {".html", ".htm"}:
        return html_pages(path)
    if ext == ".txt":
        return txt_pages(path)
    raise ValueError("unsupported file type: " + ext)


def db_path():
    return os.path.join(jetpack_home(), "index", "jetpack.db")


def catalog_path():
    return os.path.join(jetpack_home(), "index", "catalog.csv")


def connect_db():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def sha16(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def slug(text, limit=80):
    done = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return done[:limit].strip("-")


def title_for(rel):
    stem = os.path.splitext(os.path.basename(rel))[0]
    title = stem.replace("_", " ").replace("-", " ")
    title = re.sub(r"\s+", " ", title).strip()
    return title[:240] or "Untitled"


def topic_for(rel):
    parent = os.path.dirname(rel)
    if not parent or parent == ".":
        return "general"
    top = parent.replace("\\", "/").split("/")[0]
    return slug(top, 40) or "general"


def doc_id_for(rel):
    stem = os.path.splitext(rel)[0].replace("\\", "/")
    return slug(stem) or "doc"


def stored_source(path):
    """Prefer a path relative to the jetpack home. Otherwise keep the file name only."""
    path = os.path.abspath(path)
    home = os.path.abspath(jetpack_home())
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        rel = None
    if rel and not rel.startswith("..") and not os.path.isabs(rel):
        return rel.replace(os.sep, "/")
    return os.path.basename(path)


def catalog_note(flags):
    if "superseded" in (flags or ""):
        return "Older edition kept for reference; verify against the current edition."
    return ""


def write_page_files(doc_id, pages):
    doc_id = safe_doc_id(doc_id)
    dest = os.path.join(jetpack_home(), "text", doc_id)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    for i, text in enumerate(pages, 1):
        with open(os.path.join(dest, f"p{i:04d}.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
    return dest


def upsert_catalog_row(row):
    path = catalog_path()
    body = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            existing = list(csv.reader(handle))
        start = 1 if existing and existing[0] and existing[0][0] == "doc_id" else 0
        body = [item for item in existing[start:] if item and item[0] != row[0]]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CATALOG_HEADER)
        writer.writerows(body + [row])


def iter_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith(".") and name != "__pycache__")
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("~$"):
                continue
            if os.path.splitext(name)[1].lower() in EXTS:
                found.append(os.path.join(dirpath, name))
    return found


def prepare_pages(pages, scrub):
    prepared = []
    for page in pages:
        text = clean(page)
        if scrub:
            from redact import redact
            text = redact(text)
        prepared.append(text)
    return prepared


def insert_document(db, doc_id, title, topic, flags, source, kind, page_label, sha, pages):
    doc_id = safe_doc_id(doc_id)
    words = 0
    low = 0
    for text in pages:
        count = len(text.split())
        words += count
        if count < 30:
            low += 1
    db.execute("delete from chunks where doc_id=?", (doc_id,))
    db.execute(
        "insert or replace into docs values(?,?,?,?,?,?,?,?,?,?)",
        (doc_id, title, topic, flags, source, kind, len(pages), words, sha, page_label),
    )
    for page_no, text in enumerate(pages, 1):
        for seq, piece in enumerate(chunks(text)):
            cur = db.execute(
                "insert into chunks(doc_id,page,seq,text) values(?,?,?,?)",
                (doc_id, page_no, seq, piece),
            )
            db.execute(
                "insert into chunks_fts(rowid,text,title) values(?,?,?)",
                (cur.lastrowid, piece, title),
            )
    return words, low


def unique_doc_id(base, used):
    base = slug(base) or "doc"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    try:
        safe_doc_id(candidate)
    except ValueError:
        candidate = f"doc-{len(used) + 1}"
    used.add(candidate)
    return candidate


def run(library=None, redact_text=False):
    home = jetpack_home()
    root = os.path.abspath(library) if library else os.path.join(home, "library")
    if not os.path.isdir(root):
        print(
            f"No library folder at {root}.\n"
            "Create it, put pdf, docx, html, or txt files inside, and run this again.",
            file=sys.stderr,
        )
        return 2
    index = os.path.join(home, "index")
    os.makedirs(index, exist_ok=True)
    path = db_path()
    if os.path.exists(path):
        os.remove(path)
    db = connect_db()
    used = set()
    catalog_rows = []
    errors = 0
    docs = 0
    try:
        for full in iter_files(root):
            rel = os.path.relpath(full, root)
            try:
                pages, kind, label = extract(full)
                pages = prepare_pages(pages, redact_text)
            except Exception as exc:
                errors += 1
                print(f"skip {rel}: {exc}", file=sys.stderr)
                continue
            doc_id = unique_doc_id(doc_id_for(rel), used)
            title = title_for(rel)
            topic = topic_for(rel)
            flags = "redacted" if redact_text else ""
            source = rel.replace(os.sep, "/")
            write_page_files(doc_id, pages)
            words, low = insert_document(
                db, doc_id, title, topic, flags, source, kind, label, sha16(full), pages
            )
            catalog_rows.append([
                doc_id, title, topic, flags, kind, len(pages), words, low, label,
                os.path.basename(full), sha16(full), catalog_note(flags),
            ])
            docs += 1
            print(f"{doc_id:42s} {len(pages):4d} {label:8s} {words:7d}w low:{low}")
        db.commit()
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
        db.commit()
        db.execute("vacuum")
        n_chunks = db.execute("select count(*) from chunks").fetchone()[0]
    finally:
        db.close()
    with open(catalog_path(), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CATALOG_HEADER)
        writer.writerows(catalog_rows)
    print(f"docs={docs} chunks={n_chunks} db={db_path()}")
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index pdf, docx, html, and txt files into a local SQLite FTS5 jetpack.")
    parser.add_argument("library", nargs="?", help="Folder to scan. Default: $JETPACK_HOME/library")
    parser.add_argument("--redact", action="store_true", help="Scrub emails, URLs, phone numbers, street addresses, and local extra terms.")
    args = parser.parse_args(argv)
    code = run(args.library, args.redact)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
