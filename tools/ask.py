#!/usr/bin/env python3
"""Search a local jetpack index.

  python tools/ask.py "question" [-k 5] [--topic safety] [--json] [--no-examples]
  python tools/ask.py --open DOC_ID PAGE
  python tools/ask.py --list

Prints matching passages with document and page citations.
Keyword rank is SQLite FTS5 / BM25. When index/vectors.npy is present, that
rank is blended with local embeddings (reciprocal rank fusion). Search does
not call a paid search API.
"""
import argparse
import json
import os
import re
import sqlite3
import sys

from home import jetpack_home, safe_doc_id

STOP = set("""a an and are as at be by can do does for from how i in is it me my of on or should the to what when where which who why will with
you your we our this that there their they them than then into about over under per vs versus much many get need use job rms test testing tested value values minimum maximum""".split())

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

# Topic weights are generic. Document ids are not hardcoded.
DOC_PRIOR = {
    "oil-lab": 0.8, "hse": 1.15, "hr": 1.15, "ops": 1.0, "rates": 1.05, "training": 0.9,
    "standards": 1.15, "breakers": 1.1, "oil": 1.1, "doble": 1.0, "cable": 1.1, "relays": 1.05,
    "safety": 1.05, "pd": 1.0, "examples": 0.8, "brochures": 0.7, "study": 0.7,
}
PAGE_DEMOTE = {"toc": 0.35, "index": 0.35}
DOC_DEMOTE = {}
DOC_NAMED = {}
REF_W = 0.8
REF_FAMILY = [
    ("nec-", r"\bnec\b|national electrical code|\barticle \d{3}\b"),
    ("ieee-", r"\bieee\b|\bc57|\b450\b|\b1188\b|\b81\b"),
]
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
RRF_BM, RRF_VEC, VEC_W = 30, 60, 0.8
_PF = None
_MODEL = None


def db_path():
    return os.path.join(jetpack_home(), "index", "jetpack.db")


def connect():
    path = db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"No jetpack index at {path}. Run tools/ingest.py first.")
    return sqlite3.connect(path)


def demote(doc, page, flags, q=""):
    global _PF
    flags = flags or ""
    if _PF is None:
        try:
            with connect() as db:
                _PF = {(d, p): f for d, p, f in db.execute("select doc_id, page, flag from page_flags")}
        except Exception:
            _PF = {}
    named = doc in DOC_NAMED and re.search(DOC_NAMED[doc], q.lower())
    factor = PAGE_DEMOTE.get(_PF.get((doc, page)), 1.0) * (1.0 if named else DOC_DEMOTE.get(doc, 1.0))
    if "superseded" in flags and "rates" not in flags and doc not in DOC_DEMOTE:
        factor *= 0.85
    for prefix, pattern in REF_FAMILY:
        if doc.startswith(prefix) and not re.search(pattern, q.lower()):
            factor *= REF_W
    return factor


def norm(q):
    q = q.lower()
    for pattern, repl in PHRASES:
        q = re.sub(pattern, repl, q)
    return q


def groups(q):
    """One OR-group per meaningful query word (word + synonyms + split units like 15kv -> 15)."""
    found = []
    q = norm(q)
    for token in re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", q):
        if token in STOP or (len(token) < 2 and not token.isdigit()):
            continue
        group = [token] + SYN.get(token, [])
        unit = re.match(r"^(\d+)(kv|v|a|kva|hz)$", token)
        if unit:
            group.append(unit.group(1))
        if token.endswith("s") and len(token) > 3:
            group.append(token[:-1])
        found.append(list(dict.fromkeys(group)))
    return found


def terms(q):
    return list(dict.fromkeys(token for group in groups(q) for token in group))


def _phrase(token):
    return '"%s"' % token.replace('"', "")


def fts_or(gs):
    return " OR ".join(_phrase(token) for group in gs for token in group)


def fts_and(gs):
    return " AND ".join("(" + " OR ".join(_phrase(token) for token in group) + ")" for group in gs)


def _run(db, match, topic, examples):
    sql = """select c.id, c.doc_id, c.page, c.text, d.title, d.topic, d.flags, d.page_label, bm25(chunks_fts, 1.0, 0.6) as score
             from chunks_fts join chunks c on c.id = chunks_fts.rowid join docs d on d.doc_id = c.doc_id
             where chunks_fts match ? """
    args = [match]
    if topic:
        sql += " and d.topic = ?"
        args.append(topic)
    if not examples:
        sql += " and d.topic not in ('examples','brochures')"
    sql += " order by score limit 300"
    return db.execute(sql, args).fetchall()


def search(q, k=5, topic=None, examples=True):
    global _PF
    _PF = None
    gs = groups(q)
    if not gs:
        return []
    intent = bool(INTENT & set(re.findall(r"[a-z]+", q.lower())))
    rows, seen_ids = [], set()
    tries = [fts_and(gs)]
    if len(gs) > 2:
        tries += [fts_and(gs[:i] + gs[i + 1:]) for i in range(len(gs))]
    tries.append(fts_or(gs))
    with connect() as db:
        for level, match in enumerate(tries):
            level_weight = 1.0 if level == 0 else (0.8 if level < len(tries) - 1 else 0.55)
            for row in _run(db, match, topic, examples):
                if row[0] in seen_ids:
                    continue
                seen_ids.add(row[0])
                rows.append((row, level_weight))
            if len({(row[0][1], row[0][2]) for row in rows}) >= max(k * 3, 15):
                break
        ranked, page_cache = [], {}
        for (cid, doc, page, text, title, top, flags, label, score), level_weight in rows:
            if (doc, page) not in page_cache:
                page_cache[(doc, page)] = " ".join(
                    body for (body,) in db.execute("select text from chunks where doc_id=? and page=?", (doc, page))
                ).lower()
            low = page_cache[(doc, page)]
            cover = sum(1 for group in gs if any(token in low for token in group)) / len(gs)
            if cover == 1.0 and level_weight < 1.0:
                level_weight = 1.0
            weight = (-score) * (0.4 + cover) * level_weight * DOC_PRIOR.get(top, 1.0)
            if intent and re.search(r"\btable\b", low):
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
            "title": title,
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


def vector_hits(q, n=40, topic=None, examples=True):
    """Meaning-based hits from local embeddings. Empty when vectors or libraries are missing."""
    home = jetpack_home()
    vec_path = os.path.join(home, "index", "vectors.npy")
    if not os.path.exists(vec_path):
        return []
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except Exception:
        return []
    os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(home, "work", "models"))
    vecs = np.load(vec_path, mmap_mode="r")
    ids = np.load(os.path.join(home, "index", "vector_ids.npy"))
    global _MODEL
    if _MODEL is None:
        _MODEL = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=os.environ["FASTEMBED_CACHE_PATH"])
    query = np.array(list(_MODEL.query_embed([vec_query(q)]))[0], dtype=np.float32)
    norm = np.linalg.norm(query)
    if norm:
        query /= norm
    sims = vecs @ query
    order = np.argsort(-sims)[: n * 4]
    out = []
    with connect() as db:
        for index in order:
            row = db.execute(
                "select c.id,c.doc_id,c.page,c.text,d.title,d.topic,d.flags,d.page_label "
                "from chunks c join docs d on d.doc_id=c.doc_id where c.id=?",
                (int(ids[index]),),
            ).fetchone()
            if not row:
                continue
            if topic and row[5] != topic:
                continue
            if not examples and row[5] in ("examples", "brochures"):
                continue
            out.append((float(sims[index]),) + tuple(row))
            if len(out) >= n:
                break
    return out


def hybrid(q, k=5, topic=None, examples=True):
    """Reciprocal-rank fusion of BM25 (exact words) and embeddings (meaning)."""
    bm = search(q, 30, topic, examples)
    vecs = vector_hits(q, 40, topic, examples)
    if not vecs:
        return search(q, k, topic, examples), "bm25"
    query_terms = terms(q)
    score, info = {}, {}
    for i, hit in enumerate(bm):
        key = (hit["doc_id"], hit["page"])
        score[key] = score.get(key, 0) + 1.0 / (RRF_BM + i)
        info[key] = hit
    seen = set()
    for _sim, cid, doc, page, text, title, top, flags, label in vecs:
        key = (doc, page)
        if key in seen:
            continue
        seen.add(key)
        score[key] = score.get(key, 0) + VEC_W / (RRF_VEC + len(seen) - 1)
        if key not in info:
            info[key] = {
                "doc_id": doc, "title": title, "page": page, "page_label": label,
                "flags": flags, "passage": passage(text, query_terms),
            }
    with connect() as db:
        for key, hit in info.items():
            row = db.execute("select topic from docs where doc_id=?", (key[0],)).fetchone()
            top = row[0] if row else ""
            score[key] *= DOC_PRIOR.get(top, 1.0) * demote(key[0], key[1], hit.get("flags") or "", q)
    ranked = sorted(score, key=lambda key: -score[key])[:k]
    out = []
    for i, key in enumerate(ranked, 1):
        hit = dict(info[key])
        hit["rank"] = i
        hit["score"] = round(score[key] * 1000, 2)
        out.append(hit)
    return out, "hybrid"


def passage(text, query_terms, width=700):
    folded_source = re.sub(r"[ \t]{2,}", "  ", text)
    low = folded_source.lower()
    best, at = -1, 0
    for i in range(0, max(1, len(folded_source) - width + 1), 60):
        window = low[i:i + width]
        sc = sum(window.count(token) for token in query_terms)
        if sc > best:
            best, at = sc, i
    return ("…" if at else "") + folded_source[at:at + width].strip() + ("…" if at + width < len(folded_source) else "")


def annotate(hits):
    for hit in hits:
        flags = hit.get("flags") or ""
        if "superseded" in flags or "edition" in flags:
            hit["note"] = EDN
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search a local jetpack index.")
    parser.add_argument("q", nargs="*")
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--topic")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-examples", action="store_true")
    parser.add_argument("--mode", choices=["auto", "bm25", "hybrid"], default="auto")
    parser.add_argument("--open", nargs=2, metavar=("DOC_ID", "PAGE"))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.list:
            with connect() as db:
                for row in db.execute("select doc_id,topic,pages,flags,title from docs order by topic,doc_id"):
                    print(f"{row[0]:42s} {row[1]:10s} {row[2]:4d}p  {row[3]:16s} {row[4]}")
            return 0
        if args.open:
            try:
                doc_id = safe_doc_id(args.open[0])
                page = int(args.open[1])
            except ValueError as exc:
                print(exc)
                return 2
            path = os.path.join(jetpack_home(), "text", doc_id, f"p{page:04d}.txt")
            if not os.path.isfile(path):
                print(f"No stored page at text/{doc_id}/p{page:04d}.txt")
                return 1
            with open(path, encoding="utf-8") as handle:
                sys.stdout.write(handle.read())
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
            tag = f" [{hit['flags']}]" if hit.get("flags") else ""
            label = "p." if hit.get("page_label") == "page" else str(hit.get("page_label") or "p.") + " "
            warn = ("    NOTE: " + hit["note"] + "\n") if hit.get("note") else ""
            print(f"[{hit['rank']}] {hit['title']} — {label}{hit['page']}{tag}  (score {hit['score']})\n{warn}    {hit['passage']}\n")
        return 0
    except FileNotFoundError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
