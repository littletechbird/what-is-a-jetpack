"""Redaction patterns, terms files, and the cases PDF extraction produces. Text here is synthetic."""
import json
import os
import subprocess
import sys
import time
import unittest

from helpers import ROOT, HomeCase, read, write

import redact  # noqa: E402
from redact import Redactor, TermsError, load_terms  # noqa: E402


def scrub(text, terms=()):
    return Redactor(list(terms))(text)


class PatternTests(unittest.TestCase):
    def test_builtin_patterns(self):
        raw = (
            "Write person@example.com or see https://example.com/docs and www.example.com. "
            "Call 555-013-0199 or (555) 010-0199. The shop is at 123 Main Street today."
        )
        out = redact.redact(raw, terms=[])
        for secret in ("person@example.com", "example.com", "555-013-0199", "555) 010-0199", "123 Main Street"):
            self.assertNotIn(secret, out)
        for label in ("[EMAIL]", "[WEB]", "[PHONE]", "[ADDRESS]"):
            self.assertIn(label, out)
        self.assertIn("today", out)

    def test_bare_domains(self):
        for raw in ("see acmepower.com for parts", "docs at portal.acme-power.net/manuals/v2.", "Mail ACMEPOWER.COM"):
            out = scrub(raw)
            self.assertNotIn("acme", out.lower(), raw)
            self.assertIn("[WEB]", out, raw)
        self.assertEqual(scrub("Figure 3.2 and table 1.4 apply; see section 4.1.co"), "Figure 3.2 and table 1.4 apply; see section 4.1.co")
        self.assertEqual(scrub("file report.pdf and notes.txt"), "file report.pdf and notes.txt")

    def test_urls_keep_trailing_punctuation(self):
        self.assertEqual(scrub("Visit https://example.com/a/b."), "Visit [WEB].")
        self.assertEqual(scrub("(see www.example.org/x)"), "(see [WEB])")
        self.assertEqual(scrub("ftp://files.example.net/pub, then"), "[WEB], then")

    def test_phone_forms_from_pdf_text(self):
        forms = [
            "555\u00a0013\u00a00199 ext",
            "555\u2013013\u20130199",
            "555\u2014013\u20140199",
            "555 . 013 . 0199",
            "(555)\u00a0013-0199",
            "+1 555 013 0199",
            "+1-555-013-0199",
            "+44 20 7946 0958",
            "Tel: 555 013 0199",
            "fax 555 013 0199",
            "T: 555 013 0199",
            "\uff15\uff15\uff15-\uff10\uff11\uff13-\uff10\uff11\uff19\uff19",
        ]
        for raw in forms:
            out = scrub(raw)
            self.assertIn("[PHONE]", out, raw)
            self.assertNotIn("0199", out, raw)
            self.assertNotIn("0958", out, raw)

    def test_emails_split_across_a_line(self):
        for raw in (
            "contact jane.doe@\nexample.com today",
            "contact jane.doe\n@example.com today",
            "contact jane.\ndoe@example.com today",
            "contact jane.doe@example.\ncom today",
            "contact JANE.DOE@EXAMPLE.COM today",
            "contact jane+filter@mail.example.co.uk today",
        ):
            out = scrub(raw)
            self.assertNotIn("doe", out.lower(), raw)
            self.assertNotIn("filter", out, raw)
            self.assertIn("[EMAIL]", out, raw)
            self.assertTrue(out.endswith("today"), raw)

    def test_addresses(self):
        for raw in (
            "123 Main Street",
            "4500 N Lakeshore Drive Suite 210",
            "77 West 3rd Ave.",
            "9 Old Mill Rd, Unit 4",
            "1200 PARK AVENUE",
            "12\nHarbor Way",
        ):
            out = scrub(raw)
            self.assertEqual(out, "[ADDRESS]", raw)

    def test_technical_text_is_left_alone(self):
        for raw in (
            "3 phase CT polarity",
            "Check 3 Phase Motor Drive wiring",
            "480 Volt Variable Frequency Drive",
            "240 480 4160 V",
            "240 480 4160 V systems",
            "Ratings 120 208 240 480 600 V",
            "15 kV 25 kV 35 kV",
            "Readings 1.5 2.0 3.25 megohms",
            "Table 4 Way Switch",
            "Step 12 Ground Lead",
            "10⁶ ohms",
            "2 Pole Breaker Lane",
            "IEC 60270 2015 edition",
            "Model 4160 2400 1200 test set",
            "+5 V rail",
        ):
            out = scrub(raw)
            self.assertNotIn("[PHONE]", out, raw)
            self.assertNotIn("[ADDRESS]", out, raw)
            self.assertNotIn("[WEB]", out, raw)
        self.assertEqual(scrub("10⁶ ohms"), "10^6 ohms")

    def test_unicode_is_normalized_first(self):
        self.assertEqual(scrub("ｐｅｒｓｏｎ＠ｅｘａｍｐｌｅ．ｃｏｍ"), "[EMAIL]")
        self.assertIn("[PHONE]", scrub("call 555\u2011013\u20110199"))
        self.assertEqual(scrub("mail jane\u200b.doe@exam\u00adple.com"), "mail [EMAIL]")
        self.assertEqual(scrub("call +1\u00a0555\u00a0013\u00a00199"), "call [PHONE]")
        self.assertEqual(scrub("line one\r\nline two"), "line one\nline two")

    def test_non_breaking_spaces_in_numbers_are_not_phones(self):
        for raw in ("4\u00a0160\u00a0000 V", "1\u202f000\u202f000 ohms", "12\u00a0345 hours"):
            self.assertNotIn("[PHONE]", scrub(raw), repr(raw))

    def test_long_runs_finish_quickly(self):
        hostile = [
            "a" * 200_000,
            "a." * 100_000,
            "1 " * 100_000,
            ("x" * 60 + "@") * 3000,
            "555-" * 50_000,
            "A " * 80_000 + "Street",
            "http://" + "a" * 200_000,
            ("word." * 40 + " ") * 2000,
        ]
        redactor = Redactor([("Acme Corp", "[ORG]")])
        for raw in hostile:
            started = time.perf_counter()
            redactor(raw)
            self.assertLess(time.perf_counter() - started, 3.0, raw[:20])


class TermsTests(HomeCase):
    def test_redacted_ingest_sees_the_raw_page_text(self):
        import ingest

        page = "call 555\u00a0013\u00a00199 or jane\u200b.doe@example.com about breaker torque notes"
        write(os.path.join(self.home, "library", "notes.txt"), page)
        with open(os.devnull, "w") as sink:
            stdout, stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = sink
            try:
                code = ingest.run(redact_text=True)
            finally:
                sys.stdout, sys.stderr = stdout, stderr
        self.assertEqual(code, 0)
        stored = read(os.path.join(self.home, "text", "notes", "p0001.txt"))
        self.assertEqual(stored, "call [PHONE] or [EMAIL] about breaker torque notes")

    def test_extra_terms_are_literal_and_optional(self):
        raw = "Acme Corp hired Jane Doe for the cabinet inspection."
        self.assertIn("Acme Corp", redact.redact(raw, terms=[]))
        path = os.path.join(self.elsewhere, "terms.json")
        write(path, read(os.path.join(ROOT, "redact_terms.example.json")))
        out = redact.redact(raw, terms=load_terms(path))
        self.assertNotIn("Acme Corp", out)
        self.assertNotIn("Jane Doe", out)
        self.assertIn("[ORG]", out)
        self.assertIn("[NAME]", out)

    def test_example_file_is_only_placeholders(self):
        data = json.loads(read(os.path.join(ROOT, "redact_terms.example.json")))
        self.assertEqual([item["match"] for item in data["terms"]], ["Acme Corp", "Jane Doe"])
        self.assertNotIn("@", json.dumps(data))

    def test_terms_match_across_breaks_and_spacing_and_case(self):
        terms = [("Jane Doe", "[NAME]"), ("Acme Corp", "[ORG]")]
        for raw in ("Jane\nDoe signed", "Jane  Doe signed", "JANE DOE signed", "Jane\u00a0Doe signed", "Jane \n Doe signed"):
            self.assertEqual(scrub(raw, terms), "[NAME] signed", repr(raw))
        self.assertEqual(scrub("Acme Corporation is not the term", terms), "Acme Corporation is not the term")
        self.assertEqual(scrub("Acme Corp, Inc.", terms), "[ORG], Inc.")

    def test_a_list_file_and_plain_strings_are_accepted(self):
        path = os.path.join(self.elsewhere, "terms.json")
        write(path, json.dumps(["Acme Corp", {"match": "Jane Doe", "replacement": "[NAME]"}]))
        terms = load_terms(path)
        self.assertEqual(scrub("Acme Corp and Jane Doe", terms), "[REDACTED] and [NAME]")

    def test_replacements_are_not_templates_and_are_not_rescanned(self):
        path = os.path.join(self.elsewhere, "terms.json")
        write(path, json.dumps({"terms": [
            {"match": "Acme Corp", "replacement": "C:\\path\\1 \\g<0> [ORG]"},
            {"match": "Corp", "replacement": "[X]"},
            {"match": "Jane", "replacement": "Jane Doe"},
        ]}))
        warnings = []
        terms = load_terms(path, warn=warnings.append)
        self.assertEqual(scrub("Acme Corp met Jane", terms), "C:\\path\\1 \\g<0> [ORG] met [REDACTED]")
        self.assertTrue(any("contains the term" in message for message in warnings))

    def test_bad_files_raise_terms_error(self):
        path = os.path.join(self.elsewhere, "terms.json")
        for body in ("{broken", '"just text"', "42", json.dumps({"terms": "Acme Corp"}), json.dumps({"terms": {"a": 1}})):
            write(path, body)
            with self.assertRaises(TermsError, msg=body):
                load_terms(path)
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00bad")
        with self.assertRaises(TermsError):
            load_terms(path)
        with self.assertRaises(TermsError):
            load_terms(os.path.join(self.elsewhere, "missing.json"))
        self.assertEqual(load_terms(None), [])

    def test_bad_entries_are_skipped_with_a_warning(self):
        path = os.path.join(self.elsewhere, "terms.json")
        write(path, json.dumps({"terms": [42, {"match": 7}, {"match": "  "}, {"match": "Jane Doe"}]}))
        warnings = []
        terms = load_terms(path, warn=warnings.append)
        self.assertEqual(terms, [("Jane Doe", "[REDACTED]")])
        self.assertEqual(len(warnings), 2)

    def test_terms_file_comes_from_the_home_not_the_current_directory(self):
        write(os.path.join(self.elsewhere, "redact_terms.local.json"), json.dumps(["Jane Doe"]))
        self.assertIsNone(redact.discover_terms_path())
        self.assertEqual(redact.redact("Jane Doe"), "Jane Doe")
        write(os.path.join(self.home, "redact_terms.local.json"), json.dumps(["Jane Doe"]))
        self.assertEqual(redact.redact("Jane Doe"), "[REDACTED]")

    def test_cli_reports_a_bad_terms_file(self):
        path = os.path.join(self.elsewhere, "terms.json")
        write(path, "{broken")
        env = dict(os.environ, JETPACK_HOME=self.home)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "redact.py"), "--terms", path],
            input="Jane Doe", capture_output=True, text=True, env=env, cwd=self.elsewhere,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("could not read", result.stderr)
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "redact.py")],
            input="mail person@example.com", capture_output=True, text=True, env=env, cwd=self.elsewhere,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "mail [EMAIL]")


if __name__ == "__main__":
    unittest.main()
