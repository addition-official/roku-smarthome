"""Regression tests for findings from the November 2026 review (cookies,
parsing, lookups). Each failed before its fix."""

import json
import logging
import os
import threading

import pytest
import requests

from roku_smarthome import (DeviceNotFound, RokuSmartHome, RokuUnavailable,
                            SessionExpired)
from roku_smarthome.client import parse_cookie_header

from conftest import make_leaf


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


def renewing_get(rk, value, body):
    """A fake GET that renews ks.session to `value` and returns `body`."""
    def fake_get(*args, **kwargs):
        rk._session.cookies.set("ks.session", value, domain=".roku.com", path="/")
        return FakeResponse(200, body)
    return fake_get


# ----- verify=True must not save a rejected login -----

@pytest.mark.parametrize("body", [{"error": "x"}, ValueError("not json")])
def test_failed_verify_does_not_save_renewed_cookie(cookie_file, monkeypatch, body):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=good")
    # build the session the new cookie will use, then intercept its GET
    real_use = rk._use_cookies

    def use_and_patch(cookies):
        real_use(cookies)
        monkeypatch.setattr(rk._session, "get", renewing_get(rk, "renewed-bad", body))
    monkeypatch.setattr(rk, "_use_cookies", use_and_patch)
    with pytest.raises(RokuUnavailable):
        rk.import_cookie_header("ks.session=new", verify=True)
    assert json.loads(cookie_file.read_text()) == {"ks.session": "good"}
    assert RokuSmartHome(cookie_file)._cookies == {"ks.session": "good"}


def test_successful_verify_saves_renewed_cookie(cookie_file, monkeypatch):
    rk = RokuSmartHome(cookie_file)
    real_use = rk._use_cookies

    def use_and_patch(cookies):
        real_use(cookies)
        monkeypatch.setattr(rk._session, "get", renewing_get(rk, "renewed", []))
    monkeypatch.setattr(rk, "_use_cookies", use_and_patch)
    rk.import_cookie_header("ks.session=new", verify=True)
    assert json.loads(cookie_file.read_text()) == {"ks.session": "renewed"}


# ----- a failed save of a renewed cookie -----

def test_renewal_save_failure_does_not_fail_request_and_is_retried(cookie_file, monkeypatch, caplog):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=old")
    monkeypatch.setattr(rk._session, "get", renewing_get(rk, "NEWVALUE123", [make_leaf()]))
    real_write = rk._write_cookie_file
    fail = {"on": True}

    def flaky_write():
        if fail["on"]:
            raise PermissionError(13, "Permission denied")
        real_write()
    monkeypatch.setattr(rk, "_write_cookie_file", flaky_write)
    with caplog.at_level(logging.WARNING):
        assert len(rk.devices()) == 1          # request still succeeds
    assert "could not save renewed login" in caplog.text
    assert "NEWVALUE123" not in caplog.text    # no cookie value in the log
    assert json.loads(cookie_file.read_text()) == {"ks.session": "old"}
    fail["on"] = False
    rk.devices()                               # same cookie, but save is retried
    assert json.loads(cookie_file.read_text()) == {"ks.session": "NEWVALUE123"}


def test_roku_deleting_the_session_cookie_is_respected(cookie_file, monkeypatch):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=old; other=1")

    def fake_get(*args, **kwargs):
        rk._session.cookies.clear(".roku.com", "/", "ks.session")  # Max-Age=0
        return FakeResponse(200, [])
    monkeypatch.setattr(rk._session, "get", fake_get)
    rk.raw_devices()
    assert json.loads(cookie_file.read_text()) == {"other": "1"}
    assert not rk.has_session
    with pytest.raises(SessionExpired):
        rk.raw_devices()


# ----- cookie file writes -----

def test_concurrent_saves_never_leave_invalid_json(cookie_file):
    a = RokuSmartHome(cookie_file)
    b = RokuSmartHome(cookie_file)
    a._cookies = {"ks.session": "A"}
    b._cookies = {"ks.session": "B" + "x" * 500}
    errors = []

    def loop(rk):
        try:
            for _ in range(200):
                rk._write_cookie_file()
        except Exception as e:  # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=loop, args=(c,)) for c in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(cookie_file.read_text())["ks.session"] in ("A", "B" + "x" * 500)
    leftovers = [p.name for p in cookie_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symlinks")
def test_preplanted_temp_file_or_symlink_is_not_used(cookie_file, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    os.symlink(victim, cookie_file.parent / (cookie_file.name + ".tmp"))
    planted = cookie_file.parent / "planted"
    planted.write_text("")
    os.chmod(planted, 0o666)
    RokuSmartHome(cookie_file).import_cookie_header("ks.session=SECRET")
    assert victim.read_text() == "untouched"
    assert not cookie_file.is_symlink()
    assert oct(cookie_file.stat().st_mode & 0o777) == "0o600"


def test_failed_save_leaves_no_temp_copy_and_keeps_old_login(tmp_path):
    folder = tmp_path / "cookies.json"
    folder.mkdir()  # the cookie "file" is a directory
    rk = RokuSmartHome(folder)
    with pytest.raises(OSError):
        rk.import_cookie_header("ks.session=SECRET")
    assert [p.name for p in tmp_path.iterdir()] == ["cookies.json"]
    assert not rk.has_session  # the unsaved login isn't left active


# ----- pasted cookie headers -----

@pytest.mark.parametrize("pasted", [
    "-H $'cookie: a=1; ks.session=abc'",
    "curl 'https://my.roku.com/smarthome/api/v1/leaves' -H 'accept: */*' "
    "-H 'cookie: a=1; ks.session=abc' -H 'user-agent: x'",
    "curl ^\"https://my.roku.com/^\" -H ^\"cookie: a=1; ks.session=abc^\" ^",
    "curl 'https://my.roku.com' \\\n  -H 'Cookie: a=1; ks.session=abc' \\\n  --compressed",
    "curl 'https://my.roku.com' -b 'a=1; ks.session=abc' -H 'accept: */*'",
])
def test_whole_curl_commands(pasted):
    assert parse_cookie_header(pasted) == {"a": "1", "ks.session": "abc"}


@pytest.mark.parametrize("pasted", [
    "ks.session=SECRET123\nfoo=bar",
    "ks.session=SECRET123; x=y\n X-Injected: 1",
    "ks.session=abc; x=1”",          # smart quote from a chat
    "ks.session=ab\rc",
    "ks.session=café",
])
def test_characters_a_browser_never_sends_are_rejected_without_echo(cookie_file, pasted):
    rk = RokuSmartHome(cookie_file)
    with pytest.raises(ValueError) as exc:
        rk.import_cookie_header(pasted)
    assert "SECRET" not in str(exc.value) and "abc" not in str(exc.value)
    assert not cookie_file.exists()


def test_duplicate_cookie_name_keeps_first():
    assert parse_cookie_header("ks.session=NEW; ks.session=OLD")["ks.session"] == "NEW"


def test_empty_session_value_is_rejected(cookie_file):
    with pytest.raises(ValueError):
        RokuSmartHome(cookie_file).import_cookie_header("ks.session=; x=1")


def test_unsendable_saved_cookie_gives_session_expired_without_leaking(cookie_file):
    cookie_file.write_text(json.dumps({"ks.session": "SECRET”"}))
    rk = RokuSmartHome(cookie_file)
    with pytest.raises(SessionExpired) as exc:
        rk.raw_devices()
    assert "SECRET" not in str(exc.value) and "login" in str(exc.value)


# ----- lookups -----

def test_lookup_quirks(client):
    client.fake_leaves = [
        make_leaf(id=123, name="Lamp "),
        make_leaf(id="", name="Blank id"),
        make_leaf(id="d1", name=42),                    # number as a name
        make_leaf(id="d2", name="Fan", subType={"x": 1}),
    ]
    assert client.get(123, by_id=True).name == "Lamp "
    assert client.get("lamp").id == "123"
    assert client.find(" 123 ").id == "123"
    assert client.find("42").id == "d1"
    with pytest.raises(DeviceNotFound):
        client.find("")
    with pytest.raises(DeviceNotFound):
        client.find("   ")
    assert isinstance(client.get("fan").type, str)


def test_duplicate_ids_message_says_id(client):
    client.fake_leaves = [make_leaf(id="x", name="A"), make_leaf(id="x", name="B")]
    with pytest.raises(DeviceNotFound) as exc:
        client.get("x", by_id=True)
    assert "id" in str(exc.value) and "named" not in str(exc.value)
