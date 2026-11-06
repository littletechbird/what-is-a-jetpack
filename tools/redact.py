#!/usr/bin/env python3
"""Replace contact details in text.

By default this rewrites email addresses, URLs, phone numbers, and
street-like addresses. Extra literal phrases can be loaded from a JSON
file that stays on your machine (see redact_terms.example.json). Matches
in that file are literal text, not regular expressions.

  python tools/redact.py notes.txt
  python tools/redact.py --terms redact_terms.local.json < notes.txt
"""
import argparse
import json
import os
import re
import sys

from home import jetpack_home

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+\.(?:com|net|org|edu|gov|us)\b"
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?(?:\(\d{3}\)|\d{3})[-. ]\d{3}[-. ]\d{4}(?!\d)")
ADDR_RE = re.compile(
    r"(?i)\b\d{1,6}\s+(?:[NSEW]{1,2}\s+)?(?:[A-Za-z0-9]+\s+){1,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Way|Dr|Drive|Rd|Road|Hwy|Highway|Pkwy|Parkway|Ln|Lane|Ct|Court)\b\.?"
    r"(?:,?\s*(?:Suite|Ste\.?|#)\s*\w+)?"
)


def discover_terms_path():
    for path in (
        os.path.join(jetpack_home(), "redact_terms.local.json"),
        os.path.join(os.getcwd(), "redact_terms.local.json"),
    ):
        if os.path.isfile(path):
            return path
    return None


def load_terms(path):
    """Return (match, replacement) pairs. A missing file yields an empty list."""
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    raw = data.get("terms", data if isinstance(data, list) else [])
    terms = []
    for item in raw:
        if isinstance(item, str):
            match, repl = item, "[REDACTED]"
        elif isinstance(item, dict):
            match = item.get("match") or item.get("term") or ""
            repl = item.get("replacement") or "[REDACTED]"
        else:
            continue
        match = str(match).strip()
        if match:
            terms.append((match, str(repl)))
    terms.sort(key=lambda pair: len(pair[0]), reverse=True)
    return terms


def _term_pattern(match):
    escaped = re.escape(match)
    if match[0].isalnum():
        escaped = r"\b" + escaped
    if match[-1].isalnum():
        escaped = escaped + r"\b"
    return re.compile(escaped, re.I)


def redact(text, terms=None):
    """Scrub text. terms=None loads redact_terms.local.json when that file exists."""
    if terms is None:
        path = discover_terms_path()
        terms = load_terms(path) if path else []
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = URL_RE.sub("[WEB]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = ADDR_RE.sub("[ADDRESS]", text)
    for match, repl in terms:
        text = _term_pattern(match).sub(repl, text)
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Redact emails, URLs, phone numbers, and street addresses.")
    parser.add_argument("file", nargs="?", help="Text file to read. Stdin is used when omitted.")
    parser.add_argument("--terms", help="JSON file of extra literal phrases. Skips the automatic local file.")
    args = parser.parse_args(argv)
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    terms = load_terms(args.terms) if args.terms else None
    sys.stdout.write(redact(text, terms))


if __name__ == "__main__":
    main()
