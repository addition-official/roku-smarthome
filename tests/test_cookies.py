import json
import os
import stat

import pytest

from roku_smarthome import RokuSmartHome


def test_parses_simple_header(cookie_file):
    rk = RokuSmartHome(cookie_file)
    assert rk.import_cookie_header("ks.session=abc; other=1") == 2
    assert rk._cookies == {"ks.session": "abc", "other": "1"}
    assert rk.has_session


def test_bare_token_does_not_drop_other_cookies(cookie_file):
    # Browsers send tokens without "=" (e.g. "roku_test;"). The old
    # SimpleCookie parser silently threw the whole header away.
    rk = RokuSmartHome(cookie_file)
    assert rk.import_cookie_header("roku_test; ks.session=abc; x=1") == 2
    assert rk._cookies["ks.session"] == "abc"


def test_value_containing_equals_sign_is_kept_whole(cookie_file):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=a=b==; y = 2 ")
    assert rk._cookies == {"ks.session": "a=b==", "y": "2"}


@pytest.mark.parametrize("header", ["", "  ;; ", "other=1; foo=bar"])
def test_header_without_session_is_rejected_and_old_login_kept(cookie_file, header):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=good")
    with pytest.raises(ValueError):
        rk.import_cookie_header(header)
    assert rk._cookies == {"ks.session": "good"}
    assert json.loads(cookie_file.read_text()) == {"ks.session": "good"}


@pytest.mark.parametrize("pasted", [
    "Cookie: ks.session=abc; x=1",
    "cookie: ks.session=abc; x=1",
    "'ks.session=abc; x=1'",
    '"ks.session=abc; x=1"',
    "-H 'Cookie: ks.session=abc; x=1' \\",
    "-H 'cookie: ks.session=abc; x=1'",
    "-b 'ks.session=abc; x=1'",
    "  ks.session=abc; x=1  \n",
])
def test_pasted_variants_are_cleaned(cookie_file, pasted):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header(pasted)
    assert rk._cookies == {"ks.session": "abc", "x": "1"}


def test_failed_verification_keeps_old_login(cookie_file):
    from roku_smarthome import SessionExpired
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=good")

    def reject():
        raise SessionExpired("bad cookie")
    rk._fetch_leaves = reject
    with pytest.raises(SessionExpired):
        rk.import_cookie_header("ks.session=bad", verify=True)
    assert rk._cookies == {"ks.session": "good"}
    assert json.loads(cookie_file.read_text()) == {"ks.session": "good"}
    assert rk._cookie_header() == "ks.session=good"


def test_successful_verification_saves(cookie_file):
    rk = RokuSmartHome(cookie_file)
    rk._fetch_leaves = lambda: []
    assert rk.import_cookie_header("ks.session=new", verify=True) == 1
    assert json.loads(cookie_file.read_text()) == {"ks.session": "new"}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"ks.session": 5}', ""])
def test_corrupt_cookie_file_reports_session_expired(cookie_file, content):
    from roku_smarthome import SessionExpired
    cookie_file.write_text(content)
    rk = RokuSmartHome(cookie_file)  # must not crash
    assert not rk.has_session
    with pytest.raises(SessionExpired) as exc:
        rk.raw_devices()
    assert "Cookie file" in str(exc.value) and "login" in str(exc.value)
    # and logging in again repairs it
    rk.import_cookie_header("ks.session=abc")
    assert RokuSmartHome(cookie_file).has_session


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_cookie_file_is_never_world_readable(cookie_file, monkeypatch):
    # The file must be created private, not made private after writing.
    modes = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        if str(path).startswith(str(cookie_file)):
            modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)
    monkeypatch.setattr(os, "open", spy_open)
    RokuSmartHome(cookie_file).import_cookie_header("ks.session=abc")
    assert modes and all(m == 0o600 for m in modes)
    assert not (cookie_file.parent / (cookie_file.name + ".tmp")).exists()


def test_cookies_are_saved_and_reloaded(cookie_file):
    RokuSmartHome(cookie_file).import_cookie_header("ks.session=abc")
    assert json.loads(cookie_file.read_text()) == {"ks.session": "abc"}
    assert RokuSmartHome(cookie_file).has_session


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_cookie_file_is_private(cookie_file):
    RokuSmartHome(cookie_file).import_cookie_header("ks.session=abc")
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600


def test_browser_noise_cookies_are_not_sent(cookie_file):
    rk = RokuSmartHome(cookie_file)
    rk.import_cookie_header("ks.session=abc; __cf_bm=stale; _csrf=x")
    assert rk._cookie_header() == "ks.session=abc"
    assert "__cf_bm" not in rk._session.cookies.keys()


def test_missing_cookie_file_means_no_session(cookie_file):
    assert not RokuSmartHome(cookie_file).has_session
