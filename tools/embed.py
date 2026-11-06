#!/usr/bin/env python3
"""Build local embeddings for hybrid search.

Uses BAAI/bge-small-en-v1.5 through fastembed (ONNX, CPU). Unchanged chunks
keep their cached vectors. Writes index/vectors.npy and index/vector_ids.npy.
There is no paid search API. Ask still works from keywords if you skip this.
"""
import hashlib
import os
import sqlite3
import sys
import time

from home import jetpack_home


def main():
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        sys.exit("embed.py needs numpy and fastembed. Install them with: pip install -r requirements.txt")
    home = jetpack_home()
    db_file = os.path.join(home, "index", "jetpack.db")
    work = os.path.join(home, "work")
    if not os.path.exists(db_file):
        sys.exit(f"No jetpack index at {db_file}. Run tools/ingest.py first.")
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(work, "models"))
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.join(home, "index"), exist_ok=True)
    rows = sqlite3.connect(db_file).execute(
        "select c.id, d.title, c.text from chunks c join docs d on d.doc_id=c.doc_id order by c.id"
    ).fetchall()
    if not rows:
        print("chunks=0 newly_embedded=0")
        return
    texts = [f"{title}\n{body[:1500]}" for _, title, body in rows]
    keys = [hashlib.sha1(text.encode()).hexdigest() for text in texts]
    cache = {}
    keys_path = os.path.join(work, "embcache_keys.npy")
    vecs_path = os.path.join(work, "embcache_vecs.npy")
    if os.path.exists(keys_path) and os.path.exists(vecs_path):
        cached_keys = np.load(keys_path)
        cached_vecs = np.load(vecs_path)
        cache = {key: cached_vecs[i] for i, key in enumerate(cached_keys)}
    todo = [i for i, key in enumerate(keys) if key not in cache]
    started = time.time()
    if todo:
        model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=os.environ["FASTEMBED_CACHE_PATH"])
        fresh = [texts[i] for i in todo]
        for index, vector in zip(todo, model.embed(fresh, batch_size=64)):
            vector = np.asarray(vector, dtype=np.float32)
            norm = np.linalg.norm(vector)
            cache[keys[index]] = vector / norm if norm else vector
    vecs = np.stack([cache[key] for key in keys]).astype(np.float32)
    np.save(os.path.join(home, "index", "vectors.npy"), vecs)
    np.save(os.path.join(home, "index", "vector_ids.npy"), np.array([row[0] for row in rows], dtype=np.int64))
    np.save(keys_path, np.array(keys))
    np.save(vecs_path, vecs)
    print(f"chunks={len(rows)} newly_embedded={len(todo)} in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
