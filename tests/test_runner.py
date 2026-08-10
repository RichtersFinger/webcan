"""Tests for module `webcan.runner`."""

import os
import tempfile
import unittest
from pathlib import Path

from ..runner import _first_difference, _snapshot, load_app


class TestLoadApp(unittest.TestCase):
    """Test `load_app`."""
    def test_rejects_malformed_target(self):
        """Rejects targets not shaped like 'module:attribute'."""
        for target in ("", "main", ":app", "main:"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                load_app(target)

    def test_rejects_non_app_attribute(self):
        """Raises `TypeError` when the attribute is not an `App`."""
        with self.assertRaises(TypeError):
            load_app("json:dumps")


class TestSnapshot(unittest.TestCase):
    """Test `snapshot` (hot reload)."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

    def test_tracks_only_python_files(self):
        """`_snapshot` records .py files and ignores other extensions."""
        (self.tmp_path / "a.py").write_text("x = 1")
        (self.tmp_path / "b.txt").write_text("noise")

        snapshot = _snapshot([self.tmp_path])

        self.assertEqual(set(snapshot), {self.tmp_path / "a.py"})

    def test_skips_hidden_and_ignored_directories(self):
        """`_snapshot` prunes hidden directories and known noise
        directories."""
        for name in (".venv", "__pycache__"):
            (self.tmp_path / name).mkdir()
            (self.tmp_path / name / "mod.py").write_text("x = 1")

        self.assertEqual(_snapshot([self.tmp_path]), {})

    def test_mtime_change_is_detected(self):
        """`_first_difference` reports a file whose mtime changed between
        snapshots."""
        source = self.tmp_path / "a.py"
        source.write_text("x = 1")
        before = _snapshot([self.tmp_path])

        os.utime(source, (1, 1))  # force a deterministic mtime change
        after = _snapshot([self.tmp_path])

        self.assertEqual(_first_difference(before, after), source)

    def test_deleted_file_is_detected(self):
        """`_first_difference` reports files present before but missing
        after."""
        source = self.tmp_path / "a.py"
        source.write_text("x = 1")
        before = _snapshot([self.tmp_path])

        source.unlink()

        self.assertEqual(
            _first_difference(before, _snapshot([self.tmp_path])), source
        )
