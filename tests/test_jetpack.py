"""Behavior tests for the public jetpack tools. Documents here are synthetic."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ask  # noqa: E402
import ingest  # noqa: E402
import redact  # noqa: E402
from ingest import chunks  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ChunkTests(unittest.TestCase):
    def test_short_text_is_dropped(self):
        self.assertEqual(chunks("one two three"), [])
        self.assertEqual(len(chunks("one two three four")), 1)

    def test_long_line_overlaps_and_stays_near_the_size_limit(self):
        line = " ".join(f"w{i:04d}" for i in range(300))
        self.assertGreater(len(line), 1000)
        parts = chunks(line)
        self.assertGreaterEqual(len(parts), 2)
        self.assertIn(parts[0][-60:], parts[1])
        self.assertLessEqual(len(parts[0]), 1000)


class RedactTests(unittest.TestCase):
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

    def test_example_file_is_only_placeholders(self):
        data = json.loads(read(os.path.join(ROOT, "redact_terms.example.json")))
        matches = [item["match"] for item in data["terms"]]
        self.assertEqual(matches, ["Acme Corp", "Jane Doe"])
        self.assertNotIn("@", json.dumps(data))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("JETPACK_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.environ["JETPACK_HOME"] = self.home
        ask._PF = None

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JETPACK_HOME", None)
        else:
            os.environ["JETPACK_HOME"] = self._old
        self.tmp.cleanup()

    def _library(self, *parts):
        path = os.path.join(self.home, "library", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def test_pages_do_not_share_chunks_and_ask_cites_them(self):
        page1 = "lockout tagout procedure before opening the electrical cabinet " * 8
        page2 = "trench excavation shoring ladder permit depth " * 8
        write(self._library("safety", "field-notes.txt"), page1.strip() + "\f" + page2.strip())
        self.assertEqual(ingest.run(), 0)
        db = sqlite3.connect(os.path.join(self.home, "index", "jetpack.db"))
        rows = db.execute("select page, text from chunks").fetchall()
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

    def test_redact_flag_scrubs_indexed_text(self):
        write(
            self._library("note.txt"),
            "Email person@example.com about lockout tagout for Acme Corp cabinet work " * 4,
        )
        write(
            os.path.join(self.home, "redact_terms.local.json"),
            json.dumps({"terms": [{"match": "Acme Corp", "replacement": "[ORG]"}]}),
        )
        self.assertEqual(ingest.run(redact_text=True), 0)
        stored = read(os.path.join(self.home, "text", "note", "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        self.assertNotIn("Acme Corp", stored)
        self.assertIn("[EMAIL]", stored)
        self.assertIn("[ORG]", stored)

    def test_explicit_library_path_outside_home(self):
        outside = os.path.join(self.home, "drop")
        write(os.path.join(outside, "breaker-notes.txt"), "breaker trip unit instantaneous tolerance setting " * 6)
        home = os.path.join(self.home, "jp")
        os.environ["JETPACK_HOME"] = home
        try:
            self.assertEqual(ingest.run(outside), 0)
            self.assertTrue(os.path.exists(os.path.join(home, "index", "jetpack.db")))
            hits = ask.search("breaker trip")
            self.assertTrue(hits)
            self.assertNotIn(self.home, hits[0]["passage"])
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
        self.assertEqual(ingest.run(), 0)
        gamma = ask.search("hipot withstand", topic="cables")
        self.assertTrue(gamma)
        self.assertEqual(gamma[0]["page"], 1)
        epsilon = ask.search("syringe dielectric", topic="oil")
        self.assertTrue(epsilon)
        self.assertEqual(epsilon[0]["page_label"], "part")

    def test_reindex_picks_up_edits_and_remove_docs_refuses_then_drops(self):
        write(self._library("alpha.txt"), "oldtokenalpha breaker maintenance inspection torque value " * 6)
        write(self._library("beta.txt"), "kepttokenbeta cable insulation resistance megohms value " * 6)
        self.assertEqual(ingest.run(), 0)
        alpha = os.path.join(self.home, "text", "alpha", "p0001.txt")
        write(alpha, "zirconiumwidget breaker maintenance inspection torque value " * 6)
        import reindex
        reindex.main([])
        self.assertTrue(ask.search("zirconiumwidget"))
        self.assertEqual(ask.search("oldtokenalpha"), [])
        import remove_docs
        remove_docs.main(["alpha=missing-doc"])
        self.assertTrue(ask.search("zirconiumwidget"))
        remove_docs.main(["alpha=beta"])
        self.assertEqual(ask.search("zirconiumwidget"), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "text", "alpha")))
        self.assertTrue(ask.search("megohms"))

    def test_add_standard_then_reindex(self):
        path = os.path.join(self.home, "incoming.txt")
        write(path, "Contact person@example.com about glove inspection rubber insulating blanket " * 4)
        import add_standard
        import reindex
        add_standard.main([path, "glove-note", "Glove note", "", "safety"])
        stored = read(os.path.join(self.home, "text", "glove-note", "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        reindex.main([])
        hits = ask.search("insulating blanket")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_id"], "glove-note")

    def test_demote_uses_page_kind_and_family_not_a_fixed_id_list(self):
        self.assertEqual(ask.DOC_DEMOTE, {})
        self.assertEqual(ask.DOC_NAMED, {})
        self.assertIn("rms", ask.STOP)
        os.makedirs(os.path.join(self.home, "index"), exist_ok=True)
        db = sqlite3.connect(os.path.join(self.home, "index", "jetpack.db"))
        db.execute("create table page_flags(doc_id text, page int, flag text, primary key(doc_id, page))")
        db.execute("insert into page_flags values('any-doc', 3, 'toc')")
        db.commit()
        db.close()
        ask._PF = None
        self.assertAlmostEqual(ask.demote("ieee-400", 1, "", "cable test"), 0.8)
        self.assertAlmostEqual(ask.demote("ieee-400", 1, "", "ieee cable test"), 1.0)
        self.assertAlmostEqual(ask.demote("any-doc", 3, "", "cable test"), 0.35)
        self.assertAlmostEqual(ask.demote("any-doc", 1, "superseded", "cable test"), 0.85)

    def test_open_rejects_path_escape(self):
        code = ask.main(["--open", "../secret", "1"])
        self.assertEqual(code, 2)

    def test_cli_roundtrip(self):
        write(self._library("safety", "loto.txt"), "lockout tagout applies to this electrical cabinet door " * 8)
        env = os.environ.copy()
        env["JETPACK_HOME"] = self.home
        subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "ingest.py")], cwd=ROOT, env=env)
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


class TreeTests(unittest.TestCase):
    def test_gitignore_covers_generated_data_and_keeps_the_explainer(self):
        ignored = [
            "index/jetpack.db",
            "text/demo/p0001.txt",
            "library/manual.pdf",
            "work/models/x.bin",
            "vectors.npy",
            "vector_ids.npy",
            "catalog.csv",
            "redact_terms.local.json",
            "notes.docx",
            "archive.zip",
            "state/handoff.md",
            "state/seed.md",
            "state/handoff.md.bak-2026-11-06",
            "kept/notes.md",
        ]
        for rel in ignored:
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
            self.assertEqual(result.returncode, 0, rel)
        for rel in (
            "README.md",
            "LICENSE",
            "docs/jev-and-a-jetpack.jpg",
            "redact_terms.example.json",
            "requirements.txt",
            "tools/ask.py",
            "tools/home.py",
            "tools/sleep.py",
            "tools/keep.py",
        ):
            result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
            self.assertNotEqual(result.returncode, 0, rel)

    def test_tools_have_no_dollar_amounts(self):
        import re
        for dirpath, _, files in os.walk(os.path.join(ROOT, "tools")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                text = read(os.path.join(dirpath, name))
                self.assertIsNone(re.search(r"\$\d", text), name)


if __name__ == "__main__":
    unittest.main()
