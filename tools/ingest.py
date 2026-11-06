#!/usr/bin/env python3
"""Index a folder of your own documents into a local jetpack.

Scans a library directory (default: $JETPACK_HOME/library) for pdf, docx,
html, and txt files. Only regular files whose real path stays inside the
library are read. Each page is written to text/<doc_id>/pNNNN.txt and
chunked at about 1000 characters with about 150 characters of overlap.
Chunks do not cross pages. The search index is index/jetpack.db (SQLite FTS5).

The new index is built beside the old one and swapped in when the run
finishes, so a failed or interrupted run leaves the previous index in place.
Documents added with tools/add_standard.py are kept. Text for files that
are no longer in the library is removed.

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

from extract import CONTROL_RE, EXTS, clean, extract, printable
from home import DOC_ID_RE, jetpack_home, safe_doc_id, within
from redact import Redactor, TermsError, discover_terms_path, load_terms, normalize

SCHEMA_VERSION = 2
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
VECTOR_FILE = "vectors.npz"
OLD_VECTOR_FILES = ("vectors.npy", "vector_ids.npy")
MANAGED_DIRS = ("text", "index", "work", "state", "kept")
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE_RE = re.compile(r"p(\d{4,})\.txt")
CATALOG_HEADER = [
    "doc_id", "title", "topic", "flags", "kind", "pages", "words",
    "low_text_pages", "page_label", "source_file", "sha256_16", "note",
]
# The FTS table reads its text from chunks, and the triggers keep it in step,
# so a delete always removes exactly the words that were indexed.
SCHEMA = f"""
create table docs(
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
create table chunks(
    id integer primary key autoincrement,
    doc_id text not null,
    page int not null,
    seq int not null,
    title text not null,
    text text not null,
    fp text not null
);
create index ix_chunks_doc_page on chunks(doc_id, page);
create table page_flags(
    doc_id text,
    page int,
    flag text,
    primary key(doc_id, page)
);
create virtual table chunks_fts using fts5(
    text, title, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
create trigger chunks_ai after insert on chunks begin
    insert into chunks_fts(rowid, text, title) values (new.id, new.text, new.title);
end;
create trigger chunks_ad after delete on chunks begin
    insert into chunks_fts(chunks_fts, rowid, text, title) values ('delete', old.id, old.text, old.title);
end;
create trigger chunks_au after update on chunks begin
    insert into chunks_fts(chunks_fts, rowid, text, title) values ('delete', old.id, old.text, old.title);
    insert into chunks_fts(rowid, text, title) values (new.id, new.text, new.title);
end;
pragma user_version = {SCHEMA_VERSION};
"""


class IndexUnavailable(Exception):
    """There is no index, or it was built by an older version of these tools."""


def index_dir():
    return os.path.join(jetpack_home(), "index")


def db_path():
    return os.path.join(index_dir(), "jetpack.db")


def catalog_path():
    return os.path.join(index_dir(), "catalog.csv")


def text_root():
    return os.path.join(jetpack_home(), "text")


def vector_path():
    return os.path.join(index_dir(), VECTOR_FILE)


def remove_vectors():
    """Delete stored embeddings. They are only valid for the chunks they were made from."""
    removed = False
    for name in (VECTOR_FILE,) + OLD_VECTOR_FILES:
        path = os.path.join(index_dir(), name)
        if os.path.lexists(path):
            os.remove(path)
            removed = True
    return removed


def create_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def open_index(create=False):
    path = db_path()
    if not os.path.exists(path):
        if not create:
            raise IndexUnavailable(f"No jetpack index at {path}. Run tools/ingest.py first.")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return create_db(path)
    db = sqlite3.connect(path)
    try:
        version = db.execute("pragma user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        db.close()
        raise IndexUnavailable(f"Could not read {path} ({exc}). Run tools/ingest.py to rebuild it.") from None
    if version != SCHEMA_VERSION:
        db.close()
        raise IndexUnavailable(
            f"The index at {path} was built by an older version of these tools. Run tools/ingest.py to rebuild it."
        )
    return db


def connect_db():
    return open_index(create=True)


def chunk_fp(title, text):
    """Fingerprint of what a chunk's vector is made from. A vector is used only while this still matches."""
    return hashlib.sha1(f"{title}\n{text}".encode("utf-8", "surrogatepass")).hexdigest()[:20]


def chunks(text, size=1000, overlap=150):
    """Split one page into pieces of at most size characters. No text is dropped.

    Callers pass a single page so pieces never cross pages. A piece ends at a
    paragraph, line, sentence, or word break when one falls in its second
    half. The next piece starts about overlap characters earlier, at a word.
    """
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            for mark in ("\n\n", "\n", ". ", " "):
                cut = text.rfind(mark, start + size // 2, end)
                if cut > start:
                    end = cut + (1 if mark == ". " else 0)
                    break
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        space = re.compile(r"\s").search(text, nxt, end)
        if space and space.end() < end:
            nxt = space.end()
        start = nxt
    return out


LEADER = re.compile(r"(\.\s?){6,}")
IDXLINE = re.compile(r"^.{2,80}?[,:]?\s+(\d{1,4}[a-z]?(\.\d+)*)(\s*[,;-]\s*\d{1,4}[a-z]?(\.\d+)*)*\s*$")
INDEX_HEADINGS = {"index", "subject index", "general index", "alphabetical index"}


def page_kind(text, after_index=False):
    """'toc', 'index', or ''. An index page needs an Index heading, or must follow one, and index-like lines."""
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
    if len(lines) >= 8:
        ratio = sum(1 for line in lines if IDXLINE.match(line)) / len(lines)
        titled = any(line.lower().rstrip(":") in INDEX_HEADINGS for line in lines[:4])
        if (titled and ratio >= 0.4) or (after_index and ratio >= 0.5):
            return "index"
    return ""


def numbered(pages):
    pages = list(pages)
    if pages and isinstance(pages[0], str):
        return list(enumerate(pages, 1))
    return pages


def page_flags(pages):
    """Flags by page number. An index carries on only through following pages that still look like one."""
    flags, prev, prev_no = {}, "", None
    for page_no, text in numbered(pages):
        kind = page_kind(text, after_index=(prev == "index" and prev_no == page_no - 1))
        if kind:
            flags[page_no] = kind
        prev, prev_no = kind, page_no
    return flags


def sha16(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def slug(text, limit=80):
    done = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return done[:limit].strip("-")


def clean_name(text):
    return re.sub(r"\s+", " ", CONTROL_RE.sub("", normalize(text or ""))).strip()


def title_for(rel):
    stem = os.path.splitext(os.path.basename(rel))[0]
    title = clean_name(stem.replace("_", " ").replace("-", " "))
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


def describe(rel, redactor=None):
    """Source path, title, topic, id base, and extension for a library file. Every part is scrubbed when redacting."""
    parts = rel.replace(os.sep, "/").split("/")
    stem, ext = os.path.splitext(parts[-1])
    folders = parts[:-1]
    if redactor:
        folders = [redactor(part) for part in folders]
        stem = redactor(stem)
    source = "/".join(clean_name(part) for part in folders + [stem]) + ext
    return source, title_for(source), topic_for(source), doc_id_for(source), ext.lstrip(".").lower()


def stored_source(path):
    """Prefer a path relative to the jetpack home. Otherwise keep the file name only."""
    path = os.path.abspath(path)
    home = os.path.abspath(jetpack_home())
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        rel = None
    if rel and not rel.startswith("..") and not os.path.isabs(rel):
        return clean_name(rel.replace(os.sep, "/"))
    return clean_name(os.path.basename(path))


def catalog_note(flags):
    if "superseded" in (flags or ""):
        return "Older edition kept for reference; verify against the current edition."
    return ""


def add_flag(flags, flag):
    items = [item for item in (flags or "").split(",") if item]
    if flag not in items:
        items.append(flag)
    return ",".join(items)


def write_catalog(rows):
    path = catalog_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CATALOG_HEADER)
        writer.writerows(rows)
    os.replace(tmp, path)


def upsert_catalog_row(row):
    path = catalog_path()
    body = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace", newline="") as handle:
            existing = list(csv.reader(handle))
        start = 1 if existing and existing[0] and existing[0][0] == "doc_id" else 0
        body = [item for item in existing[start:] if item and item[0] != row[0]]
    write_catalog(body + [row])


def remove_path(path):
    """Remove a file, folder, or link. A link is removed, never followed."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def doc_folder(doc_id):
    """text/<doc_id> when it is a real folder inside text/. None for a link or a missing folder."""
    path = os.path.join(text_root(), safe_doc_id(doc_id))
    if os.path.islink(path) or not os.path.isdir(path) or not within(text_root(), path):
        return None
    return path


def read_page_files(folder):
    """(page_no, text) for each regular pNNNN.txt in folder, in page order. Links are skipped."""
    pages = []
    for name in os.listdir(folder):
        match = PAGE_RE.fullmatch(name)
        if not match or name != f"p{int(match.group(1)):04d}.txt":
            continue
        path = os.path.join(folder, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            pages.append((int(match.group(1)), handle.read()))
    return sorted(pages)


def new_staging():
    os.makedirs(text_root(), exist_ok=True)
    return tempfile.mkdtemp(prefix=".building-", dir=text_root())


def stage_pages(staging, doc_id, pages):
    dest = os.path.join(staging, safe_doc_id(doc_id))
    os.makedirs(dest)
    for page_no, text in numbered(pages):
        with open(os.path.join(dest, f"p{page_no:04d}.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
    return dest


def swap_in(staging, doc_id):
    """Move a staged folder to text/<doc_id>. Whatever was there, a link included, is set aside and removed."""
    target = os.path.join(text_root(), safe_doc_id(doc_id))
    aside = os.path.join(staging, ".old-" + doc_id)
    if os.path.lexists(target):
        os.rename(target, aside)
    os.rename(os.path.join(staging, doc_id), target)
    if os.path.lexists(aside):
        remove_path(aside)


def write_page_files(doc_id, pages):
    staging = new_staging()
    try:
        stage_pages(staging, doc_id, pages)
        swap_in(staging, doc_id)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return os.path.join(text_root(), doc_id)


def insert_document(db, doc_id, title, topic, flags, source, kind, page_label, sha, pages, origin="library"):
    """Replace one document's row, chunks, and page flags. pages: texts, or (page_no, text) pairs."""
    doc_id = safe_doc_id(doc_id)
    pages = numbered(pages)
    words = low = 0
    for _, text in pages:
        count = len(text.split())
        words += count
        low += count < 30
    db.execute("delete from chunks where doc_id=?", (doc_id,))
    db.execute("delete from page_flags where doc_id=?", (doc_id,))
    db.execute(
        "insert or replace into docs(doc_id,title,topic,flags,source,kind,pages,words,sha256,page_label,origin) "
        "values(?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, title, topic, flags, source, kind, len(pages), words, sha, page_label, origin),
    )
    for page_no, text in pages:
        for seq, piece in enumerate(chunks(text)):
            db.execute(
                "insert into chunks(doc_id,page,seq,title,text,fp) values(?,?,?,?,?,?)",
                (doc_id, page_no, seq, title, piece, chunk_fp(title, piece)),
            )
    db.executemany(
        "insert into page_flags values(?,?,?)",
        [(doc_id, page_no, kind) for page_no, kind in page_flags(pages).items()],
    )
    return words, low


def delete_document(db, doc_id):
    db.execute("delete from chunks where doc_id=?", (doc_id,))
    db.execute("delete from page_flags where doc_id=?", (doc_id,))
    db.execute("delete from docs where doc_id=?", (doc_id,))


def unique_doc_id(base, used, ext=""):
    """A new id that no other document holds. A clash tries the extension first, then a number."""
    base = slug(base) or "doc"
    try:
        safe_doc_id(base)
    except ValueError:
        base += "-doc"
    options = [base] + ([f"{base}-{ext}"] if ext else [])
    n = 2
    while True:
        for candidate in options:
            if candidate not in used:
                used.add(candidate)
                return candidate
        options = [f"{base}-{n}"]
        n += 1


def prepare_pages(pages, redactor=None):
    prepared = []
    for page in pages:
        # Redact the raw text: cleaning first would turn non-breaking spaces into plain ones.
        prepared.append(clean(redactor(page)) if redactor else clean(page))
    return prepared


def make_redactor(terms_path=None):
    """Built-in patterns plus the local terms file. Raises TermsError when that file cannot be used."""
    path = terms_path or discover_terms_path()
    if not path:
        print(
            "warning: --redact found no redact_terms.local.json in the jetpack home; only the built-in patterns run.",
            file=sys.stderr,
        )
    return Redactor(load_terms(path))


def previous_index(path):
    """What the last index knew: ids by source, documents added by hand, and the highest chunk id."""
    info = {"version": 0, "by_source": {}, "added": [], "max_id": 0}
    if not os.path.exists(path):
        return info
    try:
        db = sqlite3.connect(path)
        try:
            info["version"] = db.execute("pragma user_version").fetchone()[0]
            current = info["version"] == SCHEMA_VERSION
            origin = "origin" if current else "'library'"
            for row in db.execute(
                f"select doc_id, title, topic, flags, source, kind, page_label, sha256, {origin} from docs order by doc_id"
            ):
                if row[8] == "added":
                    info["added"].append(row[:8])
                else:
                    info["by_source"].setdefault(row[4], []).append((row[0], row[7]))
            seq = 0
            if current:
                found = db.execute("select seq from sqlite_sequence where name='chunks'").fetchone()
                seq = found[0] if found else 0
            top = db.execute("select max(id) from chunks").fetchone()[0] or 0
            info["max_id"] = max(seq or 0, top)
        finally:
            db.close()
    except sqlite3.Error as exc:
        print(f"note: could not read the previous index ({exc}); building a new one", file=sys.stderr)
    return info


def plan_ids(entries, previous, used):
    """Keep each file's id from the last run, then give new files ids nobody else holds."""
    for entry in entries:
        candidates = [item for item in previous["by_source"].get(entry["source"], []) if item[0] not in used]
        if len(candidates) > 1:
            sha = entry.setdefault("sha", sha16(entry["full"]))
            candidates = [item for item in candidates if item[1] == sha][:1]
        if len(candidates) == 1:
            entry["doc_id"] = candidates[0][0]
            used.add(entry["doc_id"])
    for entry in entries:
        if "doc_id" not in entry:
            entry["doc_id"] = unique_doc_id(entry["base"], used, entry["ext"])


def library_refusal(root, home):
    real = os.path.realpath(root)
    if real in (os.path.realpath(home), os.path.realpath(CODE_ROOT)):
        return "That folder is the jetpack home. Put your files in its library folder, or name another folder."
    for name in MANAGED_DIRS:
        if within(os.path.join(home, name), real):
            return f"That folder holds the jetpack's own {name} files. Name another folder."
    return None


def library_files(root, home):
    """Regular files under root with a supported extension, whose real path stays inside root.

    Returns (files, skipped). files holds (full_path, rel_path). skipped holds
    (rel_path, reason) for links that leave the library, linked folders, and
    special files such as pipes and devices.
    """
    real_root = os.path.realpath(root)
    blocked = {os.path.realpath(os.path.join(home, name)) for name in MANAGED_DIRS}
    special = {os.path.realpath(home), os.path.realpath(CODE_ROOT)}
    skipped, seen = [], {}
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.realpath(dirpath) in special:
            dirnames[:] = [name for name in dirnames if name == "library"]
            filenames = []
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") or name == "__pycache__":
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                if not within(real_root, full):
                    skipped.append((os.path.relpath(full, root), "linked folder outside the library"))
                continue
            if os.path.realpath(full) in blocked:
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("~$"):
                continue
            if os.path.splitext(name)[1].lower() not in EXTS:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            real = os.path.realpath(full)
            if not within(real_root, real):
                skipped.append((rel, "link points outside the library"))
                continue
            try:
                regular = stat.S_ISREG(os.stat(real).st_mode)
            except OSError as exc:
                skipped.append((rel, f"cannot read ({exc.strerror})"))
                continue
            if not regular:
                skipped.append((rel, "not a regular file"))
                continue
            # A link to another library file is the same document; keep the real path.
            if real in seen and not os.path.islink(seen[real][0]):
                continue
            seen[real] = (full, rel)
    return sorted(seen.values(), key=lambda item: item[1]), skipped


def clear_leftovers(building):
    for path in (building, building + "-journal"):
        if os.path.lexists(path):
            os.remove(path)
    if os.path.isdir(text_root()):
        for name in os.listdir(text_root()):
            if name.startswith(".building-"):
                shutil.rmtree(os.path.join(text_root(), name), ignore_errors=True)


def run(library=None, redact_text=False, terms_path=None):
    home = jetpack_home()
    root = os.path.abspath(library) if library else os.path.join(home, "library")
    if not os.path.isdir(root):
        print(
            f"No library folder at {root}.\n"
            "Create it, put pdf, docx, html, or txt files inside, and run this again.",
            file=sys.stderr,
        )
        return 2
    refusal = library_refusal(root, home)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    redactor = None
    if redact_text:
        try:
            redactor = make_redactor(terms_path)
        except TermsError as exc:
            print(f"{exc}. Nothing was indexed and the previous index is unchanged.", file=sys.stderr)
            return 2
    os.makedirs(index_dir(), exist_ok=True)
    final = db_path()
    building = final + ".building"
    clear_leftovers(building)
    previous = previous_index(final)
    if os.path.exists(final) and previous["version"] != SCHEMA_VERSION:
        print(
            "note: the previous index came from an older version; run tools/add_standard.py again "
            "for documents you added that way.",
            file=sys.stderr,
        )
    vectors_cleared = remove_vectors()

    files, skipped = library_files(root, home)
    for rel, why in skipped:
        shown = describe(rel, redactor)[0] if redactor else rel
        print(f"skip {printable(shown)}: {why}", file=sys.stderr)
    entries = []
    for full, rel in files:
        source, title, topic, base, ext = describe(rel, redactor)
        entries.append({"full": full, "rel": rel, "source": source, "title": title, "topic": topic, "base": base, "ext": ext})
    carried = previous["added"] if previous["version"] == SCHEMA_VERSION else []
    used = {row[0] for row in carried}
    plan_ids(entries, previous, used)

    staging = new_staging()
    db = create_db(building)
    if previous["max_id"]:
        db.execute("insert into sqlite_sequence(name, seq) values('chunks', ?)", (previous["max_id"],))
    catalog_rows, kept_ids, staged = [], set(), []
    errors = docs = n_chunks = kept_added = 0
    done = False
    try:
        for entry in entries:
            shown = printable(entry["source"])
            try:
                pages, kind, label = extract(entry["full"])
                pages = prepare_pages(pages, redactor)
                sha = entry.get("sha") or sha16(entry["full"])
            except Exception as exc:
                errors += 1
                print(f"skip {shown}: {printable(str(exc))}", file=sys.stderr)
                continue
            doc_id = entry["doc_id"]
            flags = "redacted" if redactor else ""
            stage_pages(staging, doc_id, pages)
            staged.append(doc_id)
            words, low = insert_document(
                db, doc_id, entry["title"], entry["topic"], flags, entry["source"], kind, label, sha, pages
            )
            catalog_rows.append([
                doc_id, entry["title"], entry["topic"], flags, kind, len(pages), words, low, label,
                os.path.basename(entry["source"]), sha, catalog_note(flags),
            ])
            kept_ids.add(doc_id)
            docs += 1
            print(f"{doc_id:42s} {len(pages):4d} {label:8s} {words:7d}w low:{low}")
        for doc_id, title, topic, flags, source, kind, label, sha in carried:
            folder = doc_folder(doc_id)
            pages = read_page_files(folder) if folder else []
            if not pages:
                print(f"drop {doc_id}: it was added with add_standard.py and its text folder is gone", file=sys.stderr)
                continue
            if redactor:
                pages = [(page_no, redactor(text)) for page_no, text in pages]
                title, source, flags = redactor(title), redactor(source), add_flag(flags, "redacted")
                stage_pages(staging, doc_id, pages)
                staged.append(doc_id)
            words, low = insert_document(db, doc_id, title, topic, flags, source, kind, label, sha, pages, origin="added")
            catalog_rows.append([
                doc_id, title, topic, flags, kind, len(pages), words, low, label,
                os.path.basename(source or ""), sha, catalog_note(flags),
            ])
            kept_ids.add(doc_id)
            kept_added += 1
        db.commit()
        db.execute("insert into chunks_fts(chunks_fts) values('optimize')")
        db.commit()
        db.execute("vacuum")
        n_chunks = db.execute("select count(*) from chunks").fetchone()[0]
        db.close()
        db = None
        for doc_id in staged:
            swap_in(staging, doc_id)
        removed_text = 0
        for name in os.listdir(text_root()):
            if name.startswith(".") or name in kept_ids or not DOC_ID_RE.fullmatch(name):
                continue
            remove_path(os.path.join(text_root(), name))
            removed_text += 1
        os.replace(building, final)
        write_catalog(catalog_rows)
        done = True
    finally:
        if db is not None:
            db.close()
        if not done and os.path.exists(building):
            os.remove(building)
        shutil.rmtree(staging, ignore_errors=True)
    print(f"docs={docs} added_kept={kept_added} chunks={n_chunks} text_removed={removed_text} db={final}")
    if vectors_cleared:
        print("Vectors were cleared with the old index. Run tools/embed.py to rebuild them.")
    return 1 if errors or skipped else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index pdf, docx, html, and txt files into a local SQLite FTS5 jetpack.")
    parser.add_argument("library", nargs="?", help="Folder to scan. Default: $JETPACK_HOME/library")
    parser.add_argument(
        "--redact", action="store_true",
        help="Scrub emails, URLs, phone numbers, street addresses, and local extra terms from page text, titles, ids, and paths.",
    )
    parser.add_argument("--terms", help="Terms file for --redact. Default: redact_terms.local.json in the jetpack home.")
    args = parser.parse_args(argv)
    code = run(args.library, args.redact, args.terms)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
