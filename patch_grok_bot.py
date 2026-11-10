#!/usr/bin/env python3
"""Patch Grok Bot for macOS so that it lives in the menu bar.

The patch makes three changes:

1. Turns on the menu bar icon that ships in the app behind the
   sand_menubar_tray feature gate.
2. Sets LSUIElement so that the app has no Dock icon.
3. Hides the main window on close instead of destroying it. Grok Bot drops
   desktop notifications when it has no window, so a destroyed window means
   silence until the window is opened again.

The script uses only the Python standard library and the macOS tools ditto,
codesign, pgrep, open and launchctl. It works on a copy unless you pass
--install.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
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
STATE_DIR = Path.home() / "Library/Application Support/grok-bot-menubar-patch"
BACKUP_DIR = STATE_DIR
KEPT_BACKUPS = 2
AGENT_LABEL = "com.github.steelzeh.grok-bot-menubar-patch"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
AGENT_SCRIPT = STATE_DIR / "patch_grok_bot.py"
AGENT_LOG = Path.home() / "Library/Logs/grok-bot-menubar-patch.log"
FAILURE_MARKER = STATE_DIR / "last-failure.json"

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


# --- The patch itself -------------------------------------------------------


def pad(replacement: bytes, original: bytes, shrink: int = 0) -> bytes:
    """Fill with spaces up to the old length, minus the bytes to shrink by."""
    length = len(original) - shrink
    if len(replacement) > length:
        raise PatchError("A replacement is longer than the text it replaces.")
    return replacement + b" " * (length - len(replacement))


def replace_once(
    source: bytes, pattern: bytes, replacement: bytes, label: str, shrink: int = 0
) -> bytes:
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise PatchError(
            f"Expected exactly 1 match for {label}, found {len(matches)}. "
            "This Grok Bot version needs a new patch, or it is already patched."
        )
    match = matches[0]
    new = pad(match.expand(replacement), match.group(0), shrink)
    return source[: match.start()] + new + source[match.end() :]


def enable_menubar_tray(source: bytes) -> bytes:
    """Make the sand_menubar_tray gate check always true.

    This also makes the file one byte shorter. Grok Bot keeps a V8 code
    cache of this file in its data folder, and V8 accepts that cache when
    the source has the same length. With the same length, the app would run
    the cached original code and ignore the patch.
    """
    names = re.findall(rb'var ([\w$]+)="sand_menubar_tray"', source)
    if len(names) != 1:
        raise PatchError(f"Expected 1 menu bar gate name, found {len(names)}.")
    gate = re.escape(names[0])
    pattern = rb"[\w$]+\.checkFeatureGate\(" + gate + rb",\{disableExposureLog:!0\}\)"
    return replace_once(source, pattern, b"!0", "the menu bar gate check", shrink=1)


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


def patched_asar(original: bytes) -> tuple[bytes, str]:
    """Return the patched archive and the SHA-256 of its new header."""
    data = bytearray(original)
    header, json_start, json_size = read_asar_header(original)
    if dump_header(header) != original[json_start : json_start + json_size]:
        raise PatchError("Cannot rewrite the app.asar header without changing it.")

    outer_size = struct.unpack("<I", original[4:8])[0]
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
    patched = patch_source(original[start:end])
    if len(patched) > entry["size"]:
        raise PatchError("The patched file is larger than the original.")
    # The file keeps its place in the archive. The bytes it no longer uses
    # stay as an unused gap, so no other offset has to move.
    data[start:end] = patched + b"\n" * (entry["size"] - len(patched))
    entry["size"] = len(patched)

    integrity = entry["integrity"]
    integrity["hash"], integrity["blocks"] = file_integrity(patched, integrity["blockSize"])
    new_json = dump_header(header)
    if len(new_json) != json_size:
        raise PatchError("The app.asar header changed size.")
    data[json_start : json_start + json_size] = new_json
    return bytes(data), hashlib.sha256(new_json).hexdigest()


def patch_asar(path: Path) -> str:
    """Patch the archive in place. Return the SHA-256 of the new header."""
    data, header_hash = patched_asar(path.read_bytes())
    path.write_bytes(data)
    return header_hash


def read_plist(app: Path) -> dict:
    with (app / PLIST_RELATIVE).open("rb") as handle:
        return plistlib.load(handle)


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


def is_patched(app: Path) -> bool:
    return read_plist(app).get("LSUIElement") is True


# --- Signing ----------------------------------------------------------------


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


def succeeds(*command: str) -> bool:
    return subprocess.run(command, capture_output=True).returncode == 0


def team_id(path: Path) -> str | None:
    """Return the Team ID that signed the code, or None for ad hoc code."""
    result = subprocess.run(["codesign", "-dvv", str(path)], capture_output=True, text=True)
    match = re.search(r"^TeamIdentifier=([A-Z0-9]{10})$", result.stderr, re.MULTILINE)
    return match.group(1) if match else None


def update_requirement(identifier: str, teams: list[str]) -> str:
    """Build a signing requirement that code from each of the teams passes.

    Grok Bot's updater accepts a new version only if it passes the
    requirement of the installed app. With the vendor's team in the
    requirement, a patched app still accepts the vendor's updates.
    """
    if not re.fullmatch(r"[A-Za-z0-9.-]+", identifier):
        raise PatchError(f"Unexpected bundle identifier: {identifier!r}")
    for team in teams:
        if not re.fullmatch(r"[A-Z0-9]{10}", team):
            raise PatchError(f"Unexpected Team ID: {team!r}")
    signers = " or ".join(f'certificate leaf[subject.OU] = "{team}"' for team in teams)
    return (
        f'identifier "{identifier}" and anchor apple generic '
        "and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
        "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
        f"and ({signers})"
    )


def sign(app: Path, identity: str, vendor_app: Path | None = None) -> bool:
    """Sign with a new identity and keep each component's entitlements.

    An ad hoc signature cannot pass library validation, so it runs without
    the hardened runtime. A real certificate keeps the hardened runtime.

    Return True when the new signature also accepts the vendor's updates.
    That needs a Developer ID certificate and the vendor's original app.
    """
    command = ["codesign", "--force", "--sign", identity, "--preserve-metadata=identifier,entitlements"]
    if identity != "-":
        command += ["--options", "runtime", "--timestamp=none"]
    subprocess.run(["xattr", "-cr", str(app)], check=False, stderr=subprocess.DEVNULL)
    for target in signing_targets(app):
        run(*command, str(target))

    accepts_updates = False
    vendor_team = team_id(vendor_app) if vendor_app else None
    signer_team = team_id(app)
    if vendor_team and signer_team and vendor_team != signer_team:
        requirement = update_requirement(read_plist(app)["CFBundleIdentifier"], [vendor_team, signer_team])
        run(*command, "--requirements", f"=designated => {requirement}", str(app))
        if not succeeds("codesign", "--verify", f"-R={requirement}", str(app)):
            raise PatchError(
                "This certificate cannot keep updates. Use a Developer ID Application "
                'certificate, or sign ad hoc with --identity "-".'
            )
        if not succeeds("codesign", "--verify", f"-R={requirement}", str(vendor_app)):
            raise PatchError("The original app does not pass the update requirement.")
        accepts_updates = True
    run("codesign", "--verify", "--deep", "--strict", str(app))
    return accepts_updates


def build(source_app: Path, output: Path, identity: str) -> bool:
    """Write a patched copy. Return True when it accepts the vendor's updates."""
    if output.exists():
        raise PatchError(f"{output} already exists.")
    # Fail before the large copy if this version does not fit the patch.
    patched_asar((source_app / ASAR_RELATIVE).read_bytes())
    run("ditto", str(source_app), str(output))
    header_hash = patch_asar(output / ASAR_RELATIVE)
    patch_plist(output / PLIST_RELATIVE, header_hash)
    return sign(output, identity, vendor_app=source_app)


# --- Install ----------------------------------------------------------------


def clear_code_cache(app: Path) -> None:
    """Delete Grok Bot's V8 code cache of the patched file.

    The cache is in the app's data folder. The app writes it again.
    """
    name = read_plist(app).get("CFBundleName", app.stem)
    cache = Path.home() / "Library/Application Support" / name / "v8-code-cache"
    for path in cache.glob(f"{TARGET_FILE[-1].split('.')[0]}-*.v8"):
        path.unlink(missing_ok=True)


def in_applications_folder(path: Path) -> bool:
    return "Applications" in path.resolve().parts


def app_version(app: Path) -> str:
    return str(read_plist(app)["CFBundleShortVersionString"])


def main_pids(app: Path) -> list[int]:
    """Return the process IDs of the app's main process."""
    executable = app / "Contents/MacOS" / read_plist(app)["CFBundleExecutable"]
    result = subprocess.run(
        ["pgrep", "-f", f"^{re.escape(str(executable))}( |$)"], capture_output=True, text=True
    )
    return [int(line) for line in result.stdout.split()]


def is_running(app: Path) -> bool:
    return bool(main_pids(app))


def quit_app(app: Path, timeout: float = 45) -> None:
    """Ask the app to quit, and wait until it is gone. Never force it."""
    for pid in main_pids(app):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(app):
            return
        time.sleep(0.5)
    raise PatchError("Grok Bot did not quit in time. Nothing was changed.")


def prune_backups(app: Path) -> None:
    """Keep only the newest backups that this script made."""
    backups = sorted(
        BACKUP_DIR.glob(f"{app.stem}-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for old in backups[KEPT_BACKUPS:]:
        old.unlink()


def updater_pids(app: Path) -> list[int]:
    """Return the process IDs of this app's update installer (ShipIt)."""
    pattern = f"^{re.escape(str(app))}/Contents/Frameworks/Squirrel\\.framework/.*ShipIt"
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(line) for line in result.stdout.split()]


def wait_for_updater(app: Path, timeout: float = 180) -> None:
    """Wait until the update installer is done.

    The installer starts when an update is downloaded, and it waits until
    the app quits. Then it replaces the app. Call this after the app quit.
    """
    deadline = time.monotonic() + timeout
    while updater_pids(app):
        if time.monotonic() > deadline:
            raise PatchError("The Grok Bot updater did not finish. Nothing was changed.")
        time.sleep(0.5)


def asar_state(app: Path) -> tuple[int, int, int] | None:
    try:
        stat = (app / ASAR_RELATIVE).stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def install(app: Path, identity: str, quit_running: bool = False) -> tuple[Path, bool]:
    """Replace the app, and keep a zip of the original so you can undo this.

    Return the backup path, and True when the new app accepts updates.
    """
    if is_patched(app):
        raise PatchError(f"{app} is already patched.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=app.parent, prefix=".grok-bot-patch-"))
    patched = work / app.name
    retired = work / "original" / app.name
    retired.parent.mkdir()
    stopped = False
    try:
        for _ in range(3):
            if is_running(app):
                if not quit_running:
                    raise PatchError("Quit Grok Bot first, or pass --quit.")
                quit_app(app)
                stopped = True
            # A downloaded update installs now, because the app is closed.
            wait_for_updater(app)
            if is_running(app):
                continue
            state = asar_state(app)
            if patched.exists():
                shutil.rmtree(patched)
            accepts_updates = build(app, patched, identity)
            if not is_running(app) and not updater_pids(app) and asar_state(app) == state:
                break
        else:
            raise PatchError("Grok Bot kept changing. Nothing was changed.")
        version = app_version(app)
        try:
            app.rename(retired)
        except PermissionError as error:
            raise PatchError(
                "macOS did not allow a change in the Applications folder. Allow this "
                "program in System Settings > Privacy & Security > App Management."
            ) from error
        try:
            patched.rename(app)
        except OSError:
            retired.rename(app)
            raise
        clear_code_cache(app)
        if stopped:
            subprocess.run(["open", str(app)], check=False)
            stopped = False
        backup = BACKUP_DIR / f"{app.stem}-{version}-{int(time.time())}.zip"
        run("ditto", "-c", "-k", "--keepParent", str(retired), str(backup))
        prune_backups(app)
        return backup, accepts_updates
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"warning: could not remove {work}. You can delete it.", file=sys.stderr)
        if stopped:
            subprocess.run(["open", str(app)], check=False)


def restore(app: Path, quit_running: bool = False) -> Path | None:
    """Put the newest saved original back. Return its backup path.

    Return None when the installed app is already an original.
    """
    backups = sorted(BACKUP_DIR.glob(f"{app.stem}-*.zip"), key=lambda path: path.stat().st_mtime)
    if not backups:
        raise PatchError(f"There is no backup in {BACKUP_DIR}.")
    backup = backups[-1]
    work = Path(tempfile.mkdtemp(dir=app.parent, prefix=".grok-bot-patch-"))
    stopped = False
    try:
        run("ditto", "-x", "-k", str(backup), str(work / "restored"))
        restored = work / "restored" / app.name
        if not restored.is_dir() or not succeeds("codesign", "--verify", "--deep", "--strict", str(restored)):
            raise PatchError(f"{backup} does not hold a valid app.")
        if is_running(app):
            if not quit_running:
                raise PatchError("Quit Grok Bot first, or pass --quit.")
            quit_app(app)
            stopped = True
        # A downloaded update installs now, and that also removes the patch.
        wait_for_updater(app)
        if not is_patched(app):
            return None
        retired = work / "retired" / app.name
        retired.parent.mkdir()
        app.rename(retired)
        try:
            restored.rename(app)
        except OSError:
            retired.rename(app)
            raise
        clear_code_cache(app)
        return backup
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"warning: could not remove {work}. You can delete it.", file=sys.stderr)
        if stopped:
            subprocess.run(["open", str(app)], check=False)


# --- Patch again after an update ---------------------------------------------


def bundle_state(app: Path) -> tuple[int, int, int] | None:
    """Describe the installed archive, or return None while it is not whole."""
    state = asar_state(app)
    if state is None or not succeeds("codesign", "--verify", "--deep", "--strict", str(app)):
        return None
    return state


def wait_until_stable(app: Path, timeout: float = 300, pause: float = 5) -> tuple[int, int, int]:
    """Wait until the updater is done and the app no longer changes."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = bundle_state(app)
        if state is not None and state == last:
            return state
        last = state
        time.sleep(pause)
    raise PatchError("Grok Bot kept changing. The next run tries again.")


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def notify(message: str) -> None:
    script = f"display notification {json.dumps(message)} with title \"Grok Bot menu bar patch\""
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def install_if_needed(app: Path, identity: str) -> int:
    """Patch the app again when an update replaced it. Used by the agent."""
    if not app.is_dir() or is_patched(app):
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            state = list(wait_until_stable(app))
            if is_patched(app):
                return 0
            if FAILURE_MARKER.exists() and json.loads(FAILURE_MARKER.read_text()) == state:
                return 0
            version = app_version(app)
            log(f"Grok Bot {version} is not patched. Applying the patch.")
            try:
                install(app, identity, quit_running=True)
            except (PatchError, subprocess.CalledProcessError, OSError) as error:
                FAILURE_MARKER.write_text(json.dumps(state))
                log(f"error: {error}")
                notify(f"The patch did not fit Grok Bot {version}. Grok Bot runs unpatched.")
                return 1
            FAILURE_MARKER.unlink(missing_ok=True)
            log(f"Grok Bot {version} is patched again.")
            return 0
        except PatchError as error:
            log(f"error: {error}")
            return 1


def agent_plist(python: str, script: Path, app: Path, identity: str) -> dict:
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [
            python,
            str(script),
            "--app",
            str(app),
            "--install",
            "--if-needed",
            "--identity",
            identity,
        ],
        # An update replaces the bundle, and that changes the parent folder.
        "WatchPaths": [str(app.parent), str(app)],
        "RunAtLoad": True,
        "StartInterval": 1800,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(AGENT_LOG),
        "StandardErrorPath": str(AGENT_LOG),
    }


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def remove_agent() -> None:
    subprocess.run(["launchctl", "bootout", f"{launchctl_domain()}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.unlink(missing_ok=True)
    AGENT_SCRIPT.unlink(missing_ok=True)


def install_agent(app: Path, identity: str) -> None:
    """Install a launchd agent that patches the app again after an update."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != AGENT_SCRIPT:
        shutil.copy2(source, AGENT_SCRIPT)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{launchctl_domain()}/{AGENT_LABEL}"], capture_output=True)
    with AGENT_PLIST.open("wb") as handle:
        plistlib.dump(agent_plist(sys.executable, AGENT_SCRIPT, app, identity), handle)
    run("launchctl", "bootstrap", launchctl_domain(), str(AGENT_PLIST))


# --- Command line -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move Grok Bot from the Dock to the menu bar.")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP, help="Grok Bot app to read")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="write a patched copy to this path")
    action.add_argument("--install", action="store_true", help="replace --app, and keep a zip of it")
    action.add_argument(
        "--install-agent", action="store_true", help="patch --app again after each update"
    )
    action.add_argument("--remove-agent", action="store_true", help="remove that agent")
    action.add_argument("--restore", action="store_true", help="put the saved original app back")
    parser.add_argument(
        "--quit", action="store_true", help="with --install or --restore: quit and reopen Grok Bot"
    )
    parser.add_argument(
        "--if-needed", action="store_true", help="with --install: do nothing if --app is patched"
    )
    parser.add_argument("--identity", default="-", help='codesign identity (default: "-", ad hoc)')
    args = parser.parse_args(argv)

    if shutil.which("codesign") is None:
        print("error: this script needs macOS and codesign.", file=sys.stderr)
        return 1
    if args.remove_agent:
        remove_agent()
        print("The agent is removed.")
        return 0
    if args.install and args.if_needed:
        return install_if_needed(args.app, args.identity)
    if args.output and args.output.resolve() == args.app.resolve():
        parser.error("--output must not be the same path as --app; use --install")
    if not args.app.is_dir():
        print(f"error: {args.app} does not exist.", file=sys.stderr)
        return 1

    try:
        if args.install_agent:
            install_agent(args.app, args.identity)
            print(f"The agent is installed. Its log is {AGENT_LOG}")
        elif args.restore:
            if AGENT_PLIST.exists():
                print("warning: the agent is installed and will patch the app again. Use --remove-agent.")
            backup = restore(args.app, quit_running=args.quit)
            print(f"Restored from {backup}" if backup else "Grok Bot is already an original.")
        elif args.install:
            backup, accepts_updates = install(args.app, args.identity, quit_running=args.quit)
            print(f"Installed. The original is saved in {backup}")
            print_update_note(accepts_updates)
        else:
            accepts_updates = build(args.app, args.output, args.identity)
            print(f"Patched copy written to {args.output}")
            print_update_note(accepts_updates)
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


def print_update_note(accepts_updates: bool) -> None:
    if accepts_updates:
        print("This signature accepts the vendor's updates. Use --install-agent to keep the patch.")
    else:
        print("This signature does not accept the vendor's updates.")


if __name__ == "__main__":
    raise SystemExit(main())
