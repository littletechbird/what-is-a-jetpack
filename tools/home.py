"""Resolve the jetpack root. JETPACK_HOME overrides the repository directory."""
import os
import re

DOC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
# Windows refuses these names as files or folders, with or without an extension.
RESERVED = {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(10)} | {f"lpt{n}" for n in range(10)}


def jetpack_home():
    env = os.environ.get("JETPACK_HOME")
    if env:
        return os.path.abspath(env)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def safe_doc_id(doc_id):
    """Reject ids that could escape the text/ directory or that Windows cannot store."""
    if not DOC_ID_RE.fullmatch(doc_id or ""):
        raise ValueError("doc id must be letters, numbers, dot, underscore, or hyphen")
    if doc_id.endswith("."):
        raise ValueError("doc id must not end with a dot")
    if doc_id.split(".", 1)[0].lower() in RESERVED:
        raise ValueError(f"doc id {doc_id} is a reserved device name on Windows")
    return doc_id


def within(parent, path):
    """True when path is parent or inside it, after resolving links."""
    parent = os.path.realpath(parent)
    path = os.path.realpath(path)
    try:
        return os.path.commonpath([parent, path]) == parent
    except ValueError:
        return False
