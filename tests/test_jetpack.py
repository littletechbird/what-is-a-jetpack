"""Behavior tests for the public jetpack tools. Documents here are synthetic."""
import contextlib
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest

from helpers import ROOT, HomeCase, read, write

import add_standard  # noqa: E402
import ask  # noqa: E402
import ingest  # noqa: E402
import reindex  # noqa: E402
import remove_docs  # noqa: E402
from home import safe_doc_id  # noqa: E402
from ingest import chunks  # noqa: E402

POSIX_LINKS = os.name == "posix" and hasattr(os, "symlink") and hasattr(os, "mkfifo")


def quiet(fn, *args, **kwargs):
    """Run fn and return (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class ChunkTests(unittest.TestCase):
    def test_short_text_is_kept(self):
        self.assertEqual(chunks("one two three"), ["one two three"])
        self.assertEqual(chunks("DANGER 480 V"), ["DANGER 480 V"])
        self.assertEqual(chunks("   "), [])

    def test_long_line_overlaps_and_stays_near_the_size_limit(self):
        line = " ".join(f"w{i:04d}" for i in range(300))
        self.assertGreater(len(line), 1000)
        parts = chunks(line)
        self.assertGreaterEqual(len(parts), 2)
        self.assertIn(parts[0][-60:], parts[1])
        self.assertLessEqual(len(parts[0]), 1000)

    def test_no_chunk_is_past_the_limit_and_no_word_is_lost(self):
        page = ("short line\n" * 40) + ("x" * 2500) + "\n" + " ".join(f"tail{i}" for i in range(200)) + "\nend"
        parts = chunks(page)
        self.assertTrue(all(len(part) <= 1000 for part in parts))
        joined = " ".join(parts)
        for word in ["short", "tail0", "tail199", "end"]:
            self.assertIn(word, joined)
        self.assertEqual(parts[-1].split()[-1], "end")

    def test_text_without_spaces_is_split_not_dropped(self):
        page = "絶縁抵抗" * 700
        parts = chunks(page)
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(part) <= 1000 for part in parts))
        self.assertEqual(sum(len(part) for part in parts) - 150 * (len(parts) - 1), len(page))


class IndexTests(HomeCase):
    def test_pages_do_not_share_chunks_and_ask_cites_them(self):
        page1 = "lockout tagout procedure before opening the electrical cabinet " * 8
        page2 = "trench excavation shoring ladder permit depth " * 8
        write(self.library("safety", "field-notes.txt"), page1.strip() + "\f" + page2.strip())
        self.assertEqual(quiet(ingest.run)[0], 0)
        rows = self.db().execute("select page, text from chunks").fetchall()
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

    def test_short_pages_are_indexed(self):
        write(self.library("signs", "panel.txt"), "DANGER 480 V\fArc flash boundary 42 in")
        self.assertEqual(quiet(ingest.run)[0], 0)
        danger = ask.search("danger")
        self.assertTrue(danger)
        self.assertEqual((danger[0]["doc_id"], danger[0]["page"]), ("signs-panel", 1))
        self.assertIn("DANGER 480 V", danger[0]["passage"])
        self.assertEqual(ask.search("arc flash boundary")[0]["page"], 2)

    def test_redact_flag_scrubs_indexed_text(self):
        write(
            self.library("note.txt"),
            "Email person@example.com about lockout tagout for Acme Corp cabinet work " * 4,
        )
        write(
            os.path.join(self.home, "redact_terms.local.json"),
            json.dumps({"terms": [{"match": "Acme Corp", "replacement": "[ORG]"}]}),
        )
        self.assertEqual(quiet(ingest.run, redact_text=True)[0], 0)
        stored = read(os.path.join(self.home, "text", "note", "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        self.assertNotIn("Acme Corp", stored)
        self.assertIn("[EMAIL]", stored)
        self.assertIn("[ORG]", stored)

    def test_redact_covers_titles_ids_folders_sources_and_catalog(self):
        folder = self.library("Jane Doe", "x")
        name = "jane.doe@example.com call 555-013-0199 Acme Corp notes.txt"
        write(os.path.join(os.path.dirname(folder), name), "lockout tagout cabinet inspection notes " * 6)
        write(
            os.path.join(self.home, "redact_terms.local.json"),
            json.dumps({"terms": [{"match": "Jane Doe", "replacement": "[NAME]"}, "Acme Corp"]}),
        )
        code, _, err = quiet(ingest.run, redact_text=True)
        self.assertEqual(code, 0, err)
        db = self.db()
        stored = json.dumps(db.execute("select doc_id, title, topic, source, flags from docs").fetchall())
        stored += json.dumps(db.execute("select title, text from chunks").fetchall())
        stored += read(os.path.join(self.home, "index", "catalog.csv"))
        stored += " ".join(os.listdir(os.path.join(self.home, "text")))
        _, listed, _ = quiet(ask.main, ["--list"])
        hits = ask.search("lockout tagout")
        stored += listed + json.dumps(hits)
        for secret in ("jane", "doe", "example.com", "555", "0199", "acme"):
            self.assertNotIn(secret, stored.lower(), secret)
        self.assertIn("redacted", stored)
        self.assertTrue(hits)

    def test_redact_without_a_terms_file_warns(self):
        write(self.library("note.txt"), "lockout tagout cabinet work " * 6)
        code, _, err = quiet(ingest.run, redact_text=True)
        self.assertEqual(code, 0)
        self.assertIn("no redact_terms.local.json", err)

    def test_bad_terms_file_stops_a_redacted_ingest_and_keeps_the_index(self):
        write(self.library("note.txt"), "lockout tagout for Acme Corp cabinet work " * 6)
        self.assertEqual(quiet(ingest.run)[0], 0)
        terms = os.path.join(self.home, "redact_terms.local.json")
        for bad in ("{not json", "42", json.dumps({"terms": "Acme Corp"})):
            write(terms, bad)
            code, _, err = quiet(ingest.run, redact_text=True)
            self.assertEqual(code, 2, bad)
            self.assertIn("previous index is unchanged", err)
            self.assertTrue(ask.search("lockout tagout"))
        write(terms, json.dumps(["Acme Corp", {"match": "cabinet", "replacement": "C:\\path\\1 \\g<0>"}]))
        code, _, err = quiet(ingest.run, redact_text=True)
        self.assertEqual(code, 0, err)
        stored = read(os.path.join(self.home, "text", "note", "p0001.txt"))
        self.assertNotIn("Acme Corp", stored)
        self.assertNotIn("cabinet", stored)
        self.assertIn("[REDACTED]", stored)
        self.assertIn("C:\\path\\1 \\g<0>", stored)

    def test_explicit_library_path_outside_home(self):
        outside = os.path.join(self.elsewhere, "drop")
        write(os.path.join(outside, "breaker-notes.txt"), "breaker trip unit instantaneous tolerance setting " * 6)
        self.assertEqual(quiet(ingest.run, outside)[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.home, "index", "jetpack.db")))
        hits = ask.search("breaker trip")
        self.assertTrue(hits)
        self.assertNotIn(self.elsewhere, hits[0]["passage"])

    def test_ingest_refuses_the_jetpack_home_and_its_own_folders(self):
        write(os.path.join(self.home, "requirements.txt"), "numpy pymupdf fastembed docx " * 6)
        self.assertEqual(quiet(ingest.run, self.home)[0], 2)
        os.makedirs(os.path.join(self.home, "text"))
        self.assertEqual(quiet(ingest.run, os.path.join(self.home, "text"))[0], 2)
        self.assertFalse(os.path.exists(os.path.join(self.home, "index", "jetpack.db")))
        parent = self.tmp.name
        write(self.library("real.txt"), "breaker inspection torque " * 6)
        self.assertEqual(quiet(ingest.run, parent)[0], 0)
        sources = [row[0] for row in self.db().execute("select source from docs")]
        self.assertEqual(sources, ["home/library/real.txt"])

    def test_pdf_and_docx(self):
        import pymupdf
        from docx import Document

        pdf_path = self.library("cables", "shield-test.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "gamma cable shield hipot withstand voltage")
        page = doc.new_page()
        page.insert_text((72, 72), "delta trench excavation permit depth")
        doc.save(pdf_path)
        doc.close()
        docx_path = self.library("oil", "sample.docx")
        document = Document()
        document.add_paragraph("epsilon transformer oil dielectric breakdown sample syringe")
        document.save(docx_path)
        self.assertEqual(quiet(ingest.run)[0], 0)
        gamma = ask.search("hipot withstand", topic="cables")
        self.assertTrue(gamma)
        self.assertEqual(gamma[0]["page"], 1)
        epsilon = ask.search("syringe dielectric", topic="oil")
        self.assertTrue(epsilon)
        self.assertEqual(epsilon[0]["page_label"], "part")

    def test_html_comments_scripts_and_attributes_are_not_indexed(self):
        write(
            self.library("page.html"),
            "<html><head><title>Cabinet page</title><style>.x{color:red}</style></head><body>"
            "<!-- hiddencommentword lockout -->"
            "<p>visible lockout tagout steps for the cabinet</p>"
            "<a\nhref='https://example.invalid/attrword'\ntitle='attrtitleword'\n>link text</a>"
            "<script >var scriptword = 1;</script >"
            "<SCRIPT\n type='text/javascript'>uppercasescriptword()</SCRIPT\n>"
            "<p>after the scripts</p><script>unclosedscriptword",
        )
        self.assertEqual(quiet(ingest.run)[0], 0)
        stored = read(os.path.join(self.home, "text", "page", "p0001.txt"))
        self.assertIn("visible lockout tagout", stored)
        self.assertIn("after the scripts", stored)
        for word in ("hiddencommentword", "attrword", "attrtitleword", "scriptword", "uppercasescriptword",
                     "unclosedscriptword", "color"):
            self.assertNotIn(word, stored)
            self.assertEqual(ask.search(word), [], word)

    def test_reindex_picks_up_edits_and_remove_docs_refuses_then_drops(self):
        write(self.library("alpha.txt"), "oldtokenalpha breaker maintenance inspection torque value " * 6)
        write(self.library("beta.txt"), "kepttokenbeta cable insulation resistance megohms value " * 6)
        self.assertEqual(quiet(ingest.run)[0], 0)
        alpha = os.path.join(self.home, "text", "alpha", "p0001.txt")
        write(alpha, "zirconiumwidget breaker maintenance inspection torque value " * 6)
        quiet(reindex.main, [])
        self.assertTrue(ask.search("zirconiumwidget"))
        self.assertEqual(ask.search("oldtokenalpha"), [])
        _, out, _ = quiet(remove_docs.main, ["alpha=alpha"])
        self.assertIn("REFUSED", out)
        self.assertTrue(ask.search("zirconiumwidget"))
        quiet(remove_docs.main, ["alpha=missing-doc"])
        self.assertTrue(ask.search("zirconiumwidget"))
        quiet(remove_docs.main, ["alpha=beta"])
        self.assertEqual(ask.search("zirconiumwidget"), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "text", "alpha")))
        self.assertTrue(ask.search("megohms"))
        with open(os.path.join(self.home, "index", "catalog.csv"), encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][0], "doc_id")
        self.assertEqual([row[0] for row in rows[1:]], ["beta"])

    def test_reindex_drops_deleted_pages_and_documents(self):
        write(self.library("manual.txt"), "pageonetoken breaker\fpagetwotoken breaker\fpagethreetoken breaker")
        write(self.library("other.txt"), "othertoken cable shield test notes")
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertTrue(ask.search("pagethreetoken"))
        os.remove(os.path.join(self.home, "text", "manual", "p0003.txt"))
        before = self.db().execute("select count(*) from chunks").fetchone()[0]
        _, out, _ = quiet(reindex.main, ["--dry-run"])
        self.assertIn("would drop manual page 3", out)
        self.assertEqual(self.db().execute("select count(*) from chunks").fetchone()[0], before)
        self.assertTrue(ask.search("pagethreetoken"))
        quiet(reindex.main, [])
        self.assertEqual(ask.search("pagethreetoken"), [])
        self.assertTrue(ask.search("pagetwotoken"))
        self.assertEqual(self.db().execute("select pages from docs where doc_id='manual'").fetchone()[0], 2)
        import shutil

        shutil.rmtree(os.path.join(self.home, "text", "other"))
        quiet(reindex.main, [])
        self.assertEqual(ask.search("othertoken"), [])
        self.assertIsNone(self.db().execute("select 1 from docs where doc_id='other'").fetchone())

    def test_reindex_dry_run_writes_nothing(self):
        index_page = "Index\n" + "\n".join(f"Breakers, {n}, {n + 40}" for n in range(1, 25))
        write(self.library("book.txt"), "intro text about breakers\f" + index_page)
        self.assertEqual(quiet(ingest.run)[0], 0)
        db_file = os.path.join(self.home, "index", "jetpack.db")
        db = self.db()
        db.execute("delete from page_flags")
        db.commit()
        db.close()
        write(os.path.join(self.home, "text", "book", "p0001.txt"), "edited intro text")
        stamp = os.stat(db_file).st_mtime_ns
        with open(db_file, "rb") as handle:
            raw = handle.read()
        quiet(reindex.main, ["--dry-run"])
        with open(db_file, "rb") as handle:
            self.assertEqual(handle.read(), raw)
        self.assertEqual(os.stat(db_file).st_mtime_ns, stamp)
        self.assertEqual(self.db().execute("select count(*) from page_flags").fetchone()[0], 0)

    def test_ingest_fills_page_flags_and_polarization_index_is_not_a_book_index(self):
        body = ["ordinary page about breaker maintenance and torque values " * 3 for _ in range(9)]
        pi_page = "Polarization Index\nInsulation resistance ratio\n" + "\n".join(
            f"Class {c} reading {n}.5" for c in "ABCDEFGH" for n in range(1, 3)
        )
        after = "closing notes about the winding test and the next steps"
        write(self.library("report.txt"), "\f".join(body + [pi_page, after]))
        index_page = "Index\n" + "\n".join(f"Grounding, {n}, {n + 3}" for n in range(1, 25))
        index_more = "\n".join(f"Relays, {n}" for n in range(1, 25))
        write(self.library("book.txt"), "\f".join(body + [index_page, index_more, after]))
        self.assertEqual(quiet(ingest.run)[0], 0)
        flags = dict(((doc, page), flag) for doc, page, flag in self.db().execute("select * from page_flags"))
        self.assertNotIn(("report", 10), flags)
        self.assertNotIn(("report", 11), flags)
        self.assertEqual(flags.get(("book", 10)), "index")
        self.assertEqual(flags.get(("book", 11)), "index")
        self.assertNotIn(("book", 12), flags)

    def test_add_standard_indexes_now_and_redaction_is_opt_in(self):
        path = os.path.join(self.elsewhere, "incoming.txt")
        write(path, "Contact person@example.com about glove inspection rubber insulating blanket " * 4)
        quiet(add_standard.main, [path, "glove-note", "Glove note", "", "safety"])
        stored = read(os.path.join(self.home, "text", "glove-note", "p0001.txt"))
        self.assertIn("person@example.com", stored)
        hits = ask.search("insulating blanket")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_id"], "glove-note")
        quiet(add_standard.main, [path, "glove-note", "Glove note", "", "safety", "--redact"])
        stored = read(os.path.join(self.home, "text", "glove-note", "p0001.txt"))
        self.assertNotIn("person@example.com", stored)
        self.assertIn("[EMAIL]", stored)
        self.assertIn("redacted", ask.search("insulating blanket")[0]["flags"])
        quiet(reindex.main, [])
        self.assertEqual(ask.search("insulating blanket")[0]["doc_id"], "glove-note")

    def test_add_standard_ignores_a_terms_file_in_the_current_directory(self):
        write(os.path.join(self.elsewhere, "redact_terms.local.json"), json.dumps(["Decoyterm"]))
        source = os.path.join(self.elsewhere, "incoming.txt")
        write(source, "Decoyterm wrote about glove inspection and rubber insulating blankets " * 3)
        env = dict(os.environ, JETPACK_HOME=self.home)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "add_standard.py"), source, "glove", "Glove", "--redact"],
            cwd=self.elsewhere, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no redact_terms.local.json", result.stderr)
        self.assertIn("Decoyterm", read(os.path.join(self.home, "text", "glove", "p0001.txt")))
        write(os.path.join(self.home, "redact_terms.local.json"), json.dumps(["Decoyterm"]))
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "add_standard.py"), source, "glove", "Glove", "--redact"],
            cwd=self.elsewhere, env=env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Decoyterm", read(os.path.join(self.home, "text", "glove", "p0001.txt")))

    def test_add_standard_retitle_and_fewer_pages_leave_nothing_stale(self):
        first = os.path.join(self.elsewhere, "v1.txt")
        second = os.path.join(self.elsewhere, "v2.txt")
        write(first, "breaker torque firstpagetoken\fsecondpagetoken breaker torque")
        write(second, "breaker torque revisedtoken")
        quiet(add_standard.main, [first, "std-1", "Oldtitleword Guide", "", "standards"])
        self.assertTrue(ask.search("oldtitleword"))
        self.assertTrue(ask.search("secondpagetoken"))
        quiet(add_standard.main, [second, "std-1", "Newtitleword Guide", "", "standards"])
        for _ in range(2):
            self.assertEqual(ask.search("oldtitleword"), [])
            self.assertEqual(ask.search("secondpagetoken"), [])
            self.assertEqual(ask.search("firstpagetoken"), [])
            hits = ask.search("newtitleword revisedtoken")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["title"], "Newtitleword Guide")
            self.assertFalse(os.path.exists(os.path.join(self.home, "text", "std-1", "p0002.txt")))
            quiet(reindex.main, [])

    def test_reingest_keeps_added_documents_and_removes_text_for_deleted_files(self):
        write(self.library("keep.txt"), "keeptoken breaker maintenance notes " * 4)
        write(self.library("gone.txt"), "gonetoken trench excavation notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        incoming = os.path.join(self.elsewhere, "incoming.txt")
        write(incoming, "addedtoken glove inspection notes " * 4)
        quiet(add_standard.main, [incoming, "added-note", "Added note", "", "safety"])
        os.remove(self.library("gone.txt"))
        code, out, _ = quiet(ingest.run)
        self.assertEqual(code, 0)
        self.assertIn("added_kept=1", out)
        self.assertTrue(ask.search("keeptoken"))
        self.assertEqual(ask.search("gonetoken"), [])
        self.assertEqual(ask.search("addedtoken")[0]["doc_id"], "added-note")
        self.assertFalse(os.path.exists(os.path.join(self.home, "text", "gone")))
        self.assertEqual(quiet(ask.main, ["--open", "gone", "1"])[0], 1)
        self.assertEqual(quiet(ask.main, ["--open", "added-note", "1"])[0], 0)
        catalog = read(os.path.join(self.home, "index", "catalog.csv"))
        self.assertIn("added-note", catalog)
        self.assertNotIn("gone", catalog)

    def test_doc_ids_stay_put_when_a_same_named_file_arrives(self):
        write(self.library("a-b.txt"), "firsttoken breaker notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(ask.search("firsttoken")[0]["doc_id"], "a-b")
        write(self.library("a-b.html"), "<p>" + "secondtoken cable notes " * 4 + "</p>")
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(ask.search("firsttoken")[0]["doc_id"], "a-b")
        self.assertEqual(ask.search("secondtoken")[0]["doc_id"], "a-b-html")

    def test_failed_ingest_leaves_the_previous_index(self):
        write(self.library("a.txt"), "firsttoken breaker notes " * 4)
        write(self.library("b.txt"), "secondtoken cable notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        write(self.library("a.txt"), "replacedtoken breaker notes " * 4)
        original = ingest.extract

        def interrupted(path):
            if path.endswith("b.txt"):
                raise KeyboardInterrupt
            return original(path)

        ingest.extract = interrupted
        try:
            with self.assertRaises(KeyboardInterrupt):
                quiet(ingest.run)
        finally:
            ingest.extract = original
        self.assertTrue(ask.search("firsttoken"))
        self.assertTrue(ask.search("secondtoken"))
        self.assertEqual(ask.search("replacedtoken"), [])
        self.assertIn("firsttoken", read(os.path.join(self.home, "text", "a", "p0001.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "index", "jetpack.db.building")))
        self.assertEqual([n for n in os.listdir(os.path.join(self.home, "text")) if n.startswith(".")], [])

    @unittest.skipUnless(POSIX_LINKS, "needs symlinks and named pipes")
    def test_links_and_special_files_stay_inside_the_library(self):
        secret_dir = os.path.join(self.elsewhere, "private")
        write(os.path.join(secret_dir, "secret.txt"), "outsidesecret salary notes " * 4)
        write(os.path.join(secret_dir, "inner.txt"), "innersecret salary notes " * 4)
        write(self.library("real.txt"), "realtoken breaker torque notes " * 4)
        os.symlink(os.path.join(secret_dir, "secret.txt"), self.library("link.txt"))
        os.symlink(secret_dir, self.library("linkdir"))
        os.symlink(self.library("real.txt"), self.library("alias.txt"))
        os.symlink("/dev/zero", self.library("zero.txt"))
        os.mkfifo(self.library("notes.txt"))
        code, _, err = quiet(ingest.run)
        self.assertEqual(code, 1)
        self.assertIn("link.txt", err)
        self.assertIn("notes.txt: not a regular file", err)
        self.assertEqual(ask.search("outsidesecret"), [])
        self.assertEqual(ask.search("innersecret"), [])
        self.assertEqual([hit["doc_id"] for hit in ask.search("realtoken")], ["real"])

    @unittest.skipUnless(POSIX_LINKS, "needs symlinks")
    def test_a_link_in_text_is_replaced_not_written_through(self):
        write(self.library("a.txt"), "firsttoken breaker notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        target = os.path.join(self.elsewhere, "target")
        os.makedirs(target)
        folder = os.path.join(self.home, "text", "a")
        import shutil

        shutil.rmtree(folder)
        os.symlink(target, folder)
        self.assertEqual(quiet(ask.main, ["--open", "a", "1"])[0], 1)
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(os.listdir(target), [])
        self.assertFalse(os.path.islink(folder))
        self.assertIn("firsttoken", read(os.path.join(folder, "p0001.txt")))

    def test_old_index_asks_for_a_rebuild(self):
        os.makedirs(os.path.join(self.home, "index"))
        db = sqlite3.connect(os.path.join(self.home, "index", "jetpack.db"))
        db.execute("create table docs(doc_id text primary key, title text)")
        db.commit()
        db.close()
        code, out, _ = quiet(ask.main, ["anything"])
        self.assertEqual(code, 1)
        self.assertIn("older version", out)
        write(self.library("a.txt"), "firsttoken breaker notes " * 4)
        code, _, err = quiet(ingest.run)
        self.assertEqual(code, 0)
        self.assertIn("older version", err)
        self.assertTrue(ask.search("firsttoken"))

    def test_ranking_defaults_are_neutral_and_local_file_applies(self):
        ranking = ask.load_ranking()
        self.assertEqual(ranking["topic_weights"], {})
        self.assertEqual(ranking["prefix_rules"], [])
        self.assertEqual(ranking["hidden_topics"], {"examples"})
        write(self.library("alpha", "x.txt"), "shared switchgear inspection notes " * 4)
        write(self.library("beta", "y.txt"), "shared switchgear inspection notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertAlmostEqual(ask.demote("beta-y", 1, "", "switchgear"), 1.0)
        self.assertAlmostEqual(ask.demote("beta-y", 1, "superseded", "switchgear"), 0.85)
        local = os.path.join(self.home, "ranking.local.json")
        write(local, json.dumps({"topic_weights": {"beta": 3.0}}))
        self.assertEqual(ask.search("switchgear")[0]["doc_id"], "beta-y")
        write(local, json.dumps({"topic_weights": {"alpha": 3.0}}))
        self.assertEqual(ask.search("switchgear")[0]["doc_id"], "alpha-x")
        write(local, json.dumps({"prefix_rules": [{"prefix": "alpha-", "weight": 0.1, "unless": "\\balpha\\b"}]}))
        self.assertEqual(ask.search("switchgear")[0]["doc_id"], "beta-y")
        self.assertAlmostEqual(ask.demote("alpha-x", 1, "", "switchgear"), 0.1)
        self.assertAlmostEqual(ask.demote("alpha-x", 1, "", "alpha switchgear"), 1.0)
        write(local, "{broken")
        code, _, err = quiet(ask.main, ["switchgear", "--mode", "bm25"])
        self.assertEqual(code, 0)
        self.assertIn("ignored ranking.local.json", err)

    def test_page_flags_demote_toc_pages(self):
        toc = "Table of Contents\n" + "\n".join(f"Section {n} breakers ........ {n}" for n in range(1, 12))
        write(self.library("book.txt"), toc + "\fbreakers maintenance chapter with the real breakers text")
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertAlmostEqual(ask.demote("book", 1, "", "breakers"), 0.35)
        self.assertEqual(ask.search("breakers")[0]["page"], 2)

    def test_coverage_counts_whole_words_only(self):
        noise = "their required procedure. " * 30
        real = "ir insulation resistance procedure for the motor winding"
        write(self.library("noise.txt"), noise)
        write(self.library("real.txt"), real)
        self.assertEqual(quiet(ingest.run)[0], 0)
        matcher = ask.group_matcher(["ir"])
        self.assertFalse(matcher(*ask.page_words("their required")))
        self.assertTrue(matcher(*ask.page_words("IR results")))
        self.assertEqual(ask.search("ir procedure")[0]["doc_id"], "real")

    def test_soft_words_alone_still_search(self):
        write(self.library("table.txt"), "rms value test minimum maximum readings for the panel")
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertTrue(ask.search("test value minimum"))
        self.assertTrue(ask.search("rms"))
        self.assertEqual(ask.search("the and of"), [])

    def test_non_ascii_queries(self):
        write(self.library("de.txt"), "Prüfung des Isolationswiderstands an der Schaltanlage, Ω Messung")
        write(self.library("fr.txt"), "Le résumé de l'inspection du café et des disjoncteurs")
        write(self.library("ja.txt"), "絶縁抵抗 試験 の 手順")
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(ask.search("Prüfung")[0]["doc_id"], "de")
        self.assertEqual(ask.search("Ω")[0]["doc_id"], "de")
        self.assertEqual(ask.search("résumé")[0]["doc_id"], "fr")
        self.assertEqual(ask.search("cafe")[0]["doc_id"], "fr")
        self.assertEqual(ask.search("絶縁抵抗")[0]["doc_id"], "ja")
        self.assertEqual(ask.groups("Prüfung"), [["prüfung"]])

    def test_long_queries_are_capped(self):
        write(self.library("a.txt"), "breaker torque notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        query = " ".join(f"word{i}" for i in range(800)) + " breaker"
        self.assertLessEqual(len(ask.groups(query)), ask.MAX_GROUPS)
        ask.search(query)

    def test_k_zero_is_refused_the_same_way_in_every_mode(self):
        write(self.library("a.txt"), "breaker torque notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(ask.search("breaker", k=0), [])
        self.assertEqual(ask.hybrid("breaker", k=0)[0], [])
        for mode in ("bm25", "hybrid"):
            with self.assertRaises(SystemExit):
                quiet(ask.main, ["breaker", "-k", "0", "--mode", mode])

    def test_reserved_and_case_only_doc_ids(self):
        for bad in ("CON", "nul", "nul.txt", "com1", "LPT9.pdf", "abc."):
            with self.assertRaises(ValueError, msg=bad):
                safe_doc_id(bad)
        self.assertEqual(safe_doc_id("con-notes"), "con-notes")
        write(self.library("con.txt"), "breaker torque notes " * 4)
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.assertEqual(ask.search("breaker")[0]["doc_id"], "con-doc")
        source = os.path.join(self.elsewhere, "incoming.txt")
        write(source, "glove inspection notes " * 4)
        quiet(add_standard.main, [source, "glove-note", "Glove note"])
        with self.assertRaises(SystemExit) as caught:
            quiet(add_standard.main, [source, "Glove-Note", "Glove note"])
        self.assertIn("only by case", str(caught.exception))

    def test_control_characters_are_not_stored_or_printed(self):
        write(self.library("bell\x07name.txt"), "lockout \x1b[31mred\x1b[0m tagout \u202ecabinet notes " * 3)
        self.assertEqual(quiet(ingest.run)[0], 0)
        stored = read(os.path.join(self.home, "text", "bellname", "p0001.txt"))
        self.assertNotIn("\x1b", stored)
        self.assertNotIn("\u202e", stored)
        _, out, _ = quiet(ask.main, ["lockout", "--mode", "bm25"])
        _, listed, _ = quiet(ask.main, ["--list"])
        for text in (out, listed):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
        write(os.path.join(self.home, "text", "bellname", "p0001.txt"), "hand edit \x1b[2J")
        _, opened, _ = quiet(ask.main, ["--open", "bellname", "1"])
        self.assertNotIn("\x1b", opened)

    def test_page_files_past_9999_are_read(self):
        folder = os.path.join(self.home, "text", "big")
        write(os.path.join(folder, "p9999.txt"), "nine")
        write(os.path.join(folder, "p10000.txt"), "ten thousand")
        write(os.path.join(folder, "p00001.txt"), "not a canonical name")
        self.assertEqual(ingest.read_page_files(folder), [(9999, "nine"), (10000, "ten thousand")])
        self.assertEqual(quiet(ask.main, ["--open", "big", "10000"])[:2], (0, "ten thousand"))

    def test_open_rejects_path_escape(self):
        code = quiet(ask.main, ["--open", "../secret", "1"])[0]
        self.assertEqual(code, 2)
        self.assertEqual(quiet(ask.main, ["--open", "doc", "0"])[0], 2)

    def test_cli_roundtrip(self):
        write(self.library("safety", "loto.txt"), "lockout tagout applies to this electrical cabinet door " * 8)
        env = dict(os.environ, JETPACK_HOME=self.home)
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "ingest.py")],
            cwd=self.elsewhere, env=env, check=True, capture_output=True,
        )
        out = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "lockout tagout", "--mode", "bm25", "--json"],
            cwd=self.elsewhere, env=env, text=True,
        )
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "bm25")
        self.assertTrue(payload["hits"])
        self.assertIn("lockout", payload["hits"][0]["passage"])
        listed = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "--list"],
            cwd=self.elsewhere, env=env, text=True,
        )
        self.assertIn("safety-loto", listed)
        opened = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "--open", "safety-loto", "1"],
            cwd=self.elsewhere, env=env, text=True,
        )
        self.assertIn("lockout", opened)
        missed = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tools", "ask.py"), "zebra quantum"],
            cwd=self.elsewhere, env=env, text=True,
        )
        self.assertIn("Not in the jetpack", missed)


class TreeTests(unittest.TestCase):
    def check_ignore(self, rel):
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT).returncode == 0

    def test_gitignore_covers_generated_data_and_keeps_the_explainer(self):
        ignored = [
            "index/jetpack.db",
            "index/jetpack.db.building",
            "text/demo/p0001.txt",
            "library/manual.pdf",
            "work/models/x.bin",
            "vectors.npy",
            "vector_ids.npy",
            "vectors.npz",
            "catalog.csv",
            "redact_terms.local.json",
            "redact_terms.local.json~",
            ".redact_terms.local.json.swp",
            "redact_terms.json",
            "redact_terms.local.json.bak",
            "ranking.local.json",
            ".ranking.local.json.swp",
            "results.json",
            "notes.md",
            "notes.docx",
            "archive.zip",
            "state/handoff.md",
            "state/seed.md",
            "state/handoff.md.bak-2026-11-06",
            "kept/notes.md",
        ]
        for rel in ignored:
            self.assertTrue(self.check_ignore(rel), rel)
        for rel in (
            "README.md",
            "LICENSE",
            "docs/jev-and-a-jetpack.jpg",
            "docs/sleep.md",
            "docs/home.md",
            "redact_terms.example.json",
            "ranking.example.json",
            "requirements.txt",
            "tools/ask.py",
            "tools/extract.py",
            "tools/home.py",
            "tools/sleep.py",
            "tools/keep.py",
            "tests/helpers.py",
            "tests/test_extract.py",
            "tests/test_hybrid.py",
            "tests/test_redact.py",
        ):
            self.assertFalse(self.check_ignore(rel), rel)

    def test_ranking_example_is_neutral(self):
        data = json.loads(read(os.path.join(ROOT, "ranking.example.json")))
        self.assertTrue(all(weight == 1.0 for weight in data["topic_weights"].values()))
        self.assertTrue(all(rule["weight"] == 1.0 for rule in data["prefix_rules"]))
        self.assertEqual(data["page_weights"], ask.DEFAULT_RANKING["page_weights"])
        self.assertEqual(data["superseded_weight"], ask.DEFAULT_RANKING["superseded_weight"])
        self.assertEqual(data["synonyms"], {})
        self.assertEqual(ask.DEFAULT_RANKING["topic_weights"], {})
        self.assertEqual(ask.DEFAULT_RANKING["prefix_rules"], [])

    def test_requirements_are_pinned(self):
        lines = [line.strip() for line in read(os.path.join(ROOT, "requirements.txt")).splitlines() if line.strip()]
        self.assertTrue(lines)
        self.assertTrue(all("==" in line for line in lines), lines)

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
