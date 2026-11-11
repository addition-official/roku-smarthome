# Roadmap

Ideas for improving roku-smarthome, most useful first.

## Worth doing

### 1. Tests that run on every push - done

`tests/` covers cookie parsing, device parsing, commands, caching, error handling and the CLI, with no network or Roku account needed. `.github/workflows/tests.yml` runs them on every push to `main` and every pull request that touches code. Run them locally with:

```
pip install . pytest
python -m pytest
```

### 2. Reuse one connection for commands

Every command opens a new websocket to Roku, sends one message, waits half a second (`command_wait`) and closes. That's fine for a single `on`, but slow for anything that sends many commands in a row, like a GUI brightness slider. Keeping one connection open on the background event loop and reconnecting when it drops would make the library much faster for apps. This is the biggest improvement to the library itself.

### 3. Check color temperature values

`set_brightness` and `set_color_rgb` reject out-of-range values, but `set_color_temp` sends any number. A typo like `temp lamp 30000` goes straight to Roku. It should reject values outside the bulb's range (2700-6500K on the tested bulb; check whether the device reports its own range in `supportedCommands`).

### 4. Make the CLI nicer to use

- Partial name matching, so `on lamp` works when only one device has "lamp" in its name (and a clear error listing matches when several do).
- Hex colors, like `rgb "Color bulb" #ff8000`.
- Different exit codes for "session expired" and "Roku is down", so scripts and cron jobs can tell them apart. Right now every error exits with code 1.

## Bigger features

### 5. Scenes

Requested in [#1](https://github.com/addition-official/roku-smarthome/issues/1). Needs a capture of what my.roku.com sends when a scene is listed and activated (with the cookie removed) before it can be built.

### 6. Home Assistant integration

The [Home Assistant community thread](https://community.home-assistant.io/t/roku-smart-home-integration/477613) is full of people who want this, and there's no working option for Roku Smart Home devices. It would reach the most people of anything here, but it's a separate project (a custom integration installable through HACS) built on top of this library.
