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
import json
import os
import threading
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Dict, List, Optional
import concurrent.futures
import requests
import websockets

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

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class SessionExpired(Exception):
    """The saved Roku session no longer works. Run `roku-smarthome login` again."""


class RokuUnavailable(Exception):
    """Roku's API could not be reached or returned a server error.
    The session is probably still fine; retry later."""


class UnsupportedCommand(Exception):
    """The device does not advertise this command."""


class DeviceNotFound(Exception):
    """No device on the account matches that name or id."""


class Device:
    """One Roku Smart Home device ("leaf" in Roku's API)."""

    def __init__(self, client: "RokuSmartHome", raw: dict):
        self._client = client
        self._load(raw)

    def _load(self, raw: dict):
        self.raw = raw
        self.id: str = raw["id"]
        self.name: str = raw.get("name", "")
        self.type: str = raw.get("subType") or raw.get("device", {}).get("type", "")
        self._leaf_type: str = raw.get("type", "device")
        self.model: str = raw.get("device", {}).get("model", "")
        self.local_ip: str = raw.get("device", {}).get("localIp", "")
        self.supported_commands = {
            c.get("type") for c in raw.get("supportedCommands", []) if c.get("type")
        }
        state = raw.get("state", {}) or {}
        self.power: Optional[str] = state.get("power", {}).get("power")
        self.online: Optional[bool] = state.get("online", {}).get("online")
        self.brightness: Optional[int] = state.get("brightness", {}).get("level")
        color = state.get("color", {})
        self.color_temp: Optional[int] = color.get("temperature")
        self.color_type: Optional[str] = color.get("colorType")
        self.color_rgb: Optional[list] = color.get("rgb")

    # ----- state -----

    def refresh(self) -> "Device":
        """Re-fetch this device's state from Roku, bypassing the cache."""
        fresh = self._client.get(self.id, by_id=True, max_age=0)
        self._load(fresh.raw)
        return self

    @property
    def is_on(self) -> bool:
        return self.power == "on"

    # ----- commands -----

    def send(self, command: str, parameters: dict, wait: Optional[float] = None):
        """Send any command. Raises UnsupportedCommand if the device doesn't list it."""
        if command not in self.supported_commands:
            raise UnsupportedCommand(
                f"{self.name!r} does not support {command!r} "
                f"(supports: {sorted(self.supported_commands)})"
            )
        self._client._send_command(self, command, parameters, wait=wait)

    def turn_on(self):
        self.send("power", {"power": "on"})
        self.power = "on"

    def turn_off(self):
        self.send("power", {"power": "off"})
        self.power = "off"

    def toggle(self):
        self.turn_off() if self.is_on else self.turn_on()

    def set_brightness(self, level: int):
        if not 0 <= level <= 100:
            raise ValueError("brightness must be 0..100")
        self.send("brightness", {"level": int(level)})
        self.brightness = int(level)

    def set_color_temp(self, kelvin: int):
        self.send("color", {"colorType": "temperature", "temperature": int(kelvin)})
        self.color_temp = int(kelvin)
        self.color_type = "temperature"

    def set_color_rgb(self, r: int, g: int, b: int):
        rgb = [int(r), int(g), int(b)]
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
    command_wait  seconds to keep the websocket open after sending a command, so the
                  server has time to flush it. 0.5 is plenty in practice.
    """

    def __init__(self, cookie_file: Optional[os.PathLike] = None, *,
                 timeout: float = 10.0, cache_ttl: float = 0.0,
                 command_wait: float = 0.5):
        self.cookie_file = Path(cookie_file) if cookie_file else DEFAULT_COOKIE_FILE
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)
        self.command_wait = float(command_wait)

        self._cache_lock = threading.Lock()
        self._cache_raw: Optional[List[dict]] = None
        self._cache_ts: float = 0.0

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_lock = threading.Lock()

        self._load_session()

    # ----- cookie handling -----

    def _load_session(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _UA
        self._cookies: Dict[str, str] = self._read_cookie_file()
        for k, v in self._cookies.items():
            if k not in _SKIP:
                self._session.cookies.set(k, v, domain=".roku.com", path="/")

    def _read_cookie_file(self) -> Dict[str, str]:
        if not self.cookie_file.exists():
            return {}
        return json.loads(self.cookie_file.read_text())

    def _write_cookie_file(self):
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_file.write_text(json.dumps(self._cookies, indent=2))
        try:
            self.cookie_file.chmod(0o600)
        except OSError:
            pass

    def import_cookie_header(self, header: str) -> int:
        """Store a raw `Cookie:` header value copied from the browser. Returns cookie count."""
        sc = SimpleCookie()
        sc.load(header)
        self._cookies = {k: m.value for k, m in sc.items()}
        self._write_cookie_file()
        self._load_session()
        self.invalidate()
        return len(self._cookies)

    def _persist_refreshed_cookies(self):
        changed = False
        for c in self._session.cookies:
            if c.name in _SKIP:
                continue
            if self._cookies.get(c.name) != c.value:
                self._cookies[c.name] = c.value
                changed = True
        if changed:
            self._write_cookie_file()

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
            raise SessionExpired(
                f"No session saved at {self.cookie_file}. Run: roku-smarthome login '<cookie>'"
            )
        try:
            r = self._session.get(
                LEAVES_URL,
                headers={"x-request-id": str(uuid.uuid4())},
                allow_redirects=False,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RokuUnavailable(f"could not reach Roku: {e}") from e

        if r.status_code in (301, 302, 401, 403):
            raise SessionExpired(
                f"Roku rejected the saved session (HTTP {r.status_code}). Run: roku-smarthome login '<cookie>'"
            )
        if r.status_code >= 500:
            raise RokuUnavailable(f"Roku returned HTTP {r.status_code}")
        r.raise_for_status()
        self._persist_refreshed_cookies()
        try:
            return r.json()
        except ValueError as e:
            raise RokuUnavailable("Roku returned a non-JSON device list") from e

    def raw_devices(self, max_age: Optional[float] = None) -> List[dict]:
        """Device list as raw JSON. Served from cache if it is younger than max_age
        seconds (default: self.cache_ttl). Pass max_age=0 to force a fetch."""
        ttl = self.cache_ttl if max_age is None else float(max_age)
        with self._cache_lock:
            if (self._cache_raw is not None and ttl > 0
                    and time.monotonic() - self._cache_ts <= ttl):
                return list(self._cache_raw)
            raw = self._fetch_leaves()
            self._cache_raw = raw
            self._cache_ts = time.monotonic()
            return list(raw)

    def devices(self, max_age: Optional[float] = None) -> List[Device]:
        return [Device(self, raw) for raw in self.raw_devices(max_age)]

    def get(self, name_or_id: str, by_id: bool = False,
            max_age: Optional[float] = None) -> Device:
        key = name_or_id.lower()
        for d in self.devices(max_age):
            if (by_id and d.id == name_or_id) or (not by_id and d.name.lower() == key):
                return d
        raise DeviceNotFound(name_or_id)

    # ----- websocket loop (one daemon thread, safe to call from anywhere) -----

    def _get_loop(self) -> asyncio.AbstractEventLoop:
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
                      wait: Optional[float] = None):
        msg = {
            "id": str(uuid.uuid4()),
            "type": "send_command",
            "payload": {
                "leafId": device.id,
                "leafType": device._leaf_type,
                "command": {"command": command, "parameters": parameters},
            },
        }
        w = self.command_wait if wait is None else float(wait)
        self._run(self._ws_send(msg, w), timeout=self.timeout + w + 2)
        # state changed on Roku's side; don't serve the stale list again
        self.invalidate()

    async def _ws_send(self, msg: dict, wait: float):
        headers = {"Cookie": self._cookie_header(), "Origin": ORIGIN}
        try:
            async with websockets.connect(SOCKET_URL, additional_headers=headers,
                                          open_timeout=self.timeout) as ws:
                await ws.send(json.dumps(msg))
                # Server chats on connect (init / stream_init / leaves). Give it a
                # moment so the command is flushed before we close, then leave.
                deadline = asyncio.get_running_loop().time() + wait
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
        except websockets.exceptions.InvalidStatus as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 403):
                raise SessionExpired(
                    f"Roku rejected the session on the websocket (HTTP {code}). "
                    "Run: roku-smarthome login '<cookie>'"
                ) from e
            raise RokuUnavailable(f"websocket handshake failed (HTTP {code})") from e
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise RokuUnavailable(f"websocket error: {e}") from e