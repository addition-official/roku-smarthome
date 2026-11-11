"""The REST call to Roku, with requests mocked out (no network)."""

import pytest
import requests

from roku_smarthome import RokuSmartHome, RokuUnavailable, SessionExpired


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


@pytest.mark.parametrize("code", [301, 302, 401, 403])
def test_rejected_session(rk, monkeypatch, code):
    respond_with(rk, monkeypatch, FakeResponse(code))
    with pytest.raises(SessionExpired):
        rk.raw_devices()


@pytest.mark.parametrize("response", [
    FakeResponse(500),
    FakeResponse(503),
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
