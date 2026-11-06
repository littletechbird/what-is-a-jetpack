#!/usr/bin/env python3
"""Index a folder of your own documents into a local jetpack.

Scans a library directory (default: $JETPACK_HOME/library) for pdf, docx,
html, and txt files. Each page is written to text/<doc_id>/pNNNN.txt and
chunked at about 1000 characters with about 150 characters of overlap.
Chunks do not cross pages. The search index is index/jetpack.db (SQLite FTS5).

Each run rebuilds the documents that came from the library. The new database
and page files are built aside and swapped in only when the run finishes, so a
failed run leaves the previous index in place. Documents added with
tools/add_standard.py are carried over.

  python tools/ingest.py
  python tools/ingest.py /path/to/your/files
  python tools/ingest.py --redact
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from html.parser import HTMLParser

from home import jetpack_home, printable, remove_path, remove_vector_files, safe_doc_id
from redact import TermsError, load_local_terms, scrubber

EXTS = {".pdf", ".docx", ".html", ".htm", ".txt"}
MAX_BYTES = 80 * 1024 * 1024
SKIP_DIRS = {"__pycache__", ".git", ".venv"}
GENERATED_DIRS = ("text", "index", "work")
PAGE_FILE_RE = re.compile(r"p(\d{4,})\.txt")
TOKEN_RE = re.compile(r"[^\W_]")
CATALOG_HEADER = [
    "doc_id", "title", "topic", "flags", "kind", "pages", "words",
    "low_text_pages", "page_label", "source_file", "sha256_16", "note",
]
DOC_COLUMNS = (
    "doc_id", "title", "topic", "flags", "source", "kind",
    "pages", "words", "sha256", "page_label", "origin",
)
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
    page_label text,
    origin text not null default 'library'
);
create table if not exists chunks(
    id integer primary key autoincrement,
    doc_id text,
    page int,
    seq int,
    text text,
    title text
);
create index if not exists ix_chunks_doc_page on chunks(doc_id, page);
create table if not exists page_flags(
    doc_id text,
    page int,
    flag text,
    primary key(doc_id, page)
);
create virtual table if not exists chunks_fts using fts5(
    text, title, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
create trigger if not exists chunks_ai after insert on chunks begin
    insert into chunks_fts(rowid, text, title) values (new.id, new.text, new.title);
end;
create trigger if not exists chunks_ad after delete on chunks begin
    insert into chunks_fts(chunks_fts, rowid, text, title) values ('delete', old.id, old.text, old.title);
end;
create trigger if not exists chunks_au after update on chunks begin
    insert into chunks_fts(chunks_fts, rowid, text, title) values ('delete', old.id, old.text, old.title);
    insert into chunks_fts(rowid, text, title) values (new.id, new.text, new.title);
end;
"""


class SkipFile(Exception):
    """A file left out on purpose (too large, a link, or a special file). Not an error."""


def chunks(text, size=1000, overlap=150, min_tail=200):
    """Split one page into pieces of about `size` characters.

    Callers pass a single page so pieces never cross pages. The end of the page
    is always kept: a short tail is added to the previous piece.
    """
    out, cur, carried = [], "", 0

    def overlap_of(piece):
        tail = piece[-overlap:]
        return tail[tail.find(" ") + 1:] if " " in tail else tail

    for line in text.split("\n"):
        carry = 0
        if len(line) > size:
            if cur[carried:].strip():
                out.append(cur.strip())
            cur, carried = "", 0
            while len(line) > size:
                cut = line.rfind(" ", size // 2, size)
                if cut <= 0:
                    cut = size
                out.append(line[:cut].strip())
                space = line.find(" ", cut - overlap, cut)
                start = space + 1 if space != -1 else cut - overlap
                carry = cut - start
                line = line[start:]
        if len(cur) + len(line) + 1 > size and cur[carried:].strip():
            piece = cur.strip()
            out.append(piece)
            cur = overlap_of(piece) + "\n"
            if len(cur) + len(line) + 1 > size:
                cur = ""
            carried = len(cur)
        if carry:
            carried = carry
        cur += line + "\n"
    fresh = cur[carried:].strip()
    if fresh:
        if out and len(cur.strip()) < min_tail:
            out[-1] = out[-1] + "\n" + fresh
        else:
            out.append(cur.strip())
    return [piece for piece in out if TOKEN_RE.search(piece)]


def clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00ad", "")
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\ufb00", "ff")
    text = printable(text)
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


def _docx_lines(container):
    """Paragraph text and table rows, in document order."""
    from docx.table import Table

    if hasattr(container, "iter_inner_content"):
        items = container.iter_inner_content()
    else:
        items = list(container.paragraphs) + list(container.tables)
    for item in items:
        if isinstance(item, Table):
            for row in item.rows:
                cells = [" ".join((cell.text or "").split()) for cell in row.cells]
                if any(cells):
                    yield " | ".join(cells)
        else:
            text = (item.text or "").strip()
            if text:
                yield text


def docx_pages(path):
    import zipfile

    with zipfile.ZipFile(path) as archive:
        expanded = sum(info.file_size for info in archive.infolist())
    if expanded > MAX_BYTES:
        raise ValueError("docx expands to more than 80 MiB; refusing it")
    from docx import Document

    document = Document(path)
    paras = list(_docx_lines(document))
    seen = set()
    for section in document.sections:
        for label, part in (("Header", section.header), ("Footer", section.footer)):
            if part.is_linked_to_previous:
                continue
            for text in _docx_lines(part):
                line = f"{label}: {text}"
                if line not in seen:
                    seen.add(line)
                    paras.append(line)
    return pack_parts(paras), "docx", "part"


class _HTMLText(HTMLParser):
    """Visible text only: no comments, attributes, scripts, styles, noscript, or templates."""

    SKIP = {"script", "style", "noscript", "template"}
    BREAKS = {
        "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "title", "tr", "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_pages(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        raw = handle.read()
    parser = _HTMLText()
    parser.feed(raw)
    parser.close()
    paras = []
    for piece in "".join(parser.parts).split("\n"):
        text = re.sub(r"\s+", " ", piece).strip()
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
    if ext not in EXTS:
        raise ValueError("unsupported file type: " + ext)
    if os.stat(path).st_size > MAX_BYTES:
        raise SkipFile("larger than 80 MiB")
    if ext == ".pdf":
        return pdf_pages(path)
    if ext == ".docx":
        return docx_pages(path)
    if ext in {".html", ".htm"}:
        return html_pages(path)
    return txt_pages(path)


def db_path():
    return os.path.join(jetpack_home(), "index", "jetpack.db")


def catalog_path():
    return os.path.join(jetpack_home(), "index", "catalog.csv")


def connect_db(path=None):
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    chunk_cols = {row[1] for row in db.execute("pragma table_info(chunks)")}
    if chunk_cols and "title" not in chunk_cols:
        db.close()
        raise RuntimeError(f"{path} was built by an older version of these tools. Run tools/ingest.py to rebuild it.")
    db.executescript(SCHEMA)
    doc_cols = {row[1] for row in db.execute("pragma table_info(docs)")}
    if "origin" not in doc_cols:
        db.execute("alter table docs add column origin text not null default 'library'")
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


def redact_label(text, scrub):
    """Scrub a label taken from a file or folder name, before and after its separators become spaces."""
    text = scrub(text.replace("_", " "))
    text = scrub(text.replace("-", " "))
    return re.sub(r"\s+", " ", printable(text)).strip()


def title_for(rel, scrub=None):
    stem = os.path.splitext(os.path.basename(rel))[0]
    if scrub:
        title = redact_label(stem, scrub)
    else:
        title = stem.replace("_", " ").replace("-", " ")
    title = re.sub(r"\s+", " ", printable(title)).strip()
    return title[:240] or "Untitled"


def topic_for(rel, scrub=None):
    parent = os.path.dirname(rel)
    if not parent or parent == ".":
        return "general"
    top = parent.replace("\\", "/").split("/")[0]
    if scrub:
        top = redact_label(top, scrub)
    return slug(top, 40) or "general"


def doc_id_for(rel):
    stem = os.path.splitext(rel)[0].replace("\\", "/")
    return slug(stem) or "doc"


def redacted_doc_id(key):
    """An id that reveals nothing about the file name: d- plus 16 hex digits of sha256."""
    return "d-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


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


def page_file_name(page):
    return f"p{page:04d}.txt"


def write_page_files(doc_id, pages, root=None):
    doc_id = safe_doc_id(doc_id)
    base = root or os.path.join(jetpack_home(), "text")
    dest = os.path.join(base, doc_id)
    remove_path(dest)
    os.makedirs(dest)
    for i, text in enumerate(pages, 1):
        with open(os.path.join(dest, page_file_name(i)), "w", encoding="utf-8") as handle:
            handle.write(text)
    return dest


def read_catalog():
    path = catalog_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    start = 1 if rows and rows[0] and rows[0][0] == "doc_id" else 0
    return [row for row in rows[start:] if row]


def write_catalog(rows):
    path = catalog_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CATALOG_HEADER)
        writer.writerows(rows)


def upsert_catalog_row(row):
    write_catalog([item for item in read_catalog() if item[0] != row[0]] + [row])


def _within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def iter_files(root, skip_dirs=(), home=None):
    """Yield (path, library-relative posix path) for regular files inside root.

    Symlinks, FIFOs, devices, and files that resolve outside the library are skipped.
    """
    real_root = os.path.realpath(root)
    skip = set(skip_dirs)
    looks_like_home = (
        os.path.isfile(os.path.join(root, "index", "jetpack.db"))
        or (home is not None and real_root == os.path.realpath(home))
    )
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        at_top = dirpath == root
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            if at_top and looks_like_home and name in GENERATED_DIRS:
                continue
            if os.path.realpath(os.path.join(dirpath, name)) in skip:
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("~$"):
                continue
            if os.path.splitext(name)[1].lower() not in EXTS:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                info = os.lstat(full)
            except OSError as exc:
                print(f"skip {rel}: {exc}", file=sys.stderr)
                continue
            if not stat.S_ISREG(info.st_mode):
                print(f"skip {rel}: not a regular file", file=sys.stderr)
                continue
            if not _within(os.path.realpath(full), real_root):
                print(f"skip {rel}: resolves outside the library", file=sys.stderr)
                continue
            if info.st_size > MAX_BYTES:
                print(f"skip {rel}: larger than 80 MiB", file=sys.stderr)
                continue
            yield full, rel


def prepare_pages(pages, scrub=None):
    prepared = []
    for page in pages:
        text = clean(page)
        if scrub:
            text = scrub(text)
        prepared.append(text)
    return prepared


def page_stats(pages):
    words = low = 0
    for text in pages:
        count = len(text.split())
        words += count
        if count < 30:
            low += 1
    return words, low


def put_doc(db, doc_id, title, topic, flags, source, kind, pages, words, sha, page_label, origin):
    """Replace a docs row and drop its chunks. The FTS triggers remove the old rows from search."""
    doc_id = safe_doc_id(doc_id)
    db.execute("delete from chunks where doc_id=?", (doc_id,))
    db.execute("delete from page_flags where doc_id=?", (doc_id,))
    db.execute(
        f"insert or replace into docs({', '.join(DOC_COLUMNS)}) values({', '.join('?' * len(DOC_COLUMNS))})",
        (doc_id, title, topic, flags, source, kind, pages, words, sha, page_label, origin),
    )


def insert_document(db, doc_id, title, topic, flags, source, kind, page_label, sha, pages, origin="library"):
    title = printable(title)
    words, low = page_stats(pages)
    put_doc(db, doc_id, title, topic, flags, source, kind, len(pages), words, sha, page_label, origin)
    for page_no, text in enumerate(pages, 1):
        for seq, piece in enumerate(chunks(text)):
            db.execute(
                "insert into chunks(doc_id,page,seq,text,title) values(?,?,?,?,?)",
                (doc_id, page_no, seq, piece, title),
            )
    return words, low


def _is_safe(doc_id):
    try:
        safe_doc_id(doc_id)
    except ValueError:
        return False
    return True


def _unique(base, used):
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def assign_doc_ids(rels, previous, reserved, redacting=False):
    """Map library-relative paths to doc ids.

    A path that the previous index already knew keeps its id, so a new file
    whose name slugs the same way gets the -2 suffix instead of renaming an
    existing document.
    """
    ids, used = {}, set(reserved)
    if redacting:
        for rel in rels:
            ids[rel] = _unique(redacted_doc_id(rel), used)
        return ids
    for rel in rels:
        old = previous.get(rel)
        if old and old not in used and _is_safe(old):
            ids[rel] = old
            used.add(old)
    for rel in rels:
        if rel not in ids:
            base = doc_id_for(rel)
            if not _is_safe(base):
                base = "doc-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]
            ids[rel] = _unique(base, used)
    return ids


def previous_index(path):
    """What the current index holds: library paths and ids, and the ids of added documents."""
    info = {"sources": {}, "library": set(), "added": set(), "can_copy": False}
    if not os.path.exists(path):
        return info
    try:
        db = sqlite3.connect(path)
        try:
            doc_cols = {row[1] for row in db.execute("pragma table_info(docs)")}
            if not doc_cols:
                return info
            origin = "origin" if "origin" in doc_cols else "'library'"
            for doc_id, source, kind in db.execute(f"select doc_id, source, {origin} from docs"):
                if kind == "added":
                    info["added"].add(doc_id)
                else:
                    info["library"].add(doc_id)
                    if source and source != "redacted":
                        info["sources"][source] = doc_id
            chunk_cols = {row[1] for row in db.execute("pragma table_info(chunks)")}
            tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
            info["can_copy"] = "title" in chunk_cols and "page_flags" in tables
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(f"warning: could not read the previous index ({exc}); added documents are not carried over", file=sys.stderr)
    return info


def copy_added_docs(db, old_path):
    added = "(select doc_id from old.docs where origin='added')"
    db.execute("attach database ? as old", (old_path,))
    try:
        cols = ", ".join(DOC_COLUMNS)
        db.execute(f"insert into docs({cols}) select {cols} from old.docs where origin='added'")
        db.execute(
            "insert into chunks(doc_id,page,seq,text,title) "
            f"select doc_id,page,seq,text,title from old.chunks where doc_id in {added} order by id"
        )
        db.execute(
            "insert or replace into page_flags(doc_id,page,flag) "
            f"select doc_id,page,flag from old.page_flags where doc_id in {added}"
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("detach database old")


def _match_mode(tmp, final):
    """mkstemp makes a private file; give the new database the old one's mode, or the umask default."""
    if os.path.exists(final):
        shutil.copymode(final, tmp)
    else:
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)


def swap_text(staging, text_root, built, stale):
    for doc_id in built:
        dest = os.path.join(text_root, doc_id)
        remove_path(dest)
        os.replace(os.path.join(staging, doc_id), dest)
    for doc_id in stale:
        if _is_safe(doc_id):
            remove_path(os.path.join(text_root, doc_id))


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
    generated = [os.path.realpath(os.path.join(home, name)) for name in GENERATED_DIRS]
    if any(_within(os.path.realpath(root), path) for path in generated):
        print("The library folder cannot be inside the jetpack's text/, index/, or work/ folder.", file=sys.stderr)
        return 2
    scrub = None
    if redact_text:
        try:
            scrub = scrubber(load_local_terms())
        except TermsError as exc:
            print(f"ingest: {exc}\nNothing was changed. Fix the terms file and run again.", file=sys.stderr)
            return 2
    index_dir = os.path.join(home, "index")
    text_root = os.path.join(home, "text")
    os.makedirs(index_dir, exist_ok=True)
    os.makedirs(text_root, exist_ok=True)
    final = db_path()
    previous = previous_index(final)
    files = list(iter_files(root, generated, home))
    ids = assign_doc_ids([rel for _, rel in files], previous["sources"], previous["added"], bool(scrub))
    handle, tmp_db = tempfile.mkstemp(prefix=".build-", suffix=".db", dir=index_dir)
    os.close(handle)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=text_root)
    db = None
    catalog_rows = []
    errors = docs = n_chunks = 0
    try:
        db = connect_db(tmp_db)
        if previous["added"] and previous["can_copy"]:
            copy_added_docs(db, final)
        for full, rel in files:
            try:
                pages, kind, label = extract(full)
                pages = prepare_pages(pages, scrub)
                digest = sha16(full)
            except SkipFile as exc:
                print(f"skip {rel}: {exc}", file=sys.stderr)
                continue
            except Exception as exc:
                errors += 1
                print(f"skip {rel}: {exc}", file=sys.stderr)
                continue
            doc_id = ids[rel]
            title = title_for(rel, scrub)
            topic = topic_for(rel, scrub)
            flags = "redacted" if scrub else ""
            source = "redacted" if scrub else rel
            write_page_files(doc_id, pages, root=staging)
            words, low = insert_document(db, doc_id, title, topic, flags, source, kind, label, digest, pages)
            catalog_rows.append([
                doc_id, title, topic, flags, kind, len(pages), words, low, label,
                "redacted" if scrub else os.path.basename(full), digest, catalog_note(flags),
            ])
            docs += 1
            print(f"{doc_id:42s} {len(pages):4d} {label:8s} {words:7d}w low:{low}")
        db.commit()
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
        db.commit()
        n_chunks = db.execute("select count(*) from chunks").fetchone()[0]
        db.close()
        db = None
        kept_rows = [row for row in read_catalog() if row[0] in previous["added"]]
        _match_mode(tmp_db, final)
        os.replace(tmp_db, final)
        built = [row[0] for row in catalog_rows]
        swap_text(staging, text_root, built, previous["library"] - set(built) - previous["added"])
        remove_vector_files(home)
        write_catalog(kept_rows + catalog_rows)
    finally:
        if db is not None:
            db.close()
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        shutil.rmtree(staging, ignore_errors=True)
    print(f"docs={docs} chunks={n_chunks} db={final}")
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index pdf, docx, html, and txt files into a local SQLite FTS5 jetpack.")
    parser.add_argument("library", nargs="?", help="Folder to scan. Default: $JETPACK_HOME/library")
    parser.add_argument(
        "--redact", action="store_true",
        help="Scrub emails, URLs, phone numbers, street addresses, and local extra terms from text, titles, and topics. "
             "Doc ids become hashes and source names are not stored.",
    )
    args = parser.parse_args(argv)
    code = run(args.library, args.redact)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
