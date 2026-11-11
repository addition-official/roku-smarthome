# Roadmap

Ideas for improving roku-smarthome, most useful first.

## Worth doing

### 1. Tests that run on every push - done

`tests/` covers cookie parsing, device parsing, commands, caching, error handling and the CLI, with no network or Roku account needed. `.github/workflows/tests.yml` runs them on every push to `main` and every pull request that touches code. Run them locally with:

```
pip install . pytest
python -m pytest
```

### 2. Reuse one connection for commands - done (needs hardware testing)

The client now keeps one websocket open and reuses it (`persistent=True`, the default). A background reader keeps the connection healthy, it reconnects and resends once if the connection has dropped, and it closes after `idle_timeout` seconds without a command (60 by default) and at interpreter exit. `persistent=False` restores the old one-connection-per-command behavior. Covered by `tests/test_websocket.py` against a local fake server.

Still to check on real devices: that Roku accepts several commands on one connection, and how long it keeps an idle connection open.

### 3. Check color temperature values - done

`set_color_temp` rejects values outside 1800-6500K (`COLOR_TEMP_MIN` / `COLOR_TEMP_MAX` in `client.py`). 1800K is the low end of the color bulbs in this hardware family; the tested white bulb does 2700-6500K. If Roku's device data turns out to include each bulb's own range, use that instead.

### 4. Make the CLI nicer to use

- Partial name matching, so `on lamp` works when only one device has "lamp" in its name (and a clear error listing matches when several do).
- Hex colors, like `rgb "Color bulb" #ff8000`.
- Different exit codes for "session expired" and "Roku is down", so scripts and cron jobs can tell them apart. Right now every error exits with code 1.

## Bigger features

### 5. Scenes

Requested in [#1](https://github.com/addition-official/roku-smarthome/issues/1). Needs a capture of what my.roku.com sends when a scene is listed and activated (with the cookie removed) before it can be built.

### 6. Home Assistant integration

The [Home Assistant community thread](https://community.home-assistant.io/t/roku-smart-home-integration/477613) is full of people who want this, and there's no working option for Roku Smart Home devices. It would reach the most people of anything here, but it's a separate project (a custom integration installable through HACS) built on top of this library.
