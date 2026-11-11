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


def test_empty_header_saves_no_session(cookie_file):
    rk = RokuSmartHome(cookie_file)
    assert rk.import_cookie_header("  ;; ") == 0
    assert not rk.has_session


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
