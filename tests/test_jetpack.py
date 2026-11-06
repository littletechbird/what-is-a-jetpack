"""Behavior tests for the public jetpack tools. Documents here are synthetic."""
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import add_standard  # noqa: E402
import ask  # noqa: E402
import embed  # noqa: E402
import home  # noqa: E402
import ingest  # noqa: E402
import redact  # noqa: E402
import reindex  # noqa: E402
import remove_docs  # noqa: E402
from ingest import chunks  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def quiet(fn, *args, **kwargs):
    """Run fn with stdout and stderr captured. Returns (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class ChunkTests(unittest.TestCase):
    def test_short_text_is_kept_and_blank_text_is_not(self):
        self.assertEqual(chunks("one two three"), ["one two three"])
        self.assertEqual(chunks("DANGER 480 V"), ["DANGER 480 V"])
        self.assertEqual(chunks("  \n\t \n"), [])
        self.assertEqual(chunks("... ---"), [])

    def test_long_line_overlaps_and_stays_near_the_size_limit(self):
        line = " ".join(f"w{i:04d}" for i in range(300))
        self.assertGreater(len(line), 1000)
        parts = chunks(line)
        self.assertGreaterEqual(len(parts), 2)
        self.assertIn(parts[0][-60:], parts[1])
        self.assertLessEqual(len(parts[0]), 1000)

    def test_long_lines_are_not_glued_together(self):
        first = " ".join(f"a{i:04d}" for i in range(150))
        second = " ".join(f"b{i:04d}" for i in range(250))
        self.assertEqual((len(first) // 100, len(second) // 100), (8, 14))
        parts = chunks(first + "\n" + second)
        self.assertTrue(all(len(part) <= 1000 for part in parts), [len(part) for part in parts])
        joined = " ".join(parts)
        for token in first.split() + second.split():
            self.assertIn(token, joined)

    def test_tail_of_a_long_page_is_kept(self):
        lines = [f"line {i} breaker inspection torque record filler words" for i in range(80)]
        page = "\n".join(lines + ["uniquetailtoken"])
        parts = chunks(page)
        self.assertIn("uniquetailtoken", parts[-1])
        self.assertTrue(all(len(part) <= 1250 for part in parts))


class RedactTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("JETPACK_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["JETPACK_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JETPACK_HOME", None)
        else:
            os.environ["JETPACK_HOME"] = self._old
        self.tmp.cleanup()

    def test_builtin_patterns(self):
        raw = (
            "Write person@example.com or see https://example.com/docs and www.example.com. "
            "Call 555-013-0199 or (555) 010-0199. The shop is at 123 Main Street today."
        )
        out = redact.redact(raw, terms=[])
        for secret in ("person@example.com", "example.com", "555-013-0199", "555) 010-0199", "123 Main Street"):
            self.assertNotIn(secret, out)
        self.assertIn("[EMAIL]", out)
        self.assertIn("[WEB]", out)
        self.assertIn("[PHONE]", out)
        self.assertIn("[ADDRESS]", out)
        self.assertIn("today", out)

    def test_recall_covers_domains_unicode_dashes_and_line_breaks(self):
        terms = redact.parse_terms([{"match": "Jane Doe", "replacement": "[NAME]"}])
        raw = (
            "See acmepower.com for parts. Call 555\u2013013\u20130199 or 555\u00a0010\u2014 0199? "
            "Write person@\nexample.com or dial 555-013-\n0199. Ask Jane\nDoe or Jane  Doe or Ja\u200bne Doe. "
            "Use 3 phase CT polarity, the same way, at 240 480 4160 volts."
        )
        out = redact.redact(raw, terms)
        for secret in ("acmepower", "555", "person@", "Jane", "Doe"):
            self.assertNotIn(secret, out)
        self.assertEqual(out.count("[NAME]"), 3)
        self.assertIn("[WEB]", out)
        self.assertIn("[EMAIL]", out)
        self.assertIn("[PHONE]", out)
        self.assertIn("3 phase CT polarity", out)
        self.assertIn("the same way", out)
        self.assertIn("240 480 4160", out)
        self.assertNotIn("[ADDRESS]", out)

    def test_extra_terms_are_literal_and_optional(self):
        raw = "Acme Corp hired Jane Doe for the cabinet inspection."
        self.assertIn("Acme Corp", redact.redact(raw, terms=[]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "terms.json")
            write(path, read(os.path.join(ROOT, "redact_terms.example.json")))
            out = redact.redact(raw, terms=redact.load_terms(path))
        self.assertNotIn("Acme Corp", out)
        self.assertNotIn("Jane Doe", out)
        self.assertIn("[ORG]", out)
        self.assertIn("[NAME]", out)

    def test_terms_file_list_form_and_literal_replacement(self):
        path = os.path.join(self.tmp.name, "terms.json")
        write(path, json.dumps(["Acme Corp"]))
        self.assertEqual(redact.load_terms(path), [("Acme Corp", "[REDACTED]")])
        terms = redact.parse_terms({"terms": [
            {"match": "Jane Doe", "replacement": "\\g<0>"},
            {"match": "Acme Corp", "replacement": "[A\\1]"},
        ]})
        out = redact.redact("Jane Doe met Acme Corp.", terms)
        self.assertEqual(out, "\\g<0> met [A\\1].")

    def test_bad_terms_files_are_errors(self):
        bad_json = os.path.join(self.tmp.name, "bad.json")
        write(bad_json, "{not json")
        with self.assertRaises(redact.TermsError):
            redact.load_terms(bad_json)
        for shape in ({"terms": "Acme"}, [1, 2], {"other": []}, [{"replacement": "x"}]):
            with self.assertRaises(redact.TermsError):
                redact.parse_terms(shape)
        code, _, err = quiet(redact.main, ["--terms", bad_json, os.path.join(ROOT, "LICENSE")])
        self.assertEqual(code, 2)
        self.assertIn("bad.json", err)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "redact.py"), "--terms", bad_json],
            input="x", text=True, capture_output=True, env=dict(os.environ, JETPACK_HOME=self.tmp.name),
        )
        self.assertNotEqual(result.returncode, 0)

    def test_terms_come_only_from_the_jetpack_home(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        write(os.path.join(elsewhere, "redact_terms.local.json"), json.dumps(["Acme Corp"]))
        old_cwd = os.getcwd()
        os.chdir(elsewhere)
        try:
            terms, _, err = quiet(redact.load_local_terms)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(terms, [])
        self.assertIn("built-in patterns only", err)
        write(os.path.join(self.tmp.name, "redact_terms.local.json"), json.dumps(["Jane Doe"]))
        terms, _, err = quiet(redact.load_local_terms)
        self.assertEqual(terms, [("Jane Doe", "[REDACTED]")])
        self.assertIn("redact_terms.local.json", err)

    def test_long_hostile_tokens_stay_fast(self):
        for text in ("a" * 200000 + " @", "a." * 100000 + "@x", "5-" * 100000, "x" * 200000 + ".com"):
            started = time.time()
            redact.redact(text, [])
            self.assertLess(time.time() - started, 2.0, text[:12])

    def test_example_file_is_only_placeholders(self):
        data = json.loads(read(os.path.join(ROOT, "redact_terms.example.json")))
        matches = [item["match"] for item in data["terms"]]
        self.assertEqual(matches, ["Acme Corp", "Jane Doe"])
        self.assertNotIn("@", json.dumps(data))


class DocIdTests(unittest.TestCase):
    def test_safe_doc_id_rules(self):
        for bad in ("con", "NUL", "com1", "lpt9", "con.txt", "aux.notes", "abc.", "Alpha", "../x", "a/b", "", "-x"):
            with self.assertRaises(ValueError, msg=bad):
                home.safe_doc_id(bad)
        for good in ("alpha", "con-notes", "a.b", "com10", "d-0123456789abcdef"):
            self.assertEqual(home.safe_doc_id(good), good)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("JETPACK_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.environ["JETPACK_HOME"] = self.home
        ask._PF = None
        self._embed = ask._embed_query

    def tearDown(self):
        ask._embed_query = self._embed
        if self._old is None:
            os.environ.pop("JETPACK_HOME", None)
        else:
            os.environ["JETPACK_HOME"] = self._old
        self.tmp.cleanup()

    @property
    def db_file(self):
        return os.path.join(self.home, "index", "jetpack.db")

    def _library(self, *parts):
        path = os.path.join(self.home, "library", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def ingest(self, *args, **kwargs):
        code, _, err = quiet(ingest.run, *args, **kwargs)
        self.last_err = err
        return code

    def reindex(self, *args):
        _, out, err = quiet(reindex.main, list(args))
        self.last_err = err
        return out

    def sql(self, query, args=()):
        db = sqlite3.connect(self.db_file)
        try:
            return db.execute(query, args).fetchall()
        finally:
            db.close()

    def add(self, *args):
        result, _, _ = quiet(add_standard.main, list(args))
        return result

    def fts_ok(self):
        db = sqlite3.connect(self.db_file)
        try:
            db.execute("insert into chunks_fts(chunks_fts) values('integrity-check')")
        finally:
            db.close()

    def write_vectors(self, vector_for, keys=None):
        import numpy as np

        rows = self.sql("select c.id, c.title, c.text, d.topic from chunks c join docs d on d.doc_id=c.doc_id order by c.id")
        index = os.path.join(self.home, "index")
        np.save(os.path.join(index, "vectors.npy"), np.array([vector_for(row) for row in rows], dtype=np.float32))
        np.save(os.path.join(index, "vector_ids.npy"), np.array([row[0] for row in rows], dtype=np.int64))
        fingerprints = keys if keys is not None else [home.chunk_fingerprint(row[1], row[2]) for row in rows]
        np.save(os.path.join(index, "vector_keys.npy"), np.array(fingerprints))
        return rows

    def stub_query_vector(self, vector):
        import numpy as np

        query = np.array(vector, dtype=np.float32)
        query /= np.linalg.norm(query)
        ask._embed_query = lambda text: query

    def test_pages_do_not_share_chunks_and_ask_cites_them(self):
        page1 = "lockout tagout procedure before opening the electrical cabinet " * 8
        page2 = "trench excavation shoring ladder permit depth " * 8
        write(self._library("safety", "field-notes.txt"), page1.strip() + "\f" + page2.strip())
        self.assertEqual(self.ingest(), 0)
        rows = self.sql("select page, text from chunks")
        self.assertTrue(rows)
        for page, text in rows:
            if page == 1:
                self.assertNotIn("trench", text)
                self.assertIn("lockout", text)
            if page == 2:
                self.assertNotIn("lockout", text)
                self.assertIn("trench", text)
        page_files = sorted(os.listdir(os.path.join(self.home, "text", "safety-field-notes")))
        self.assertEqual(page_files, ["p0001.txt", "p0002.txt"])
        lock = ask.search("lockout tagout", k=3)
        trench = ask.search("trench excavation", k=3)
        self.assertTrue(lock and lock[0]["page"] == 1)
        self.assertTrue(trench and trench[0]["page"] == 2)
        self.assertEqual(lock[0]["doc_id"], "safety-field-notes")
        self.assertEqual(ask.search("lockout tagout", topic="other"), [])
        self.assertTrue(ask.search("lockout tagout", topic="safety"))
        schema = self.sql("select sql from sqlite_master where name='chunks'")[0][0].lower()
        self.assertIn("autoincrement", schema)
        self.fts_ok()

    def test_redact_flag_scrubs_indexed_text(self):
        write(
            self._library("note.txt"),
            "Email person@example.com about lockout tagout for Acme Corp cabinet work " * 4,
        )
        write(
            os.path.join(self.home, "redact_terms.local.json"),
            json.dumps({"terms": [{"match": "Acme Corp", "replacement": "[ORG]"}]}),
        )
        self.assertEqual(self.ingest(redact_text=True), 0)
        self.assertIn("redact_terms.local.json", self.last_err)
        doc_id = ingest.redacted_doc_id("note.txt")
        stored = read(os.path.join(self.home, "text", doc_id, "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        self.assertNotIn("Acme Corp", stored)
        self.assertIn("[EMAIL]", stored)
        self.assertIn("[ORG]", stored)

    def test_redact_flag_covers_every_stored_field(self):
        rel = "Acme Corp/Jane_Doe-acmepower.com-555-013-0199.txt"
        write(self._library(*rel.split("/")), "Lockout tagout for Acme Corp cabinet, call 555\u2013013\u20130199. " * 4)
        write(os.path.join(self.home, "redact_terms.local.json"), json.dumps(["Acme Corp", "Jane Doe"]))
        self.assertEqual(self.ingest(redact_text=True), 0)
        expected = "d-" + hashlib.sha256(rel.encode()).hexdigest()[:16]
        docs = self.sql("select doc_id, title, topic, flags, source, origin from docs")
        self.assertEqual(len(docs), 1)
        doc_id, title, topic, flags, source, origin = docs[0]
        self.assertEqual(doc_id, expected)
        self.assertEqual(source, "redacted")
        self.assertEqual((flags, origin), ("redacted", "library"))
        self.assertEqual(os.listdir(os.path.join(self.home, "text")), [expected])
        with open(os.path.join(self.home, "index", "catalog.csv"), encoding="utf-8", newline="") as handle:
            catalog = list(csv.DictReader(handle))
        self.assertEqual(catalog[0]["source_file"], "redacted")
        self.assertEqual(catalog[0]["doc_id"], expected)
        stored = " ".join(str(value) for value in (title, topic, flags, source))
        stored += " ".join(text + " " + chunk_title for text, chunk_title in self.sql("select text, title from chunks"))
        stored += " ".join(catalog[0][key] for key in ("title", "topic", "flags", "source_file", "note"))
        stored += read(os.path.join(self.home, "text", expected, "p0001.txt"))
        for secret in ("acme", "jane", "doe", "acmepower", "555", "0199"):
            self.assertNotIn(secret, stored.lower(), secret)
        self.assertNotEqual(title, "Untitled")
        self.assertEqual(ask.search("lockout tagout")[0]["doc_id"], expected)

    def test_bad_terms_file_stops_ingest_before_the_index_changes(self):
        write(self._library("a.txt"), "breaker maintenance torque inspection record " * 6)
        self.assertEqual(self.ingest(), 0)
        before = read(os.path.join(self.home, "text", "a", "p0001.txt"))
        with open(self.db_file, "rb") as handle:
            db_before = handle.read()
        write(self._library("a.txt"), "replacementtoken text " * 6)
        write(os.path.join(self.home, "redact_terms.local.json"), "{broken")
        self.assertEqual(self.ingest(redact_text=True), 2)
        self.assertIn("redact_terms.local.json", self.last_err)
        with open(self.db_file, "rb") as handle:
            self.assertEqual(handle.read(), db_before)
        self.assertEqual(read(os.path.join(self.home, "text", "a", "p0001.txt")), before)
        self.assertTrue(ask.search("torque inspection"))

    def test_failed_run_keeps_the_previous_index_and_text(self):
        write(self._library("a.txt"), "oldalphatoken breaker maintenance record " * 6)
        write(self._library("b.txt"), "betatoken cable insulation record " * 6)
        self.assertEqual(self.ingest(), 0)
        write(self._library("a.txt"), "newalphatoken breaker maintenance record " * 6)
        real = ingest.insert_document
        calls = []

        def flaky(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        with mock.patch.object(ingest, "insert_document", flaky):
            with self.assertRaises(RuntimeError):
                self.ingest()
        self.assertTrue(ask.search("oldalphatoken"))
        self.assertEqual(ask.search("newalphatoken"), [])
        self.assertIn("oldalphatoken", read(os.path.join(self.home, "text", "a", "p0001.txt")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "index"))), ["catalog.csv", "jetpack.db"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "text"))), ["a", "b"])

    def test_oversized_files_and_zip_bombs_are_skipped(self):
        from docx import Document

        write(self._library("ok.txt"), "breaker maintenance torque inspection record " * 6)
        huge = self._library("huge.txt")
        with open(huge, "wb") as handle:
            handle.truncate(ingest.MAX_BYTES + 1)
        docx_path = self._library("bomb.docx")
        document = Document()
        document.add_paragraph("zipbombtoken " * 2000)
        document.save(docx_path)
        import zipfile

        with zipfile.ZipFile(docx_path) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
        size = os.path.getsize(docx_path)
        self.assertGreater(expanded, size)
        with mock.patch.object(ingest, "MAX_BYTES", (expanded + size) // 2):
            code = self.ingest()
        self.assertEqual(code, 1)
        self.assertIn("huge.txt: larger than 80 MiB", self.last_err)
        self.assertIn("expands", self.last_err)
        self.assertEqual([row[0] for row in self.sql("select doc_id from docs")], ["ok"])

    def test_symlinks_and_special_files_are_skipped(self):
        secret = os.path.join(self.home, "secret.txt")
        write(secret, "outsidesecrettoken private notes " * 6)
        write(self._library("real.txt"), "breaker maintenance torque inspection record " * 6)
        os.symlink(secret, self._library("link.txt"))
        if os.path.exists("/dev/zero"):
            os.symlink("/dev/zero", self._library("zero.txt"))
        if hasattr(os, "mkfifo"):
            os.mkfifo(self._library("pipe.txt"))
        self.assertEqual(self.ingest(), 0)
        self.assertEqual([row[0] for row in self.sql("select doc_id from docs")], ["real"])
        self.assertEqual(ask.search("outsidesecrettoken"), [])
        self.assertIn("link.txt: not a regular file", self.last_err)

    def test_html_skips_comments_scripts_and_attributes(self):
        write(self._library("panel.html"), (
            "<html><head><title>Panel page</title><style>.styletoken{color:red}</style>"
            '<script>var scripttoken = "</p>";</script></head><body><!-- commenttoken -->'
            '<p>Breaker panel <a href="https://hreftoken.example.com" title="attrtoken">linktexttoken</a> clearance</p>'
            "<noscript>noscripttoken</noscript><template><p>templatetoken</p></template>"
            "<p>torque &amp; inspection</p></body></html>"
        ))
        self.assertEqual(self.ingest(), 0)
        stored = read(os.path.join(self.home, "text", "panel", "p0001.txt"))
        for token in ("linktexttoken", "Breaker panel", "torque & inspection"):
            self.assertIn(token, stored)
        for token in ("styletoken", "scripttoken", "commenttoken", "hreftoken", "attrtoken", "noscripttoken", "templatetoken"):
            self.assertNotIn(token, stored)
            self.assertEqual(ask.search(token), [], token)
        self.assertTrue(ask.search("linktexttoken"))

    def test_explicit_library_path_outside_home(self):
        outside = os.path.join(self.home, "drop")
        write(os.path.join(outside, "breaker-notes.txt"), "breaker trip unit instantaneous tolerance setting " * 6)
        jp = os.path.join(self.home, "jp")
        os.environ["JETPACK_HOME"] = jp
        try:
            self.assertEqual(self.ingest(outside), 0)
            self.assertTrue(os.path.exists(os.path.join(jp, "index", "jetpack.db")))
            hits = ask.search("breaker trip")
            self.assertTrue(hits)
            self.assertNotIn(self.home, hits[0]["passage"])
            db = sqlite3.connect(os.path.join(jp, "index", "jetpack.db"))
            (source,), = db.execute("select source from docs").fetchall()
            db.close()
            self.assertEqual(source, "breaker-notes.txt")
            self.assertFalse(os.path.isabs(source))
            catalog = read(os.path.join(jp, "index", "catalog.csv"))
            self.assertNotIn(self.home, catalog)
            self.assertIn(",breaker-notes.txt,", catalog)
        finally:
            os.environ["JETPACK_HOME"] = self.home

    def test_pdf_and_docx(self):
        import pymupdf
        from docx import Document

        pdf_path = self._library("cables", "shield-test.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "gamma cable shield hipot withstand voltage")
        page = doc.new_page()
        page.insert_text((72, 72), "delta trench excavation permit depth")
        doc.save(pdf_path)
        doc.close()
        docx_path = self._library("oil", "sample.docx")
        document = Document()
        document.add_paragraph("epsilon transformer oil dielectric breakdown sample syringe")
        document.save(docx_path)
        self.assertEqual(self.ingest(), 0)
        gamma = ask.search("hipot withstand", topic="cables")
        self.assertTrue(gamma)
        self.assertEqual(gamma[0]["page"], 1)
        epsilon = ask.search("syringe dielectric", topic="oil")
        self.assertTrue(epsilon)
        self.assertEqual(epsilon[0]["page_label"], "part")

    def test_docx_keeps_table_order_headers_footers_and_repeated_cells(self):
        from docx import Document

        document = Document()
        document.add_paragraph("beforetable paragraph text")
        cells = document.add_table(rows=1, cols=3).rows[0].cells
        for cell, value in zip(cells, ("15", "15", "600")):
            cell.text = value
        document.add_paragraph("aftertable paragraph text")
        document.sections[0].header.paragraphs[0].text = "headertoken"
        document.sections[0].footer.paragraphs[0].text = "footertoken"
        document.save(self._library("table.docx"))
        self.assertEqual(self.ingest(), 0)
        stored = read(os.path.join(self.home, "text", "table", "p0001.txt"))
        self.assertLess(stored.index("beforetable"), stored.index("15 | 15 | 600"))
        self.assertLess(stored.index("15 | 15 | 600"), stored.index("aftertable"))
        self.assertIn("Header: headertoken", stored)
        self.assertIn("Footer: footertoken", stored)
        self.assertTrue(ask.search("footertoken"))

    def test_reindex_picks_up_edits_and_remove_docs_refuses_then_drops(self):
        write(self._library("alpha.txt"), "oldtokenalpha breaker maintenance inspection torque value " * 6)
        write(self._library("beta.txt"), "kepttokenbeta cable insulation resistance megohms value " * 6)
        self.assertEqual(self.ingest(), 0)
        alpha = os.path.join(self.home, "text", "alpha", "p0001.txt")
        write(alpha, "zirconiumwidget breaker maintenance inspection torque value " * 6)
        self.reindex()
        self.assertTrue(ask.search("zirconiumwidget"))
        self.assertEqual(ask.search("oldtokenalpha"), [])
        quiet(remove_docs.main, ["alpha=missing-doc"])
        self.assertTrue(ask.search("zirconiumwidget"))
        code, out, _ = quiet(remove_docs.main, ["alpha=alpha"])
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        self.assertTrue(ask.search("zirconiumwidget"))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "text", "alpha")))
        quiet(remove_docs.main, ["alpha=beta"])
        self.assertEqual(ask.search("zirconiumwidget"), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "text", "alpha")))
        self.assertTrue(ask.search("megohms"))
        self.fts_ok()

    def test_reindex_dry_run_commits_nothing(self):
        write(self._library("alpha.txt"), "drytokenold breaker maintenance inspection torque " * 6)
        self.assertEqual(self.ingest(), 0)
        write(os.path.join(self.home, "text", "alpha", "p0001.txt"),
              "Table of Contents\nIntro ........ 1\nScope ........ 2\ndrytokennew\n")
        before = self.sql("select id, text, title from chunks order by id")
        out = self.reindex("--dry-run")
        self.assertIn("would update alpha 1", out)
        self.assertIn("dry run", out)
        self.assertEqual(self.sql("select id, text, title from chunks order by id"), before)
        self.assertEqual(self.sql("select count(*) from page_flags"), [(0,)])
        self.assertTrue(ask.search("drytokenold"))
        self.assertEqual(ask.search("drytokennew"), [])
        self.reindex()
        self.assertEqual(self.sql("select flag from page_flags"), [("toc",)])
        self.assertTrue(ask.search("drytokennew"))

    def test_reindex_drops_deleted_pages_and_missing_folders(self):
        write(self._library("two.txt"), ("pageonetoken breaker record " * 6) + "\f" + ("pagetwotoken cable record " * 6))
        self.assertEqual(self.ingest(), 0)
        os.remove(os.path.join(self.home, "text", "two", "p0002.txt"))
        self.reindex()
        self.assertEqual(ask.search("pagetwotoken"), [])
        self.assertTrue(ask.search("pageonetoken"))
        import shutil

        shutil.rmtree(os.path.join(self.home, "text", "two"))
        self.reindex()
        self.assertIn("text/two is missing", self.last_err)
        self.assertEqual(ask.search("pageonetoken"), [])
        self.assertEqual(self.sql("select doc_id from docs"), [("two",)])
        self.fts_ok()

    def test_add_standard_redacts_only_when_asked(self):
        path = os.path.join(self.home, "incoming.txt")
        write(path, "Contact person@example.com about glove inspection rubber insulating blanket " * 4)
        self.assertEqual(self.add(path, "glove-note", "Glove note", "", "safety"), "glove-note")
        self.assertIn("person@example.com", read(os.path.join(self.home, "text", "glove-note", "p0001.txt")))
        write(os.path.join(self.home, "redact_terms.local.json"), json.dumps(["Jane Doe"]))
        doc_id = self.add(path, "Jane-Doe-Gloves", "Jane Doe gloves", "", "safety", "--redact")
        self.assertEqual(doc_id, ingest.redacted_doc_id("jane-doe-gloves"))
        stored = read(os.path.join(self.home, "text", doc_id, "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        self.assertIn("[EMAIL]", stored)
        row = self.sql("select title, source, origin, flags from docs where doc_id=?", (doc_id,))[0]
        self.assertEqual(row[1:3], ("redacted", "added"))
        self.assertNotIn("Jane", row[0])
        self.assertIn("redacted", row[3])
        catalog = read(os.path.join(self.home, "index", "catalog.csv"))
        self.assertNotIn("Jane", catalog)
        with open(os.path.join(self.home, "index", "catalog.csv"), encoding="utf-8", newline="") as handle:
            rows = {row["doc_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows[doc_id]["source_file"], "redacted")
        self.assertEqual(rows["glove-note"]["source_file"], "incoming.txt")
        self.reindex()
        hits = ask.search("insulating blanket")
        self.assertEqual({hit["doc_id"] for hit in hits}, {"glove-note", doc_id})

    def test_add_standard_lowercases_ids_and_clears_old_chunks_now(self):
        path = os.path.join(self.home, "incoming.txt")
        write(path, "firstbodytoken glove inspection rubber insulating " * 4)
        self.assertEqual(self.add(path, "Glove-Note", "Glovetitleword note", "", "safety"), "glove-note")
        self.reindex()
        self.assertTrue(ask.search("firstbodytoken"))
        self.assertTrue(ask.search("glovetitleword"))
        write(path, "secondbodytoken blanket inspection rubber insulating " * 4)
        self.add(path, "glove-note", "Blanket memo", "", "safety")
        self.assertEqual(ask.search("firstbodytoken"), [])
        self.assertEqual(ask.search("glovetitleword"), [])
        self.reindex()
        self.assertTrue(ask.search("secondbodytoken"))
        self.assertEqual(ask.search("glovetitleword"), [])
        self.fts_ok()

    def test_retitle_in_docs_reaches_the_search_index(self):
        write(self._library("memo.txt"), "breaker maintenance torque inspection record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.assertTrue(ask.search("memo"))
        db = sqlite3.connect(self.db_file)
        db.execute("update docs set title='Freshname record' where doc_id='memo'")
        db.commit()
        db.close()
        self.reindex()
        self.assertEqual(ask.search("memo"), [])
        self.assertTrue(ask.search("freshname"))
        self.fts_ok()

    def test_reingest_drops_removed_library_text_and_keeps_added_docs(self):
        write(self._library("keep.txt"), "keeptoken breaker maintenance record " * 6)
        write(self._library("gone.txt"), "gonetoken cable insulation record " * 6)
        self.assertEqual(self.ingest(), 0)
        extra = os.path.join(self.home, "extra.txt")
        write(extra, "addedtoken glove inspection rubber insulating " * 4)
        self.add(extra, "added-doc", "Added doc", "", "safety")
        self.reindex()
        os.remove(self._library("gone.txt"))
        self.assertEqual(self.ingest(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "text", "gone")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "text", "added-doc")))
        self.assertEqual(ask.search("gonetoken"), [])
        self.assertTrue(ask.search("addedtoken"))
        self.assertTrue(ask.search("keeptoken"))
        origins = dict(self.sql("select doc_id, origin from docs"))
        self.assertEqual(origins, {"keep": "library", "added-doc": "added"})
        catalog_ids = [row.split(",")[0] for row in read(os.path.join(self.home, "index", "catalog.csv")).splitlines()[1:]]
        self.assertEqual(sorted(catalog_ids), ["added-doc", "keep"])
        self.fts_ok()

    def test_doc_ids_stay_stable_when_a_new_file_shares_a_slug(self):
        write(self._library("b", "x.txt"), "nestedtoken breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(self.sql("select doc_id from docs"), [("b-x",)])
        write(self._library("b-x.txt"), "roottoken cable record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(dict(self.sql("select source, doc_id from docs")), {"b/x.txt": "b-x", "b-x.txt": "b-x-2"})
        self.assertEqual(ask.search("nestedtoken")[0]["doc_id"], "b-x")

    def test_library_at_the_home_skips_generated_folders(self):
        write(self._library("a.txt"), "librarytoken breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        os.makedirs(os.path.join(self.home, "work"), exist_ok=True)
        write(os.path.join(self.home, "work", "scratch.txt"), "worktoken " * 6)
        self.assertEqual(self.ingest(self.home), 0)
        self.assertEqual(self.sql("select doc_id from docs"), [("library-a",)])
        self.assertEqual(ask.search("worktoken"), [])
        self.assertEqual(self.ingest(os.path.join(self.home, "text")), 2)

    def test_page_numbers_past_9999(self):
        write(self._library("big.txt"), "breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        write(os.path.join(self.home, "text", "big", "p10000.txt"), "tenthousandtoken page text")
        self.reindex()
        hits = ask.search("tenthousandtoken")
        self.assertEqual(hits[0]["page"], 10000)
        code, out, _ = quiet(ask.main, ["--open", "big", "10000"])
        self.assertEqual(code, 0)
        self.assertIn("tenthousandtoken", out)

    def test_symlinked_text_folder_is_replaced_and_unsafe_ids_are_refused(self):
        elsewhere = os.path.join(self.home, "elsewhere")
        write(os.path.join(elsewhere, "keep.txt"), "do not touch")
        os.makedirs(os.path.join(self.home, "text"))
        os.symlink(elsewhere, os.path.join(self.home, "text", "linked"))
        ingest.write_page_files("linked", ["hello page"])
        dest = os.path.join(self.home, "text", "linked")
        self.assertFalse(os.path.islink(dest))
        self.assertEqual(os.listdir(dest), ["p0001.txt"])
        self.assertEqual(os.listdir(elsewhere), ["keep.txt"])
        write(self._library("a.txt"), "breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        db = sqlite3.connect(self.db_file)
        db.execute("insert into docs(doc_id, title, origin) values('../escape', 'x', 'added')")
        db.commit()
        db.close()
        self.reindex()
        self.assertIn("refusing doc id '../escape'", self.last_err)
        self.assertTrue(ask.search("breaker"))

    def test_page_kind_needs_a_real_index_signal(self):
        pi_page = "Polarization Index\n" + "\n".join(f"Reading {i} min {1000 + i * 37}" for i in range(1, 26))
        self.assertEqual(reindex.page_kind(pi_page), "")
        self.assertEqual(reindex.page_kind("Test Polarization\nIndex values\n1 min 1250\n10 min 3100\n"), "")
        back_index = "Index\nBreakers, 12, 45\nCable testing, 67\nGrounding, 88\nTorque, 101\n"
        self.assertEqual(reindex.page_kind(back_index), "index")

    def test_danger_label_and_tail_token_are_searchable(self):
        write(self._library("label.txt"), "DANGER 480 V")
        body = "\n".join(f"line {i} breaker inspection torque record filler words" for i in range(80))
        write(self._library("long.txt"), body + "\nuniquetailtoken")
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(ask.search("danger")[0]["doc_id"], "label")
        self.assertEqual(ask.search("uniquetailtoken")[0]["doc_id"], "long")

    def test_coverage_and_passages_use_whole_tokens(self):
        gs = ask.groups("ir")
        self.assertEqual(ask.coverage(gs, ask.page_index("their required work")), 0.0)
        self.assertEqual(ask.coverage(gs, ask.page_index("IR reading at 1 kV")), 1.0)
        self.assertEqual(ask.coverage(ask.groups("480"), ask.page_index("4800 volts")), 0.0)
        text = "their required filler " * 60 + "IR reading here" + " their required filler" * 60
        self.assertIn("IR reading", ask.passage(text, ["ir"]))

    def test_queries_fold_diacritics_and_keep_test_words(self):
        write(self._library("de.txt"), "Prüfung der Isolation: test values for the minimum insulation resistance " * 4)
        self.assertEqual(self.ingest(), 0)
        for query in ("Prufung", "Prüfung", "Pru\u0308fung", "PRÜFUNG"):
            self.assertTrue(ask.search(query), query)
        self.assertTrue(ask.search("test values"))
        self.assertEqual(ask.groups("test values minimum"), [["test"], ["values", "value"], ["minimum"]])
        self.assertIn("rms", ask.STOP)

    def test_query_groups_are_capped(self):
        question = " ".join(f"word{i}" for i in range(40))
        self.assertEqual(len(ask.groups(question)), ask.MAX_GROUPS)
        self.assertEqual(ask.MAX_GROUPS, 32)

    def test_control_characters_do_not_reach_storage_or_the_terminal(self):
        write(self._library("esc.txt"), "breaker \x1b[2J record \x07 torque inspection " * 6)
        self.assertEqual(self.ingest(), 0)
        self.assertNotIn("\x1b", read(os.path.join(self.home, "text", "esc", "p0001.txt")))
        path = os.path.join(self.home, "x.txt")
        write(path, "glove \x1b]0;pwned\x07 inspection record " * 4)
        self.add(path, "esc-added", "Bad\x1b[31m title", "", "safety")
        self.reindex()
        db = sqlite3.connect(self.db_file)
        db.execute("update chunks set text = text || char(27) || '[2J'")
        db.commit()
        db.close()
        for argv in (["--list"], ["glove", "--mode", "bm25"], ["breaker", "--mode", "bm25"], ["--open", "esc-added", "1"]):
            _, out, _ = quiet(ask.main, argv)
            self.assertNotIn("\x1b", out, argv)
            self.assertNotIn("\x07", out, argv)

    def test_demote_defaults_are_neutral(self):
        for name in ("DOC_DEMOTE", "DOC_NAMED", "DOC_PRIOR", "REF_FAMILY"):
            self.assertFalse(hasattr(ask, name), name)
        self.assertIn("rms", ask.STOP)
        os.makedirs(os.path.join(self.home, "index"), exist_ok=True)
        db = sqlite3.connect(self.db_file)
        db.execute("create table page_flags(doc_id text, page int, flag text, primary key(doc_id, page))")
        db.execute("insert into page_flags values('any-doc', 3, 'toc')")
        db.commit()
        db.close()
        ask._PF = None
        self.assertAlmostEqual(ask.demote("ieee-400", 1, "", "cable test"), 1.0)
        self.assertAlmostEqual(ask.demote("nec-70", 1, "", "cable test"), 1.0)
        self.assertAlmostEqual(ask.demote("any-doc", 3, "", "cable test"), 0.35)
        self.assertAlmostEqual(ask.demote("any-doc", 1, "superseded", "cable test"), 0.85)
        self.assertAlmostEqual(ask.demote("any-doc", 1, "superseded rates", "cable test"), 0.85)
        for topic in ("hr", "rates", "oil-lab", "examples", "safety"):
            self.assertEqual(ask.topic_weight(topic), 1.0)

    def test_ranking_file_sets_weights_prefix_demotion_and_exclusions(self):
        write(self._library("safety", "one.txt"), "grounding clamp inspection record " * 6)
        write(self._library("general", "two.txt"), "grounding clamp inspection record " * 6)
        write(self._library("examples", "three.txt"), "grounding clamp inspection record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(len(ask.search("grounding clamp", examples=False)), 3)
        write(os.path.join(self.home, "ranking.local.json"), json.dumps({
            "topic_weights": {"safety": 3.0},
            "demote_prefixes": [{"prefix": "ieee-", "pattern": "ieee", "weight": 0.8}],
            "exclude_topics": ["examples"],
        }))
        self.assertEqual(ask.topic_weight("safety"), 3.0)
        self.assertEqual(ask.topic_weight("general"), 1.0)
        self.assertAlmostEqual(ask.demote("ieee-400", 1, "", "cable test"), 0.8)
        self.assertAlmostEqual(ask.demote("ieee-400", 1, "", "IEEE cable test"), 1.0)
        self.assertEqual(ask.search("grounding clamp")[0]["doc_id"], "safety-one")
        hits = ask.search("grounding clamp", examples=False)
        self.assertEqual({hit["doc_id"] for hit in hits}, {"safety-one", "general-two"})

    def test_ingest_and_embed_remove_stale_vector_files(self):
        write(self._library("a.txt"), "breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        self.assertEqual(self.ingest(), 0)
        for name in home.VECTOR_FILES:
            self.assertFalse(os.path.exists(os.path.join(self.home, "index", name)), name)
        os.remove(self._library("a.txt"))
        self.assertEqual(self.ingest(), 0)
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        import numpy as np

        index = os.path.join(self.home, "index")
        for name in home.VECTOR_FILES:
            np.save(os.path.join(index, name), np.zeros((1, 3), dtype=np.float32))
        quiet(embed.main)
        for name in home.VECTOR_FILES:
            self.assertFalse(os.path.exists(os.path.join(index, name)), name)

    def test_embed_uses_a_model_keyed_cache_and_writes_fingerprints(self):
        import numpy as np

        write(self._library("a.txt"), ("breaker record " * 6) + "\f" + ("cable record " * 6))
        self.assertEqual(self.ingest(), 0)
        rows = self.sql("select id, title, text from chunks order by id")
        keys = [embed.MODEL + ":" + hashlib.sha256(f"{title}\n{text[:1500]}".encode()).hexdigest() for _, title, text in rows]
        vecs = np.eye(len(rows), 4, dtype=np.float32)
        work = os.path.join(self.home, "work")
        os.makedirs(work)
        np.save(os.path.join(work, "embcache_keys.npy"), np.array(keys))
        np.save(os.path.join(work, "embcache_vecs.npy"), vecs)
        with mock.patch.dict(sys.modules, {"fastembed": None}):
            _, out, _ = quiet(embed.main)
        self.assertIn("newly_embedded=0", out)
        index = os.path.join(self.home, "index")
        self.assertEqual(list(np.load(os.path.join(index, "vector_ids.npy"))), [row[0] for row in rows])
        self.assertEqual(
            [str(key) for key in np.load(os.path.join(index, "vector_keys.npy"))],
            [home.chunk_fingerprint(title, text) for _, title, text in rows],
        )
        self.assertTrue(np.array_equal(np.load(os.path.join(index, "vectors.npy")), vecs))

    def test_stale_or_misaligned_vectors_fall_back_to_keywords(self):
        import numpy as np

        write(self._library("a.txt"), "breaker maintenance torque inspection record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.stub_query_vector([1.0, 0.0, 0.0])
        rows = self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        hits, mode = ask.hybrid("torque inspection")
        self.assertEqual(mode, "hybrid")
        self.assertTrue(hits)
        self.write_vectors(lambda row: [1.0, 0.0, 0.0], keys=["0" * 64] * len(rows))
        hits, mode = ask.hybrid("torque inspection")
        self.assertEqual(mode, "bm25")
        self.assertEqual(hits, ask.search("torque inspection"))
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        os.remove(os.path.join(self.home, "index", "vector_keys.npy"))
        self.assertEqual(ask.hybrid("torque inspection")[1], "bm25")
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        np.save(os.path.join(self.home, "index", "vector_ids.npy"), np.array([1, 2, 3], dtype=np.int64))
        self.assertEqual(ask.hybrid("torque inspection")[1], "bm25")
        os.remove(os.path.join(self.home, "index", "vector_ids.npy"))
        self.assertEqual(ask.hybrid("torque inspection")[1], "bm25")
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        write(os.path.join(self.home, "text", "a", "p0001.txt"), "rewritten breaker torque inspection page " * 6)
        self.reindex()
        self.assertEqual(ask.hybrid("torque inspection")[1], "bm25")

    def test_hybrid_says_nothing_matches_for_an_unrelated_question(self):
        write(self._library("a.txt"), "breaker maintenance torque inspection record " * 6)
        write(self._library("b.txt"), "cable insulation resistance megohm record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.write_vectors(lambda row: [1.0, 0.0, 0.0])
        self.stub_query_vector([0.5, 1.0, 0.0])
        self.assertEqual(ask.search("chocolate cake recipe"), [])
        self.assertEqual(ask.hybrid("chocolate cake recipe"), ([], "hybrid"))
        _, out, _ = quiet(ask.main, ["chocolate", "cake", "recipe"])
        self.assertIn("Not in the jetpack (no matching passages).", out)
        self.stub_query_vector([1.0, 0.1, 0.0])
        hits, mode = ask.hybrid("chocolate cake recipe")
        self.assertEqual(mode, "hybrid")
        self.assertEqual(len(hits), 2)

    def test_fuse_thresholds_and_single_weighting(self):
        write(self._library("a.txt"), "breaker record " * 6)
        self.assertEqual(self.ingest(), 0)
        db = sqlite3.connect(self.db_file)
        db.execute("insert into page_flags values('a', 1, 'toc')")
        db.commit()
        db.close()

        def vhit(doc, page=1):
            return {"doc_id": doc, "title": doc, "page": page, "page_label": "page", "flags": "", "topic": "general", "text": "x"}

        self.assertEqual(ask.fuse([], [(0.50, vhit("weak"))], 5), [])
        self.assertEqual([hit["doc_id"] for hit in ask.fuse([], [(0.60, vhit("close"))], 5)], ["close"])
        bm = [{"doc_id": "a", "title": "A", "page": 1, "page_label": "page", "flags": "", "passage": "p", "rank": 1, "score": 9}]
        fused = ask.fuse(bm, [(0.40, vhit("weak")), (0.46, vhit("near"))], 5)
        self.assertEqual([hit["doc_id"] for hit in fused], ["a", "near"])
        self.assertEqual(fused[0]["score"], round(1000 / ask.RRF_BM, 2))
        self.assertEqual(ask.fuse(bm, [], 0), [])

    def test_vector_topic_filter_applies_before_the_cutoff(self):
        for i in range(6):
            write(self._library("alpha", f"a{i}.txt"), f"alpha{i} breaker record " * 6)
        write(self._library("beta", "b.txt"), "beta cable record " * 6)
        self.assertEqual(self.ingest(), 0)
        self.write_vectors(lambda row: [0.8, 0.6, 0.0] if row[3] == "beta" else [1.0, 0.0, 0.0])
        self.stub_query_vector([1.0, 0.0, 0.0])
        hits = ask.vector_hits("anything", n=1, topic="beta")
        self.assertEqual([hit["doc_id"] for _, hit in hits], ["beta-b"])

    def test_k_zero_returns_no_hits(self):
        write(self._library("safety", "loto.txt"), "lockout tagout applies to this electrical cabinet door " * 8)
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(ask.search("lockout", k=0), [])
        self.assertEqual(ask.hybrid("lockout", k=0)[0], [])
        _, out, _ = quiet(ask.main, ["lockout", "-k", "0", "--json"])
        self.assertEqual(json.loads(out)["hits"], [])
        with self.assertRaises(SystemExit):
            quiet(ask.main, ["lockout", "-k", "-1"])

    def test_open_rejects_path_escape(self):
        code, _, _ = quiet(ask.main, ["--open", "../secret", "1"])
        self.assertEqual(code, 2)

    def test_cli_roundtrip(self):
        write(self._library("safety", "loto.txt"), "lockout tagout applies to this electrical cabinet door " * 8)
        env = os.environ.copy()
        env["JETPACK_HOME"] = self.home
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ingest.py")], cwd=ROOT, env=env, check=True, capture_output=True)
        out = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "lockout tagout", "--mode", "bm25", "--json"],
            cwd=ROOT, env=env, text=True,
        )
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "bm25")
        self.assertTrue(payload["hits"])
        self.assertIn("lockout", payload["hits"][0]["passage"])
        listed = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "--list"],
            cwd=ROOT, env=env, text=True,
        )
        self.assertIn("safety-loto", listed)
        opened = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "--open", "safety-loto", "1"],
            cwd=ROOT, env=env, text=True,
        )
        self.assertIn("lockout", opened)


class HomeTests(unittest.TestCase):
    def test_default_home_is_the_repo_and_git_checkouts_get_a_warning(self):
        old = os.environ.pop("JETPACK_HOME", None)
        warned = home._GIT_WARNED
        home._GIT_WARNED = False
        try:
            path, _, err = quiet(home.jetpack_home)
            self.assertEqual(path, ROOT)
            if os.path.exists(os.path.join(ROOT, ".git")):
                self.assertIn("inside a git worktree", err)
            _, _, again = quiet(home.jetpack_home)
            self.assertEqual(again, "")
        finally:
            home._GIT_WARNED = warned
            if old is not None:
                os.environ["JETPACK_HOME"] = old


class TreeTests(unittest.TestCase):
    def test_gitignore_covers_generated_and_private_files(self):
        ignored = [
            "index/jetpack.db",
            "text/demo/p0001.txt",
            "library/manual.pdf",
            "work/models/x.bin",
            "vectors.npy",
            "vector_ids.npy",
            "vector_keys.npy",
            "catalog.csv",
            "redact_terms.local.json",
            "redact_terms.mine.json",
            "redact_terms.local.json.bak",
            "redact_terms.local.json~",
            ".redact_terms.local.json.swp",
            "ranking.local.json",
            "results.json",
            "notes.docx",
            "mail.eml",
            "mail.msg",
            "memo.rtf",
            "memo.odt",
            "archive.zip",
        ]
        for rel in ignored:
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
            self.assertEqual(result.returncode, 0, rel)
        kept = (
            "README.md", "LICENSE", "docs/jev-and-a-jetpack.jpg", "redact_terms.example.json",
            "ranking.example.json", "requirements.txt", "tools/ask.py", "tests/test_jetpack.py",
        )
        for rel in kept:
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
            self.assertNotEqual(result.returncode, 0, rel)

    def _public_text(self):
        names = ["README.md", "redact_terms.example.json", "ranking.example.json", ".gitignore", "requirements.txt"]
        for folder in ("tools", "tests"):
            for name in sorted(os.listdir(os.path.join(ROOT, folder))):
                if name.endswith(".py"):
                    names.append(os.path.join(folder, name))
        return {name: read(os.path.join(ROOT, name)) for name in names}

    def test_tools_have_no_dollar_amounts(self):
        for dirpath, _, files in os.walk(os.path.join(ROOT, "tools")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                text = read(os.path.join(dirpath, name))
                self.assertIsNone(re.search(r"\$\d", text), name)

    def test_public_files_use_placeholder_contacts_only(self):
        for name, text in self._public_text().items():
            text = redact.normalize(text)
            for email in re.findall(r"[\w.+-]+@(?:\s*)[\w-]+(?:\.[\w-]+)+", text):
                self.assertTrue(email.endswith("example.com"), (name, email))
            for phone in redact.PHONE_RE.findall(text):
                self.assertEqual(re.sub(r"\D", "", phone)[-10:-7], "555", (name, phone))

    def test_ranking_example_is_generic_and_valid(self):
        data = json.loads(read(os.path.join(ROOT, "ranking.example.json")))
        parsed = ask.parse_ranking(data)
        self.assertEqual(set(parsed["topic_weights"]), {"safety", "standards"})
        self.assertEqual(parsed["exclude_topics"], ["examples"])


if __name__ == "__main__":
    unittest.main()
