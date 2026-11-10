#!/usr/bin/env python3
"""Patch a copy of Grok Bot for macOS so that it lives in the menu bar.

The patch makes three changes:

1. Turns on the menu bar icon that ships in the app behind the
   sand_menubar_tray feature gate.
2. Sets LSUIElement so that the app has no Dock icon.
3. Hides the main window on close instead of destroying it. Grok Bot drops
   desktop notifications when it has no window, so a destroyed window means
   silence until the window is opened again.

The script uses only the Python standard library and the macOS tools ditto,
codesign and pgrep. It works on a copy unless you pass --install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_APP = Path("/Applications/Grok Bot.app")
ASAR_RELATIVE = "Contents/Resources/app.asar"
PLIST_RELATIVE = "Contents/Info.plist"
ASAR_INTEGRITY_KEY = "Resources/app.asar"
TARGET_FILE = ("dist", "electron-main", "main-app.cjs")
BACKUP_DIR = Path.home() / "Library/Application Support/grok-bot-menubar-patch"

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


class PatchError(Exception):
    """The app does not look the way this patch expects."""


def pad(replacement: bytes, original: bytes) -> bytes:
    """Fill with spaces, so that no offset in the archive has to move."""
    if len(replacement) > len(original):
        raise PatchError("A replacement is longer than the text it replaces.")
    return replacement + b" " * (len(original) - len(replacement))


def replace_once(source: bytes, pattern: bytes, replacement: bytes, label: str) -> bytes:
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise PatchError(
            f"Expected exactly 1 match for {label}, found {len(matches)}. "
            "This Grok Bot version needs a new patch, or it is already patched."
        )
    match = matches[0]
    new = pad(match.expand(replacement), match.group(0))
    return source[: match.start()] + new + source[match.end() :]


def enable_menubar_tray(source: bytes) -> bytes:
    """Make the sand_menubar_tray gate check always true."""
    names = re.findall(rb'var ([\w$]+)="sand_menubar_tray"', source)
    if len(names) != 1:
        raise PatchError(f"Expected 1 menu bar gate name, found {len(names)}.")
    gate = re.escape(names[0])
    pattern = rb"[\w$]+\.checkFeatureGate\(" + gate + rb",\{disableExposureLog:!0\}\)"
    return replace_once(source, pattern, b"!0", "the menu bar gate check")


def hide_window_on_close(source: bytes) -> bytes:
    """Drop the desktop mount condition from the hide-on-close check.

    The quit and restart-for-update conditions stay, so Cmd+Q still quits.
    """
    pattern = (
        rb"(function [\w$]+\(\)\{return![\w$]+&&[\w$]+\?\.isRestartingForUpdate\(\)!==!0)"
        rb"&&[\w$]+\(\)\}"
    )
    return replace_once(source, pattern, rb"\1}", "the window close check")


def patch_source(source: bytes) -> bytes:
    return hide_window_on_close(enable_menubar_tray(source))


def read_asar_header(data: bytes) -> tuple[dict, int, int]:
    """Return the header, the position of its JSON, and the JSON length."""
    if len(data) < 16:
        raise PatchError("app.asar is too short.")
    pickle_size, outer_size, inner_size, json_size = struct.unpack("<4I", data[:16])
    if pickle_size != 4 or json_size > inner_size or json_size > outer_size:
        raise PatchError("app.asar has an unknown header format.")
    return json.loads(data[16 : 16 + json_size]), 16, json_size


def dump_header(header: dict) -> bytes:
    return json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()


def file_integrity(content: bytes, block_size: int) -> tuple[str, list[str]]:
    blocks = [
        hashlib.sha256(content[start : start + block_size]).hexdigest()
        for start in range(0, len(content), block_size)
    ]
    return hashlib.sha256(content).hexdigest(), blocks or [hashlib.sha256(b"").hexdigest()]


def patch_asar(path: Path) -> str:
    """Patch the archive in place. Return the SHA-256 of the new header."""
    data = bytearray(path.read_bytes())
    header, json_start, json_size = read_asar_header(bytes(data))
    original_json = bytes(data[json_start : json_start + json_size])
    if dump_header(header) != original_json:
        raise PatchError("Cannot rewrite the app.asar header without changing it.")

    outer_size = struct.unpack("<I", data[4:8])[0]
    entry = header
    for part in TARGET_FILE:
        files = entry.get("files")
        if files is None or part not in files:
            raise PatchError("/".join(TARGET_FILE) + " is not in app.asar.")
        entry = files[part]
    if entry.get("unpacked"):
        raise PatchError("/".join(TARGET_FILE) + " is outside app.asar.")

    start = 8 + outer_size + int(entry["offset"])
    end = start + entry["size"]
    patched = patch_source(bytes(data[start:end]))
    if len(patched) != entry["size"]:
        raise PatchError("The patched file changed size.")
    data[start:end] = patched

    integrity = entry["integrity"]
    integrity["hash"], integrity["blocks"] = file_integrity(patched, integrity["blockSize"])
    new_json = dump_header(header)
    if len(new_json) != json_size:
        raise PatchError("The app.asar header changed size.")
    data[json_start : json_start + json_size] = new_json
    path.write_bytes(data)
    return hashlib.sha256(new_json).hexdigest()


def patch_plist(path: Path, header_hash: str) -> None:
    with path.open("rb") as handle:
        plist = plistlib.load(handle)
    try:
        plist["ElectronAsarIntegrity"][ASAR_INTEGRITY_KEY]["hash"] = header_hash
    except KeyError as error:
        raise PatchError("Info.plist has no app.asar integrity entry.") from error
    plist["LSUIElement"] = True
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def signing_targets(app: Path) -> list[Path]:
    """List the code to sign. Inner code comes first, the app itself last."""
    files = [
        path
        for path in app.rglob("*")
        if path.is_file() and not path.is_symlink() and is_macho(path)
    ]
    bundles = [
        path
        for path in app.rglob("*")
        if path.is_dir() and not path.is_symlink() and path.suffix in {".app", ".framework"}
    ]
    depth = lambda path: len(path.parts)
    return sorted(files, key=depth, reverse=True) + sorted(bundles, key=depth, reverse=True) + [app]


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr, end="")
        raise subprocess.CalledProcessError(result.returncode, command)


def sign(app: Path, identity: str) -> None:
    """Sign with a new identity and keep each component's entitlements.

    An ad hoc signature cannot pass library validation, so it runs without
    the hardened runtime. A real certificate keeps the hardened runtime.
    """
    command = ["codesign", "--force", "--sign", identity, "--preserve-metadata=entitlements"]
    if identity != "-":
        command += ["--options", "runtime", "--timestamp=none"]
    subprocess.run(["xattr", "-cr", str(app)], check=False, stderr=subprocess.DEVNULL)
    for target in signing_targets(app):
        run(*command, str(target))
    run("codesign", "--verify", "--deep", "--strict", str(app))


def build(source_app: Path, output: Path, identity: str) -> None:
    if output.exists():
        raise PatchError(f"{output} already exists.")
    run("ditto", str(source_app), str(output))
    header_hash = patch_asar(output / ASAR_RELATIVE)
    patch_plist(output / PLIST_RELATIVE, header_hash)
    sign(output, identity)


def in_applications_folder(path: Path) -> bool:
    return "Applications" in path.resolve().parts


def app_version(app: Path) -> str:
    with (app / PLIST_RELATIVE).open("rb") as handle:
        return str(plistlib.load(handle)["CFBundleShortVersionString"])


def is_running(app: Path) -> bool:
    with (app / PLIST_RELATIVE).open("rb") as handle:
        executable = app / "Contents/MacOS" / plistlib.load(handle)["CFBundleExecutable"]
    return subprocess.run(["pgrep", "-f", f"^{re.escape(str(executable))}"], capture_output=True).returncode == 0


def install(app: Path, identity: str) -> Path:
    """Replace the app, and keep a zip of the original so you can undo this."""
    if is_running(app):
        raise PatchError("Quit Grok Bot first.")
    with tempfile.TemporaryDirectory(dir=app.parent, prefix=".grok-bot-patch-") as work:
        patched = Path(work) / app.name
        build(app, patched, identity)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{app.stem}-{app_version(app)}-{int(time.time())}.zip"
        run("ditto", "-c", "-k", "--keepParent", str(app), str(backup))
        retired = Path(work) / "original.app"
        app.rename(retired)
        try:
            patched.rename(app)
        except OSError:
            retired.rename(app)
            raise
        return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move Grok Bot from the Dock to the menu bar.")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP, help="Grok Bot app to read")
    parser.add_argument("--output", type=Path, help="where to write the patched copy")
    parser.add_argument("--install", action="store_true", help="replace --app, and keep a zip of it")
    parser.add_argument("--identity", default="-", help='codesign identity (default: "-", ad hoc)')
    args = parser.parse_args(argv)

    if bool(args.output) == args.install:
        parser.error("use either --output PATH or --install")
    if shutil.which("codesign") is None:
        print("error: this script needs macOS and codesign.", file=sys.stderr)
        return 1
    if args.output and args.output.resolve() == args.app.resolve():
        parser.error("--output must not be the same path as --app; use --install")
    if not args.app.is_dir():
        print(f"error: {args.app} does not exist.", file=sys.stderr)
        return 1

    try:
        if args.install:
            backup = install(args.app, args.identity)
            print(f"Installed. The original is saved in {backup}")
        else:
            build(args.app, args.output, args.identity)
            print(f"Patched copy written to {args.output}")
            if not in_applications_folder(args.output):
                print(
                    "warning: do not start this copy from here. Outside an Applications "
                    "folder, Grok Bot offers to move itself to /Applications, and that "
                    "replaces the app installed there.",
                    file=sys.stderr,
                )
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: {' '.join(error.cmd)} failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
