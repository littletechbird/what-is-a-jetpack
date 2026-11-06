"""Shared helpers: the jetpack root, doc id checks, and small text utilities.

JETPACK_HOME overrides the repository directory.
"""
import hashlib
import os
import re
import shutil
import sys

DOC_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,120}")
RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
VECTOR_FILES = ("vectors.npy", "vector_ids.npy", "vector_keys.npy")

_GIT_CHECKED = {}
_GIT_WARNED = False


def jetpack_home():
    env = os.environ.get("JETPACK_HOME")
    if env:
        home = os.path.abspath(env)
    else:
        home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _warn_if_inside_git(home)
    return home


def _inside_git(path):
    current = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _warn_if_inside_git(home):
    global _GIT_WARNED
    if _GIT_WARNED:
        return
    if home not in _GIT_CHECKED:
        _GIT_CHECKED[home] = _inside_git(home)
    if _GIT_CHECKED[home]:
        _GIT_WARNED = True
        print(
            f"jetpack: the home folder {home} is inside a git worktree. library/, text/, index/, and work/ "
            "are gitignored here, but check git status before you commit, or set JETPACK_HOME to a folder "
            "outside the repository.",
            file=sys.stderr,
        )


def safe_doc_id(doc_id):
    """Reject ids that could escape the text/ directory or clash on Windows or case-insensitive disks."""
    doc_id = doc_id or ""
    if (
        not DOC_ID_RE.fullmatch(doc_id)
        or doc_id.endswith(".")
        or doc_id.split(".")[0] in RESERVED_NAMES
    ):
        raise ValueError(
            "doc id must be lowercase letters, numbers, dot, underscore, or hyphen, "
            "must not end in a dot, and must not be a Windows device name such as con or nul"
        )
    return doc_id


def printable(text):
    """Drop control characters except newline and tab, so stored or printed text cannot drive a terminal."""
    return CONTROL_RE.sub("", text or "")


def chunk_fingerprint(title, text):
    return hashlib.sha256(((title or "") + "\n" + (text or "")).encode("utf-8")).hexdigest()


def remove_path(path):
    """Remove a file, a symlink (without following it), or a directory tree."""
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def remove_vector_files(home=None):
    index = os.path.join(home or jetpack_home(), "index")
    for name in VECTOR_FILES:
        try:
            os.remove(os.path.join(index, name))
        except FileNotFoundError:
            pass
