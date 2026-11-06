"""Behavior tests for the sleep and keep commands. Files here are synthetic."""
import datetime
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLEEP = os.path.join(ROOT, "tools", "sleep.py")
KEEP = os.path.join(ROOT, "tools", "keep.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class SleepKeepTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("JETPACK_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "jetpack")
        self.scratch = os.path.join(self.tmp.name, "scratch")
        os.makedirs(self.home)
        os.makedirs(self.scratch)
        os.environ["JETPACK_HOME"] = self.home
        self.env = os.environ.copy()
        self.handoff = os.path.join(self.home, "state", "handoff.md")
        self.seed = os.path.join(self.home, "state", "seed.md")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JETPACK_HOME", None)
        else:
            os.environ["JETPACK_HOME"] = self._old
        self.tmp.cleanup()

    def sleep(self, *args):
        return subprocess.run(
            [sys.executable, SLEEP, *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def keep(self, *args):
        return subprocess.run(
            [sys.executable, KEEP, *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def test_init_writes_both_templates(self):
        result = self.sleep("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = read(self.handoff)
        seed = read(self.seed)
        self.assertIn("# Handoff", handoff)
        self.assertIn("## Done today", handoff)
        self.assertIn("## Pointers", handoff)
        self.assertIn("# Seed", seed)
        self.assertIn("## Preferences", seed)
        self.assertIn("## Standing rules", seed)
        self.assertIn("## How to work", seed)
        self.assertIn(self.handoff, result.stdout)
        self.assertIn(self.seed, result.stdout)

    def test_write_handoff_and_seed_store_text_and_second_write_backs_up(self):
        notes = os.path.join(self.scratch, "notes.md")
        rules = os.path.join(self.scratch, "rules.md")
        write(notes, "Cabinet inspection is done.\n")
        write(rules, "Read the handoff before starting.\n")
        handoff = self.sleep("write-handoff", notes)
        seed = self.sleep("write-seed", rules)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        self.assertEqual(read(self.handoff), "Cabinet inspection is done.\n")
        self.assertEqual(read(self.seed), "Read the handoff before starting.\n")
        stamp = datetime.date.today().isoformat()
        self.assertFalse(os.path.exists(f"{self.handoff}.bak-{stamp}"))
        self.assertFalse(os.path.exists(f"{self.seed}.bak-{stamp}"))

        write(notes, "Trench permit is still open.\n")
        write(rules, "Keep pointers, not dumps.\n")
        handoff = self.sleep("write-handoff", notes)
        seed = self.sleep("write-seed", rules)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        handoff_bak = f"{self.handoff}.bak-{stamp}"
        seed_bak = f"{self.seed}.bak-{stamp}"
        self.assertTrue(os.path.isfile(handoff_bak), handoff_bak)
        self.assertTrue(os.path.isfile(seed_bak), seed_bak)
        self.assertEqual(read(handoff_bak), "Cabinet inspection is done.\n")
        self.assertEqual(read(seed_bak), "Read the handoff before starting.\n")
        self.assertEqual(read(self.handoff), "Trench permit is still open.\n")
        self.assertEqual(read(self.seed), "Keep pointers, not dumps.\n")

    def test_twin_copies_both_files_to_another_folder(self):
        notes = os.path.join(self.scratch, "notes.md")
        rules = os.path.join(self.scratch, "rules.md")
        write(notes, "Done the cabinet check.\n")
        write(rules, "Standing rule: land a copy at home.\n")
        self.assertEqual(self.sleep("write-handoff", notes).returncode, 0)
        self.assertEqual(self.sleep("write-seed", rules).returncode, 0)
        durable = os.path.join(self.scratch, "durable")
        result = self.sleep("twin", durable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read(os.path.join(durable, "handoff.md")), "Done the cabinet check.\n")
        self.assertEqual(read(os.path.join(durable, "seed.md")), "Standing rule: land a copy at home.\n")

    def test_wake_returns_zero_when_files_exist_and_reports_a_missing_pointer(self):
        self.assertEqual(self.sleep("init").returncode, 0)
        wake = self.sleep("wake")
        self.assertEqual(wake.returncode, 0, wake.stderr)
        self.assertIn("handoff:", wake.stdout)
        self.assertIn("seed:", wake.stdout)

        missing = os.path.join(self.scratch, "not-there.md")
        notes = os.path.join(self.scratch, "notes.md")
        write(notes, "# Handoff\n\n## Pointers\n\n- " + missing + "\n")
        self.assertEqual(self.sleep("write-handoff", notes).returncode, 0)
        wake = self.sleep("wake")
        self.assertIn("missing", wake.stdout)
        self.assertIn(missing, wake.stdout)
        self.assertNotEqual(wake.returncode, 0)

    def test_keep_copies_into_kept_and_check_finds_it(self):
        source = os.path.join(self.scratch, "field-note.md")
        write(source, "Lockout applies to this cabinet.\n")
        result = self.keep(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = os.path.join(self.home, "kept", "field-note.md")
        self.assertEqual(read(dest), "Lockout applies to this cabinet.\n")
        self.assertIn(dest, result.stdout)
        check = self.keep("--check", source)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn(dest, check.stdout)

    def test_check_fails_when_no_copy_exists_outside_the_jetpack_home(self):
        source = os.path.join(self.scratch, "only-scratch.md")
        write(source, "This copy has not been kept.\n")
        check = self.keep("--check", source)
        self.assertNotEqual(check.returncode, 0)
        self.assertIn("no lasting copy", check.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "kept", "only-scratch.md")))

    def test_keep_refuses_a_destination_that_leaves_the_keep_folder(self):
        source = os.path.join(self.scratch, "field-note.md")
        write(source, "Do not write this outside kept.\n")
        result = self.keep("--as", "../escape", source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leave the keep folder", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "escape")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "kept", "escape")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape")))


if __name__ == "__main__":
    unittest.main()
