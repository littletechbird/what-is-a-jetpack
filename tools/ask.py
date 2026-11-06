#!/usr/bin/env python3
"""Search a local jetpack index.

  python tools/ask.py "question" [-k 5] [--topic safety] [--json] [--no-examples]
  python tools/ask.py --open DOC_ID PAGE
  python tools/ask.py --list

Prints matching passages with document and page citations.
Keyword rank is SQLite FTS5 / BM25. When index/vectors.npz is present, that
rank is blended with local embeddings (reciprocal rank fusion). A vector is
used only while its chunk still has the text it was embedded from. When
keyword search finds nothing, the answer is a miss, with or without vectors.
Search does not call a paid search API.

Topic weights and document-prefix rules are local settings. They go in
ranking.local.json in the jetpack home; ranking.example.json shows the shape.
"""
import argparse
import bisect
import json
import math
import os
import re
import sys
import unicodedata
from contextlib import closing

from extract import printable
from home import jetpack_home, safe_doc_id, within
from ingest import EMBED_MODEL, IndexUnavailable, open_index, text_root, vector_path
from redact import normalize

STOP = set("""a an and are as at be by can do does for from how i in is it me my of on or should the to what when where which who why will with
you your we our this that there their they them than then into about over under per vs versus much many get need use""".split())
# Common in questions and weak on their own. Dropped only when other words are left.
SOFT = set("job rms test testing tested value values minimum maximum".split())
MAX_GROUPS = 12

SYN = {
    "ir": ["insulation", "resistance", "megohm"],
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
    "dlro": ["dlro", "contact resistance", "micro-ohm", "microhm", "micro ohm"],
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
INTENT = {"minimum", "maximum", "limit", "limits", "value", "values", "voltage", "acceptable", "acceptance", "table", "level", "levels"}
PHRASES = [
    (r"\bhi[- ]?pot\b", "hipot"),
    (r"\bhigh[- ]potential\b", "hipot"),
    (r"\block[- ]?out[ /-]*tag[- ]?out\b", "loto"),
    (r"\bassured (equipment )?grounding( conductor)?( program)?\b", "aegcp assured grounding"),
]
EDN = "Older edition: a newer edition may exist. Check the current edition before relying on this for compliance."
VEC_EXPAND = {
    "cord": "extension cord set flexible cord assured equipment grounding",
    "gfci": "ground fault circuit interrupter",
    "aegcp": "assured equipment grounding conductor program cord sets",
    "loto": "lockout tagout",
    "dlro": "contact resistance micro-ohm test",
    "hipot": "dielectric withstand high-potential test",
    "dga": "dissolved gas analysis",
    "tdcg": "total dissolved combustible gas",
    "dosh": "Cal/OSHA permit",
    "scan": "infrared thermographic survey",
}
RRF_BM, RRF_VEC, VEC_W = 30, 60, 0.8
RANKING_NAME = "ranking.local.json"
# Neutral on purpose. Library-specific weights belong in ranking.local.json.
DEFAULT_RANKING = {
    "topic_weights": {},
    "prefix_rules": [],
    "hidden_topics": ["examples"],
    "page_weights": {"toc": 0.35, "index": 0.35},
    "superseded_weight": 0.85,
    "synonyms": {},
}
TOKEN_RE = re.compile(r"[^\W_]+(?:\.[0-9]+)*")
_PF = None
_MODEL = None
NOTES = []


def _note(message):
    if message not in NOTES:
        NOTES.append(message)


def _weight(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def load_ranking():
    """Neutral defaults, overridden by ranking.local.json in the jetpack home. A bad file or entry is noted and ignored."""
    ranking = {
        "topic_weights": {},
        "prefix_rules": [],
        "hidden_topics": set(DEFAULT_RANKING["hidden_topics"]),
        "page_weights": dict(DEFAULT_RANKING["page_weights"]),
        "superseded_weight": DEFAULT_RANKING["superseded_weight"],
        "synonyms": {},
    }
    path = os.path.join(jetpack_home(), RANKING_NAME)
    if not os.path.isfile(path):
        return ranking
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError) as exc:
        _note(f"ignored {RANKING_NAME}: {exc}")
        return ranking
    for key in ("topic_weights", "page_weights"):
        table = data.get(key, {})
        if isinstance(table, dict):
            ranking[key].update({str(name): float(value) for name, value in table.items() if _weight(value)})
    if _weight(data.get("superseded_weight")):
        ranking["superseded_weight"] = float(data["superseded_weight"])
    hidden = data.get("hidden_topics")
    if isinstance(hidden, list):
        ranking["hidden_topics"] = {str(name) for name in hidden}
    synonyms = data.get("synonyms", {})
    if isinstance(synonyms, dict):
        for word, alternatives in synonyms.items():
            if isinstance(alternatives, list):
                ranking["synonyms"][norm(str(word))] = [norm(str(item)) for item in alternatives]
    rules = data.get("prefix_rules", [])
    for number, item in enumerate(rules if isinstance(rules, list) else [], 1):
        if not isinstance(item, dict) or not isinstance(item.get("prefix"), str) or not _weight(item.get("weight")):
            _note(f"ignored prefix rule {number} in {RANKING_NAME}: it needs a prefix and a weight")
            continue
        try:
            unless = re.compile(item["unless"], re.I) if item.get("unless") else None
        except (re.error, TypeError) as exc:
            _note(f"ignored prefix rule {number} in {RANKING_NAME}: {exc}")
            continue
        ranking["prefix_rules"].append((item["prefix"], float(item["weight"]), unless))
    return ranking


def connect():
    return open_index()


def _page_flags(db):
    return {(d, p): f for d, p, f in db.execute("select doc_id, page, flag from page_flags")}


def demote(doc, page, flags, q="", ranking=None):
    global _PF
    ranking = ranking or load_ranking()
    flags = flags or ""
    if _PF is None:
        try:
            with closing(connect()) as db:
                _PF = _page_flags(db)
        except Exception:
            _PF = {}
    factor = ranking["page_weights"].get(_PF.get((doc, page)), 1.0)
    if "superseded" in flags:
        factor *= ranking["superseded_weight"]
    low = q.lower()
    for prefix, weight, unless in ranking["prefix_rules"]:
        if doc.startswith(prefix) and not (unless and unless.search(low)):
            factor *= weight
    return factor


def norm(q):
    q = normalize(q).lower()
    for pattern, repl in PHRASES:
        q = re.sub(pattern, repl, q)
    return q


def fold(text):
    """Case and accents folded the way the FTS tokenizer folds them, for word checks in Python."""
    text = unicodedata.normalize("NFKD", normalize(text).casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def stem(word):
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def groups(q, ranking=None):
    """One OR-group per meaningful query word (word + synonyms + split units like 15kv -> 15)."""
    syn = dict(SYN)
    if ranking:
        syn.update(ranking["synonyms"])
    found, soft, seen = [], [], set()
    for token in TOKEN_RE.findall(norm(q)):
        if token in seen or token in STOP:
            continue
        if len(token) < 2 and token.isascii() and not token.isdigit():
            continue
        seen.add(token)
        group = [token] + syn.get(token, [])
        unit = re.match(r"^(\d+)(kv|v|a|kva|hz)$", token)
        if unit:
            group.append(unit.group(1))
        if token.endswith("s") and len(token) > 3:
            group.append(token[:-1])
        (soft if token in SOFT else found).append(list(dict.fromkeys(group)))
    return (found or soft)[:MAX_GROUPS]


def terms(q, ranking=None):
    return list(dict.fromkeys(token for group in groups(q, ranking) for token in group))


def _phrase(token):
    return '"%s"' % token.replace('"', "")


def fts_or(gs):
    return " OR ".join(_phrase(token) for group in gs for token in group)


def fts_and(gs):
    return " AND ".join("(" + " OR ".join(_phrase(token) for token in group) + ")" for group in gs)


def group_matcher(group):
    """True when a page holds one of the group's words as a whole word, or one of its phrases."""
    singles, phrases = set(), []
    for token in group:
        words = TOKEN_RE.findall(fold(token))
        if len(words) == 1:
            singles.add(stem(words[0]))
        elif words:
            phrases.append(re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + r"(?!\w)"))

    def matches(folded, stems):
        return bool(singles & stems) or any(pattern.search(folded) for pattern in phrases)

    return matches


def page_words(text):
    folded = fold(text)
    return folded, {stem(word) for word in TOKEN_RE.findall(folded)}


def _run(db, match, topic, hidden):
    sql = """select c.id, c.doc_id, c.page, c.text, d.title, d.topic, d.flags, d.page_label, bm25(chunks_fts, 1.0, 0.6) as score
             from chunks_fts join chunks c on c.id = chunks_fts.rowid join docs d on d.doc_id = c.doc_id
             where chunks_fts match ? """
    args = [match]
    if topic:
        sql += " and d.topic = ?"
        args.append(topic)
    if hidden:
        sql += " and d.topic not in (%s)" % ",".join("?" * len(hidden))
        args.extend(hidden)
    sql += " order by score limit 300"
    return db.execute(sql, args).fetchall()


def search(q, k=5, topic=None, examples=True):
    global _PF
    if k <= 0:
        return []
    ranking = load_ranking()
    gs = groups(q, ranking)
    if not gs:
        return []
    intent = bool(INTENT & set(TOKEN_RE.findall(norm(q))))
    matchers = [group_matcher(group) for group in gs]
    hidden = [] if examples else sorted(ranking["hidden_topics"])
    rows, seen_ids = [], set()
    tries = [fts_and(gs)]
    if len(gs) > 2:
        tries += [fts_and(gs[:i] + gs[i + 1:]) for i in range(len(gs))]
    tries.append(fts_or(gs))
    with closing(connect()) as db:
        _PF = _page_flags(db)
        for level, match in enumerate(tries):
            level_weight = 1.0 if level == 0 else (0.8 if level < len(tries) - 1 else 0.55)
            for row in _run(db, match, topic, hidden):
                if row[0] in seen_ids:
                    continue
                seen_ids.add(row[0])
                rows.append((row, level_weight))
            if len({(row[0][1], row[0][2]) for row in rows}) >= max(k * 3, 15):
                break
        ranked, page_cache = [], {}
        for (cid, doc, page, text, title, top, flags, label, score), level_weight in rows:
            if (doc, page) not in page_cache:
                page_cache[(doc, page)] = page_words(" ".join(
                    body for (body,) in db.execute("select text from chunks where doc_id=? and page=?", (doc, page))
                ))
            folded, stems = page_cache[(doc, page)]
            cover = sum(1 for matches in matchers if matches(folded, stems)) / len(gs)
            if cover == 1.0 and level_weight < 1.0:
                level_weight = 1.0
            weight = (-score) * (0.4 + cover) * level_weight * ranking["topic_weights"].get(top, 1.0)
            if intent and re.search(r"\btable\b", folded):
                weight *= 1.25
            weight *= demote(doc, page, flags, q, ranking)
            ranked.append((weight, cid, doc, page, text, title, flags, label))
    ranked.sort(reverse=True)
    query_terms = terms(q, ranking)
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


def _load_model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding

        cache = os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(jetpack_home(), "work", "models"))
        _MODEL = TextEmbedding(EMBED_MODEL, cache_dir=cache)
    return _MODEL


def load_vectors():
    """(vectors, ids, fingerprints) from index/vectors.npz. None when missing, unreadable, or from another model."""
    path = vector_path()
    if not os.path.exists(path):
        return None
    try:
        import numpy as np

        with np.load(path, allow_pickle=False) as data:
            vecs, ids, fps, model = data["vectors"], data["ids"], data["fps"], str(data["model"])
    except Exception:
        _note("ignored index/vectors.npz because it could not be read; run tools/embed.py")
        return None
    if model != EMBED_MODEL or vecs.ndim != 2 or not len(vecs) == len(ids) == len(fps):
        _note("ignored index/vectors.npz because it does not match this version; run tools/embed.py")
        return None
    return vecs, ids, fps


def vector_hits(q, n=40, topic=None, examples=True, ranking=None):
    """Meaning-based hits from local embeddings, best first. Empty when vectors or libraries are missing.

    A vector counts only when its chunk id still exists with the same
    fingerprint. Topic filters apply before the top hits are taken.
    """
    ranking = ranking or load_ranking()
    data = load_vectors()
    if data is None:
        return []
    try:
        import numpy as np

        model = _load_model()
    except Exception:
        return []
    vecs, ids, fps = data
    hidden = set() if examples else ranking["hidden_topics"]
    out = []
    with closing(connect()) as db:
        current = {
            cid: (fp, top)
            for cid, fp, top in db.execute("select c.id, c.fp, d.topic from chunks c join docs d on d.doc_id = c.doc_id")
        }
        valid = np.zeros(len(ids), dtype=bool)
        fresh = 0
        for i, (cid, fp) in enumerate(zip(ids.tolist(), fps.tolist())):
            row = current.get(cid)
            if row is None or row[0] != fp:
                continue
            fresh += 1
            if (topic and row[1] != topic) or row[1] in hidden:
                continue
            valid[i] = True
        if fresh < len(current):
            _note(f"{len(current) - fresh} chunks have no current vector; run tools/embed.py to add them")
        if not valid.any():
            return []
        query = np.asarray(list(model.query_embed([vec_query(q)]))[0], dtype=np.float32)
        length = np.linalg.norm(query)
        if length:
            query /= length
        sims = np.where(valid, vecs @ query, -np.inf)
        for index in np.argsort(-sims)[: n * 4]:
            if not valid[index]:
                break
            row = db.execute(
                "select c.id,c.doc_id,c.page,c.text,d.title,d.topic,d.flags,d.page_label "
                "from chunks c join docs d on d.doc_id=c.doc_id where c.id=?",
                (int(ids[index]),),
            ).fetchone()
            if not row:
                continue
            weight = max(float(sims[index]), 0.0) * ranking["topic_weights"].get(row[5], 1.0)
            weight *= demote(row[1], row[2], row[6], q, ranking)
            out.append((weight,) + tuple(row))
    out.sort(key=lambda hit: -hit[0])
    return out[:n]


def hybrid(q, k=5, topic=None, examples=True):
    """Reciprocal-rank fusion of BM25 (exact words) and embeddings (meaning). A keyword miss stays a miss."""
    if k <= 0:
        return [], "bm25"
    if not os.path.exists(vector_path()):
        return search(q, k, topic, examples), "bm25"
    bm = search(q, max(30, k), topic, examples)
    if not bm:
        return [], "hybrid"
    ranking = load_ranking()
    vecs = vector_hits(q, 40, topic, examples, ranking)
    if not vecs:
        return search(q, k, topic, examples), "bm25"
    query_terms = terms(q, ranking)
    score, info = {}, {}
    for i, hit in enumerate(bm):
        key = (hit["doc_id"], hit["page"])
        score[key] = score.get(key, 0) + 1.0 / (RRF_BM + i)
        info[key] = hit
    seen = set()
    for _, _, doc, page, text, title, _, flags, label in vecs:
        key = (doc, page)
        if key in seen:
            continue
        seen.add(key)
        score[key] = score.get(key, 0) + VEC_W / (RRF_VEC + len(seen) - 1)
        info.setdefault(key, {
            "doc_id": doc, "title": title, "page": page, "page_label": label,
            "flags": flags, "passage": passage(text, query_terms),
        })
    out = []
    for i, key in enumerate(sorted(score, key=lambda key: -score[key])[:k], 1):
        hit = dict(info[key])
        hit["rank"] = i
        hit["score"] = round(score[key] * 1000, 2)
        out.append(hit)
    return out, "hybrid"


def _terms_pattern(query_terms):
    words = sorted({term for term in query_terms if term}, key=len, reverse=True)
    if not words:
        return None
    body = "|".join(r"\s+".join(map(re.escape, term.split())) for term in words)
    return re.compile(r"(?<!\w)(?:" + body + r")(?!\w)", re.I)


def passage(text, query_terms, width=700):
    folded_source = re.sub(r"[ \t]{2,}", "  ", text)
    pattern = _terms_pattern(query_terms)
    starts = [m.start() for m in pattern.finditer(folded_source)] if pattern else []
    best, at = -1, 0
    for i in range(0, max(1, len(folded_source) - width + 1), 60):
        count = bisect.bisect_left(starts, i + width) - bisect.bisect_left(starts, i)
        if count > best:
            best, at = count, i
    return ("…" if at else "") + folded_source[at:at + width].strip() + ("…" if at + width < len(folded_source) else "")


def annotate(hits):
    for hit in hits:
        flags = hit.get("flags") or ""
        if "superseded" in flags or "edition" in flags:
            hit["note"] = EDN
    return hits


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def open_page(doc_id, page):
    """Print one stored page. Links inside text/ are not followed."""
    try:
        doc_id = safe_doc_id(doc_id)
        page = int(page)
        if page < 1:
            raise ValueError("page must be 1 or more")
    except ValueError as exc:
        print(exc)
        return 2
    folder = os.path.join(text_root(), doc_id)
    path = os.path.join(folder, f"p{page:04d}.txt")
    if os.path.islink(folder) or os.path.islink(path) or not os.path.isfile(path) or not within(text_root(), path):
        print(f"No stored page at text/{doc_id}/p{page:04d}.txt")
        return 1
    with open(path, encoding="utf-8", errors="replace") as handle:
        sys.stdout.write(printable(handle.read()))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search a local jetpack index.")
    parser.add_argument("q", nargs="*")
    parser.add_argument("-k", type=_positive, default=5, help="Number of passages to show (1 or more).")
    parser.add_argument("--topic")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-examples", action="store_true", help="Leave out topics listed as hidden_topics (default: examples).")
    parser.add_argument("--mode", choices=["auto", "bm25", "hybrid"], default="auto")
    parser.add_argument("--open", nargs=2, metavar=("DOC_ID", "PAGE"))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    del NOTES[:]
    try:
        if args.open:
            return open_page(*args.open)
        if args.list:
            with closing(connect()) as db:
                for doc, topic, pages, flags, title in db.execute(
                    "select doc_id,topic,pages,flags,title from docs order by topic,doc_id"
                ):
                    print(f"{doc:42s} {printable(topic):10s} {pages or 0:4d}p  {printable(flags):16s} {printable(title)}")
            return 0
        question = " ".join(args.q)
        examples = not args.no_examples
        if args.mode == "bm25":
            hits, mode = search(question, args.k, args.topic, examples), "bm25"
        else:
            hits, mode = hybrid(question, args.k, args.topic, examples)
        annotate(hits)
        for note in NOTES:
            print("note: " + note, file=sys.stderr)
        if args.json:
            print(json.dumps({"question": question, "mode": mode, "hits": hits}, indent=1))
            return 0
        if not hits:
            print("Not in the jetpack (no matching passages).")
            return 0
        for hit in hits:
            tag = f" [{printable(hit['flags'])}]" if hit.get("flags") else ""
            label = "p." if hit.get("page_label") == "page" else str(hit.get("page_label") or "p.") + " "
            warn = ("    NOTE: " + hit["note"] + "\n") if hit.get("note") else ""
            print(
                f"[{hit['rank']}] {printable(hit['title'])} — {label}{hit['page']}{tag}  (score {hit['score']})\n"
                f"{warn}    {printable(hit['passage'])}\n"
            )
        return 0
    except IndexUnavailable as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
