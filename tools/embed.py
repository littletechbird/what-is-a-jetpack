#!/usr/bin/env python3
"""Build local embeddings for hybrid search.

Uses BAAI/bge-small-en-v1.5 through fastembed (ONNX, CPU). Unchanged chunks
keep their cached vectors. Writes index/vectors.npz, which holds each
vector with its chunk id, a fingerprint of the chunk's title and text, and
the model name. Search uses a vector only while that fingerprint still
matches the chunk, so a rebuilt or edited index never borrows an old vector.
There is no paid search API. Ask still works from keywords if you skip this.
"""
import argparse
import hashlib
import os
import sys
import time

from home import jetpack_home
from ingest import EMBED_MODEL, IndexUnavailable, OLD_VECTOR_FILES, open_index, remove_vectors, vector_path


def load_model():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL, cache_dir=os.environ["FASTEMBED_CACHE_PATH"])


def save_vectors(np, path, vectors, ids, fps):
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        np.savez(handle, vectors=vectors, ids=ids, fps=fps, model=np.array(EMBED_MODEL))
    os.replace(tmp, path)


def main(argv=None, model=None):
    argparse.ArgumentParser(
        description="Embed the index's chunks for hybrid search. Unchanged chunks reuse cached vectors."
    ).parse_args(argv)
    try:
        import numpy as np
        if model is None:
            import fastembed  # noqa: F401
    except ImportError:
        sys.exit("embed.py needs numpy and fastembed. Install them with: pip install -r requirements.txt")
    home = jetpack_home()
    work = os.path.join(home, "work")
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(work, "models"))
    try:
        db = open_index()
    except IndexUnavailable as exc:
        sys.exit(str(exc))
    try:
        rows = db.execute("select c.id, c.title, c.text, c.fp from chunks c order by c.id").fetchall()
    finally:
        db.close()
    if not rows:
        cleared = remove_vectors()
        print("chunks=0 newly_embedded=0" + (" old_vectors_removed" if cleared else ""))
        return 0
    os.makedirs(work, exist_ok=True)
    texts = [f"{title}\n{body[:1500]}" for _, title, body, _ in rows]
    keys = [hashlib.sha1(f"{EMBED_MODEL}\n{text}".encode("utf-8", "surrogatepass")).hexdigest() for text in texts]
    cache = {}
    keys_path = os.path.join(work, "embcache_keys.npy")
    vecs_path = os.path.join(work, "embcache_vecs.npy")
    if os.path.exists(keys_path) and os.path.exists(vecs_path):
        try:
            cached_keys = np.load(keys_path)
            cached_vecs = np.load(vecs_path)
            if len(cached_keys) == len(cached_vecs):
                cache = {str(key): cached_vecs[i] for i, key in enumerate(cached_keys)}
        except (OSError, ValueError):
            cache = {}
    todo = [i for i, key in enumerate(keys) if key not in cache]
    started = time.time()
    if todo:
        model = model or load_model()
        fresh = [texts[i] for i in todo]
        for index, vector in zip(todo, model.embed(fresh, batch_size=64)):
            vector = np.asarray(vector, dtype=np.float32)
            norm = np.linalg.norm(vector)
            cache[keys[index]] = vector / norm if norm else vector
    vecs = np.stack([cache[key] for key in keys]).astype(np.float32)
    save_vectors(
        np, vector_path(), vecs,
        np.array([row[0] for row in rows], dtype=np.int64),
        np.array([row[3] for row in rows]),
    )
    for name in OLD_VECTOR_FILES:
        old = os.path.join(home, "index", name)
        if os.path.lexists(old):
            os.remove(old)
    np.save(keys_path, np.array(keys))
    np.save(vecs_path, vecs)
    print(f"chunks={len(rows)} newly_embedded={len(todo)} in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
