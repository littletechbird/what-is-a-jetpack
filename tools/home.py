"""Resolve the jetpack root. JETPACK_HOME overrides the repository directory."""
import os
import re

DOC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def jetpack_home():
    env = os.environ.get("JETPACK_HOME")
    if env:
        return os.path.abspath(env)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def safe_doc_id(doc_id):
    """Reject ids that could escape the text/ directory."""
    if not DOC_ID_RE.fullmatch(doc_id or ""):
        raise ValueError("doc id must be letters, numbers, dot, underscore, or hyphen")
    return doc_id
