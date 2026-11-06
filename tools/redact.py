#!/usr/bin/env python3
"""Replace contact details in text.

By default this rewrites email addresses, URLs and bare domains, phone
numbers, and street-like addresses. Text is Unicode-normalized first, so
non-breaking spaces, full-width characters, and dash variants are caught.
Extra literal phrases can be loaded from a JSON file that stays on your
machine (see redact_terms.example.json). Matches in that file are literal
text, not regular expressions. A space in a phrase also matches a line
break or a run of spaces.

  python tools/redact.py notes.txt
  python tools/redact.py --terms redact_terms.local.json < notes.txt
"""
import argparse
import json
import os
import re
import sys
import unicodedata

from home import jetpack_home

TERMS_NAME = "redact_terms.local.json"
DEFAULT_REPLACEMENT = "[REDACTED]"

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SEP = rf"[ \t]*[-.{_DASHES}][ \t]*"
_BREAK = r"(?:[ \t]*\n[ \t]*)?"
_END = r"""(?:[^\s<>"'.,;:!?)\]}]|[.,;:!?)\]}](?=[^\s<>"'.,;:!?)\]}]))"""
_LOCAL = r"[\w+-]+(?:\.[\w+-]+)*"

# Each pattern starts only at the beginning of a run, so long runs of word
# characters are scanned once instead of once per character. The local part
# of an email may break across one line, never more, for the same reason.
EMAIL_RE = re.compile(
    rf"(?<![\w.+-]){_LOCAL}(?:\.[ \t]*\n[ \t]*{_LOCAL})?{_BREAK}@{_BREAK}"
    rf"\w[\w-]*(?:{_BREAK}\.{_BREAK}\w[\w-]*)*{_BREAK}\.{_BREAK}[A-Za-z]{{2,}}(?![\w-])"
)
URL_RE = re.compile(rf"(?i)(?<![\w.-])(?:https?://|ftp://|www\.)[^\s<>\"']*{_END}")
DOMAIN_RE = re.compile(
    r"(?<![\w.@/-])(?:[A-Za-z0-9][\w-]*\.)+"
    r"(?:(?i:com|net|org|edu|gov|mil|info|biz)|io|co|us|uk|ca|de|au)(?![\w-])"
    rf"(?:/[^\s<>\"']*{_END})?"
)
PHONE_RE = re.compile(
    r"(?<![\w.+])(?:"
    # (555) 010-0199, with any separator after the area code
    rf"(?:\+?1[ \t]*[-.{_DASHES}]?[ \t]*)?\(\d{{3}}\)[ \t]*[-.{_DASHES}]?[ \t]*\d{{3}}(?:{_SEP}|[ \t]+)\d{{4}}"
    # 555-010-0199, 555 010-0199, 555-010 0199; spaces alone like "240 480 4160" are not enough
    rf"|(?:\+?1(?:{_SEP}|[ \t]+))?\d{{3}}(?:{_SEP}\d{{3}}(?:{_SEP}|[ \t]+)|[ \t]+\d{{3}}{_SEP})\d{{4}}"
    # +1 555 010 0199 and other international forms with a plus sign
    rf"|\+\d{{1,3}}(?:(?:{_SEP}|[ \t]+)\(?\d{{2,4}}\)?){{2,5}}"
    r")(?!\d)(?![.,]\d)"
)
# Normalizing turns a non-breaking space into a plain one, and plain spaces
# alone are not enough for a phone. Groups of 3-3-4 joined by non-breaking
# spaces are phone formatting, not thousands separators, so match them first.
_HARD = "\u00a0\u2007\u202f"
HARD_SPACE_PHONE_RE = re.compile(
    rf"(?<![\w.+])(?:\+?1[{_HARD}])?(?:\(\d{{3}}\)|\d{{3}})[{_HARD}]\d{{3}}[{_HARD}]\d{{4}}(?!\d)"
)
# Invisible characters that can sit inside an email or a name and hide it from the patterns.
_INVISIBLE_RE = re.compile("[\u00ad\u200b\u2060\ufeff]")
LABELED_PHONE_RE = re.compile(
    r"((?i:\b(?:tel|telephone|phone|ph|fax|cell|mobile|mob|direct|office)\b)\.?[ \t]*[:#]?[ \t]*|\b[TFM]:[ \t]*)"
    r"(\d{3}[ \t]+\d{3}[ \t]+\d{4})(?!\d)"
)
_FULL = "Street|Avenue|Boulevard|Drive|Road|Highway|Parkway|Lane|Court|Place|Circle|Terrace|Way|Trail|Square|Plaza"
_ABBR = "St|Ave|Blvd|Dr|Rd|Hwy|Pkwy|Ln|Ct|Pl|Cir|Ter|Trl|Sq"
_W = r"(?:[ \t]+|[ \t]*\n[ \t]*)"
# A number followed by a unit or a count word is a rating, not a house number:
# "480 Volt Variable Frequency Drive" and "3 Phase Motor Drive" are left alone.
_NOT_A_STREET = (
    r"(?!(?:[kKmM]?(?:V|A|W|VA)|kVA|KVA|Hz|HZ|HP|Hp|AWG|Volts?|VOLTS?|Amps?|AMPS?|Amperes?|Watts?|WATTS?"
    r"|Ohms?|OHMS?|Phase|PHASE|Ph|PH|Poles?|POLES?|Wires?|WIRES?|Way|WAY|Pins?|PINS?|Steps?|STEPS?|Horsepower"
    r"|Tons?|Points?|Speeds?|Stages?|Ports?|Gang|Circuits?|CIRCUITS?|Cycles?|Seconds?|Minutes?|Hours?|Days?"
    r"|Years?|Percent|Times)(?![\w-]))"
)
# Case-sensitive on purpose: street names are capitalized, so "3 phase CT" is left alone.
ADDR_RE = re.compile(
    rf"(?<![\w.])\d{{1,6}}[A-Z]?{_W}{_NOT_A_STREET}(?:(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West)\.?{_W})?"
    rf"(?:(?:[A-Z][A-Za-z'.-]*|\d+(?:st|nd|rd|th)){_W}){{1,4}}"
    rf"(?:{_FULL}|{_FULL.upper()}|(?:{_ABBR})\.?)(?![\w-])"
    r"(?:[ \t]+(?:N|S|E|W|NE|NW|SE|SW)(?![\w-])\.?)?"
    r"(?:,?[ \t]*(?:Suite|Ste\.?|Unit|Apt\.?|#)[ \t]*[\w-]+)?"
)

_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SUPER_RUN = re.compile("[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")


class TermsError(ValueError):
    """The local terms file cannot be used. Redaction must not go ahead without it."""


def normalize(text):
    """NFKC, with superscripts kept readable: 10⁶ becomes 10^6, not 106."""
    text = _SUPER_RUN.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPT), text)
    return unicodedata.normalize("NFKC", text)


def discover_terms_path():
    """The terms file lives in the jetpack home. The current directory is not searched."""
    path = os.path.join(jetpack_home(), TERMS_NAME)
    return path if os.path.isfile(path) else None


def load_terms(path, warn=None):
    """Return (match, replacement) pairs. A missing path yields an empty list.

    Raises TermsError when the file exists but cannot be read as terms.
    Entries that are not usable are skipped and reported through warn.
    """
    warn = warn or (lambda message: print("warning: " + message, file=sys.stderr))
    if not path:
        return []
    if not os.path.isfile(path):
        raise TermsError(f"terms file not found: {os.path.basename(path)}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TermsError(f"could not read {os.path.basename(path)}: {exc}") from None
    if isinstance(data, dict):
        raw = data.get("terms", [])
    elif isinstance(data, list):
        raw = data
    else:
        raise TermsError(f"{os.path.basename(path)} must hold a list of terms or an object with a terms list")
    if not isinstance(raw, list):
        raise TermsError(f"terms in {os.path.basename(path)} must be a list")
    terms = []
    for number, item in enumerate(raw, 1):
        if isinstance(item, str):
            match, repl = item, DEFAULT_REPLACEMENT
        elif isinstance(item, dict):
            match = item.get("match", item.get("term", ""))
            repl = item.get("replacement", DEFAULT_REPLACEMENT)
        else:
            warn(f"term {number} is not text or an object; skipped")
            continue
        if not isinstance(match, str) or not isinstance(repl, str):
            warn(f"term {number} has a match or replacement that is not text; skipped")
            continue
        match = normalize(match).strip()
        repl = normalize(repl) or DEFAULT_REPLACEMENT
        if not match:
            continue
        if match.casefold() in repl.casefold():
            warn(f"term {number} has a replacement that contains the term; using {DEFAULT_REPLACEMENT}")
            repl = DEFAULT_REPLACEMENT
        terms.append((match, repl))
    terms.sort(key=lambda pair: len(pair[0]), reverse=True)
    return terms


def _term_pattern(match):
    body = r"\s+".join(re.escape(word) for word in match.split())
    if match[0].isalnum():
        body = r"(?<!\w)" + body
    if match[-1].isalnum():
        body += r"(?!\w)"
    return body


class Redactor:
    """Built-in patterns plus literal terms, compiled once."""

    def __init__(self, terms=()):
        self.terms = [(normalize(match).strip(), repl) for match, repl in terms if normalize(match).strip()]
        self.replacements = [repl for _, repl in self.terms]
        self.pattern = None
        if self.terms:
            self.pattern = re.compile("|".join(f"({_term_pattern(match)})" for match, _ in self.terms), re.I)

    def __call__(self, text):
        text = HARD_SPACE_PHONE_RE.sub("[PHONE]", text)
        text = _INVISIBLE_RE.sub("", normalize(text)).replace("\r\n", "\n").replace("\r", "\n")
        text = EMAIL_RE.sub("[EMAIL]", text)
        text = URL_RE.sub("[WEB]", text)
        text = DOMAIN_RE.sub(_domain, text)
        text = LABELED_PHONE_RE.sub(lambda m: m.group(1) + "[PHONE]", text)
        text = PHONE_RE.sub(_phone, text)
        text = ADDR_RE.sub("[ADDRESS]", text)
        if self.pattern is not None:
            # One pass, with a function, so a replacement is never read as a
            # template and never scanned again by a later term.
            text = self.pattern.sub(lambda m: self.replacements[m.lastindex - 1], text)
        return text


def _domain(match):
    """Section numbers such as 4.1.co are left alone: some label before the ending needs a letter."""
    found = match.group(0)
    labels = found.split("/", 1)[0].split(".")[:-1]
    if not any(ch.isalpha() for label in labels for ch in label):
        return found
    return "[WEB]"


def _phone(match):
    found = match.group(0)
    if found.startswith("+") and sum(ch.isdigit() for ch in found) < 9:
        return found
    return "[PHONE]"


def redact(text, terms=None):
    """Scrub text. terms=None loads redact_terms.local.json from the jetpack home when it exists."""
    if terms is None:
        terms = load_terms(discover_terms_path())
    return Redactor(terms)(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Redact emails, URLs, phone numbers, and street addresses.")
    parser.add_argument("file", nargs="?", help="Text file to read. Stdin is used when omitted.")
    parser.add_argument("--terms", help="JSON file of extra literal phrases. Default: redact_terms.local.json in the jetpack home.")
    args = parser.parse_args(argv)
    try:
        terms = load_terms(args.terms or discover_terms_path())
    except TermsError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = sys.stdin.read()
    sys.stdout.write(Redactor(terms)(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
