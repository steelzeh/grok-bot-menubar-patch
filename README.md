# Grok Bot menu bar patch

A small script that moves Grok Bot for macOS from the Dock to the menu bar.

This project is not made by or connected to Anysphere or xAI. It contains no Grok Bot code. It only holds the short text patterns that it searches for in your own copy of the app.

## What it changes

| Change | Why |
| --- | --- |
| Turns on the menu bar icon | The app already contains one, behind a feature gate named `sand_menubar_tray` that is off by default. |
| Removes the Dock icon | The script sets `LSUIElement` in the app's `Info.plist`. |
| Hides the window on close | The app normally destroys its window on close. It drops desktop notifications when it has no window, and it must load the whole interface again to reopen. |

Cmd+Q and the Quit item in the menu bar icon still quit the app.

## Use it

You need macOS and Python 3.9 or newer.

Replace the installed app. `--quit` closes Grok Bot and opens it again:

```sh
python3 patch_grok_bot.py --install --quit
```

The script saves a zip of the original app in `~/Library/Application Support/grok-bot-menubar-patch/`. It keeps the 2 newest zips.

Put the original app back:

```sh
python3 patch_grok_bot.py --restore --quit
```

## Keep updates

Grok Bot's updater accepts a new version only if it passes the signing rule of the installed app. The default signature of this script is ad hoc (no certificate), and then **updates stop**.

With your own Developer ID Application certificate, updates keep working:

```sh
python3 patch_grok_bot.py --install --quit --identity "Developer ID Application: Your Name (TEAMID)"
```

The script then writes a signing rule that accepts two signers: the vendor and you. The patched app passes it, and so does each update from the vendor.

An update installs the vendor's original app, so the patch is gone after each update. Run the same `--install` command again. Or install a launchd agent that does it for you:

```sh
python3 patch_grok_bot.py --install-agent --identity "Developer ID Application: Your Name (TEAMID)"
python3 patch_grok_bot.py --remove-agent
```

The agent runs when the Applications folder changes, at login, and every 30 minutes. It does nothing while the app is patched. After an update it quits Grok Bot, patches it, and opens it again. Its log is `~/Library/Logs/grok-bot-menubar-patch.log`.

## Try a copy first

Write a patched copy and leave the installed app alone:

```sh
mkdir -p ~/Applications
python3 patch_grok_bot.py --output ~/Applications/"Grok Bot Test.app"
```

> **Warning:** only start a patched copy from inside an Applications folder, and give it a different name. A copy that starts from any other folder offers to move itself to `/Applications`, and that replaces the Grok Bot installed there. Keep a backup of the installed app before you try a copy.

To try the copy without your own login and data, start it with its own data folders and with updates off:

```sh
env SAND_DISABLE_UPDATES=1 SAND_USER_DATA_DIR=/tmp/grok-test/ud SAND_DATA_ROOT=/tmp/grok-test/root \
  ~/Applications/"Grok Bot Test.app/Contents/MacOS/Grok Bot"
```

## What you give up

- **macOS treats it as a new app.** It may ask again for Keychain, microphone, camera and other permissions.
- **The app leaves Cmd+Tab and the top menu bar.** macOS does this for every app without a Dock icon. The Dock unread badge also goes away. The menu bar icon shows the unread count.
- **The menu bar icon can be hidden by macOS.** If you do not see it, look in System Settings > Menu Bar, and check that there is room next to the notch.
- **The menu bar icon is not a released feature.** The vendor has not turned it on, so it can be unfinished or change.

## How it works

- The script finds its targets with patterns that ignore the minified names, and it needs exactly one match for each. If a new version changes that code, the script stops with an error and leaves the app unchanged.
- It edits one file inside `app.asar` in place. The file keeps its position, so no other offset moves. It updates the hashes that Electron checks at startup.
- The patched file is one byte shorter than the original. Grok Bot keeps a V8 code cache of that file in its data folder, and V8 accepts a cache when the source has the same length. With the same length, the app would run the cached original code and ignore the patch. `--install` also deletes that cache file. The app writes it again.
- It signs all code again and keeps each component's entitlements.
- `--install` waits for Grok Bot's own updater. A downloaded update installs when the app quits, and the script patches the result.

## Tested

Grok Bot 0.79.3 and 0.79.4, Apple Silicon, macOS 26:

- The app has no Dock icon.
- The menu bar icon exists and shows the recent bots, Open, Settings and Quit.
- The close button hides the window, and the window process stays alive.
- Open shows the window again without a restart. Quit ends the app.
- A patched 0.79.3 with a Developer ID signature downloaded and installed the vendor's 0.79.4 update.
- `--install` and `--restore` on a real installation.

Not tested yet:

- A real notification that arrives while the window is hidden.
- The launchd agent (`--install-agent`).
- Intel Macs.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use a small synthetic archive, so they do not need Grok Bot.
