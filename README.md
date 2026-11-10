# Grok Bot menu bar patch

Moves Grok Bot for macOS out of the Dock and into the menu bar.

Not affiliated with Anysphere or xAI. This repo holds no Grok Bot code, only the short text patterns the script looks for in your installed copy.

## What it changes

- Turns on the menu bar icon that ships inside Grok Bot but is switched off.
- Removes the Dock icon.
- Hides the window when you close it, instead of destroying it. Grok Bot drops desktop notifications while it has no window, so a closed window used to mean no notifications.

Cmd+Q and Quit in the menu still quit the app.

## Install

You need macOS and Python 3.9 or newer.

```sh
python3 patch_grok_bot.py --install --quit
```

This closes Grok Bot, patches it and opens it again. The original app is saved as a zip in `~/Library/Application Support/grok-bot-menubar-patch/`.

To undo:

```sh
python3 patch_grok_bot.py --restore --quit
```

## Updates

The default signature has no certificate, and then Grok Bot can't update itself. Sign with your own Developer ID certificate to keep updates working:

```sh
python3 patch_grok_bot.py --install --quit --identity "Developer ID Application: Your Name (TEAMID)"
```

An update installs a clean copy from Anysphere, so run the command again after each update. `--install-agent` adds a launchd job that does it for you, and `--remove-agent` removes it.

## If the icon doesn't show

Open System Settings > Menu Bar and turn on Grok Bot. If you ever started Grok Bot from a terminal, macOS can list its icon under that terminal app, so turn that app on too.

## Side effects

- macOS may ask again for Keychain, camera and microphone access, because the app's signature changed.
- Without a Dock icon, Grok Bot is not in Cmd+Tab and has no menus at the top of the screen.
- The menu bar icon is an unreleased Grok Bot feature, so it may change.
- If a new Grok Bot version changes the code the script looks for, the script stops with an error and leaves the app alone.
- Don't start a patched copy from outside an Applications folder. Grok Bot then offers to move itself to /Applications, and that replaces your installed app.

Tested on Grok Bot 0.79.3 and 0.79.4, Apple Silicon, macOS 26. Not tested yet: notifications while the window is hidden, and the launchd job.

## Tests

```sh
python3 -m unittest discover -s tests
```
