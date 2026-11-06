#!/usr/bin/env python3
"""Replace contact details in text.

By default this rewrites email addresses, URLs and bare domains, phone
numbers, and street-like addresses. Extra literal phrases can be loaded from
$JETPACK_HOME/redact_terms.local.json, a file that stays on your machine (see
redact_terms.example.json). The file holds {"terms": [...]} or a plain JSON
list. Matches are literal text, not regular expressions, and the words of a
phrase may be separated by any whitespace, including a line break.

Text is normalized before matching (NFKC, unicode spaces to spaces, unicode
dashes to "-", zero-width characters removed), and the normalized text is
what comes out.

  python tools/redact.py notes.txt
  python tools/redact.py --terms my_terms.json < notes.txt
"""
import argparse
import functools
import json
import os
import re
import sys
import unicodedata

from home import jetpack_home

TERMS_FILE = "redact_terms.local.json"

_SPACES = "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_TABLE = {ord(ch): " " for ch in _SPACES}
_TABLE.update({ord(ch): "-" for ch in _DASHES})
_TABLE.update({ord(ch): None for ch in _ZERO_WIDTH})
_TABLE.update({0x2028: "\n", 0x2029: "\n"})

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[ \t]*\n)?[ \t]*[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(
    r"(?i)(?<![\w@.])(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,;:!?)\]]"
    r"|(?<![\w@.])(?:[a-z0-9][a-z0-9-]{0,62}\.){1,8}(?:com|net|org|edu|gov|us|io|info|biz)\b(?:/[^\s<>\"']*)?"
)
_GAP = r"(?:[ \t]*\n)?[ \t]*"
_HARD = _GAP + r"[-.]" + _GAP
_SOFT = r"(?:" + _HARD + r"|[ \t]+(?:\n[ \t]*)?|\n[ \t]*)"
# A phone needs a plus, parentheses, or a dash/dot separator. Space-separated
# digit groups such as "240 480 4160" are left alone.
PHONE_RE = re.compile(
    r"(?<![\w.+])(?:"
    r"\+1?" + _SOFT + r"?(?:\(\d{3}\)|\d{3})" + _SOFT + r"?\d{3}" + _SOFT + r"?\d{4}"
    r"|(?:1" + _SOFT + r")?\(\d{3}\)" + _SOFT + r"?\d{3}" + _SOFT + r"\d{4}"
    r"|(?:1" + _HARD + r")?\d{3}(?:" + _HARD + r"\d{3}" + _SOFT + r"|" + _SOFT + r"\d{3}" + _HARD + r")\d{4}"
    r")(?!\w)"
)
ADDR_RE = re.compile(
    r"\b\d{1,6}[ \t]+(?:(?:N|S|E|W|NE|NW|SE|SW)\.?[ \t]+)?"
    r"(?:(?:[A-Z][a-z][A-Za-z'.-]*|\d{1,3}(?:st|nd|rd|th))[ \t]+){1,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Way|Dr|Drive|Rd|Road|Hwy|Highway|Pkwy|Parkway"
    r"|Ln|Lane|Ct|Court|Pl|Place|Ter|Terrace|Cir|Circle|Sq|Square|Trl|Trail)\b\.?"
    r"(?:,?[ \t]*(?:Suite|Ste\.?|Unit|#)[ \t]*[\w-]+)?"
)


class TermsError(ValueError):
    """A terms file that cannot be read or has the wrong shape."""


def normalize(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_TABLE)


def local_terms_path():
    return os.path.join(jetpack_home(), TERMS_FILE)


def parse_terms(data, where="terms"):
    """Return (match, replacement) pairs, longest match first."""
    if isinstance(data, dict) and isinstance(data.get("terms"), list):
        raw = data["terms"]
    elif isinstance(data, list):
        raw = data
    else:
        raise TermsError(f'{where}: expected {{"terms": [...]}} or a JSON list of strings')
    terms = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            match, repl = item, "[REDACTED]"
        elif isinstance(item, dict):
            match = item.get("match", item.get("term"))
            repl = item.get("replacement", "[REDACTED]")
            if not isinstance(match, str) or not isinstance(repl, str):
                raise TermsError(f"{where}: item {i} needs a string \"match\" and an optional string \"replacement\"")
        else:
            raise TermsError(f"{where}: item {i} must be a string or an object")
        match = normalize(match).strip()
        if match:
            terms.append((match, repl))
    terms.sort(key=lambda pair: len(pair[0]), reverse=True)
    return terms


def load_terms(path):
    """Read a terms file. Raises TermsError when it is missing, is not JSON, or has the wrong shape."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise TermsError(f"{path}: file not found") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TermsError(f"{path}: {exc}") from None
    return parse_terms(data, path)


def load_local_terms():
    """Load $JETPACK_HOME/redact_terms.local.json once for a run, and say on stderr what was used."""
    path = local_terms_path()
    if not os.path.isfile(path):
        print(f"redact: no {TERMS_FILE} in {os.path.dirname(path)}; using the built-in patterns only.", file=sys.stderr)
        return []
    terms = load_terms(path)
    print(f"redact: extra terms from {path}", file=sys.stderr)
    return terms


@functools.lru_cache(maxsize=4096)
def _term_pattern(match):
    body = r"\s+".join(re.escape(word) for word in match.split())
    if match[0].isalnum():
        body = r"\b" + body
    if match[-1].isalnum():
        body = body + r"\b"
    return re.compile(body, re.I)


def redact(text, terms=()):
    """Normalize and scrub text. terms are (match, replacement) pairs from load_terms or load_local_terms.

    Extra terms run first, so a name glued to a domain or phone number in a
    file name is still found.
    """
    text = normalize(text)
    for match, repl in terms or ():
        text = _term_pattern(match).sub(lambda _found, repl=repl: repl, text)
    if "@" in text:
        text = EMAIL_RE.sub("[EMAIL]", text)
    text = URL_RE.sub("[WEB]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = ADDR_RE.sub("[ADDRESS]", text)
    return text


def scrubber(terms):
    """A one-argument redact function with the terms already loaded."""
    return lambda text: redact(text, terms)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Redact emails, URLs, phone numbers, and street addresses.")
    parser.add_argument("file", nargs="?", help="Text file to read. Stdin is used when omitted.")
    parser.add_argument("--terms", help=f"JSON file of extra literal phrases. Replaces $JETPACK_HOME/{TERMS_FILE}.")
    args = parser.parse_args(argv)
    try:
        if args.terms:
            terms = load_terms(args.terms)
            print(f"redact: extra terms from {args.terms}", file=sys.stderr)
        else:
            terms = load_local_terms()
    except TermsError as exc:
        print(f"redact: {exc}", file=sys.stderr)
        return 2
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    sys.stdout.write(redact(text, terms))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
