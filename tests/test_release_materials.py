import os
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release_materials import sanitize_tree, zip_tree, reject_private_paths, load_manifest


class ReleaseMaterialsTests(unittest.TestCase):
    def test_portable_manifest_resolves_relative_tools_and_public_placeholders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "manifest.json"
            path.write_text(json.dumps({"python": {"venv_python": "venv/bin/python"}, "tools": {"ffmpeg": {"path": "${PORTABLE_ROOT}/tools/ffmpeg"}, "wacli": {"path": "tools/wacli"}}}))
            result = load_manifest(path, root, root / "project")
            self.assertEqual(result["python"]["venv_python"], str(root / "venv/bin/python"))
            self.assertEqual(result["tools"]["ffmpeg"]["path"], str(root / "tools/ffmpeg"))
            self.assertEqual(result["tools"]["wacli"]["path"], str(root / "tools/wacli"))

    def test_source_metadata_paths_are_replaced_without_touching_upstream_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "build.sh"
            source.write_text('source /Users/private-builder/project/env.sh\nCopyright Upstream Author\n')
            sanitize_tree(root, {"/Users/private-builder/project": "${PROJECT_ROOT}"})
            self.assertEqual(source.read_text(), 'source ${PROJECT_ROOT}/env.sh\nCopyright Upstream Author\n')

    def test_binary_paths_are_rejected_without_byte_patching(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = b'\x00\xff/Users/private-builder/code'
            (root / "tool").write_bytes(data)
            with self.assertRaises(ValueError):
                sanitize_tree(root, {"/Users/private-builder": "${HOME}"})
            self.assertEqual((root / "tool").read_bytes(), data)
            with self.assertRaises(ValueError):
                reject_private_paths(root, ["/Users/private-builder"])

    def test_archive_preserves_internal_symlinks_and_executable_mode_without_finder_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Sample.app"
            root.mkdir()
            (root / "tool").write_text("executable")
            (root / "tool").chmod(0o755)
            (root / "alias").symlink_to("tool")
            (root / ".DS_Store").write_text("private finder metadata")
            (root / "._tool").write_text("private xattr")
            archive = Path(folder) / "app.zip"
            zip_tree(root, archive)
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(set(zipped.namelist()), {"Sample.app/tool", "Sample.app/alias"})
                self.assertEqual(zipped.read("Sample.app/alias"), b"tool")
                self.assertEqual(zipped.getinfo("Sample.app/alias").external_attr >> 16 & 0o170000, 0o120000)
                self.assertTrue(zipped.getinfo("Sample.app/tool").external_attr >> 16 & 0o100)
