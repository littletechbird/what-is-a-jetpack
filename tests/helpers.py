"""Shared setup for the tool tests. Every test gets its own JETPACK_HOME and working directory."""
import hashlib
import importlib
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FakeModel:
    """Bag-of-words vectors, so texts that share words point the same way. No download."""

    dim = 1024

    def __init__(self, constant=False):
        self.constant = constant
        self.calls = 0

    def _vector(self, text):
        import numpy as np

        vector = np.zeros(self.dim, dtype=np.float32)
        if self.constant:
            vector[0] = 1.0
            return vector
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        return vector

    def embed(self, texts, batch_size=64):
        self.calls += 1
        return [self._vector(text) for text in texts]

    def query_embed(self, texts):
        return [self._vector(text) for text in texts]


class HomeCase(unittest.TestCase):
    """A fresh jetpack home and a fresh working directory, so no real local file can leak in."""

    def setUp(self):
        self._old_home = os.environ.get("JETPACK_HOME")
        self._old_cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(self.home)
        os.makedirs(self.elsewhere)
        os.environ["JETPACK_HOME"] = self.home
        os.chdir(self.elsewhere)
        import ask

        ask._PF = None
        ask._MODEL = None
        del ask.NOTES[:]

    def tearDown(self):
        import ask

        ask._MODEL = None
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("JETPACK_HOME", None)
        else:
            os.environ["JETPACK_HOME"] = self._old_home
        self.tmp.cleanup()

    def library(self, *parts):
        path = os.path.join(self.home, "library", *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def db(self):
        import sqlite3

        return sqlite3.connect(os.path.join(self.home, "index", "jetpack.db"))

    def tool(self, name):
        return importlib.import_module(name)
