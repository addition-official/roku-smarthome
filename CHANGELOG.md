# Changelog

What changed in each release on [PyPI](https://pypi.org/project/roku-smarthome/). Work on `main` that isn't on PyPI yet goes under **Unreleased** at the top.

## Unreleased

- README: the shared connection and live updates are tested on real hardware, not only a fake server.

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
