# Changelog

What changed in each release on [PyPI](https://pypi.org/project/roku-smarthome/). Work on `main` that isn't on PyPI yet goes under **Unreleased** at the top.

## Unreleased

### Fixed
- Devices in a room group couldn't be found or controlled. Roku returns a room as one `group` leaf with its devices nested inside it, and only the top-level list was searched. `get()` and `find()` now look in groups too. An exact name beats a partial one wherever it is, so a device's exact name finds it even when a group's name contains it, and neither guesses between two devices that match the same way.

### Added
- `Device.is_group`, `Device.members` (a group's devices) and `Device.group_id` (the group a device is in). A group's `type` is `"group"` (it was empty).
- `devices(include_members=True)`: each group followed by the devices in it.
- `status` shows each group's devices indented under it.

### Known gap
- Live updates (`on_change`, `watch`) still don't report changes to a device in a group: Roku's socket sends only the group's combined state for it. A follow-up will re-fetch members after a group's report.

## 0.3.2

Fixes from a multi-agent security, edge-case and bug review. Every fix has a regression test that failed before it.

### Fixed
- After a long idle, the first device-list request could fail with "Remote end closed connection without response", because Roku had closed the reused connection (seen on real hardware during a live-updates reconnect). It's now retried once on a fresh connection.
- Live updates could stop for good, without any message, if the connection dropped again while a reconnect was loading the device list.
- Subscribing and unsubscribing on two threads at once could leave a callback registered but never called.
- A server that accepted and then dropped the connection caused a reconnect and a full device-list fetch every ~2 seconds forever. Reconnects now back off to 60 s.
- After `os.fork()`, every call in the child process hung until it timed out.
- Closing the connection (idle timeout or `close()`) could cut off a command still waiting for Roku's reply and report it as failed although it ran.
- A device list fetched during a reconnect could overwrite a newer change and report the old state. A change made while subscribing could be missed.
- One malformed message from Roku stopped the background reader and left its connection open.
- `login` with verification could save the rejected cookie to disk if Roku's response wasn't a device list.
- If saving a renewed cookie failed (e.g. read-only folder), the request failed and the renewal was never saved. Now the request succeeds and the save is retried.
- Two programs saving the cookie file at the same moment could leave it as invalid JSON. In a shared folder, a file planted by another user could expose the cookie.
- Roku deleting the session cookie (logout) was undone.
- Pasted cookies with line breaks or curly quotes broke every request and could print the cookie in error messages; they're now rejected. Whole "Copy as cURL" commands (bash, `$'...'` and Windows cmd quoting) now work.
- Lookups: numeric ids, names with surrounding spaces, `find("")`, and non-text names from Roku.
- CLI: `watch | head` never exited; `status | head` and Ctrl-C printed tracebacks; `login` with closed input crashed; a terminal that can't show emoji turned a successful command into an error; device names could send escape sequences to the terminal; `watch --seconds` accepted invalid values; a change could print above `watch`'s header.
- The source package's tests couldn't run (missing `conftest.py`).

### Added
- The session cookie is only ever sent to Roku over TLS (`wss://` on roku.com) or to localhost; any other `socket_url` is refused.
- `rk.live_error`: why live updates are retrying (e.g. `SessionExpired`), or `None` while connected. `watch` exits with code 3 when the login expires.
- CLI exit code 130 for Ctrl-C.

### Changed
- Registering the same `on_change` callback twice no longer calls it twice per change.
- On Windows, saving the cookie file retries briefly if another program has it open.
- Callbacks fire only when a device's visible state changes, not for reports that only update Roku's internal timestamps.
- The `login` prompt doesn't show the pasted cookie. Hints no longer suggest putting the cookie on the command line.
- The `websockets` library's debug logs no longer include the session cookie.
- Releases run the full test suite and must be tagged on `main`. Tests run on Python 3.10 to 3.14. Building requires setuptools 77+.

## 0.3.1

- README: the shared connection and live updates are tested on real hardware, not only a fake server.
- GitHub Actions updated to versions that run on Node 24 (Node 20 is being retired). No code changes.

## 0.3.0

### Added
- Commands reuse one connection to Roku, so several in a row go out almost instantly. The connection closes after 60 seconds without a command, on `close()`, at the end of a `with` block, and when the program exits. `persistent=False` restores one connection per command.
- The CLI accepts any unique part of a device name (`roku-smarthome on porch`). In Python, use `rk.find("porch")`.
- `rgb` accepts hex colors: `roku-smarthome rgb "Color bulb" "#ff8000"`.
- Separate CLI exit codes: 1 error, 2 bad arguments, 3 session expired, 4 Roku unavailable.
- `login` accepts a whole "Copy as cURL" line or a `Cookie:` prefix.
- Live updates: `rk.on_change(callback)` is called whenever a device changes, from anywhere (the Roku app, voice assistants, the device's button). `roku-smarthome watch` prints changes as they happen.
- Commands wait for Roku's reply and raise the new `CommandFailed` if Roku reports an error (CLI exit code 5). A missing reply raises `RokuUnavailable`. `confirm=False` skips the wait.

### Fixed
- A bad or empty paste at `login` overwrote the saved session. The cookie is now checked against Roku before it's saved.
- If Roku renewed the session cookie without a `Domain=` attribute, the old and new copies were both sent.
- Commands crashed with `websockets` 13.
- An expired login could be reported as "Roku unavailable".
- Crashes on null fields in Roku's data, a corrupt cookie file, HTTP 400/404/429, 303/307/308 redirects, or a device list in the wrong format. These now raise `SessionExpired` or `RokuUnavailable`.
- With `persistent=False`, Roku closing the connection after a delivered command raised an error.
- `get()` picked the first of several devices with the same name; it now refuses and lists their ids.
- Brightness, color temperature and RGB rounded non-whole numbers and accepted booleans; they're now rejected.
- Color temperatures outside 1800-6500K were sent to the bulb; they're now rejected.
- Editing the list returned by `raw_devices()` changed the cache.
- The cookie file was briefly readable by other users while being written.

### Changed
- Requires Python 3.10+ (3.9 is end of life) and `websockets` 14+.
- `import_cookie_header()` raises `ValueError` if there's no `ks.session` cookie in the header.

## 0.2.2

- `from roku_smarthome import *` works (`RokuUnavailable` was missing).
- README: RGB color confirmed working on a color bulb ([#1](https://github.com/addition-official/roku-smarthome/issues/1)).
- Releases are published to PyPI automatically from GitHub.

## 0.2.1

- Fixed `login` saving no cookies when the pasted header contained a bare token like `roku_test;`.

## 0.2.0 and earlier

Initial releases.
