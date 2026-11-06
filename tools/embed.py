#!/usr/bin/env python3
"""Build local embeddings for hybrid search.

Uses BAAI/bge-small-en-v1.5 through fastembed (ONNX, CPU). Unchanged chunks
keep their cached vectors; the cache key includes the model name. Writes
index/vectors.npy, index/vector_ids.npy, and index/vector_keys.npy (a
fingerprint of each chunk's title and text, so ask.py can drop a vector whose
chunk changed). There is no paid search API. Ask still works from keywords if
you skip this.
"""
import hashlib
import os
import sqlite3
import sys
import time

from home import VECTOR_FILES, chunk_fingerprint, jetpack_home, remove_vector_files

MODEL = "BAAI/bge-small-en-v1.5"


def _write(np, path, array):
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        np.save(handle, array)
    return tmp


def _save(np, path, array):
    os.replace(_write(np, path, array), path)


def save_vectors(np, index, vecs, ids, fingerprints):
    """Replace the three vector files. The fingerprint file is absent while the others change."""
    paths = [os.path.join(index, name) for name in VECTOR_FILES]
    temps = [_write(np, path, array) for path, array in zip(paths, (vecs, ids, fingerprints))]
    try:
        os.remove(paths[2])
    except FileNotFoundError:
        pass
    for tmp, path in zip(temps, paths):
        os.replace(tmp, path)


def main():
    try:
        import numpy as np
    except ImportError:
        sys.exit("embed.py needs numpy and fastembed. Install them with: pip install -r requirements.txt")
    home = jetpack_home()
    index = os.path.join(home, "index")
    db_file = os.path.join(index, "jetpack.db")
    work = os.path.join(home, "work")
    if not os.path.exists(db_file):
        sys.exit(f"No jetpack index at {db_file}. Run tools/ingest.py first.")
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(work, "models"))
    os.makedirs(work, exist_ok=True)
    db = sqlite3.connect(db_file)
    try:
        rows = db.execute("select id, title, text from chunks order by id").fetchall()
    finally:
        db.close()
    if not rows:
        remove_vector_files(home)
        print("chunks=0 newly_embedded=0 (old vector files removed)")
        return
    texts = [f"{title or ''}\n{body[:1500]}" for _, title, body in rows]
    keys = [MODEL + ":" + hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    fingerprints = [chunk_fingerprint(title, body) for _, title, body in rows]
    cache = {}
    keys_path = os.path.join(work, "embcache_keys.npy")
    vecs_path = os.path.join(work, "embcache_vecs.npy")
    if os.path.exists(keys_path) and os.path.exists(vecs_path):
        cached_keys = np.load(keys_path)
        cached_vecs = np.load(vecs_path)
        if len(cached_keys) == len(cached_vecs):
            cache = {str(key): cached_vecs[i] for i, key in enumerate(cached_keys)}
    todo = [i for i, key in enumerate(keys) if key not in cache]
    started = time.time()
    if todo:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            sys.exit("embed.py needs numpy and fastembed. Install them with: pip install -r requirements.txt")
        model = TextEmbedding(MODEL, cache_dir=os.environ["FASTEMBED_CACHE_PATH"])
        fresh = [texts[i] for i in todo]
        for i, vector in zip(todo, model.embed(fresh, batch_size=64)):
            vector = np.asarray(vector, dtype=np.float32)
            norm = np.linalg.norm(vector)
            cache[keys[i]] = vector / norm if norm else vector
    vecs = np.stack([cache[key] for key in keys]).astype(np.float32)
    save_vectors(np, index, vecs, np.array([row[0] for row in rows], dtype=np.int64), np.array(fingerprints))
    _save(np, keys_path, np.array(keys))
    _save(np, vecs_path, vecs)
    print(f"chunks={len(rows)} newly_embedded={len(todo)} in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
