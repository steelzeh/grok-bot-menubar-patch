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

You need macOS and Python 3.9 or newer. Quit Grok Bot first.

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

Replace the installed app. The script saves a zip of the original in `~/Library/Application Support/grok-bot-menubar-patch/`:

```sh
python3 patch_grok_bot.py --install
```

To undo the patch, unzip that file and put the app back in `/Applications`.

## What you give up

- **Automatic updates will probably stop.** The patched app no longer has Anysphere's signature, and the updater expects it. To update, download a fresh Grok Bot and run the script on it.
- **macOS will treat it as a new app.** It may ask again for Keychain, microphone, camera and other permissions.
- **Ad hoc signing changes on each build.** Pass `--identity "Your Certificate Name"` to sign with your own certificate, so that permissions are more likely to survive a rebuild.
- **The app leaves Cmd+Tab and the top menu bar.** macOS does this for every app without a Dock icon. The Dock unread badge also goes away.

## New Grok Bot versions

The script finds its targets with patterns that ignore the minified names, and it needs exactly one match for each. If a new version changes that code, the script stops with an error and leaves the app unchanged.

Tested on Grok Bot 0.79.3, Apple Silicon, with a copy that was not logged in:

- The app has no Dock icon.
- The menu bar icon shows Open, Settings and Quit.
- The close button hides the window, and the window process stays alive.
- Open shows the window again, and Quit ends the app.

Not tested yet:

- A real notification that arrives while the window is hidden.
- The `--install` mode on a real installation. Only `--output` was run.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests use a small synthetic archive, so they do not need Grok Bot.
