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

### 4. Make the CLI nicer to use - done

- Partial name matching: `on porch` works when only one device has "porch" in its name; ambiguous names list the matches. Also available in Python as `rk.find()`.
- Hex colors: `rgb "Color bulb" "#ff8000"` (or `#f80`), alongside three numbers.
- Distinct exit codes: 1 error, 2 bad arguments, 3 session expired, 4 Roku unavailable.

### 5. Report errors Roku sends back for commands

Commands are fire-and-forget: the background reader discards everything Roku sends on the websocket, so if Roku answers a command with an error, the caller never hears about it. Fixing this needs a capture of what Roku actually sends back after a good and a bad command (the diagnostic script from the 0.2.3 hardware testing prints every message). Then the client can match replies to the command `id` and raise on errors.

## Bigger features

### 6. Scenes

Requested in [#1](https://github.com/addition-official/roku-smarthome/issues/1). Needs a capture of what my.roku.com sends when a scene is listed and activated (with the cookie removed) before it can be built.

### 7. Home Assistant integration

The [Home Assistant community thread](https://community.home-assistant.io/t/roku-smart-home-integration/477613) is full of people who want this, and there's no working option for Roku Smart Home devices. It would reach the most people of anything here, but it's a separate project (a custom integration installable through HACS) built on top of this library.
