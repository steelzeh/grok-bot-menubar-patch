import hashlib
import json
import os
import plistlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import patch_grok_bot as patcher

GATE = (
    b'var G$="sand_menubar_tray";'
    b"function a(e,t){let n=!1,r=()=>{n||e.checkFeatureGate(G$,{disableExposureLog:!0})&&(n=!0,b(t))};r()}"
)
CLOSE = b"function c(){return!q&&u?.isRestartingForUpdate()!==!0&&m()}"
SOURCE = b"before;" + GATE + b";" + CLOSE + b";after"


def make_asar(path, content, block_size=16):
    file_hash, blocks = patcher.file_integrity(content, block_size)
    header = {
        "files": {
            "first.txt": {"size": 3, "offset": "0"},
            "dist": {
                "files": {
                    "electron-main": {
                        "files": {
                            "main-app.cjs": {
                                "size": len(content),
                                "offset": "3",
                                "integrity": {
                                    "algorithm": "SHA256",
                                    "hash": file_hash,
                                    "blockSize": block_size,
                                    "blocks": blocks,
                                },
                            }
                        }
                    }
                }
            },
        }
    }
    text = patcher.dump_header(header)
    padding = (4 - len(text) % 4) % 4
    inner = 4 + len(text) + padding
    outer = 4 + inner
    path.write_bytes(
        struct.pack("<4I", 4, outer, inner, len(text)) + text + b"\0" * padding + b"abc" + content
    )


class SourcePatchTests(unittest.TestCase):
    def test_changes_both_checks_and_the_length(self):
        patched = patcher.patch_source(SOURCE)
        # A new length makes V8 reject a code cache of the original file.
        self.assertEqual(len(patched), len(SOURCE) - 1)
        self.assertNotIn(b"checkFeatureGate", patched)
        self.assertIn(b"n||!0", patched)
        self.assertNotIn(b"&&m()", patched)
        self.assertIn(b"isRestartingForUpdate()!==!0", patched)
        self.assertIn(b"return!q&&", patched)

    def test_refuses_when_already_patched(self):
        with self.assertRaises(patcher.PatchError):
            patcher.patch_source(patcher.patch_source(SOURCE))

    def test_refuses_two_close_checks(self):
        with self.assertRaises(patcher.PatchError):
            patcher.hide_window_on_close(CLOSE + CLOSE)

    def test_refuses_missing_gate(self):
        with self.assertRaises(patcher.PatchError):
            patcher.enable_menubar_tray(CLOSE)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.asar = Path(self.directory.name) / "app.asar"

    def read_target(self):
        data = self.asar.read_bytes()
        header, start, size = patcher.read_asar_header(data)
        entry = header["files"]["dist"]["files"]["electron-main"]["files"]["main-app.cjs"]
        base = 8 + struct.unpack("<I", data[4:8])[0] + int(entry["offset"])
        return data, entry, data[base : base + entry["size"]], data[start : start + size]

    def test_updates_file_and_integrity(self):
        make_asar(self.asar, SOURCE)
        before_size = self.asar.stat().st_size
        header_hash = patcher.patch_asar(self.asar)
        _, entry, content, header_json = self.read_target()
        self.assertEqual(self.asar.stat().st_size, before_size)
        self.assertEqual(content, patcher.patch_source(SOURCE))
        self.assertEqual(entry["size"], len(SOURCE) - 1)
        file_hash, blocks = patcher.file_integrity(content, entry["integrity"]["blockSize"])
        self.assertEqual(entry["integrity"]["hash"], file_hash)
        self.assertEqual(entry["integrity"]["blocks"], blocks)
        self.assertEqual(header_hash, hashlib.sha256(header_json).hexdigest())

    def test_leaves_other_files_alone(self):
        make_asar(self.asar, SOURCE)
        patcher.patch_asar(self.asar)
        data, _, _, _ = self.read_target()
        base = 8 + struct.unpack("<I", data[4:8])[0]
        self.assertEqual(data[base : base + 3], b"abc")

    def test_plist_gets_hash_and_no_dock_key(self):
        path = Path(self.directory.name) / "Info.plist"
        with path.open("wb") as handle:
            plistlib.dump(
                {"CFBundleName": "x", "ElectronAsarIntegrity": {"Resources/app.asar": {"hash": "old", "algorithm": "SHA256"}}},
                handle,
            )
        patcher.patch_plist(path, "new")
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertTrue(plist["LSUIElement"])
        self.assertEqual(plist["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"], "new")
        self.assertEqual(plist["CFBundleName"], "x")


class PathTests(unittest.TestCase):
    def test_knows_applications_folders(self):
        self.assertTrue(patcher.in_applications_folder(Path("/Applications/Grok Bot.app")))
        self.assertTrue(patcher.in_applications_folder(Path.home() / "Applications/Grok Bot Test.app"))
        self.assertFalse(patcher.in_applications_folder(Path("/tmp/Grok Bot.app")))


class SigningTests(unittest.TestCase):
    def test_requirement_accepts_both_teams(self):
        requirement = patcher.update_requirement("com.example.app", ["AAAAAAAAAA", "BBBBBBBBBB"])
        self.assertIn('identifier "com.example.app"', requirement)
        self.assertIn(
            '(certificate leaf[subject.OU] = "AAAAAAAAAA" or certificate leaf[subject.OU] = "BBBBBBBBBB")',
            requirement,
        )

    def test_requirement_refuses_odd_input(self):
        with self.assertRaises(patcher.PatchError):
            patcher.update_requirement('com.example" or anchor apple', ["AAAAAAAAAA"])
        with self.assertRaises(patcher.PatchError):
            patcher.update_requirement("com.example.app", ['A" or 1'])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def make_app(self, **plist):
        app = self.root / "Grok Bot.app"
        (app / "Contents").mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "Grok Bot", **plist}, handle)
        return app

    def test_knows_an_unpatched_app(self):
        self.assertFalse(patcher.is_patched(self.make_app()))

    def test_knows_a_patched_app(self):
        self.assertTrue(patcher.is_patched(self.make_app(LSUIElement=True)))

    def test_if_needed_leaves_a_patched_app_alone(self):
        app = self.make_app(LSUIElement=True)
        self.assertEqual(patcher.install_if_needed(app, "-"), 0)
        self.assertEqual(patcher.install_if_needed(self.root / "missing.app", "-"), 0)

    def test_keeps_only_the_newest_backups(self):
        original = patcher.BACKUP_DIR
        patcher.BACKUP_DIR = self.root
        self.addCleanup(setattr, patcher, "BACKUP_DIR", original)
        for index in range(4):
            path = self.root / f"Grok Bot-1.0-{index}.zip"
            path.write_bytes(b"x")
            os.utime(path, (index, index))
        (self.root / "other.zip").write_bytes(b"x")
        patcher.prune_backups(self.root / "Grok Bot.app")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["Grok Bot-1.0-2.zip", "Grok Bot-1.0-3.zip", "other.zip"],
        )

    def test_agent_runs_the_if_needed_install(self):
        plist = patcher.agent_plist("/usr/bin/python3", Path("/x/patch.py"), Path("/Applications/Grok Bot.app"), "Cert")
        self.assertEqual(
            plist["ProgramArguments"],
            ["/usr/bin/python3", "/x/patch.py", "--app", "/Applications/Grok Bot.app", "--install", "--if-needed", "--identity", "Cert"],
        )
        self.assertEqual(plist["WatchPaths"], ["/Applications", "/Applications/Grok Bot.app"])


if __name__ == "__main__":
    unittest.main()
