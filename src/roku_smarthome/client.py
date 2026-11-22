"""
Unofficial client for Roku Smart Home devices.

Talks to the same endpoints that https://my.roku.com/smarthome uses:
  - GET  https://my.roku.com/smarthome/api/v1/leaves          (device list + state)
  - WSS  wss://aspen-sockets.aspen.msc.roku.com/v1/socket    (commands)

Authentication is the browser session cookie. Roku re-issues the session
cookie with a fresh one-year expiry on every request, so as long as this
client is used at least once a year the login never expires.
"""

import asyncio
import atexit
import copy
import json
import logging
import operator
import re
import tempfile
import os
import threading
import time
import uuid
import weakref
from urllib.parse import urlsplit
from pathlib import Path
from typing import Callable, Dict, List, Optional
import concurrent.futures
import requests
import websockets.exceptions
# The new asyncio client. On websockets 13 the top-level websockets.connect
# is still the legacy client, which does not accept additional_headers.
from websockets.asyncio.client import connect as ws_connect
from websockets.protocol import State

log = logging.getLogger(__name__)


class _RedactCookies(logging.Filter):
    """websockets logs every handshake header at DEBUG level, the Cookie
    header included. Keep the session out of logs."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "ookie" in msg:
            record.msg = re.sub(r"(?i)(cookie:\s*)\S.*", r"\1<redacted>", msg)
            record.args = ()
        return True


_ws_log = logging.getLogger("roku_smarthome.websockets")
_ws_log.addFilter(_RedactCookies())

LEAVES_URL = "https://my.roku.com/smarthome/api/v1/leaves"
SOCKET_URL = "wss://aspen-sockets.aspen.msc.roku.com/v1/socket"
ORIGIN = "https://my.roku.com"

DEFAULT_COOKIE_FILE = Path(
    os.environ.get("ROKU_SMARTHOME_COOKIES")
    or Path.home() / ".config" / "roku-smarthome" / "cookies.json"
)

# Browser-side noise that the API does not need. Dropping these avoids
# sending a stale Cloudflare cookie.
_SKIP = {
    "__cf_bm", "_cflb", "_csrf", "_uc", "_usn", "amoeba", "BVBRANDID",
    "ks.locale", "ks.privacy.ccpa", "x-amz-continuous-deployment-state",
}

# White color temperature range accepted by set_color_temp. The tested white
# bulb does 2700-6500K; the color bulbs in this hardware family go down to 1800K.
COLOR_TEMP_MIN = 1800
COLOR_TEMP_MAX = 6500

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _whole_number(value, what: str) -> int:
    """Accept ints (and floats like 50.0); reject 50.9, True, "50" and the like
    instead of silently rounding them."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a whole number, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{what} must be a whole number, got {value!r}")
        return int(value)
    try:
        return operator.index(value)
    except TypeError:
        raise ValueError(f"{what} must be a whole number, got {value!r}") from None


_CURL_COOKIE_HEADER = re.compile(
    r"""(?:-H|--header)\s+\$?(['"])\s*cookie:\s*(.*?)\1""", re.IGNORECASE | re.DOTALL)
_CURL_COOKIE_FLAG = re.compile(
    r"""(?:^|\s)(?:-b|--cookie)\s+\$?(['"])(.*?)\1""", re.DOTALL)


def _clean_cookie_header(header: str) -> str:
    """Strip what tends to come along when the header is copied from DevTools
    or a "Copy as cURL" line: -H/-b flags, quotes, a 'Cookie:' prefix and the
    trailing line-continuation backslash. From a whole cURL command, only the
    cookie header is kept. Handles bash ($'...') and Windows cmd (^"...^")
    quoting."""
    h = header.strip()
    if '^"' in h:  # "Copy as cURL (cmd)": ^ escapes every special character
        h = re.sub(r"\^(.)", r"\1", h)
    m = _CURL_COOKIE_HEADER.search(h) or _CURL_COOKIE_FLAG.search(h)
    if m:
        return m.group(2).strip()
    h = h.rstrip("\\").strip()
    for flag in ("--header", "--cookie", "-H", "-b"):
        if h.startswith(flag + " "):
            h = h[len(flag):].strip()
            break
    if len(h) >= 2 and h[0] == h[-1] and h[0] in "'\"":
        h = h[1:-1].strip()
    if h.lower().startswith("cookie:"):
        h = h[len("cookie:"):].strip()
    return h


# what a browser can put in a Cookie header: printable ASCII only
_COOKIE_TEXT = re.compile(r"^[\x20-\x7e]*$")


def parse_cookie_header(header: str) -> Dict[str, str]:
    """Parse a Cookie header by hand: SimpleCookie silently returns nothing if
    the header contains a bare token without '=' (browsers emit these, e.g.
    'roku_test;'). If a name appears twice, the first one wins, as browsers
    list the most specific cookie first.

    Raises ValueError (without echoing the cookie) for characters a browser
    never sends, such as line breaks or smart quotes from a chat or document;
    they would break every request or leak the cookie into error messages."""
    cookies: Dict[str, str] = {}
    for part in _clean_cookie_header(header).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if not (_COOKIE_TEXT.match(k) and _COOKIE_TEXT.match(v)):
            raise ValueError(
                f"the pasted cookie {k!r} contains characters a browser wouldn't "
                "send (a line break, or a curly quote from a chat or document). "
                "Copy the Cookie header straight from the browser's DevTools.")
        cookies.setdefault(k, v)
    return cookies


def _leaves(items) -> List[dict]:
    """The usable entries of a leaf list from Roku (dicts with an id)."""
    return [r for r in (items if isinstance(items, list) else [])
            if isinstance(r, dict) and r.get("id") is not None]


def _flatten_leaves(leaves) -> List[tuple]:
    """Roku's device list with each group's members right after the group, as
    (raw, group_id) pairs. A room group comes back from Roku as one leaf of type
    "group" with its devices nested in "memberLeaves"; they are real devices
    with their own ids, state and commands. Roku's app puts each device in one
    room, so a device normally appears once. If one ever appears again (also at
    the top level, or in a second group), it is listed only where it first
    appears, top-level entries first, so nothing is lost or listed twice."""
    top = _leaves(leaves)
    seen = {_text(r["id"]) for r in top}
    out: List[tuple] = []

    def add_members(group):
        if group.get("type") != "group":
            return
        for m in _leaves(group.get("memberLeaves")):
            if _text(m["id"]) not in seen:
                seen.add(_text(m["id"]))
                out.append((m, _text(group["id"])))
                add_members(m)

    for raw in top:
        out.append((raw, None))
        add_members(raw)
    return out


def _socket_url_allowed(url: str) -> bool:
    """The session cookie is only ever sent to Roku over TLS, or to this
    machine (for testing against a local fake server)."""
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if u.scheme == "wss" and (host == "roku.com" or host.endswith(".roku.com")):
        return True
    return u.scheme in ("ws", "wss") and host in ("localhost", "127.0.0.1", "::1")


# Windows: another program (antivirus, search indexer, an editor) briefly
# holding the cookie file open makes os.replace fail with PermissionError.
_RETRY_REPLACE = os.name == "nt"


def _replace(src, dst):
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _RETRY_REPLACE or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _text(value) -> str:
    """Roku's JSON may carry a number or null where a string is expected."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


class SessionExpired(Exception):
    """The saved Roku session no longer works. Run `roku-smarthome login` again."""


class RokuUnavailable(Exception):
    """Roku's API could not be reached or returned a server error.
    The session is probably still fine; retry later."""


class UnsupportedCommand(Exception):
    """The device does not advertise this command."""


class DeviceNotFound(Exception):
    """No device on the account matches that name or id."""


class CommandFailed(Exception):
    """Roku received the command and reported that it failed, e.g. "failed to
    execute command" for a bad value or "forbidden request" for a device that
    isn't on the account. `reply` holds Roku's message."""

    def __init__(self, message: str, reply: Optional[dict] = None):
        super().__init__(message)
        self.reply = reply


class Device:
    """One Roku Smart Home device ("leaf" in Roku's API)."""

    def __init__(self, client: "RokuSmartHome", raw: dict,
                 group_id: Optional[str] = None):
        self._client = client
        #: id of the group this device is a member of, or None
        self.group_id: Optional[str] = group_id
        self._load(raw)

    def _load(self, raw: dict):
        # Roku's JSON can have nulls anywhere; treat a null like a missing key.
        def obj(parent, key) -> dict:
            v = parent.get(key)
            return v if isinstance(v, dict) else {}

        self.raw = raw
        self.id: str = _text(raw["id"])
        self.name: str = _text(raw.get("name"))
        hw = obj(raw, "device")
        self._leaf_type: str = _text(raw.get("type")) or "device"
        self.type: str = _text(raw.get("subType") or hw.get("type")) or (
            "group" if self._leaf_type == "group" else "")
        self.model: str = _text(hw.get("model"))
        self.local_ip: str = _text(hw.get("localIp"))
        commands = raw.get("supportedCommands")
        self.supported_commands = {
            c.get("type") for c in (commands if isinstance(commands, list) else [])
            if isinstance(c, dict) and c.get("type")
        }
        state = obj(raw, "state")
        self.power: Optional[str] = obj(state, "power").get("power")
        self.online: Optional[bool] = obj(state, "online").get("online")
        self.brightness: Optional[int] = obj(state, "brightness").get("level")
        color = obj(state, "color")
        self.color_temp: Optional[int] = color.get("temperature")
        self.color_type: Optional[str] = color.get("colorType")
        self.color_rgb: Optional[list] = color.get("rgb")
        self._members: Optional[List["Device"]] = None

    @property
    def members(self) -> List["Device"]:
        """For a group, the devices in it (each with group_id set), as of when
        the group was fetched; otherwise empty."""
        if self._members is None:
            nested = self.raw.get("memberLeaves") if self.is_group else None
            self._members = [Device(self._client, m, group_id=self.id) for m in _leaves(nested)]
        return self._members

    @property
    def is_group(self) -> bool:
        """A Roku room group. It takes the same commands as a device (they go
        to every member) and reports its members' combined state."""
        return self._leaf_type == "group"

    # ----- state -----

    def refresh(self) -> "Device":
        """Re-fetch this device's state from Roku, bypassing the cache."""
        fresh = self._client.get(self.id, by_id=True, max_age=0)
        self.group_id = fresh.group_id
        self._load(fresh.raw)
        return self

    @property
    def is_on(self) -> bool:
        return self.power == "on"

    # ----- commands -----

    def send(self, command: str, parameters: dict, wait: Optional[float] = None,
             confirm: Optional[bool] = None) -> Optional[dict]:
        """Send any command. Raises UnsupportedCommand if the device doesn't list it.
        With confirmation on (the client default), waits for Roku's reply and
        raises CommandFailed if Roku reports an error; returns the reply."""
        if command not in self.supported_commands:
            raise UnsupportedCommand(
                f"{self.name!r} does not support {command!r} "
                f"(supports: {sorted(self.supported_commands)})"
            )
        return self._client._send_command(self, command, parameters, wait=wait,
                                          confirm=confirm)

    def turn_on(self):
        self.send("power", {"power": "on"})
        self.power = "on"

    def turn_off(self):
        self.send("power", {"power": "off"})
        self.power = "off"

    def toggle(self):
        """Flip power based on the state this object last saw. If something
        else may have changed the device since, call refresh() first."""
        self.turn_off() if self.is_on else self.turn_on()

    def set_brightness(self, level: int):
        level = _whole_number(level, "brightness")
        if not 0 <= level <= 100:
            raise ValueError("brightness must be 0..100")
        self.send("brightness", {"level": level})
        self.brightness = level

    def set_color_temp(self, kelvin: int):
        kelvin = _whole_number(kelvin, "color temperature")
        if not COLOR_TEMP_MIN <= kelvin <= COLOR_TEMP_MAX:
            raise ValueError(
                f"color temperature must be {COLOR_TEMP_MIN}..{COLOR_TEMP_MAX} K")
        self.send("color", {"colorType": "temperature", "temperature": kelvin})
        self.color_temp = kelvin
        self.color_type = "temperature"

    def set_color_rgb(self, r: int, g: int, b: int):
        rgb = [_whole_number(v, "rgb value") for v in (r, g, b)]
        if any(not 0 <= v <= 255 for v in rgb):
            raise ValueError("rgb values must be 0..255")
        self.send("color", {"colorType": "rgb", "rgb": rgb})
        self.color_type = "rgb"
        self.color_rgb = rgb

    def __repr__(self):
        return f"<Device {self.name!r} type={self.type} power={self.power} online={self.online}>"


class RokuSmartHome:
    """
    cookie_file   where the session cookies live (default ~/.config/roku-smarthome/cookies.json)
    timeout       seconds for the REST call and the websocket open
    cache_ttl     default max age (seconds) for the device list. 0 = always fetch.
                  A hub polling from several threads should set this to a few seconds
                  so they all share one request.
    persistent    keep one websocket open and reuse it for every command (default).
                  Much faster when sending several commands in a row. If the
                  connection has dropped, it reconnects and resends once.
                  False opens a new connection per command (the pre-0.3.0 behavior).
    idle_timeout  with persistent=True, close the websocket after this many seconds
                  without a command; the next command reopens it. 0 = never.
    confirm       wait for Roku's reply to each command and raise CommandFailed if
                  it reports an error (default). False sends without waiting, a
                  little faster for rapid-fire commands like a slider.
    reply_timeout with confirm=True, seconds to wait for Roku's reply before raising
                  RokuUnavailable. Replies usually take 0.1 to 2 seconds.
    command_wait  seconds a connection must stay open after the last command
                  before it is closed. Roku drops a command if the connection
                  closes right after it, so keep this around 0.5.

    Call close() (or use `with RokuSmartHome() as rk:`) when you are done. It is
    also called automatically when the interpreter exits.

    For live updates, register a callback with on_change(); see its docstring.
    """

    def __init__(self, cookie_file: Optional[os.PathLike] = None, *,
                 timeout: float = 10.0, cache_ttl: float = 0.0,
                 command_wait: float = 0.5, persistent: bool = True,
                 idle_timeout: float = 60.0, confirm: bool = True,
                 reply_timeout: float = 5.0):
        self.cookie_file = Path(cookie_file) if cookie_file else DEFAULT_COOKIE_FILE
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)
        self.command_wait = float(command_wait)
        self.persistent = bool(persistent)
        self.idle_timeout = float(idle_timeout)
        self.confirm = bool(confirm)
        self.reply_timeout = float(reply_timeout)
        self.socket_url = SOCKET_URL

        self._cache_lock = threading.Lock()
        self._cache_raw: Optional[List[dict]] = None
        self._cache_ts: float = 0.0

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()

        # persistent websocket state; only touched from the event loop thread
        self._ws = None
        self._ws_lock: Optional[asyncio.Lock] = None
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._connects = 0  # connections opened, for tests and debugging
        self._last_send = 0.0  # loop time of the last command on self._ws
        self._pending: Dict[str, tuple] = {}  # command id -> (websocket, Future)

        # live updates (on_change)
        self._listeners: List[Callable[["Device"], None]] = []
        self._listeners_lock = threading.Lock()
        # serializes subscribe/unsubscribe/close so starting and stopping
        # live updates can't interleave between threads
        self._transition_lock = threading.Lock()
        self._listening = False             # touched on the event loop thread
        self._live_error: Optional[Exception] = None
        self._reconnect_delay = 1.0
        self._seeding = False
        self._seed_buffer: List[dict] = []
        self._pid = os.getpid()
        self._live: Dict[str, dict] = {}    # leaf id -> raw device, kept current
        self._live_alias: Dict[str, str] = {}  # device.id -> leaf id
        self._reconnect_task: Optional[asyncio.Task] = None
        self._events: Optional[concurrent.futures.ThreadPoolExecutor] = None

        atexit.register(_close_at_exit, weakref.ref(self))

        self._load_session()

    # ----- cookie handling -----

    def _load_session(self):
        self._cookie_error: Optional[str] = None
        self._cookies_dirty = False  # in memory, not yet saved (a save failed)
        self._use_cookies(self._read_cookie_file())

    def _use_cookies(self, cookies: Dict[str, str]):
        self._cookies: Dict[str, str] = dict(cookies)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _UA
        self._sync_jar()

    def _sync_jar(self):
        """Make the session hold exactly one copy of each saved cookie. Roku can
        renew a cookie without a Domain attribute, which would otherwise leave
        an old .roku.com copy next to the new my.roku.com one, both sent."""
        jar = self._session.cookies
        for c in list(jar):
            if c.name not in _SKIP:
                jar.clear(c.domain, c.path, c.name)
        for k, v in self._cookies.items():
            if k not in _SKIP:
                jar.set(k, v, domain=".roku.com", path="/")

    def _read_cookie_file(self) -> Dict[str, str]:
        if not self.cookie_file.exists():
            return {}
        try:
            data = json.loads(self.cookie_file.read_text())
        except (OSError, ValueError) as e:
            self._cookie_error = f"Cookie file {self.cookie_file} is unreadable ({e})."
            return {}
        if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            self._cookie_error = f"Cookie file {self.cookie_file} is not in the expected format."
            return {}
        return data

    def _write_cookie_file(self):
        # Write a new private temp file and swap it in. mkstemp creates a file
        # with a unique random name, mode 0600, and refuses to follow an
        # existing file or symlink, so another user can't pre-plant it, two
        # processes saving at once can't write into each other's temp file,
        # and a crash mid-write can't leave the real file half-written.
        path = self.cookie_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(self._cookies, indent=2))
            _replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._cookies_dirty = False

    def import_cookie_header(self, header: str, verify: bool = False) -> int:
        """Store a `Cookie:` header value copied from the browser (a "Copy as cURL"
        line or a 'Cookie:' prefix is fine too). Returns the cookie count.

        Raises ValueError if there is no ks.session cookie in it. With verify=True,
        the new cookies are checked against Roku first; if that fails, the
        previously saved session is kept and the error is raised."""
        cookies = parse_cookie_header(header)
        if "ks.session" not in cookies:
            raise ValueError(
                "no ks.session cookie found. Copy the whole Cookie header from the "
                "'leaves' request on my.roku.com/smarthome")
        if not cookies["ks.session"]:
            raise ValueError("the ks.session cookie is empty; copy the Cookie header again")
        old_cookies, old_error = self._cookies, self._cookie_error
        self._cookie_error = None
        self._use_cookies(cookies)
        self.invalidate()
        try:
            if verify:
                self._verifying = True  # don't save renewed cookies until verified
                try:
                    self.raw_devices(max_age=0)
                finally:
                    self._verifying = False
            self._write_cookie_file()
        except BaseException:
            # keep the previous login, in memory and on disk
            self._cookie_error = old_error
            self._use_cookies(old_cookies)
            self.invalidate()
            raise
        return len(self._cookies)

    def _persist_refreshed_cookies(self):
        """Save cookies Roku renewed (or deleted) in this response. A failed
        save is logged, not raised: the request itself worked, and the save is
        retried on the next request."""
        changed = self._cookies_dirty
        in_jar = set()
        for c in self._session.cookies:
            if c.name in _SKIP:
                continue
            in_jar.add(c.name)
            if self._cookies.get(c.name) != c.value:
                self._cookies[c.name] = c.value
                changed = True
        # A cookie we set in the jar is only missing if Roku deleted it
        # (e.g. Max-Age=0 on logout); don't put the dead value back.
        for name in [k for k in self._cookies if k not in _SKIP and k not in in_jar]:
            del self._cookies[name]
            changed = True
        self._sync_jar()
        if changed:
            if getattr(self, "_verifying", False):
                self._cookies_dirty = True  # import_cookie_header saves after verifying
                return
            try:
                self._write_cookie_file()
            except OSError as e:
                self._cookies_dirty = True
                log.warning("roku-smarthome: could not save renewed login to %s (%s); "
                            "will retry", self.cookie_file, e.strerror or e)

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items() if k not in _SKIP)

    @property
    def has_session(self) -> bool:
        return "ks.session" in self._cookies

    # ----- devices -----

    def invalidate(self):
        """Drop the cached device list so the next call fetches fresh."""
        with self._cache_lock:
            self._cache_raw = None
            self._cache_ts = 0.0

    def _fetch_leaves(self) -> List[dict]:
        if not self.has_session:
            reason = self._cookie_error or f"No session saved at {self.cookie_file}."
            raise SessionExpired(f"{reason} Run: roku-smarthome login")
        try:
            for attempt in (1, 2):
                try:
                    r = self._session.get(
                        LEAVES_URL,
                        headers={"x-request-id": str(uuid.uuid4())},
                        allow_redirects=False,
                        timeout=self.timeout,
                    )
                    break
                except requests.ConnectionError as e:
                    # After a long idle, the reused keep-alive connection may
                    # already be closed by Roku ("Remote end closed connection
                    # without response"). One retry gets a fresh connection.
                    # Timeouts aren't retried: that would only double the wait.
                    if attempt == 2 or isinstance(e, requests.Timeout):
                        raise
                    log.debug("roku-smarthome: retrying device list on a fresh connection (%s)", e)
        except requests.RequestException as e:
            raise RokuUnavailable(f"could not reach Roku: {e}") from e
        except (ValueError, UnicodeError) as e:
            # a header http.client refuses; its message would include the
            # cookie, so don't repeat it
            raise SessionExpired(
                "the saved cookie can't be sent (it contains invalid characters). "
                "Run: roku-smarthome login") from None

        code = r.status_code
        # a redirect means "go log in"
        if 300 <= code < 400 or code in (401, 403):
            raise SessionExpired(
                f"Roku rejected the saved session (HTTP {code}). Run: roku-smarthome login"
            )
        if code == 429:
            raise RokuUnavailable("Roku is rate limiting requests (HTTP 429); slow down and retry")
        if code >= 400:
            raise RokuUnavailable(f"Roku returned HTTP {code}")
        try:
            data = r.json()
        except ValueError as e:
            raise RokuUnavailable("Roku returned a non-JSON device list") from e
        if not isinstance(data, list):
            raise RokuUnavailable(
                f"Roku returned an unexpected device list ({type(data).__name__}, not a list)")
        # only now: a response that isn't a real device list (e.g. an error
        # page) must not get its cookies saved
        self._persist_refreshed_cookies()
        return data

    def raw_devices(self, max_age: Optional[float] = None) -> List[dict]:
        """Device list as raw JSON. Served from cache if it is younger than max_age
        seconds (default: self.cache_ttl). Pass max_age=0 to force a fetch."""
        ttl = self.cache_ttl if max_age is None else float(max_age)
        with self._cache_lock:
            if (self._cache_raw is None or ttl <= 0
                    or time.monotonic() - self._cache_ts > ttl):
                self._cache_raw = self._fetch_leaves()
                self._cache_ts = time.monotonic()
            # a deep copy, so callers editing the result can't change the cache
            return copy.deepcopy(self._cache_raw)

    def devices(self, max_age: Optional[float] = None,
                include_members: bool = False) -> List[Device]:
        """The devices on the account as Roku lists them: a room group is one
        entry, with its devices in `group.members`. With include_members=True,
        each group is followed by the devices in it (each with `group_id` set)."""
        raw = self.raw_devices(max_age)
        if not include_members:
            return [Device(self, r) for r in _leaves(raw)]
        return [Device(self, r, group_id=g) for r, g in _flatten_leaves(raw)]

    def _lookup_pools(self, max_age: Optional[float]) -> List[List[Device]]:
        """Where get() and find() look, in order: the top-level entries, then
        the devices in groups."""
        devices = self.devices(max_age, include_members=True)
        return [[d for d in devices if d.group_id is None],
                [d for d in devices if d.group_id is not None]]

    def get(self, name_or_id: str, by_id: bool = False,
            max_age: Optional[float] = None) -> Device:
        """Exact lookup by name (any case) or, with by_id=True, by id. Raises
        DeviceNotFound if nothing matches, or if several devices share the name."""
        wanted = _text(name_or_id).strip()
        key = wanted.lower()
        for pool in self._lookup_pools(max_age):
            matches = [d for d in pool
                       if (d.id == wanted if by_id else d.name.strip().lower() == key)]
            if matches:
                break
        if len(matches) == 1:
            return matches[0]
        if matches and by_id:
            raise DeviceNotFound(f"several devices have the id {wanted!r}")
        if matches:
            ids = ", ".join(d.id for d in matches)
            raise DeviceNotFound(
                f"several devices are named {wanted!r} (ids: {ids}); use get(id, by_id=True)")
        raise DeviceNotFound(wanted)

    def find(self, query: str, max_age: Optional[float] = None) -> Device:
        """Forgiving lookup for people typing names: an exact name (any case),
        then a device id, then any device whose name contains `query`, as long
        as only one does. Each test looks at the top-level entries before the
        devices in groups. Raises DeviceNotFound listing the candidates."""
        pools = self._lookup_pools(max_age)
        query = _text(query).strip()
        if not query:
            raise DeviceNotFound("no device name given")
        q = query.lower()
        tests = (lambda d: d.name.strip().lower() == q,
                 lambda d: d.id == query,
                 lambda d: q in d.name.lower())
        for test in tests:
            for pool in pools:
                match = [d for d in pool if test(d)]
                if len(match) == 1:
                    return match[0]
                if len(match) > 1:
                    names = ", ".join(repr(d.name) for d in match)
                    raise DeviceNotFound(f"{query!r} matches several devices: {names}")
        names = ", ".join(repr(d.name) for d in pools[0] + pools[1]) or "none"
        raise DeviceNotFound(f"no device matches {query!r} (devices: {names})")

    # ----- websocket loop (one daemon thread, safe to call from anywhere) -----

    def _check_fork(self):
        """After os.fork() the child has this object but not the event loop
        thread (or other threads holding locks). Start over cleanly there
        instead of waiting on a loop nobody runs."""
        if self._pid == os.getpid():
            return
        self._pid = os.getpid()
        self._loop = None
        self._loop_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._listeners_lock = threading.Lock()
        self._transition_lock = threading.Lock()
        self._listeners = []
        self._listening = False
        self._live, self._live_alias = {}, {}
        self._reconnect_task = None
        self._events = None
        self._ws = None
        self._ws_lock = None
        self._idle_handle = None
        self._pending = {}

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        self._check_fork()
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                t = threading.Thread(target=loop.run_forever,
                                     name="roku-smarthome-ws", daemon=True)
                t.start()
                self._loop = loop
            return self._loop

    def _run(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._get_loop())
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise RokuUnavailable("websocket command timed out") from e

    # ----- commands -----

    def _send_command(self, device: Device, command: str, parameters: dict,
                      wait: Optional[float] = None,
                      confirm: Optional[bool] = None) -> Optional[dict]:
        msg = {
            "id": str(uuid.uuid4()),
            "type": "send_command",
            "payload": {
                "leafId": device.id,
                "leafType": device._leaf_type,
                "command": {"command": command, "parameters": parameters},
            },
        }
        confirm = self.confirm if confirm is None else bool(confirm)
        reply_wait = self.reply_timeout if confirm else 0.0
        if self.persistent:
            # worst case: close a dead connection, open, a failed send, close,
            # open again, then wait for the reply
            reply = self._run(self._ws_send_persistent(msg, confirm),
                              timeout=4 * self.timeout + reply_wait + 2)
        else:
            w = self.command_wait if wait is None else float(wait)
            # open, send and wait, then the close handshake
            reply = self._run(self._ws_send_once(msg, w, confirm),
                              timeout=2 * self.timeout + max(w, reply_wait) + 2)
        # state may have changed on Roku's side; don't serve the stale list again
        self.invalidate()
        if reply is not None and reply.get("error"):
            raise CommandFailed(
                f"Roku could not run {command!r} on {device.name or device.id!r}: "
                f"{reply['error']}", reply)
        return reply

    @staticmethod
    def _reply_to(raw, command_id: str) -> Optional[dict]:
        """Roku's reply to command `command_id` if `raw` is one: send_command_resp
        on success, or send_command with an "error" field on failure."""
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if (isinstance(m, dict) and m.get("id") == command_id
                and m.get("type") in ("send_command_resp", "send_command")):
            return m
        return None

    def _no_reply(self) -> RokuUnavailable:
        return RokuUnavailable(
            f"Roku did not confirm the command within {self.reply_timeout:g} s; "
            "it may or may not have been applied")

    # ----- websocket plumbing (runs on the event loop thread) -----

    async def _ws_open(self):
        if not _socket_url_allowed(self.socket_url):
            raise ValueError(
                f"refusing to send the Roku session to {self.socket_url!r}: "
                "socket_url must be a wss:// address on roku.com (or localhost)")
        headers = {"Cookie": self._cookie_header(), "Origin": ORIGIN}
        try:
            ws = await ws_connect(self.socket_url, additional_headers=headers,
                                  open_timeout=self.timeout, logger=_ws_log)
        except websockets.exceptions.InvalidStatus as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 403):
                raise SessionExpired(
                    f"Roku rejected the session on the websocket (HTTP {code}). "
                    "Run: roku-smarthome login"
                ) from e
            raise RokuUnavailable(f"websocket handshake failed (HTTP {code})") from e
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise RokuUnavailable(f"websocket error: {e}") from e
        self._connects += 1
        return ws

    async def _ws_send_once(self, msg: dict, wait: float, confirm: bool):
        """Open a connection, send one command, then either wait for Roku's
        reply (confirm) or linger `wait` seconds so Roku processes it; close."""
        ws = await self._ws_open()
        try:
            try:
                await ws.send(json.dumps(msg))
            except (OSError, websockets.exceptions.WebSocketException) as e:
                raise RokuUnavailable(f"websocket error: {e}") from e
            # Server chats on connect (init / stream_init / leaves); skip that.
            loop = asyncio.get_running_loop()
            sent_at = loop.time()
            deadline = sent_at + (self.reply_timeout if confirm else wait)
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except (OSError, websockets.exceptions.WebSocketException) as e:
                    if confirm:
                        raise RokuUnavailable(
                            "connection closed before Roku confirmed the command; "
                            "it may or may not have been applied") from e
                    # Unconfirmed: the command was already delivered, so a close
                    # by Roku isn't a failure (a retry would send it twice).
                    break
                if confirm:
                    reply = self._reply_to(raw, msg["id"])
                    if reply is not None:
                        # still give the device the full wait before closing
                        linger = sent_at + wait - loop.time()
                        if linger > 0:
                            await asyncio.sleep(linger)
                        return reply
            if confirm:
                raise self._no_reply()
            return None
        finally:
            await ws.close()

    def _lock(self) -> asyncio.Lock:
        if self._ws_lock is None:
            self._ws_lock = asyncio.Lock()
        return self._ws_lock

    async def _ws_send_persistent(self, msg: dict, confirm: bool) -> Optional[dict]:
        """Send over the shared connection, opening it if needed. If the
        connection turns out to be dead, reconnect and resend once. With
        confirm, then wait (outside the lock) for Roku's reply."""
        data = json.dumps(msg)
        cid = msg["id"]
        loop = asyncio.get_running_loop()
        fut = loop.create_future() if confirm else None
        try:
            async with self._lock():
                for attempt in (1, 2):
                    fresh = self._ws is None or self._ws.state is not State.OPEN
                    if fresh:
                        await self._ws_drop()
                        self._ws = await self._ws_open()
                        loop.create_task(self._ws_reader(self._ws))
                    if fut is not None:
                        self._pending[cid] = (self._ws, fut)
                    try:
                        await self._ws.send(data)
                        # Even a confirmed command gets the full wait before a
                        # close: Roku's reply can arrive before the device acts.
                        self._last_send = loop.time()
                        break
                    except (OSError, websockets.exceptions.ConnectionClosed) as e:
                        self._pending.pop(cid, None)
                        await self._ws_drop()
                        if fresh or attempt == 2:
                            raise RokuUnavailable(f"websocket error: {e}") from e
                self._arm_idle_timer()
            if fut is None:
                return None
            try:
                return await asyncio.wait_for(fut, timeout=self.reply_timeout)
            except asyncio.TimeoutError:
                raise self._no_reply() from None
        finally:
            self._pending.pop(cid, None)

    async def _ws_reader(self, ws):
        # Keep reading so the server's chatter can't fill the receive buffer
        # (a full buffer stops ping/pong processing and the connection dies),
        # and hand Roku's replies to the commands waiting for them.
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async for raw in ws:
                try:
                    self._dispatch(raw)
                except Exception:
                    # one odd message must not stop the reader
                    log.debug("roku-smarthome: ignored a message it couldn't handle",
                              exc_info=True)
        except Exception:
            pass
        try:  # make sure it's really closed, whatever ended the loop
            await asyncio.wait_for(ws.close(), timeout=self.timeout)
        except Exception:
            pass
        was_current = self._ws is ws  # not replaced or dropped on purpose
        if was_current:
            self._ws = None
        for cid, (owner, fut) in list(self._pending.items()):
            if owner is ws and not fut.done():
                fut.set_exception(RokuUnavailable(
                    "connection closed before Roku confirmed the command; "
                    "it may or may not have been applied"))
        if loop.time() - started >= 60:
            self._reconnect_delay = 1.0  # it was healthy; reconnect quickly
        if (self._listening and was_current
                and (self._reconnect_task is None or self._reconnect_task.done())):
            self._reconnect_task = loop.create_task(self._reconnect())

    def _dispatch(self, raw):
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(m, dict):
            return
        if m.get("type") == "leaf_state_report":
            self._apply_state_report(m.get("payload"))
            return
        cid = m.get("id")
        if not isinstance(cid, str):
            return
        entry = self._pending.get(cid)
        if entry is not None and m.get("type") in ("send_command_resp", "send_command"):
            fut = entry[1]
            if not fut.done():
                fut.set_result(m)

    def _arm_idle_timer(self):
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self.idle_timeout > 0 and not self._listening:
            loop = asyncio.get_running_loop()
            self._idle_handle = loop.call_later(
                self.idle_timeout, lambda: loop.create_task(self._ws_close_locked()))

    async def _ws_drop(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=self.timeout)
            except Exception:
                pass

    async def _ws_close_locked(self, force: bool = False):
        async with self._lock():
            if self._listening and not force:
                return  # an idle close queued just before live updates started
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                self._idle_handle = None
            ws = self._ws
            if ws is not None:
                # Let commands still waiting for Roku's reply get it...
                waiting = [f for owner, f in self._pending.values()
                           if owner is ws and not f.done()]
                if waiting:
                    await asyncio.wait(waiting, timeout=self.reply_timeout)
                # ...and Roku drops a command if the connection closes right
                # after it, so give the last one time to be processed.
                linger = self._last_send + self.command_wait - asyncio.get_running_loop().time()
                if linger > 0:
                    await asyncio.sleep(linger)
            await self._ws_drop()

    # ----- live updates -----

    def on_change(self, callback: Callable[["Device"], None]) -> Callable[[], None]:
        """Call `callback(device)` whenever Roku reports that a device changed,
        whether from this program, the Roku app, a voice assistant or the
        device's own button. `device` is a Device with the new state.

        Keeps the connection to Roku open (no idle timeout) until the last
        callback is removed or close() is called, reconnecting automatically if
        it drops; changes missed while disconnected are reported on reconnect.

        Callbacks run one at a time on a separate thread, so they may send
        commands or call devices(). Exceptions in a callback are logged and
        don't stop updates. Returns a function that removes the callback.
        Registering the same callback again does nothing (it is still called
        once per change).
        """
        self._check_fork()
        with self._transition_lock:
            with self._listeners_lock:
                if callback not in self._listeners:  # registering twice is a no-op
                    self._listeners.append(callback)
            if not self._listening:
                try:
                    self._run(self._start_listening(), timeout=3 * self.timeout + 5)
                except BaseException:
                    with self._listeners_lock:
                        if callback in self._listeners:
                            self._listeners.remove(callback)
                    raise

        def unsubscribe():
            self._remove_listener(callback)
        return unsubscribe

    def _remove_listener(self, callback):
        self._check_fork()
        with self._transition_lock:
            with self._listeners_lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
                last = not self._listeners
            loop = self._loop
            if last and self._listening and loop is not None and loop.is_running():
                try:
                    self._run(self._stop_listening(), timeout=self.timeout + 2)
                except Exception:
                    pass

    @property
    def listening(self) -> bool:
        """True while live updates are on (some on_change callback is registered)."""
        return self._listening

    @property
    def live_error(self) -> Optional[Exception]:
        """Why live updates are currently disconnected and retrying (e.g.
        SessionExpired, RokuUnavailable), or None while connected. Retries
        continue in the background; check this to notice a dead login."""
        return self._live_error

    async def _start_listening(self):
        if self._listening:
            return
        self._listening = True
        self._live_error = None
        self._reconnect_delay = 1.0
        try:
            # connect first, so changes during the device-list fetch are caught
            async with self._lock():
                if self._ws is None or self._ws.state is not State.OPEN:
                    await self._ws_drop()
                    self._ws = await self._ws_open()
                    asyncio.get_running_loop().create_task(self._ws_reader(self._ws))
                self._arm_idle_timer()  # cancels the idle timer while listening
            await self._seed_live(fire=False)
        except BaseException:
            self._listening = False
            self._live, self._live_alias = {}, {}
            if self._ws is not None:
                self._arm_idle_timer()
            raise

    async def _stop_listening(self):
        if not self._listening:
            return
        self._listening = False
        self._live_error = None
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._live, self._live_alias = {}, {}
        if self._ws is not None:
            self._arm_idle_timer()

    async def _seed_live(self, fire: bool):
        """Load full device details from the REST list. Reports that arrive
        while it's being fetched are applied on top, since the list may be
        older than them. With fire, report every device whose visible state
        differs from what we had (changes missed while offline); reports
        received during the fetch are reported either way."""
        loop = asyncio.get_running_loop()
        self._seed_buffer = []
        self._seeding = True
        try:
            fresh = await loop.run_in_executor(None, self.raw_devices, 0)
        finally:
            self._seeding = False
        buffered, self._seed_buffer = self._seed_buffer, []
        live = {_text(r["id"]): r for r in fresh
                if isinstance(r, dict) and r.get("id") is not None}
        alias = {}
        for leaf_id, r in live.items():
            hw = r.get("device")
            if isinstance(hw, dict) and hw.get("id") is not None:
                alias[_text(hw["id"])] = leaf_id
        fetched = {k: self._visible(v) for k, v in live.items()}
        touched = set()
        for item in buffered:
            leaf_id = self._merge_report(live, alias, item)
            if leaf_id is not None:
                touched.add(leaf_id)
        old, self._live, self._live_alias = self._live, live, alias
        for leaf_id, r in live.items():
            now = self._visible(r)
            if fire:
                if now != self._visible(old.get(leaf_id)):
                    self._fire(r)
            elif leaf_id in touched and now != fetched[leaf_id]:
                self._fire(r)

    def _visible(self, raw: Optional[dict]):
        """The state a callback cares about, ignoring Roku's bookkeeping
        fields (updated, eventId) that change on every report."""
        if raw is None:
            return None
        try:
            d = Device(self, raw)
        except Exception:
            return None
        rgb = tuple(d.color_rgb) if isinstance(d.color_rgb, list) else d.color_rgb
        return (d.name, d.power, d.online, d.brightness, d.color_temp, d.color_type, rgb)

    @staticmethod
    def _merge_report(live: dict, alias: dict, item) -> Optional[str]:
        """Apply one report item to `live`; returns the leaf id it changed."""
        if not isinstance(item, dict) or not isinstance(item.get("state"), dict):
            return None
        rid = _text(item.get("id"))
        leaf_id = rid if rid in live else alias.get(rid)
        if leaf_id is None:
            return None
        raw = live[leaf_id]
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        raw["state"] = {**state, **item["state"]}
        return leaf_id

    async def _reconnect(self):
        # The delay doubles on every attempt, up to 60 s, and only resets once
        # a connection has stayed up for a minute: a server that accepts the
        # connection and then drops it right away can't cause a fast loop.
        while self._listening:
            delay = self._reconnect_delay
            self._reconnect_delay = min(delay * 2, 60.0)
            await asyncio.sleep(delay)
            if not self._listening:
                return
            try:
                async with self._lock():
                    if self._ws is None or self._ws.state is not State.OPEN:
                        await self._ws_drop()
                        self._ws = await self._ws_open()
                        asyncio.get_running_loop().create_task(self._ws_reader(self._ws))
                await self._seed_live(fire=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._live_error = e
                level = logging.ERROR if isinstance(e, SessionExpired) else logging.WARNING
                log.log(level, "roku-smarthome: live updates reconnect failed (%s); "
                        "retrying in %.0f s", e, self._reconnect_delay)
                continue
            if self._ws is not None and self._ws.state is State.OPEN:
                self._live_error = None
                return
            # the new connection dropped while the device list was loading

    def _apply_state_report(self, payload):
        # {"type": "leaf_state_report", "payload": [{"id": ..., "type": "device",
        #   "state": {"power": {"power": "on", "updated": ..., "eventId": ...}}}]}
        # Only the parts of the state that changed are included.
        if not self._listening or not isinstance(payload, list):
            return
        for item in payload:
            if self._seeding:
                self._seed_buffer.append(copy.deepcopy(item))
            rid = _text(item.get("id")) if isinstance(item, dict) else ""
            leaf_id = rid if rid in self._live else self._live_alias.get(rid)
            before = self._visible(self._live.get(leaf_id)) if leaf_id else None
            if self._merge_report(self._live, self._live_alias, item) is None:
                continue
            raw = self._live[leaf_id]
            if self._visible(raw) != before:
                self._fire(raw)

    def _fire(self, raw: dict):
        if not self._listeners:
            return
        if self._events is None:
            self._events = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="roku-smarthome-events")
        self._events.submit(self._call_listeners, copy.deepcopy(raw))

    def _call_listeners(self, raw: dict):
        try:
            device = Device(self, raw)
        except Exception:
            log.exception("roku-smarthome: could not read a device update")
            return
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(device)
            except Exception:
                log.exception("roku-smarthome: on_change callback %r failed", cb)

    # ----- cleanup -----

    def close(self):
        """Stop live updates and close the shared websocket, flushing anything
        still being sent. Safe to call more than once; the next command reopens it."""
        if self._pid != os.getpid():
            return  # a forked child: the parent's connection isn't ours to close
        with self._transition_lock:
            with self._listeners_lock:
                self._listeners.clear()
            loop = self._loop
            if loop is None or loop.is_closed() or not loop.is_running():
                return
            try:
                self._run(self._stop_listening(), timeout=self.timeout + 2)
            except Exception:
                pass
            try:
                self._run(self._ws_close_locked(force=True),
                          timeout=self.reply_timeout + self.command_wait + self.timeout + 2)
            except Exception:
                pass

    def __enter__(self) -> "RokuSmartHome":
        return self

    def __exit__(self, *exc):
        self.close()


def _close_at_exit(ref):
    client = ref()
    if client is not None:
        client.close()
