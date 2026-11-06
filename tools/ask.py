#!/usr/bin/env python3
"""Search a local jetpack index.

  python tools/ask.py "question" [-k 5] [--topic safety] [--json] [--no-examples]
  python tools/ask.py --open DOC_ID PAGE
  python tools/ask.py --list

Prints matching passages with document and page citations.
Keyword rank is SQLite FTS5 / BM25. When index/vectors.npy is present, that
rank is blended with local embeddings (reciprocal rank fusion). A passage found
only by meaning must be close to the question, so a question that matches
nothing gets no answer. Search does not call a paid search API.

Ranking is neutral by default. $JETPACK_HOME/ranking.local.json can weight
topics, demote doc id prefixes, and name the topics --no-examples leaves out
(see ranking.example.json).
"""
import argparse
import bisect
import contextlib
import json
import os
import re
import sqlite3
import sys
import unicodedata

from home import VECTOR_FILES, chunk_fingerprint, jetpack_home, printable, safe_doc_id

STOP = set("""a an and are as at be by can do does for from how i in is it me my of on or should the to what when where which who why will with
you your we our this that there their they them than then into about over under per vs versus much many get need use rms""".split())

SYN = {
    "ir": ["insulation", "resistance", "megohm"],
    "megger": ["megger", "insulation", "resistance", "insulation resistance", "megohm", "megohms"],
    "dga": ["dga", "dissolved", "gas", "dissolved gas", "dissolved gases", "dissolved gas analysis", "gas-in-oil"],
    "vlf": ["vlf", "very", "low", "frequency"],
    "pf": ["power", "factor"],
    "ttr": ["turns", "ratio"],
    "hipot": ["hipot", "hi-pot", "withstand", "high potential", "high-potential", "dielectric withstand"],
    "trip": ["trip"],
    "kv": ["kv"],
    "acetylene": ["acetylene", "c2h2"],
    "hydrogen": ["hydrogen", "h2"],
    "megohms": ["megohms", "megohm"],
    "loto": ["loto", "lockout", "tagout"],
    "lockout": ["lockout", "loto"],
    "tagout": ["tagout", "loto"],
    "ppe": ["ppe", "personal", "protective", "equipment"],
    "injury": ["injury", "injuries", "incident", "accident"],
    "cord": ["cord", "cords", "cord set", "cord sets", "flexible cord", "extension cord", "assured equipment grounding"],
    "cords": ["cords", "cord", "cord set", "cord sets", "flexible cord", "extension cord", "assured equipment grounding"],
    "extension": ["extension", "extension cord", "cord set", "flexible cord"],
    "flexible": ["flexible", "flexible cord", "cord set", "extension cord"],
    "gfci": ["gfci", "ground fault circuit interrupter", "ground-fault circuit interrupter", "assured equipment grounding"],
    "aegcp": ["aegcp", "assured equipment grounding", "cord set"],
    "assured": ["assured", "aegcp"],
    "lock": ["lock", "lockout", "loto"],
    "tag": ["tag", "tagout", "loto"],
    "dlro": ["dlro", "contact resistance", "ductor", "micro-ohm", "microhm", "micro ohm"],
    "ductor": ["ductor", "dlro", "contact resistance"],
    "withstand": ["withstand", "hipot", "high-potential"],
    "dissolved": ["dissolved", "dga"],
    "tdcg": ["tdcg", "total dissolved combustible gas", "combustible gas"],
    "scan": ["scan", "scanning", "infrared", "thermographic", "thermography"],
    "infrared": ["infrared", "thermographic", "thermography", "ir scan"],
    "thermography": ["thermography", "thermographic", "infrared"],
    "thermographic": ["thermographic", "thermography", "infrared"],
    "sf6": ["sf6", "sulfur hexafluoride"],
    "ifr": ["ifr"],
    "ift": ["ift", "interfacial tension"],
    "nn": ["nn", "neutralization number", "acid number"],
    "trench": ["trench", "trenches", "excavation", "excavations"],
    "excavation": ["excavation", "excavations", "trench", "trenching"],
    "dosh": ["dosh", "permit", "division of occupational safety", "department of occupational safety", "cal/osha"],
    "gloves": ["gloves", "glove", "rubber insulating", "insulating gloves"],
    "blankets": ["blankets", "blanket", "rubber insulating"],
    "torque": ["torque", "bolt-torque", "pound-feet", "lb-ft"],
    "multiplier": ["multiplier", "conversion factor", "correction factor"],
    "correction": ["correction", "conversion", "multiplier"],
    "conversion": ["conversion", "correction", "multiplier"],
    "480": ["480", "600"],
    "480v": ["480", "600"],
    "often": ["frequency", "interval", "matrix"],
    "frequency": ["frequency", "interval"],
    "limits": ["limits", "limit", "condition", "levels", "normal", "abnormal"],
    "sample": ["sample", "sampling"],
    "syringe": ["syringe"],
    "cable": ["cable", "cables"],
    "breaker": ["breaker", "breakers"],
}

PAGE_DEMOTE = {"toc": 0.35, "index": 0.35}
SUPERSEDED_W = 0.85
RANKING_FILE = "ranking.local.json"
DEFAULT_RANKING = {"topic_weights": {}, "demote_prefixes": [], "exclude_topics": []}
MAX_GROUPS = 32
WORD = re.compile(r"[^\W_]+(?:\.\d+)*")
INFLECTIONS = ("s", "es", "d", "ed", "ing")
INTENT = {"minimum", "maximum", "limit", "limits", "value", "values", "voltage", "acceptable", "acceptance", "table", "level", "levels"}
PHRASES = [
    (r"\bhi[- ]?pot\b", "hipot"),
    (r"\bhigh[- ]potential\b", "hipot"),
    (r"\block[- ]?out[ /-]*tag[- ]?out\b", "loto"),
    (r"\bductor\b", "dlro"),
    (r"\bassured (equipment )?grounding( conductor)?( program)?\b", "aegcp assured grounding"),
]
EDN = "Older edition: a newer edition may exist. Check the current edition before relying on this for compliance."
VEC_EXPAND = {
    "cord": "extension cord set flexible cord assured equipment grounding",
    "gfci": "ground fault circuit interrupter",
    "aegcp": "assured equipment grounding conductor program cord sets",
    "loto": "lockout tagout",
    "megger": "insulation resistance test",
    "dlro": "contact resistance micro-ohm test",
    "hipot": "dielectric withstand high-potential test",
    "dga": "dissolved gas analysis",
    "tdcg": "total dissolved combustible gas",
    "dosh": "Cal/OSHA permit",
    "scan": "infrared thermographic survey",
}
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RRF_BM, RRF_VEC, VEC_W = 30, 60, 0.8
# Cosine floors for passages found only by meaning.
VEC_MIN_SIM = 0.45        # when keyword search also found something
VEC_MIN_SIM_ALONE = 0.55  # when keyword search found nothing
_PF = None
_MODEL = None
_RANKING = {}


def db_path():
    return os.path.join(jetpack_home(), "index", "jetpack.db")


def connect():
    path = db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"No jetpack index at {path}. Run tools/ingest.py first.")
    return sqlite3.connect(path)


def fold(text):
    """Casefold and strip diacritics, the way the FTS tokenizer sees words."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def parse_ranking(data):
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    weights = data.get("topic_weights", {})
    if not isinstance(weights, dict) or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in weights.values()
    ):
        raise ValueError("topic_weights must map topics to numbers")
    rules = []
    for item in data.get("demote_prefixes", []):
        if not isinstance(item, dict) or not isinstance(item.get("prefix"), str) or not item["prefix"]:
            raise ValueError("each demote_prefixes item needs a prefix")
        pattern = item.get("pattern") or ""
        weight = item.get("weight", 0.8)
        if not isinstance(pattern, str) or not isinstance(weight, (int, float)) or isinstance(weight, bool):
            raise ValueError("demote_prefixes pattern must be text and weight a number")
        rules.append((item["prefix"], re.compile(pattern, re.I) if pattern else None, float(weight)))
    exclude = data.get("exclude_topics", [])
    if not isinstance(exclude, list) or not all(isinstance(topic, str) for topic in exclude):
        raise ValueError("exclude_topics must be a list of topics")
    return {
        "topic_weights": {str(key): float(value) for key, value in weights.items()},
        "demote_prefixes": rules,
        "exclude_topics": list(exclude),
    }


def ranking():
    """Optional local ranking rules from $JETPACK_HOME/ranking.local.json. Neutral when absent."""
    path = os.path.join(jetpack_home(), RANKING_FILE)
    try:
        info = os.stat(path)
    except OSError:
        return DEFAULT_RANKING
    key = (path, info.st_mtime_ns, info.st_size)
    if key not in _RANKING:
        try:
            with open(path, encoding="utf-8") as handle:
                _RANKING[key] = parse_ranking(json.load(handle))
        except (OSError, ValueError, re.error) as exc:
            print(f"ask: ignoring {path}: {exc}", file=sys.stderr)
            _RANKING[key] = DEFAULT_RANKING
    return _RANKING[key]


def topic_weight(topic):
    return ranking()["topic_weights"].get(topic or "", 1.0)


def demote(doc, page, flags, q=""):
    global _PF
    if _PF is None:
        try:
            with contextlib.closing(connect()) as db:
                _PF = {(d, p): f for d, p, f in db.execute("select doc_id, page, flag from page_flags")}
        except Exception:
            _PF = {}
    factor = PAGE_DEMOTE.get(_PF.get((doc, page)), 1.0)
    if "superseded" in (flags or ""):
        factor *= SUPERSEDED_W
    folded = fold(q)
    for prefix, pattern, weight in ranking()["demote_prefixes"]:
        if doc.startswith(prefix) and not (pattern and pattern.search(folded)):
            factor *= weight
    return factor


def norm(q):
    q = fold(q)
    for pattern, repl in PHRASES:
        q = re.sub(pattern, repl, q)
    return q


def groups(q):
    """One OR-group per meaningful query word (word + synonyms + split units like 15kv -> 15). At most 32."""
    found, seen = [], set()
    for token in WORD.findall(norm(q)):
        if token in STOP or token in seen or (len(token) < 2 and not token.isdigit()):
            continue
        seen.add(token)
        group = [token] + SYN.get(token, [])
        unit = re.match(r"^(\d+)(kv|v|a|kva|hz)$", token)
        if unit:
            group.append(unit.group(1))
        if token.endswith("s") and len(token) > 3:
            group.append(token[:-1])
        found.append(list(dict.fromkeys(group)))
        if len(found) >= MAX_GROUPS:
            break
    return found


def terms(q):
    return list(dict.fromkeys(token for group in groups(q) for token in group))


def _variants(token):
    if token.isalpha() and len(token) >= 3:
        return {token} | {token + ending for ending in INFLECTIONS}
    return {token}


def page_index(text):
    """Whole-word view of a page: (set of tokens, space-joined tokens for phrase checks)."""
    tokens = WORD.findall(fold(text))
    return set(tokens), " " + " ".join(tokens) + " "


def _has(term, index):
    words, joined = index
    parts = WORD.findall(fold(term))
    if len(parts) == 1:
        return not words.isdisjoint(_variants(parts[0]))
    return bool(parts) and (" " + " ".join(parts) + " ") in joined


def coverage(gs, index):
    """Share of query groups with a whole-token match on the page. "ir" does not match "their"."""
    if not gs:
        return 0.0
    return sum(1 for group in gs if any(_has(token, index) for token in group)) / len(gs)


def _phrase(token):
    return '"%s"' % token.replace('"', "")


def fts_or(gs):
    return " OR ".join(_phrase(token) for group in gs for token in group)


def fts_and(gs):
    return " AND ".join("(" + " OR ".join(_phrase(token) for token in group) + ")" for group in gs)


def _topic_filter(topic, excluded):
    sql, args = "", []
    if topic:
        sql += " and d.topic = ?"
        args.append(topic)
    if excluded:
        sql += " and d.topic not in (" + ",".join("?" * len(excluded)) + ")"
        args.extend(excluded)
    return sql, args


def _run(db, match, topic, excluded):
    sql = """select c.id, c.doc_id, c.page, c.text, d.title, d.topic, d.flags, d.page_label, bm25(chunks_fts, 1.0, 0.6) as score
             from chunks_fts join chunks c on c.id = chunks_fts.rowid join docs d on d.doc_id = c.doc_id
             where chunks_fts match ?"""
    extra, args = _topic_filter(topic, excluded)
    sql += extra + " order by score limit 300"
    return db.execute(sql, [match] + args).fetchall()


def search(q, k=5, topic=None, examples=True):
    global _PF
    _PF = None
    if k <= 0:
        return []
    gs = groups(q)
    if not gs:
        return []
    excluded = [] if examples else ranking()["exclude_topics"]
    intent = bool(INTENT & set(WORD.findall(fold(q))))
    rows, seen_ids = [], set()
    tries = [fts_and(gs)]
    if len(gs) > 2:
        tries += [fts_and(gs[:i] + gs[i + 1:]) for i in range(len(gs))]
    tries.append(fts_or(gs))
    with contextlib.closing(connect()) as db:
        for level, match in enumerate(tries):
            level_weight = 1.0 if level == 0 else (0.8 if level < len(tries) - 1 else 0.55)
            for row in _run(db, match, topic, excluded):
                if row[0] in seen_ids:
                    continue
                seen_ids.add(row[0])
                rows.append((row, level_weight))
            if len({(row[0][1], row[0][2]) for row in rows}) >= max(k * 3, 15):
                break
        ranked, page_cache = [], {}
        for (cid, doc, page, text, title, top, flags, label, score), level_weight in rows:
            if (doc, page) not in page_cache:
                page_cache[(doc, page)] = page_index(" ".join(
                    body for (body,) in db.execute("select text from chunks where doc_id=? and page=?", (doc, page))
                ))
            index = page_cache[(doc, page)]
            cover = coverage(gs, index)
            if cover == 1.0 and level_weight < 1.0:
                level_weight = 1.0
            weight = (-score) * (0.4 + cover) * level_weight * topic_weight(top)
            if intent and "table" in index[0]:
                weight *= 1.25
            weight *= demote(doc, page, flags, q)
            ranked.append((weight, cid, doc, page, text, title, flags, label))
    ranked.sort(reverse=True)
    query_terms = terms(q)
    out, seen = [], set()
    for weight, cid, doc, page, text, title, flags, label in ranked:
        if (doc, page) in seen:
            continue
        seen.add((doc, page))
        out.append({
            "rank": len(out) + 1,
            "doc_id": doc,
            "title": printable(title),
            "page": page,
            "page_label": label,
            "flags": flags,
            "score": round(weight, 2),
            "passage": passage(text, query_terms),
        })
        if len(out) >= k:
            break
    return out


def vec_query(q):
    folded = norm(q)
    extra = [value for key, value in VEC_EXPAND.items() if re.search(r"\b" + key, folded)]
    return q + (" (" + "; ".join(extra) + ")" if extra else "")


def _embed_query(text):
    """Normalized query vector from the local embedding model."""
    import numpy as np
    from fastembed import TextEmbedding

    global _MODEL
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(jetpack_home(), "work", "models"))
    if _MODEL is None:
        _MODEL = TextEmbedding(EMBED_MODEL, cache_dir=os.environ["FASTEMBED_CACHE_PATH"])
    query = np.asarray(list(_MODEL.query_embed([text]))[0], dtype=np.float32)
    size = np.linalg.norm(query)
    return query / size if size else query


def load_vectors():
    """(vectors, chunk ids, fingerprints), or None when the files are missing or do not line up."""
    try:
        import numpy as np
    except ImportError:
        return None
    index = os.path.join(jetpack_home(), "index")
    paths = [os.path.join(index, name) for name in VECTOR_FILES]
    if not all(os.path.isfile(path) for path in paths):
        return None
    try:
        vecs = np.load(paths[0], mmap_mode="r")
        ids = np.load(paths[1])
        keys = np.load(paths[2])
    except (OSError, ValueError):
        return None
    if vecs.ndim != 2 or len(vecs) == 0 or not (len(vecs) == len(ids) == len(keys)):
        return None
    return vecs, ids, keys


def vector_hits(q, n=40, topic=None, examples=True, floor=VEC_MIN_SIM, loaded=None):
    """Meaning-based hits as (cosine, hit) pairs, best first, at or above floor.

    None means vectors cannot be used: missing, misaligned, or stale for every
    candidate. A vector whose chunk text or title changed since embed.py ran is
    dropped by its fingerprint.
    """
    loaded = loaded or load_vectors()
    if loaded is None:
        return None
    import numpy as np

    vecs, ids, keys = loaded
    try:
        sims = np.asarray(vecs @ _embed_query(vec_query(q)), dtype=np.float32)
    except Exception:
        return None
    excluded = [] if examples else ranking()["exclude_topics"]
    out, checked, stale = [], 0, 0
    try:
        with contextlib.closing(connect()) as db:
            if topic or excluded:
                extra, args = _topic_filter(topic, excluded)
                allowed = np.fromiter(
                    (cid for (cid,) in db.execute(
                        "select c.id from chunks c join docs d on d.doc_id = c.doc_id where 1=1" + extra, args
                    )),
                    dtype=np.int64,
                )
                sims = np.where(np.isin(ids, allowed), sims, -np.inf)
            for index in np.argsort(-sims, kind="stable"):
                sim = float(sims[index])
                if not sim >= floor:
                    break
                checked += 1
                row = db.execute(
                    "select c.doc_id, c.page, c.text, c.title, d.title, d.topic, d.flags, d.page_label "
                    "from chunks c join docs d on d.doc_id = c.doc_id where c.id = ?",
                    (int(ids[index]),),
                ).fetchone()
                if row is None or chunk_fingerprint(row[3], row[2]) != str(keys[index]):
                    stale += 1
                    continue
                doc, page, text, _, title, top, flags, label = row
                out.append((sim, {
                    "doc_id": doc, "title": printable(title), "page": page, "page_label": label,
                    "flags": flags, "topic": top, "text": text,
                }))
                if len(out) >= n:
                    break
    except sqlite3.Error:
        return None
    if checked and stale == checked:
        return None
    return out


def fuse(bm, vecs, k, q=""):
    """Reciprocal-rank fusion of keyword hits and (cosine, hit) vector hits.

    Keyword hits arrive already weighted by topic and page kind, so only the
    vector side is weighted here. A vector hit needs VEC_MIN_SIM to join keyword
    hits and VEC_MIN_SIM_ALONE when keyword search found nothing.
    """
    if k <= 0:
        return []
    floor = VEC_MIN_SIM if bm else VEC_MIN_SIM_ALONE
    query_terms = terms(q)
    score, info = {}, {}
    for i, hit in enumerate(bm):
        key = (hit["doc_id"], hit["page"])
        score[key] = score.get(key, 0) + 1.0 / (RRF_BM + i)
        info.setdefault(key, hit)
    seen = set()
    for sim, hit in vecs or ():
        if sim < floor:
            continue
        key = (hit["doc_id"], hit["page"])
        if key in seen:
            continue
        seen.add(key)
        weight = topic_weight(hit.get("topic")) * demote(key[0], key[1], hit.get("flags") or "", q)
        score[key] = score.get(key, 0) + weight * VEC_W / (RRF_VEC + len(seen) - 1)
        if key not in info:
            info[key] = {
                "doc_id": hit["doc_id"], "title": printable(hit.get("title")), "page": hit["page"],
                "page_label": hit.get("page_label"), "flags": hit.get("flags"),
                "passage": passage(hit.get("text") or "", query_terms),
            }
    out = []
    for i, key in enumerate(sorted(score, key=lambda key: -score[key])[:k], 1):
        hit = dict(info[key])
        hit["rank"] = i
        hit["score"] = round(score[key] * 1000, 2)
        out.append(hit)
    return out


def hybrid(q, k=5, topic=None, examples=True):
    """Keyword rank fused with embeddings. Falls back to keywords when vectors are missing or stale."""
    loaded = load_vectors()
    if loaded is None:
        return search(q, k, topic, examples), "bm25"
    if k <= 0:
        return [], "hybrid"
    bm = search(q, max(k, 30), topic, examples)
    floor = VEC_MIN_SIM if bm else VEC_MIN_SIM_ALONE
    vecs = vector_hits(q, 40, topic, examples, floor, loaded)
    if vecs is None:
        return search(q, k, topic, examples), "bm25"
    return fuse(bm, vecs, k, q), "hybrid"


def passage(text, query_terms, width=700):
    text = printable(re.sub(r"[ \t]{2,}", "  ", text))
    wanted = set()
    for term in query_terms:
        for word in WORD.findall(fold(term)):
            wanted |= _variants(word)
    starts = [m.start() for m in WORD.finditer(text) if fold(m.group()) in wanted]
    best, at = -1, 0
    for i in range(0, max(1, len(text) - width + 1), 60):
        count = bisect.bisect_left(starts, i + width) - bisect.bisect_left(starts, i)
        if count > best:
            best, at = count, i
    return ("…" if at else "") + text[at:at + width].strip() + ("…" if at + width < len(text) else "")


def annotate(hits):
    for hit in hits:
        flags = hit.get("flags") or ""
        if "superseded" in flags or "edition" in flags:
            hit["note"] = EDN
    return hits


def _count(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search a local jetpack index.")
    parser.add_argument("q", nargs="*")
    parser.add_argument("-k", type=_count, default=5, help="Number of hits (0 or more).")
    parser.add_argument("--topic")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-examples", action="store_true", help="Leave out the exclude_topics listed in ranking.local.json.")
    parser.add_argument("--mode", choices=["auto", "bm25", "hybrid"], default="auto")
    parser.add_argument("--open", nargs=2, metavar=("DOC_ID", "PAGE"))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.list:
            with contextlib.closing(connect()) as db:
                for row in db.execute("select doc_id,topic,pages,flags,title from docs order by topic,doc_id"):
                    print(f"{row[0]:42s} {row[1] or '':10s} {row[2] or 0:4d}p  {row[3] or '':16s} {printable(row[4])}")
            return 0
        if args.open:
            try:
                doc_id = safe_doc_id(args.open[0].lower())
                page = int(args.open[1])
            except ValueError as exc:
                print(exc)
                return 2
            path = os.path.join(jetpack_home(), "text", doc_id, f"p{page:04d}.txt")
            if not os.path.isfile(path):
                print(f"No stored page at text/{doc_id}/p{page:04d}.txt")
                return 1
            with open(path, encoding="utf-8", errors="replace") as handle:
                sys.stdout.write(printable(handle.read()))
            return 0
        question = " ".join(args.q)
        examples = not args.no_examples
        if args.mode == "bm25":
            hits, mode = search(question, args.k, args.topic, examples), "bm25"
        else:
            hits, mode = hybrid(question, args.k, args.topic, examples)
        annotate(hits)
        if args.json:
            print(json.dumps({"question": question, "mode": mode, "hits": hits}, indent=1))
            return 0
        if not hits:
            print("Not in the jetpack (no matching passages).")
            return 0
        for hit in hits:
            tag = f" [{printable(hit['flags'])}]" if hit.get("flags") else ""
            label = "p." if hit.get("page_label") == "page" else printable(str(hit.get("page_label") or "p.")) + " "
            warn = ("    NOTE: " + hit["note"] + "\n") if hit.get("note") else ""
            print(f"[{hit['rank']}] {printable(hit['title'])} — {label}{hit['page']}{tag}  (score {hit['score']})\n{warn}    {printable(hit['passage'])}\n")
        return 0
    except FileNotFoundError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
