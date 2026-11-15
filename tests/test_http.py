"""The REST call to Roku, with requests mocked out (no network)."""

import pytest
import requests

from roku_smarthome import RokuSmartHome, RokuUnavailable, SessionExpired
from roku_smarthome.client import LEAVES_URL as LEAVES


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, bad_json=False):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def rk(cookie_file):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=old")
    return rk


def respond_with(rk, monkeypatch, response):
    def fake_get(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(rk._session, "get", fake_get)


def test_no_saved_session_raises_session_expired(cookie_file):
    with pytest.raises(SessionExpired):
        RokuSmartHome(cookie_file).raw_devices()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308, 401, 403])
def test_rejected_session(rk, monkeypatch, code):
    respond_with(rk, monkeypatch, FakeResponse(code))
    with pytest.raises(SessionExpired):
        rk.raw_devices()


@pytest.mark.parametrize("response", [
    FakeResponse(500),
    FakeResponse(503),
    FakeResponse(400),
    FakeResponse(404),
    FakeResponse(429),
    FakeResponse(200, {"error": "something"}),  # an object, not a list
    FakeResponse(200, "a string"),
    FakeResponse(200, bad_json=True),
    requests.ConnectionError("dns failure"),
])
def test_roku_unavailable(rk, monkeypatch, response):
    respond_with(rk, monkeypatch, response)
    with pytest.raises(RokuUnavailable):
        rk.raw_devices()


def test_success_returns_devices(rk, monkeypatch):
    respond_with(rk, monkeypatch, FakeResponse(200, [{"id": "a", "name": "Lamp"}]))
    assert [d.name for d in rk.devices()] == ["Lamp"]


def test_renewed_session_cookie_is_saved(rk, monkeypatch, cookie_file):
    # Roku re-issues ks.session on every request; that is what keeps the
    # login alive, so the new value has to make it to disk.
    def fake_get(*args, **kwargs):
        rk._session.cookies.set("ks.session", "renewed", domain=".roku.com", path="/")
        return FakeResponse(200, [])
    monkeypatch.setattr(rk._session, "get", fake_get)
    rk.raw_devices()
    assert RokuSmartHome(cookie_file)._cookies["ks.session"] == "renewed"


def test_renewed_cookie_without_domain_replaces_old_copy(rk, monkeypatch, cookie_file):
    # If Roku renews ks.session without a Domain= attribute, requests stores it
    # as a host-only my.roku.com cookie next to our .roku.com copy. Only the
    # new value may be sent afterwards.
    from http.client import HTTPMessage
    from requests.cookies import extract_cookies_to_jar

    class Orig:
        def __init__(self, msg): self.msg = msg
        def info(self): return self.msg

    class Resp:
        def __init__(self, msg): self._original_response = Orig(msg)

    def fake_get(url, **kwargs):
        h = HTTPMessage()
        h["Set-Cookie"] = "ks.session=renewed; Path=/; Secure; HttpOnly"
        req = requests.Request("GET", url).prepare()
        extract_cookies_to_jar(rk._session.cookies, req, Resp(h))
        return FakeResponse(200, [])
    monkeypatch.setattr(rk._session, "get", fake_get)
    rk.raw_devices()

    sent = rk._session.prepare_request(requests.Request("GET", LEAVES)).headers["Cookie"]
    assert sent == "ks.session=renewed"
    assert [c.value for c in rk._session.cookies if c.name == "ks.session"] == ["renewed"]
    assert RokuSmartHome(cookie_file)._cookies["ks.session"] == "renewed"


def test_cloudflare_cookie_from_server_is_kept_in_session(rk, monkeypatch):
    # Cookies in the skip list aren't saved, but a fresh one Roku sets during
    # this session should still be sent back, as before.
    def fake_get(*args, **kwargs):
        rk._session.cookies.set("__cf_bm", "fresh", domain=".roku.com", path="/")
        return FakeResponse(200, [])
    monkeypatch.setattr(rk._session, "get", fake_get)
    rk.raw_devices()
    assert rk._session.cookies.get("__cf_bm") == "fresh"
    assert "__cf_bm" not in rk._cookies


def test_returned_device_list_is_a_copy(rk, monkeypatch):
    respond_with(rk, monkeypatch, FakeResponse(200, [{"id": "a", "name": "Lamp", "state": {}}]))
    rk.cache_ttl = 60
    first = rk.raw_devices()
    first[0]["name"] = "changed"
    first[0]["state"]["power"] = {"power": "on"}
    first.append({"id": "b"})
    again = rk.raw_devices()
    assert again == [{"id": "a", "name": "Lamp", "state": {}}]


def test_stale_keepalive_connection_is_retried_once(rk, monkeypatch):
    # Seen on real hardware after a long idle: the first request on a reused
    # connection fails with RemoteDisconnected; a fresh connection works.
    from http.client import RemoteDisconnected
    calls = {"n": 0}

    def flaky_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError(
                ("Connection aborted.",
                 RemoteDisconnected("Remote end closed connection without response")))
        return FakeResponse(200, [{"id": "a", "name": "Lamp"}])
    monkeypatch.setattr(rk._session, "get", flaky_get)
    assert [d.name for d in rk.devices()] == ["Lamp"]
    assert calls["n"] == 2


def test_connection_error_twice_is_unavailable(rk, monkeypatch):
    calls = {"n": 0}

    def down(*args, **kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("down")
    monkeypatch.setattr(rk._session, "get", down)
    with pytest.raises(RokuUnavailable):
        rk.raw_devices()
    assert calls["n"] == 2


def test_timeout_is_not_retried(rk, monkeypatch):
    calls = {"n": 0}

    def slow(*args, **kwargs):
        calls["n"] += 1
        raise requests.ConnectTimeout("timed out")
    monkeypatch.setattr(rk._session, "get", slow)
    with pytest.raises(RokuUnavailable):
        rk.raw_devices()
    assert calls["n"] == 1
