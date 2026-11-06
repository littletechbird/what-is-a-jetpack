"""Hybrid search and stored vectors. A bag-of-words model stands in for the real one; nothing is downloaded."""
import contextlib
import io
import json
import os
import shutil

import numpy as np

from helpers import FakeModel, HomeCase, write

import ask  # noqa: E402
import embed  # noqa: E402
import ingest  # noqa: E402
import reindex  # noqa: E402


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class HybridTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.model = FakeModel()
        ask._MODEL = self.model

    def vectors(self):
        return os.path.join(self.home, "index", "vectors.npz")

    def build(self, files):
        for rel, text in files.items():
            write(self.library(*rel.split("/")), text)
        code, out, err = quiet(ingest.run)
        self.assertEqual(code, 0, err)
        return out

    def embed(self, model=None):
        code, out, _ = quiet(embed.main, [], model=model or self.model)
        self.assertEqual(code, 0)
        return out

    def test_hybrid_blends_keyword_and_vector_hits(self):
        self.build({
            "a/breaker.txt": "breaker trip unit torque settings for the main breaker " * 4,
            "b/cable.txt": "cable insulation resistance megohm readings for feeders " * 4,
        })
        self.embed()
        hits, mode = ask.hybrid("breaker trip torque")
        self.assertEqual(mode, "hybrid")
        self.assertEqual(hits[0]["doc_id"], "a-breaker")
        self.assertEqual(hits[0]["score"], round((1 / ask.RRF_BM + ask.VEC_W / ask.RRF_VEC) * 1000, 2))
        code, out, _ = quiet(ask.main, ["breaker trip torque", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["mode"], "hybrid")

    def test_a_keyword_miss_is_a_miss_even_with_vectors(self):
        self.build({"a/breaker.txt": "breaker trip unit torque settings " * 4})
        self.embed()
        self.assertEqual(ask.hybrid("zebra giraffe"), ([], "hybrid"))
        code, out, _ = quiet(ask.main, ["zebra giraffe"])
        self.assertEqual(code, 0)
        self.assertIn("Not in the jetpack", out)

    def test_rebuild_clears_vectors_and_ids_are_never_reused(self):
        self.build({
            "a/old.txt": "oldsubject breaker torque notes " * 4,
            "a/keep.txt": "keepsubject cable megohm notes " * 4,
        })
        self.embed()
        saved = self.vectors() + ".saved"
        shutil.copy(self.vectors(), saved)
        before = {row[0] for row in self.db().execute("select id from chunks")}
        os.remove(self.library("a", "old.txt"))
        out = self.build({"a/new.txt": "newsubject breaker torque notes " * 4})
        self.assertIn("Vectors were cleared", out)
        self.assertFalse(os.path.exists(self.vectors()))
        after = {row[0] for row in self.db().execute("select id from chunks")}
        self.assertFalse(before & after)
        self.assertGreater(min(after), max(before))
        shutil.copy(saved, self.vectors())
        hits, mode = ask.hybrid("newsubject breaker")
        self.assertEqual(mode, "bm25")
        self.assertEqual([hit["doc_id"] for hit in hits], ["a-new"])
        self.assertEqual(ask.vector_hits("oldsubject breaker torque"), [])
        self.assertTrue(any("no current vector" in note for note in ask.NOTES))

    def test_ids_keep_growing_across_an_empty_rebuild(self):
        self.build({"a/one.txt": "first breaker notes " * 4})
        first = self.db().execute("select max(id) from chunks").fetchone()[0]
        os.remove(self.library("a", "one.txt"))
        self.assertEqual(quiet(ingest.run)[0], 0)
        self.build({"a/two.txt": "second breaker notes " * 4})
        self.assertGreater(self.db().execute("select min(id) from chunks").fetchone()[0], first)

    def test_a_changed_chunk_does_not_use_its_old_vector(self):
        self.build({"a/doc.txt": "alphaword breaker torque settings " * 4})
        self.embed()
        self.assertTrue(ask.vector_hits("alphaword breaker"))
        cid = self.db().execute("select id from chunks").fetchone()[0]
        db = self.db()
        db.execute("update chunks set text=?, fp=? where id=?", ("betaword cable notes", ingest.chunk_fp("doc", "betaword cable notes"), cid))
        db.commit()
        db.close()
        self.assertEqual(ask.vector_hits("alphaword breaker"), [])
        hits, mode = ask.hybrid("betaword")
        self.assertEqual((mode, hits[0]["doc_id"]), ("bm25", "a-doc"))

    def test_reindex_edits_need_a_new_embed(self):
        self.build({"a/doc.txt": "alphaword breaker torque settings " * 4 + "\f" + "second page cable notes " * 4})
        self.embed()
        write(os.path.join(self.home, "text", "a-doc", "p0001.txt"), "gammaword relay coordination study " * 4)
        quiet(reindex.main, [])
        self.assertEqual([hit[3] for hit in ask.vector_hits("gammaword relay")], [2])
        out = self.embed()
        self.assertIn("newly_embedded=1", out)
        self.assertEqual(ask.vector_hits("gammaword relay")[0][2:4], ("a-doc", 1))

    def test_embed_with_no_chunks_removes_old_vectors(self):
        self.build({"a/doc.txt": "breaker notes " * 4})
        self.embed()
        for name in ("vectors.npy", "vector_ids.npy"):
            write(os.path.join(self.home, "index", name), "old")
        db = self.db()
        db.execute("delete from chunks")
        db.commit()
        db.close()
        out = self.embed()
        self.assertIn("chunks=0", out)
        for name in ("vectors.npz", "vectors.npy", "vector_ids.npy"):
            self.assertFalse(os.path.exists(os.path.join(self.home, "index", name)), name)

    def test_missing_unreadable_or_foreign_vectors_fall_back_to_keywords(self):
        self.build({"a/doc.txt": "breaker torque notes " * 4})
        self.assertEqual(ask.hybrid("breaker")[1], "bm25")
        write(self.vectors(), "not numpy")
        hits, mode = ask.hybrid("breaker")
        self.assertEqual((mode, hits[0]["doc_id"]), ("bm25", "a-doc"))
        self.assertTrue(any("could not be read" in note for note in ask.NOTES))
        del ask.NOTES[:]
        self.embed()
        with np.load(self.vectors()) as data:
            parts = {key: data[key] for key in data.files}
        parts["model"] = np.array("some/other-model")
        with open(self.vectors(), "wb") as handle:
            np.savez(handle, **parts)
        self.assertEqual(ask.hybrid("breaker")[1], "bm25")
        self.assertTrue(any("does not match" in note for note in ask.NOTES))

    def test_vector_hits_filter_topics_before_taking_the_top(self):
        files = {f"crowd/doc{i}.txt": "breaker torque notes " * 4 for i in range(12)}
        files["quiet/one.txt"] = "breaker torque spare notes " * 4
        files["examples/demo.txt"] = "breaker torque notes " * 4
        self.build(files)
        self.embed()
        hits = ask.vector_hits("breaker torque notes", n=1, topic="quiet")
        self.assertEqual([hit[2] for hit in hits], ["quiet-one"])
        everything = ask.vector_hits("breaker torque notes", n=40, examples=False)
        self.assertNotIn("examples-demo", [hit[2] for hit in everything])
        self.assertEqual(len(everything), 13)

    def test_topic_weights_count_once(self):
        self.build({"a/one.txt": "breaker torque notes " * 4, "b/two.txt": "breaker torque notes " * 4})
        self.embed()
        write(os.path.join(self.home, "ranking.local.json"), json.dumps({"topic_weights": {"b": 1.5}}))
        bm = ask.search("breaker torque", k=2)
        hits, _ = ask.hybrid("breaker torque", k=2)
        self.assertEqual([hit["doc_id"] for hit in hits], [hit["doc_id"] for hit in bm])
        self.assertEqual(hits[0]["doc_id"], "b-two")
        self.assertEqual(hits[0]["score"], round((1 / ask.RRF_BM + ask.VEC_W / ask.RRF_VEC) * 1000, 2))

    def test_embed_cache_is_reused_and_keyed_by_model(self):
        self.build({"a/doc.txt": "breaker torque notes " * 4, "b/two.txt": "cable notes " * 4})
        self.assertIn("newly_embedded=2", self.embed())
        again = FakeModel()
        self.assertIn("newly_embedded=0", self.embed(again))
        self.assertEqual(again.calls, 0)
        old = embed.EMBED_MODEL
        embed.EMBED_MODEL = "other/model"
        try:
            self.assertIn("newly_embedded=2", self.embed(FakeModel()))
        finally:
            embed.EMBED_MODEL = old

    def test_vectors_are_written_atomically_with_ids_and_fingerprints(self):
        self.build({"a/doc.txt": "breaker torque notes " * 4})
        self.embed()
        rows = self.db().execute("select id, fp from chunks order by id").fetchall()
        with np.load(self.vectors()) as data:
            self.assertEqual(data["ids"].tolist(), [row[0] for row in rows])
            self.assertEqual(data["fps"].tolist(), [row[1] for row in rows])
            self.assertEqual(str(data["model"]), ingest.EMBED_MODEL)
        self.assertFalse(os.path.exists(self.vectors() + ".tmp"))
